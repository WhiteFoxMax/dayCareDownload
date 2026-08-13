"""
download_procare_photos.py

Downloads all of your child's photos AND videos from the Procare web portal at
FULL RESOLUTION, scrolling back through history until it reaches a target date.

How it gets the originals
-------------------------
Procare's feed (div.activity-list) shows resized thumbnails. Opening an item
mounts a viewer:

    div.modal > div.modal__window > div.photo-viewer > div.modal__header
        > a.action-button[download][href="https://private.cdn.procareconnect.com/..."]

That anchor's href IS the signed URL of the ORIGINAL file. So phase 2 opens each
media item, reads the href straight out of the DOM, and fetches it — no need to
trigger a real browser download, and no new tabs (the anchor is target="_blank",
so clicking it would spawn one).

Downloads run through the BROWSER's session (Playwright APIRequestContext),
because the CDN uses domain-scoped CloudFront signed cookies/URLs — plain
`requests` gets 403 Forbidden. Signed URLs also expire (note the Expires= param),
so 403 is retried with backoff.

Files are named by a hash of their CONTENTS, so re-running never duplicates.

Nothing here bypasses authentication — you log in manually.
"""

import argparse
import base64
import datetime as dt
import hashlib
import os
import re
import sys
import time
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
START_URL = "https://schools.procareconnect.com/"
PHOTO_DIR = "procare_photos"             # phase 1: feed-resolution thumbnails
FULLRES_DIR = "procare_photos_fullres"   # phase 2: ORIGINALS  <-- the good ones
VIDEO_DIR = "procare_videos"
FAILED_LOG = "failed_urls.txt"
MANIFEST = "discovered_urls.txt"

# Scroll back until the oldest activity-date is at/older than this.
TARGET_DATE = dt.date(2025, 7, 1)        # Q3 2025 — adjust if you enrolled earlier

PHASE1_THUMBNAILS = False                # thumbnails are a backup; originals are better
PHASE2_ORIGINALS = True                  # the full-resolution click-through pass
SCROLL_ONLY = False                      # --scroll-only: prove the scroll, download nothing

SCROLL_WAIT_MAX = 8.0                    # max seconds to wait for a scroll to load more
MAX_SCROLLS = 3000
STABLE_ROUNDS_AFTER_TARGET = 4
STABLE_ROUNDS_BEFORE_TARGET = 15

VIEWER_TIMEOUT_MS = 6000                 # how long to wait for the modal + anchor
MAX_GALLERY_STEPS = 40                   # max photos to page through inside one item

MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5
FINAL_RETRY_PASSES = 3
MIN_IMAGE_BYTES = 8 * 1024

# --- Procare DOM selectors (from the live page) ---------------------------- #
SEL_ACTIVITY_LIST = "div.activity-list"
SEL_ACTIVITY = "div.activity"
SEL_ACTIVITY_DATE = "div.activity-date"
SEL_MODAL = "div.modal"
SEL_DL_ANCHOR = ("div.modal a.action-button[href], div.modal a[download][href], "
                 "a.action-button[href], a[download][href]")
SEL_CLOSE_BTN = "div.modal__header button.buttonv2--icononly, div.modal button[aria-label*='lose']"

URL_BLOCKLIST = ("sprite", "icon", "logo", "favicon", ".svg")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")

MEDIA_URL_RE = re.compile(
    r'https?://[^"\'\\\s]+?\.(?:jpg|jpeg|png|gif|webp|heic|mp4|mov|m4v|webm)(?:\?[^"\'\\\s]*)?',
    re.I,
)
RETRYABLE_STATUS = {403, 408, 425, 429, 500, 502, 503, 504}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

CONTENT_HASHES = set()   # hashes of files already saved (content-based dedupe)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def looks_like_media_url(url: str) -> bool:
    if not url or not url.startswith("http") or url.startswith("data:"):
        return False
    low = url.lower()
    return not any(bad in low for bad in URL_BLOCKLIST)


def url_ext(url: str) -> str:
    return os.path.splitext(urlparse(url).path)[1].lower()


