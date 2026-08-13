"""
procare_download.py

Downloads every photo and video from the Procare gallery, week by week, back to
a target date.

Speed design
------------
Two decoupled stages:

  BROWSERS (N workers)   walk the gallery week by week and collect full-size
                         media URLs. Most come straight out of the gallery's
                         JSON API, so no viewer/modal is opened at all; tiles
                         the JSON misses fall back to clicking.
  DOWNLOADERS (M threads) pull those URLs off a queue and fetch them with
                         plain HTTP. The CDN links are CloudFront SIGNED URLs
                         (Expires + Signature + Key-Pair-Id), so they need no
                         browser and no cookies — they parallelise freely.

That split is what makes it fast: browsing never waits on bytes, and bytes never
wait on browsing.

Login happens ONCE; the session is saved to procare_session.json and reused.
Your password is never stored — only the session Procare itself issued.

Files land FLAT in procare_media/ as  YYYYMMDD_<contenthash>.<ext>
  date = the item's own date from the API when available,
         else a date embedded in the CDN filename,
         else the Monday of the week it appeared in.
Content-hash naming means re-runs never duplicate.

  --nav-test   walk back to the target date WITHOUT downloading, to prove the
               whole range is reachable and count what's there.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE = "https://schools.procareconnect.com"
OUT_DIR = "procare_media"
SESSION_FILE = "procare_session.json"
MANIFEST = "procare_manifest.csv"

DEFAULT_TARGET = dt.date(2025, 8, 18)
MAX_WEEKS = 200

WEEK_LOAD_TIMEOUT = 25.0
VIEWER_TIMEOUT_MS = 12000
TILE_SETTLE_ROUNDS = 3
MAX_TILE_RETRIES = 3

SEL_DROPDOWN = "div.date-filter .dropdown-portal__header"
SEL_DATE_TITLE = '[data-testid="datepicker-title"]'
SEL_TILE = "div.gallery__item"
SEL_DL_ANCHOR = "a.action-button[href], a[download][href]"

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CDN_URL_RE = re.compile(r"https://private\.cdn\.procareconnect\.com/[^\"'\\\s]+")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# Shared state
PRINT_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
CONTENT_HASHES = set()
DONE_KEYS = {"photos": set(), "videos": set()}
MANIFEST_ROWS = []
COOKIE_LIST = []
STATS = {}
DL_QUEUE = queue.Queue()


def log(who, msg):
    with PRINT_LOCK:
        print(f"[{who:<8}] {msg}", flush=True)


def bump(tab, field, n=1):
    with STATE_LOCK:
        STATS.setdefault(tab, {}).setdefault(field, 0)
        STATS[tab][field] += n


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def parse_week_start(title, not_after):
    """'Aug 10 - Aug 16' -> date. No year in the label, so infer walking back."""
    if not title:
        return None
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})", title)
    if not m:
        return None
    mon = MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return None
    day = int(m.group(2))
    for year in (not_after.year, not_after.year - 1, not_after.year - 2):
        try:
            d = dt.date(year, mon, day)
        except ValueError:
            continue
        if d <= not_after + dt.timedelta(days=3):
            return d
    return None


def date_from_url(url):
    m = re.search(r"open-uri(\d{4})(\d{2})(\d{2})", url or "")
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def parse_iso_date(s):
    if not isinstance(s, str) or len(s) < 8:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def harvest(node, urls, dates):
    """
    Walk decoded JSON collecting, per item dict, the full-size media URL and the
    item's date, keyed by every UUID in that dict. A tile's UUID (from its
    thumbnail URL) then resolves to the original file with no viewer needed.
    Only maps when a dict holds exactly one full-size URL, so items can't
    cross-contaminate.
    """
    if isinstance(node, dict):
        strings = [v for v in node.values() if isinstance(v, str)]
        cdn = [u for s in strings for u in CDN_URL_RE.findall(s)]
        full = [u for u in cdn
                if "/thumb/" not in u and not re.search(r"profile_pic|avatar", u, re.I)]
        found = None
        for key in ("captured_at", "taken_at", "created_at", "activity_date",
                    "date", "created_on", "uploaded_at"):
            d = parse_iso_date(node.get(key))
            if d:
                found = d
                break
        if full or found:
            uuids = {u for s in strings for u in UUID_RE.findall(s)}
            if len(set(full)) == 1:
                for u in uuids:
                    urls.setdefault(u, full[0])
            if found:
                for u in uuids:
                    dates.setdefault(u, found)
        for v in node.values():
            harvest(v, urls, dates)
    elif isinstance(node, list):
        for v in node:
            harvest(v, urls, dates)


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
def ext_for(url, content_type, is_video):
    ext = os.path.splitext((url or "").split("?")[0])[1].lower()
    if ext in IMAGE_EXTS + VIDEO_EXTS:
        return ext
    ct = (content_type or "").lower()
    for key, e in (("mp4", ".mp4"), ("quicktime", ".mov"), ("webm", ".webm"),
                   ("jpeg", ".jpg"), ("png", ".png"), ("webp", ".webp"),
                   ("gif", ".gif"), ("heic", ".heic")):
        if key in ct:
            return e
    return ".mp4" if is_video else ".jpg"


def load_existing_hashes():
    if not os.path.isdir(OUT_DIR):
        return
    for name in os.listdir(OUT_DIR):
        m = re.match(r"\d{8}_([0-9a-f]{12})", name)
        if m:
            CONTENT_HASHES.add(m.group(1))


def save_file(data, when, url, content_type, is_video):
    digest = hashlib.sha1(data).hexdigest()[:12]
    with STATE_LOCK:
        if digest in CONTENT_HASHES:
            return None, "dup"
        CONTENT_HASHES.add(digest)
    name = f"{when.strftime('%Y%m%d')}_{digest}{ext_for(url, content_type, is_video)}"
    path = os.path.join(OUT_DIR, name)
    if os.path.exists(path):
        return name, "dup"
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return name, "saved"


def progress_path(tab):
    return f".progress_{tab}.json"


def load_progress(tab):
    try:
        with open(progress_path(tab)) as fh:
            return set(json.load(fh))
    except Exception:
        return set()


def save_progress(tab):
    try:
        with STATE_LOCK:
            keys = sorted(DONE_KEYS[tab])
        with open(progress_path(tab), "w") as fh:
            json.dump(keys, fh)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Download pool — plain HTTP, no browser (CloudFront signed URLs)
# --------------------------------------------------------------------------- #
def cookies_for(url):
    host = urlparse(url).hostname or ""
    jar = {}
    for c in COOKIE_LIST:
        dom = (c.get("domain") or "").lstrip(".")
        if dom and (host == dom or host.endswith("." + dom)):
            jar[c["name"]] = c["value"]
    return jar


def downloader(idx):
    """Pull jobs off the queue and fetch them. One requests.Session per thread."""
    session = requests.Session()
    while True:
        job = DL_QUEUE.get()
        if job is None:
            DL_QUEUE.task_done()
            return
        url, when, tab, key, week = job
        is_video = (tab == "videos")
        try:
            data = ct = None
            for attempt in range(4):
                try:
                    r = session.get(url, headers={"User-Agent": UA, "Referer": BASE},
                                    cookies=cookies_for(url), timeout=180)
                    if r.ok:
                        data = r.content
                        ct = r.headers.get("Content-Type", "")
                        break
                    if r.status_code not in (403, 408, 429, 500, 502, 503, 504):
                        break
                except Exception:
                    pass
                time.sleep(1.0 * (2 ** attempt))

            if not data:
                bump(tab, "misses")
                log(f"dl{idx}", f"FAILED {url[:70]}")
            else:
                name, status = save_file(data, when, url, ct, is_video)
                with STATE_LOCK:
                    DONE_KEYS[tab].add(key)
                if status == "saved":
                    bump(tab, "saved")
                    with STATE_LOCK:
                        MANIFEST_ROWS.append((name, when.isoformat(), week, url))
                    n = STATS.get(tab, {}).get("saved", 0)
                    if n % 25 == 0:
                        log(f"dl{idx}", f"{tab}: {n} saved "
                                        f"({len(data) // 1024} KB latest)")
                else:
                    bump(tab, "dups")
        finally:
            DL_QUEUE.task_done()


# --------------------------------------------------------------------------- #
# Page helpers
# --------------------------------------------------------------------------- #
def set_weekly(page):
    try:
        cur = page.eval_on_selector(SEL_DROPDOWN, "e => e.innerText.trim()") or ""
        if "week" in cur.lower():
            return True
        page.click(SEL_DROPDOWN, timeout=5000)
        time.sleep(0.8)
        page.evaluate(r"""
        () => {
            for (const e of document.querySelectorAll('div, li, span, button, [role=option]')) {
                if (e.children.length) continue;
                if ((e.innerText || '').trim().toLowerCase() === 'weekly') {
                    (e.closest('[role=option], li, div') || e).click(); return;
                }
            }
        }""")
        time.sleep(2.0)
        return True
    except Exception:
        return False


def read_title(page):
    try:
        return page.eval_on_selector(SEL_DATE_TITLE, "e => e.innerText.trim()")
    except Exception:
        return None


def click_prev_week(page):
    js = r"""
    () => {
        const arrows = [...document.querySelectorAll('div.date-filter .datepicker__arrow')]
            .filter(a => !String(a.className).includes('dropdown-portal'));
        const t = document.querySelector('[data-testid="datepicker-title"]');
        const tx = t ? t.getBoundingClientRect().x : Infinity;
        let prev = arrows.find(a => /left|prev|back/i.test(String(a.className)));
        if (!prev) prev = arrows.filter(a => a.getBoundingClientRect().x < tx)[0];
        if (!prev) return false;
        (prev.closest('button, [role=button], a') || prev).click();
        return true;
    }"""
    try:
        return bool(page.evaluate(js))
    except Exception:
        return False


def mark_tiles(page):
    js = r"""
    () => {
        document.querySelectorAll('[data-tile]').forEach(e => e.removeAttribute('data-tile'));
        const root = document.querySelector('div.gallery') || document.body;
        const out = [];
        let n = 0;
        root.querySelectorAll('div.gallery__item').forEach(el => {
            const b = getComputedStyle(el).backgroundImage;
            let src = '';
            if (b && b.includes('url(')) {
                const m = b.match(/url\(["']?(.*?)["']?\)/);
                if (m) src = m[1];
            }
            if (!src) { const i = el.querySelector('img'); if (i) src = i.currentSrc || i.src || ''; }
            if (!/private\.cdn\.procareconnect\.com/.test(src)) return;
            if (/\.svg(\?|$)|logo|avatar|profile_pic/i.test(src)) return;
            el.setAttribute('data-tile', String(n));
            out.push({ idx: n, src: src });
            n++;
        });
        return out;
    }"""
    try:
        return page.evaluate(js)
    except Exception:
        return []


def wait_title_change(page, old, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.2)
        if read_title(page) != old:
            return True
    return False


def wait_for_week(page, old_title):
    """Wait for the label to change, then for the tile count to settle."""
    t0 = time.time()
    changed = wait_title_change(page, old_title, WEEK_LOAD_TIMEOUT)
    stable, last = 0, -1
    while time.time() - t0 < WEEK_LOAD_TIMEOUT and stable < TILE_SETTLE_ROUNDS:
        time.sleep(0.35)
        try:
            n = page.evaluate("() => document.querySelectorAll('div.gallery__item').length")
        except Exception:
            n = last
        stable = stable + 1 if n == last else 0
        last = n
    return changed


def wait_modal_closed(page, timeout=4.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if page.evaluate("() => !document.querySelector('div.modal')"):
                return True
        except Exception:
            return True
        time.sleep(0.08)
    return False


def open_tile_get_href(page, idx, last_href=None):
    """Fallback path: open the viewer and read the download anchor."""
    href = None
    try:
        wait_modal_closed(page, 3.0)
        el = page.query_selector(f'[data-tile="{idx}"]')
        if not el:
            return None
        el.scroll_into_view_if_needed(timeout=4000)
        try:
            el.click(timeout=6000)
        except Exception:
            try:
                page.keyboard.press("Escape")
                wait_modal_closed(page, 3.0)
                el.click(timeout=6000, force=True)
            except Exception:
                return None
        deadline = time.time() + (VIEWER_TIMEOUT_MS / 1000.0)
        while time.time() < deadline:
            try:
                h = page.eval_on_selector(SEL_DL_ANCHOR, "a => a.getAttribute('href')")
            except Exception:
                h = None
            if h and h != last_href:
                href = h
                break
            time.sleep(0.1)
    except Exception:
        href = None
    finally:
        try:
            page.keyboard.press("Escape")
            wait_modal_closed(page, 4.0)
        except Exception:
            pass
    return href


def open_gallery(p, tab, gid, headless, on_json):
    browser = p.chromium.launch(headless=headless)
    context = browser.new_context(storage_state=SESSION_FILE,
                                  viewport={"width": 1500, "height": 950})
    page = context.new_page()

    def on_response(resp):
        try:
            if "json" not in resp.headers.get("content-type", "").lower():
                return
            body = resp.text()
            if body and len(body) < 4_000_000:
                on_json(json.loads(body))
        except Exception:
            pass

    page.on("response", on_response)
    page.goto(f"{BASE}/dashboard/gallery/{gid}/{tab}", wait_until="domcontentloaded")
    time.sleep(3.0)
    set_weekly(page)
    time.sleep(2.0)
    return browser, context, page


# --------------------------------------------------------------------------- #
# Worker: collect URLs (downloads happen in the pool)
# --------------------------------------------------------------------------- #
def run_tab(tab, gid, target_date, headless, worker=0, skip_weeks=0,
            max_weeks=MAX_WEEKS):
    who = tab if worker == 0 else f"{tab[:3]}#{worker}"
    media_urls, item_dates = {}, {}
    with STATE_LOCK:
        DONE_KEYS[tab] |= load_progress(tab)
    weeks = 0

    with sync_playwright() as p:
        browser, context, page = open_gallery(
            p, tab, gid, headless, lambda j: harvest(j, media_urls, item_dates))

        if skip_weeks:
            log(who, f"skipping back {skip_weeks} week(s) to my slice...")
            for _ in range(skip_weeks):
                before = read_title(page)
                if not click_prev_week(page):
                    break
                wait_title_change(page, before)
            log(who, f"slice starts at {read_title(page)!r}")

        cursor = dt.date.today()

        for w in range(max_weeks):
            title = read_title(page)
            wk = parse_week_start(title, cursor)
            if wk:
                cursor = wk
            week_date = wk or dt.date.today()

            tiles = mark_tiles(page)
            with STATE_LOCK:
                already = set(DONE_KEYS[tab])
            todo = []
            for t in tiles:
                m = UUID_RE.search(t["src"])
                key = m.group(0) if m else t["src"]
                if key not in already:
                    todo.append((t, key))

            queued = 0
            last_href = None
            for t, key in todo:
                href = media_urls.get(key)
                if href:
                    bump(tab, "fast")
                else:
                    for attempt in range(MAX_TILE_RETRIES):
                        href = open_tile_get_href(page, t["idx"], last_href)
                        if href:
                            break
                        time.sleep(0.8 * (attempt + 1))
                    if href:
                        bump(tab, "clicked")
                        last_href = href
                if not href:
                    bump(tab, "misses")
                    continue
                when = (item_dates.get(key) or date_from_url(href)
                        or date_from_url(t["src"]) or week_date)
                DL_QUEUE.put((href, when, tab, key, title or ""))
                queued += 1

            log(who, f"week {title!r} ({week_date})  tiles={len(tiles)} "
                     f"queued={queued}  [q={DL_QUEUE.qsize()}]")
            save_progress(tab)
            weeks += 1

            if wk and wk <= target_date:
                log(who, f"reached target week {wk} <= {target_date}")
                break
            before = title
            if not click_prev_week(page):
                log(who, "could not click previous week — stopping")
                break
            if not wait_for_week(page, before):
                log(who, "week label did not change — stopping")
                break

        browser.close()

    bump(tab, "weeks", weeks)
    log(who, f"browsing done ({weeks} weeks)")


# --------------------------------------------------------------------------- #
# Navigation test — no downloads, proves the range is reachable
# --------------------------------------------------------------------------- #
def run_nav_test(tab, gid, target_date, headless, results):
    who = f"nav-{tab[:3]}"
    weeks, total_tiles, gaps = 0, 0, []
    first_week = last_week = None
    prev_date = None
    t0 = time.time()

    with sync_playwright() as p:
        browser, context, page = open_gallery(p, tab, gid, headless, lambda j: None)
        cursor = dt.date.today()

        for w in range(MAX_WEEKS):
            title = read_title(page)
            wk = parse_week_start(title, cursor)
            if wk:
                cursor = wk
            tiles = mark_tiles(page)
            total_tiles += len(tiles)
            weeks += 1
            if first_week is None:
                first_week = wk
            last_week = wk

            # Each step must move back exactly 7 days, or we skipped a week.
            if prev_date and wk and (prev_date - wk).days != 7:
                gaps.append(f"{prev_date} -> {wk} ({(prev_date - wk).days}d)")
            prev_date = wk

            if weeks % 5 == 0 or (wk and wk <= target_date):
                log(who, f"{title!r} ({wk})  tiles={len(tiles)}  "
                         f"running total={total_tiles}  {time.time() - t0:.0f}s")

            if wk and wk <= target_date:
                log(who, f"REACHED TARGET {wk} <= {target_date}")
                break
            before = title
            if not click_prev_week(page):
                log(who, "!! prev-week click failed — STOPPED EARLY")
                break
            if not wait_for_week(page, before):
                log(who, "!! week label did not change — STOPPED EARLY")
                break

        browser.close()

    results[tab] = {"weeks": weeks, "tiles": total_tiles, "first": first_week,
                    "last": last_week, "gaps": gaps,
                    "reached": bool(last_week and last_week <= target_date),
                    "secs": time.time() - t0}
    log(who, f"done: {weeks} weeks, {total_tiles} tiles, "
             f"oldest {last_week}, {len(gaps)} gap(s)")


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
def ensure_session(force_relogin=False):
    have = os.path.exists(SESSION_FILE) and not force_relogin
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            storage_state=SESSION_FILE if have else None,
            viewport={"width": 1500, "height": 950})
        page = context.new_page()
        page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded")
        time.sleep(4.0)

        def gallery_id():
            m = re.search(r"/dashboard/gallery/([0-9a-f-]{36})", page.url)
            if m:
                return m.group(1)
            href = page.evaluate(
                """() => { const a = document.querySelector('a[href*="/dashboard/gallery/"]');
                           return a ? a.getAttribute('href') : null; }""")
            if href:
                mm = re.search(r"gallery/([0-9a-f-]{36})", href)
                if mm:
                    return mm.group(1)
            return None

        try:
            logged_in = page.evaluate(
                """() => !document.querySelector('input[type=password]') &&
                         location.pathname.startsWith('/dashboard')""")
        except Exception:
            logged_in = False

        if have and logged_in:
            print("  Reusing saved session — no login needed.")
        else:
            print("\n" + "=" * 70)
            print("  Please LOG IN in the browser window (one time only).")
            print(f"  The session is then saved to {SESSION_FILE} and reused.")
            print("=" * 70)
            input("\nPress ENTER once you are logged in... ")

        gid = gallery_id()
        if not gid:
            page.evaluate(r"""() => {
                for (const b of document.querySelectorAll('button, a')) {
                    const t = (b.innerText || '').trim().toLowerCase();
                    if (t.includes('photos/videos')) { b.click(); return; } } }""")
            for _ in range(20):
                time.sleep(0.5)
                gid = gallery_id()
                if gid:
                    break

        context.storage_state(path=SESSION_FILE)
        try:
            os.chmod(SESSION_FILE, 0o600)
        except Exception:
            pass
        global COOKIE_LIST
        COOKIE_LIST = context.cookies()
        browser.close()
        return gid


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Download all Procare photos & videos.")
    ap.add_argument("--target-date", metavar="YYYY-MM-DD",
                    help=f"Stop once this week is reached (default {DEFAULT_TARGET})")
    ap.add_argument("--nav-test", action="store_true",
                    help="Walk back to the target date WITHOUT downloading, to "
                         "verify the full range is reachable and count the media.")
    ap.add_argument("--photos-only", action="store_true")
    ap.add_argument("--videos-only", action="store_true")
    ap.add_argument("--relogin", action="store_true")
    ap.add_argument("--show", action="store_true",
                    help="Show the browser windows (default: hidden).")
    ap.add_argument("--workers", type=int, default=1, metavar="N",
                    help="Split the photo date range across N browsers (default 1).")
    ap.add_argument("--dl-threads", type=int, default=8, metavar="M",
                    help="Parallel download threads (default 8).")
    args = ap.parse_args()

    target = (dt.date.fromisoformat(args.target_date)
              if args.target_date else DEFAULT_TARGET)
    os.makedirs(OUT_DIR, exist_ok=True)
    load_existing_hashes()

    gid = ensure_session(args.relogin)
    if not gid:
        print("!! Could not determine the gallery id.")
        sys.exit(1)

    tabs = ["photos", "videos"]
    if args.photos_only:
        tabs = ["photos"]
    if args.videos_only:
        tabs = ["videos"]

    print(f"  gallery = {gid}\n  target  = {target}\n"
          f"  existing files = {len(CONTENT_HASHES)}\n")
    headless = not args.show
    t0 = time.time()

    # ---------------- nav test ---------------- #
    if args.nav_test:
        print("  NAV TEST — no downloads, just proving we can reach the target.\n")
        results = {}
        threads = [threading.Thread(target=run_nav_test,
                                    args=(tab, gid, target, headless, results))
                   for tab in tabs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        print("\n" + "=" * 70)
        ok = True
        for tab in tabs:
            r = results.get(tab, {})
            mark = "OK " if r.get("reached") else "!! "
            ok &= bool(r.get("reached"))
            print(f"  {mark}{tab:<7}: {r.get('weeks', 0):>3} weeks  "
                  f"{r.get('tiles', 0):>4} tiles  "
                  f"{r.get('first')} -> {r.get('last')}  "
                  f"({r.get('secs', 0):.0f}s)")
            for g in r.get("gaps", [])[:5]:
                print(f"       gap: {g}")
        print("=" * 70)
        print("  Every week reachable — safe to run the real download."
              if ok else
              "  Did NOT reach the target — see the messages above.")
        return

    # ---------------- real run ---------------- #
    dl_threads = [threading.Thread(target=downloader, args=(i + 1,), daemon=True)
                  for i in range(args.dl_threads)]
    for t in dl_threads:
        t.start()
    print(f"  {args.dl_threads} download threads started\n")

    total_weeks = max(1, ((dt.date.today() - target).days // 7) + 2)
    jobs = []
    for tab in tabs:
        n = args.workers if (tab == "photos" and args.workers > 1) else 1
        if n == 1:
            jobs.append((tab, 0, 0, MAX_WEEKS))
        else:
            chunk = -(-total_weeks // n)
            for i in range(n):
                if i * chunk >= total_weeks:
                    break
                jobs.append((tab, i + 1, i * chunk, chunk))

    browsers = [threading.Thread(target=run_tab,
                                 args=(tab, gid, target, headless, wk, skip, mx))
                for tab, wk, skip, mx in jobs]
    for t in browsers:
        t.start()
    for t in browsers:
        t.join()

    print("\n  browsing finished — waiting for downloads to drain "
          f"({DL_QUEUE.qsize()} left)...")
    DL_QUEUE.join()
    for _ in dl_threads:
        DL_QUEUE.put(None)

    for tab in tabs:
        save_progress(tab)
    if MANIFEST_ROWS:
        new = not os.path.exists(MANIFEST)
        with open(MANIFEST, "a") as fh:
            if new:
                fh.write("filename,date,week,source_url\n")
            for name, date, week, url in sorted(MANIFEST_ROWS):
                fh.write(f'"{name}","{date}","{week}","{url}"\n')

    print("\n" + "=" * 70)
    total = 0
    for tab in tabs:
        s = STATS.get(tab, {})
        total += s.get("saved", 0)
        print(f"  {tab:<7}: {s.get('saved', 0):>4} saved  {s.get('dups', 0):>4} dup  "
              f"{s.get('misses', 0):>3} miss  {s.get('weeks', 0):>3} weeks  "
              f"(fast={s.get('fast', 0)}, clicked={s.get('clicked', 0)})")
    print(f"  TOTAL  : {total} new file(s) in ./{OUT_DIR}/  "
          f"in {(time.time() - t0) / 60:.1f} min")
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted — files and progress kept. Re-run to resume.")
        sys.exit(0)