def is_video_url(url: str) -> bool:
    return url_ext(url) in VIDEO_EXTS


def ext_from(url: str = "", content_type: str = "", default: str = ".jpg") -> str:
    ext = url_ext(url)
    if ext in IMAGE_EXTS + VIDEO_EXTS:
        return ext
    ct = (content_type or "").lower()
    for key, e in (("mp4", ".mp4"), ("quicktime", ".mov"), ("webm", ".webm"),
                   ("jpeg", ".jpg"), ("png", ".png"), ("webp", ".webp"),
                   ("gif", ".gif"), ("heic", ".heic")):
        if key in ct:
            return e
    return default


def target_path(url: str, content_type: str = "") -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    video = is_video_url(url) or (content_type or "").lower().startswith("video/")
    d = VIDEO_DIR if video else PHOTO_DIR
    return os.path.join(d, f"procare_{digest}{ext_from(url, content_type)}")


def already_downloaded(url: str) -> bool:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    for d in (PHOTO_DIR, VIDEO_DIR):
        if os.path.isdir(d):
            for name in os.listdir(d):
                if digest in name and not name.endswith(".part"):
                    return True
    return False


def load_existing_content_hashes():
    for d in (FULLRES_DIR, VIDEO_DIR):
        if os.path.isdir(d):
            for name in os.listdir(d):
                m = re.match(r"procare_([0-9a-f]{16})", name)
                if m:
                    CONTENT_HASHES.add(m.group(1))


def save_by_content(data, out_dir, url="", content_type="", default_ext=".jpg"):
    """Save bytes named by CONTENT hash. Returns 'saved' | 'dup' | 'small'."""
    if not data:
        return "small"
    digest = hashlib.sha1(data).hexdigest()[:16]
    if digest in CONTENT_HASHES:
        return "dup"
    ext = ext_from(url, content_type, default_ext)
    if ext not in VIDEO_EXTS and len(data) < MIN_IMAGE_BYTES:
        return "small"
    path = os.path.join(out_dir, f"procare_{digest}{ext}")
    CONTENT_HASHES.add(digest)
    if os.path.exists(path):
        return "dup"
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return "saved"


def parse_date_label(text: str):
    """Parse an activity-date label like 'Aug 12, 2026'."""
    if not text:
        return None
    m = re.search(r'\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b', text)
    if m:
        mon = MONTHS.get(m.group(1)[:3].lower())
        if mon:
            try:
                return dt.date(int(m.group(3)), mon, int(m.group(2)))
            except ValueError:
                return None
    m = re.search(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b', text)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def oldest_activity_date(page):
    """Read every div.activity-date label and return the oldest one."""
    try:
        labels = page.eval_on_selector_all(
            SEL_ACTIVITY_DATE, "els => els.map(e => e.textContent.trim())")
    except Exception:
        return None
    dates = [d for d in (parse_date_label(t) for t in labels) if d]
    return min(dates) if dates else None


# --------------------------------------------------------------------------- #
# Fetching (browser session — required for the signed CDN)
# --------------------------------------------------------------------------- #
def fetch_via_playwright_api(context, url, referer):
    resp = context.request.get(url, headers={"Referer": referer, "Accept": "*/*"},
                               timeout=180000)
    if resp.status in RETRYABLE_STATUS:
        raise RuntimeError(f"HTTP {resp.status} (retryable)")
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status}")
    return resp.body(), (resp.headers or {}).get("content-type", "")


def fetch_via_page_fetch(page, url):
    js = r"""
    async (url) => {
        const r = await fetch(url, { credentials: 'include', mode: 'cors' });
        if (!r.ok) return { ok: false, status: r.status };
        const buf = new Uint8Array(await (await r.blob()).arrayBuffer());
        let bin = ''; const CH = 0x8000;
        for (let i = 0; i < buf.length; i += CH)
            bin += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
        return { ok: true, b64: btoa(bin), type: r.headers.get('content-type') || '' };
    }
    """
    res = page.evaluate(js, url)
    if not res or not res.get("ok"):
        raise RuntimeError(f"in-page fetch HTTP {(res or {}).get('status', '?')}")
    return base64.b64decode(res["b64"]), res.get("type", "")


def fetch_via_requests(session, cookie_list, url, referer):
    host = urlparse(url).hostname or ""
    jar = {}
    for c in cookie_list:
        dom = (c.get("domain") or "").lstrip(".")
        if dom and (host == dom or host.endswith("." + dom)):
            jar[c["name"]] = c["value"]
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
        "Referer": referer, "Accept": "*/*",
    }
    resp = session.get(url, headers=headers, cookies=jar, timeout=180)
    if resp.status_code in RETRYABLE_STATUS:
        raise RuntimeError(f"HTTP {resp.status_code} (retryable)")
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "")


def fetch_bytes(url, context, page, session, cookie_list, referer):
    """All three methods with exponential backoff. Returns (data, content_type)."""
    methods = [
        lambda: fetch_via_playwright_api(context, url, referer),
        lambda: fetch_via_page_fetch(page, url),
        lambda: fetch_via_requests(session, cookie_list, url, referer),
    ]
    for attempt in range(MAX_RETRIES):
        for fn in methods:
            try:
                data, ct = fn()
                if data:
                    return data, ct
            except Exception:
                continue
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
    return None, ""


def download_thumb(url, context, page, session, cookie_list, referer):
    """Phase-1 download (URL-hash naming). Returns 'photo'|'video'|'skip'|None."""
    if already_downloaded(url):
        return "skip"
    data, ct = fetch_bytes(url, context, page, session, cookie_list, referer)
    if not data:
        return None
    video = is_video_url(url) or ct.lower().startswith("video/")
    if not video and len(data) < MIN_IMAGE_BYTES:
        return "skip"
    path = target_path(url, ct)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return "video" if video else "photo"


# --------------------------------------------------------------------------- #
# Feed scrolling
# --------------------------------------------------------------------------- #
def collect_dom_media_urls(page) -> set:
    js = r"""
    () => {
        const urls = new Set();
        const add = v => { if (v) urls.add(v); };
        document.querySelectorAll('img').forEach(img => {
            ['src','data-src','currentSrc'].forEach(a => add(img.getAttribute(a)));
            const ss = img.getAttribute('srcset');
            if (ss) ss.split(',').forEach(p => add(p.trim().split(' ')[0]));
        });
        document.querySelectorAll('video').forEach(v => { add(v.getAttribute('src')); add(v.getAttribute('poster')); });
        document.querySelectorAll('source').forEach(s => add(s.getAttribute('src')));
        document.querySelectorAll('a[href]').forEach(a => {
            if (/\.(jpg|jpeg|png|gif|webp|heic|mp4|mov|m4v|webm)/i.test(a.href)) add(a.href);
        });
        return Array.from(urls);
    }
    """
    try:
        return {u for u in page.evaluate(js) if looks_like_media_url(u)}
    except Exception:
        return set()


def activity_count(page) -> int:
    try:
        return page.evaluate("() => document.querySelectorAll('div.activity').length")
    except Exception:
        return 0


def scroll_feed(page):
    """
    Scroll the feed's real container to the bottom.

    Confirmed by diagnose_scroll.py: the feed lives in `section.section`
    (overflow-y:auto), NOT the window and NOT div.activity-list (whose
    scrollHeight equals its clientHeight, so it never scrolls itself).
    Walking up from the activity list finds it generically.
    """
    js = r"""
    (listSel) => {
        const scrolls = el => el && el.scrollHeight > el.clientHeight + 50 &&
            /(auto|scroll|overlay)/.test(getComputedStyle(el).overflowY);

        // Walk up from the activity list to the nearest scrolling ancestor.
        let el = document.querySelector(listSel), target = null;
        while (el) { if (scrolls(el)) { target = el; break; } el = el.parentElement; }

        // Fall back to the known container, then any scrollable element.
        if (!target && scrolls(document.querySelector('section.section')))
            target = document.querySelector('section.section');
        if (!target)
            target = [...document.querySelectorAll('*')].find(scrolls);

        if (target) {
            target.scrollTop = target.scrollHeight;
            return { ok: true, top: target.scrollTop, h: target.scrollHeight };
        }
        window.scrollTo(0, document.body.scrollHeight);
        return { ok: false };
    }
    """
    try:
        return page.evaluate(js, SEL_ACTIVITY_LIST)
    except Exception:
        return {"ok": False}


def press_end_key(page):
    """Fallback scroll: click the feed then hit End (also confirmed working)."""
    try:
        box = page.query_selector(SEL_ACTIVITY_LIST)
        if box:
            b = box.bounding_box()
            if b:
                page.mouse.click(b["x"] + 5, b["y"] + 5)
        page.keyboard.press("End")
    except Exception:
        pass


def scroll_and_wait(page, timeout=SCROLL_WAIT_MAX):
    """
    Scroll once, then poll until new activities appear (or timeout).

    Polling beats a fixed sleep: fast when the network is quick, patient when
    it isn't. Returns the new activity count.
    """
    before = activity_count(page)
    scroll_feed(page)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        now = activity_count(page)
        if now > before:
            return now
    # Nothing new — try the End-key route before giving up on this round.
    press_end_key(page)
    deadline = time.time() + 2.5
    while time.time() < deadline:
        time.sleep(0.4)
        now = activity_count(page)
        if now > before:
            return now
    return activity_count(page)


def click_load_more(page) -> bool:
    js = r"""
    () => {
        const wanted = ['load more','show more','see more','load earlier','earlier',
                        'previous','older','view more','load older'];
        for (const el of document.querySelectorAll('button, a, [role=button]')) {
            const t = (el.innerText || '').trim().toLowerCase();
            if (t && t.length < 40 && wanted.some(w => t.includes(w))) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) { el.click(); return true; }
            }
        }
        return false;
    }
    """
    try:
        return bool(page.evaluate(js))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# PHASE 2 — open each media activity and read the download anchor's href
# --------------------------------------------------------------------------- #
def mark_media_activities(page) -> int:
    """
    Tag each div.activity that holds real media with data-dlitem="N".

    Skips non-media activities (Nap, meals, sign-in) whose only <img> is a small
    activity icon.
    """
    js = r"""
    (sel) => {
        document.querySelectorAll('[data-dlitem]').forEach(e => {
            e.removeAttribute('data-dlitem'); e.removeAttribute('data-dlkind');
        });
        let n = 0;
        document.querySelectorAll(sel).forEach(act => {
            let kind = null;

            // Video markers inside the activity
            if (act.querySelector('video, [class*="video" i], [class*="play" i], [aria-label*="video" i]'))
                kind = 'video';

            // Otherwise: a reasonably large image = a real photo (not an icon)
            if (!kind) {
                for (const img of act.querySelectorAll('img')) {
                    const r = img.getBoundingClientRect();
                    const nat = (img.naturalWidth || 0);
                    const src = (img.currentSrc || img.src || '').toLowerCase();
                    if (/sprite|logo|favicon|\.svg/.test(src)) continue;
                    if (Math.max(r.width, nat) >= 100 || /\/photos\/|\/videos\//.test(src)) {
                        kind = 'photo';
                        break;
                    }
                }
            }
            if (kind) {
                act.setAttribute('data-dlitem', String(n++));
                act.setAttribute('data-dlkind', kind);
            }
        });
        return n;
    }
    """
    try:
        return page.evaluate(js, SEL_ACTIVITY)
    except Exception:
        return 0


def read_download_hrefs(page):
    """Read every download-anchor href currently in the open viewer."""
    js = r"""
    (sel) => Array.from(document.querySelectorAll(sel))
        .map(a => a.getAttribute('href') || a.getAttribute('download') || '')
        .filter(h => h && h.startsWith('http'))
    """
    try:
        return page.evaluate(js, SEL_DL_ANCHOR)
    except Exception:
        return []


def viewer_next(page) -> bool:
    """Click a 'next' arrow inside the viewer (multi-photo activities). True if clicked."""
    js = r"""
    () => {
        const modal = document.querySelector('div.modal');
        if (!modal) return false;
        for (const el of modal.querySelectorAll('button, a, [role=button], [class*=next i], [aria-label*=next i]')) {
            const cls = (el.className && el.className.baseVal !== undefined)
                        ? el.className.baseVal : (el.className || '');
            const lbl = ((el.getAttribute('aria-label') || '') + ' ' + cls).toLowerCase();
            if (lbl.includes('next') || lbl.includes('right') || lbl.includes('forward')) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0 && !el.disabled) { el.click(); return true; }
            }
        }
        return false;
    }
    """
    try:
        return bool(page.evaluate(js))
    except Exception:
        return False


def close_viewer(page):
    try:
        btn = page.query_selector(SEL_CLOSE_BTN)
        if btn:
            btn.click(timeout=2000)
        else:
            page.keyboard.press("Escape")
        time.sleep(0.2)
        if page.query_selector(SEL_MODAL):
            page.keyboard.press("Escape")
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass


def harvest_originals(page, context, session, cookie_list, referer):
    """
    Open every media activity, read the ORIGINAL file URL from the viewer's
    a.action-button[href], and download it. Handles multi-photo activities by
    paging through the viewer's next arrow.
    """
    count = mark_media_activities(page)
    if not count:
        print("  No media activities found — check that the feed is loaded.")
        return 0, 0

    print(f"  Found {count} media activit(ies). Opening each (~{count * 1.6 / 60:.0f} min)...")
    photos = videos = dups = misses = 0
    done_hrefs = set()

    for idx in range(count):
        el = page.query_selector(f'[data-dlitem="{idx}"]')
        if not el:
            continue
        kind = el.get_attribute("data-dlkind") or "photo"

        try:
            el.scroll_into_view_if_needed(timeout=4000)
            try:
                el.click(timeout=4000)
            except Exception:
                page.evaluate('(i) => document.querySelector(`[data-dlitem="${i}"]`)?.click()', idx)

            # Wait for the viewer's download anchor to mount.
            try:
                page.wait_for_selector(SEL_DL_ANCHOR, timeout=VIEWER_TIMEOUT_MS, state="attached")
            except Exception:
                close_viewer(page)
                misses += 1
                continue

            # Page through every photo in this activity.
            for step in range(MAX_GALLERY_STEPS):
                hrefs = [h for h in read_download_hrefs(page) if h not in done_hrefs]
                for href in hrefs:
                    done_hrefs.add(href)
                    vid = is_video_url(href)
                    out_dir = VIDEO_DIR if vid else FULLRES_DIR
                    data, ct = fetch_bytes(href, context, page, session, cookie_list, referer)
                    if not data:
                        print(f"    [{idx + 1}/{count}] fetch failed: {href[:60]}...")
                        misses += 1
                        continue
                    res = save_by_content(data, out_dir, url=href, content_type=ct,
                                          default_ext=".mp4" if vid else ".jpg")
                    if res == "saved":
                        if vid or ct.lower().startswith("video/"):
                            videos += 1
                        else:
                            photos += 1
                        print(f"    [{idx + 1}/{count}] {'video' if vid else 'photo'} "
                              f"saved ({len(data) // 1024} KB)")
                    elif res == "dup":
                        dups += 1

                # More photos in this activity?
                if not viewer_next(page):
                    break
                time.sleep(0.6)

        except Exception as exc:
            print(f"    [{idx + 1}/{count}] error: {str(exc)[:60]}")

        close_viewer(page)
        time.sleep(0.25)

        if (idx + 1) % 25 == 0:
            print(f"    ... {idx + 1}/{count} items | {photos} photos, {videos} videos, "
                  f"{dups} dupes, {misses} misses")

    print(f"  Phase 2 done: {photos} photos, {videos} videos "
          f"({dups} already had, {misses} misses)")
    return photos, videos


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    for d in (PHOTO_DIR, FULLRES_DIR, VIDEO_DIR):
        os.makedirs(d, exist_ok=True)
    load_existing_content_hashes()

    seen_urls, network_media_urls, failed = set(), set(), set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1500, "height": 950},
                                      accept_downloads=True)
        page = context.new_page()

        def on_response(response):
            try:
                ct = response.headers.get("content-type", "").lower()
                if ct.startswith("image/") or ct.startswith("video/"):
                    if looks_like_media_url(response.url):
                        network_media_urls.add(response.url)
                elif "json" in ct or ct.startswith("text/"):
                    body = response.text()
                    if body and len(body) < 5_000_000:
                        for m in MEDIA_URL_RE.findall(body):
                            if looks_like_media_url(m):
                                network_media_urls.add(m)
            except Exception:
                pass

        page.on("response", on_response)

        print(f"Opening {START_URL} ...")
        page.goto(START_URL, wait_until="domcontentloaded")

        print("\n" + "=" * 70)
        print("  LOG IN NOW in the browser window that just opened.")
        print("  Complete any 2FA, then go to the dashboard showing the")
        print("  activity feed with your child's photos.")
        print(f"  The script scrolls back to {TARGET_DATE}, then opens each")
        print("  photo/video to grab the full-resolution original.")
        print("=" * 70)
        input("\nPress ENTER once you're on the feed to begin... ")

        cookie_list = context.cookies()
        referer = page.url
        session = requests.Session()
        thumbs = 0

        # ------------------- PHASE 1: scroll ONLY (no downloads) ----------------- #
        # Downloading inside this loop is what made it look stuck: each failed URL
        # burns ~10s of retries before the next scroll. Collect URLs now (cheap),
        # fetch bytes afterwards.
        print(f"\nPHASE 1 — scrolling back to {TARGET_DATE} (no downloads yet)...")
        t0 = time.time()
        stable, last_count = 0, activity_count(page)
        oldest, target_reached = None, False

        for i in range(MAX_SCROLLS):
            seen_urls |= collect_dom_media_urls(page) | network_media_urls

            d = oldest_activity_date(page)
            if d and (oldest is None or d < oldest):
                oldest = d
            if oldest and oldest <= TARGET_DATE:
                target_reached = True

            n_act = scroll_and_wait(page)
            click_load_more(page)

            mins = (time.time() - t0) / 60
            print(f"[scroll {i + 1}] activities={n_act} urls={len(seen_urls)} "
                  f"| oldest={oldest or '?'} | {mins:.1f} min"
                  f"{'  << TARGET REACHED' if target_reached else ''}")

            limit = STABLE_ROUNDS_AFTER_TARGET if target_reached else STABLE_ROUNDS_BEFORE_TARGET
            if n_act == last_count:
                stable += 1
                if stable >= limit:
                    print(f"\nDone scrolling. Oldest post loaded: {oldest}")
                    break
                if stable % 3 == 0:      # nudge a stuck lazy-loader
                    try:
                        page.mouse.wheel(0, -1200)
                        time.sleep(0.8)
                        scroll_feed(page)
                    except Exception:
                        pass
            else:
                stable = 0
            last_count = n_act

        seen_urls |= collect_dom_media_urls(page) | network_media_urls
        print(f"\nFeed fully loaded: {activity_count(page)} activities, "
              f"{len(seen_urls)} media URLs seen, oldest = {oldest}")

        if SCROLL_ONLY:
            print("\n--scroll-only: stopping here, nothing downloaded.")
            input("\nPress ENTER to close the browser and exit... ")
            browser.close()
            return

        # ------------------- PHASE 1b: thumbnails (optional) --------------------- #
        if PHASE1_THUMBNAILS:
            todo = sorted(seen_urls)
            print(f"\nPHASE 1b — downloading {len(todo)} thumbnail(s)...")
            for n, url in enumerate(todo, 1):
                r = download_thumb(url, context, page, session, cookie_list, referer)
                if r in ("photo", "video"):
                    thumbs += 1
                elif r is None:
                    failed.add(url)
                if n % 25 == 0 or n == len(todo):
                    print(f"  {n}/{len(todo)} | saved={thumbs} failed={len(failed)}")

        # ------------------- PHASE 2: full-resolution originals ------------------ #
        fr_photos = fr_videos = 0
        if PHASE2_ORIGINALS:
            print("\nPHASE 2 — opening each item for the full-resolution original...")
            fr_photos, fr_videos = harvest_originals(page, context, session,
                                                     cookie_list, referer)

        # ------------------- Retry failures ------------------- #
        for pass_no in range(1, FINAL_RETRY_PASSES + 1):
            if not failed:
                break
            print(f"\nRetry pass {pass_no}/{FINAL_RETRY_PASSES} on {len(failed)} item(s)...")
            cookie_list = context.cookies()
            still = set()
            for url in sorted(failed):
                if download_thumb(url, context, page, session, cookie_list, referer) is None:
                    still.add(url)
            print(f"  recovered {len(failed) - len(still)}, still failing {len(still)}")
            failed = still
            if failed:
                time.sleep(4)

        # ------------------- Reports ------------------- #
        with open(MANIFEST, "w") as fh:
            fh.write("\n".join(sorted(seen_urls)))
        if failed:
            with open(FAILED_LOG, "w") as fh:
                fh.write("\n".join(sorted(failed)))
        elif os.path.exists(FAILED_LOG):
            os.remove(FAILED_LOG)

        def n_files(d):
            return len([f for f in os.listdir(d)
                        if not f.endswith(".part") and not f.startswith(".")])

        print("\n" + "=" * 70)
        print(f"  FULL-RES photos : {n_files(FULLRES_DIR):>5}  -> ./{FULLRES_DIR}/   <-- keep these")
        print(f"  Videos          : {n_files(VIDEO_DIR):>5}  -> ./{VIDEO_DIR}/")
        print(f"  Thumbnails      : {n_files(PHOTO_DIR):>5}  -> ./{PHOTO_DIR}/   (backup)")
        print(f"  Oldest loaded   : {oldest}   (target {TARGET_DATE})")
        print(f"  Failed          : {len(failed)}")
        print("=" * 70)

        input("\nPress ENTER to close the browser and exit... ")
        browser.close()


def parse_args():
    ap = argparse.ArgumentParser(
        description="Download Procare photos/videos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python download_procare_photos.py --test        # scroll + thumbnails only (fast)
  python download_procare_photos.py               # thumbnails + full-res originals
  python download_procare_photos.py --fullres     # originals only, skip thumbnails
  python download_procare_photos.py --test --max-scrolls 10   # quick smoke test
""")
    ap.add_argument("--scroll-only", action="store_true",
                    help="Prove the scroll works: load the whole feed back to the "
                         "target date and report, downloading nothing.")
    ap.add_argument("--test", action="store_true",
                    help="TEST RUN: scroll to the target date and download "
                         "thumbnails only. Skips the slow full-res pass.")
    ap.add_argument("--fullres", action="store_true",
                    help="Skip thumbnails; only do the full-resolution pass (default).")
    ap.add_argument("--thumbs", action="store_true",
                    help="Also download feed thumbnails alongside the originals.")
    ap.add_argument("--target-date", metavar="YYYY-MM-DD",
                    help=f"Scroll back to this date (default {TARGET_DATE}).")
    ap.add_argument("--max-scrolls", type=int, metavar="N",
                    help="Cap the number of scroll rounds (handy for smoke tests).")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.scroll_only:
        SCROLL_ONLY = True
        print(">> SCROLL ONLY: loading the whole feed, downloading nothing\n")
    elif args.test:
        PHASE2_ORIGINALS = False      # skip the slow click-through pass
        PHASE1_THUMBNAILS = True
        print(">> TEST RUN: scrolling + thumbnails only (no full-res pass)\n")
    else:
        PHASE1_THUMBNAILS = bool(args.thumbs)
        PHASE2_ORIGINALS = True
        print(">> FULL RUN: originals via the viewer's download link"
              f"{' + thumbnails' if args.thumbs else ''}\n")

    if args.target_date:
        TARGET_DATE = dt.date.fromisoformat(args.target_date)
    if args.max_scrolls:
        MAX_SCROLLS = args.max_scrolls

    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user. Whatever was downloaded is kept.")
        sys.exit(0)
