"""
procare_download.py — bulk-download photos and videos from the Procare parent portal.

Walks the gallery's weekly date filter back to a target date and saves every
item at full resolution.

Architecture
------------
Three decoupled pieces, so nothing waits on anything it doesn't have to:

  Gallery   drives one browser tab. Changing week is *network*-driven: it waits
            for the gallery's own JSON payload rather than polling the DOM, and
            reads items straight out of that JSON. No photo viewer is opened.
  Walker    steps back week by week, turning payloads into MediaItems and
            pushing them onto a queue. Falls back to clicking a tile and reading
            its download link only for items the JSON doesn't cover.
  Downloads a thread pool fetching over plain HTTP. The CDN links are CloudFront
            signed URLs (signature in the query string), so they need neither
            browser nor cookies and parallelise freely.

Login happens once; the session is saved and reused. The password is never
stored — only the session Procare itself issued.

Output: flat files named  YYYYMMDD_<contenthash>.<ext>  so re-runs never
duplicate, plus a CSV manifest.
"""

from __future__ import annotations

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
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
BASE = "https://schools.procareconnect.com"
SESSION_FILE = "procare_session.json"
DEFAULT_OUT = "procare_media"
MANIFEST_NAME = "procare_manifest.csv"

DEFAULT_TARGET = dt.date(2025, 8, 18)
MAX_WEEKS = 200
MAX_FEED_SCROLLS = 600     # feed loads ~30 activities per scroll

# Timing. Week changes are detected via the network, so these are just ceilings.
WEEK_TIMEOUT = 20.0        # hard cap waiting for a week to load
PAYLOAD_GRACE = 0.35       # settle time after the week's JSON arrives
VIEWER_TIMEOUT = 12.0      # fallback path: waiting for the viewer's link
TILE_RETRIES = 3

# Procare DOM selectors — the things most likely to break on a redesign.
SEL_DROPDOWN = "div.date-filter .dropdown-portal__header"     # Daily/Weekly picker
SEL_DATE_TITLE = '[data-testid="datepicker-title"]'           # "Aug 10 - Aug 16"
SEL_ARROW = "div.date-filter .datepicker__arrow"              # < and > week arrows
SEL_TILE = "div.gallery__item"                                # one media tile
SEL_MODAL = "div.modal"
SEL_DL_ANCHOR = "a.action-button[href], a[download][href]"    # full-size link

# Dashboard activity feed — holds photos that never appear in the gallery.
SEL_ACTIVITY_LIST = "div.activity-list"
SEL_ACTIVITY = "div.activity"
SEL_ACTIVITY_DATE = "div.activity-date"

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CDN_RE = re.compile(r"https://private\.cdn\.procareconnect\.com/[^\"'\\\s]+")
DATE_KEYS = ("captured_at", "taken_at", "created_at", "activity_date",
             "date", "created_on", "uploaded_at")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

PRINT_LOCK = threading.Lock()


def log(who: str, msg: str) -> None:
    with PRINT_LOCK:
        print(f"[{who:<9}] {msg}", flush=True)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def parse_week_start(title: str | None, not_after: dt.date) -> dt.date | None:
    """'Aug 10 - Aug 16' -> date. The label carries no year, so infer it."""
    if not title:
        return None
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})", title)
    if not m:
        return None
    month = MONTHS.get(m.group(1)[:3].lower())
    if not month:
        return None
    day = int(m.group(2))
    for year in (not_after.year, not_after.year - 1, not_after.year - 2):
        try:
            d = dt.date(year, month, day)
        except ValueError:
            continue
        if d <= not_after + dt.timedelta(days=3):
            return d
    return None


def parse_iso(s) -> dt.date | None:
    if not isinstance(s, str) or len(s) < 8:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def date_in_url(url: str | None) -> dt.date | None:
    """Some CDN filenames embed the date, e.g. open-uri20260810-1-clgmbz."""
    m = re.search(r"open-uri(\d{4})(\d{2})(\d{2})", url or "")
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


# --------------------------------------------------------------------------- #
# Media items
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MediaItem:
    url: str                 # full-size, signed CDN URL
    key: str                 # stable id (signature-free, so it survives reloads)
    when: dt.date
    tab: str
    week: str = ""

    @property
    def is_video(self) -> bool:
        return (os.path.splitext(urlparse(self.url).path)[1].lower() in VIDEO_EXTS
                or "/attachments/" in self.url)


def stable_key(url: str) -> str:
    """
    Identify a file across reloads.

    The query string holds a per-request CloudFront signature, so it can't be
    part of the key. The path's first UUID is stable; fall back to the path.
    """
    path = urlparse(url).path
    m = UUID_RE.search(path)
    return m.group(0) if m else path


# Procare names its renditions explicitly, so pick by key rather than by
# guessing from the URL. An item looks like:
#   {thumb_url, medium_url, main_url, video_file_url, is_video, date, ...}
# and an activity wrapper carries photo_url + activity_time + activity_type.
URL_KEYS_FULL = ("video_file_url", "main_url", "photo_url")
URL_KEYS_IGNORE = ("thumb_url", "medium_url", "small_url", "profile_pic_url")


def pick_full_size(node: dict) -> str | None:
    """
    The full-size URL for one item dict.

    For a video, only the video file is wanted — its poster frame is a
    near-duplicate of a frame we don't need. Otherwise the main rendition.
    """
    if node.get("is_video") and isinstance(node.get("video_file_url"), str):
        return node["video_file_url"]
    # A video activity's photo_url is just the poster frame; the real file is on
    # the nested item, which recursion reaches anyway.
    if "video" in str(node.get("activity_type", "")).lower() and node.get("photo_url"):
        return None
    for key in URL_KEYS_FULL:
        val = node.get(key)
        if isinstance(val, str) and CDN_RE.match(val):
            return val
    return None


def pick_full_size_heuristic(urls: list[str]) -> str | None:
    """
    Fallback for dicts whose keys we don't recognise.

    Prefers the largest named rendition. Earlier this bailed out whenever two
    non-thumbnail URLs were present — which is every photo item, since they
    carry both main_url and medium_url — silently dropping the item.
    """
    full = [u for u in dict.fromkeys(urls)
            if "/thumb/" not in u and not re.search(r"profile_pic|avatar", u, re.I)]
    if not full:
        return None
    for marker in ("/attachments/", "/original/", "/main/", "/medium/"):
        hits = [u for u in full if marker in u]
        if hits:
            return hits[0]
    return full[0]


def item_date(node: dict) -> dt.date | None:
    for key in ("activity_time", "activity_date", "captured_at", "taken_at",
                "date", "created_at", "created_on", "uploaded_at"):
        when = parse_iso(node.get(key))
        if when:
            return when
    return None


def items_from_json(node, tab: str, out: list[MediaItem], fallback: dt.date,
                    inherited_date: dt.date | None = None) -> None:
    """
    Walk a decoded JSON body collecting one MediaItem per media-bearing dict.

    Dates are inherited downwards: an activity wrapper carries activity_time
    while the nested photo object may only have created_at (the upload time),
    so the wrapper's date wins for anything nested inside it.
    """
    if isinstance(node, dict):
        here = item_date(node) or inherited_date
        chosen = pick_full_size(node)
        if not chosen:
            strings = [v for k, v in node.items()
                       if isinstance(v, str) and k not in URL_KEYS_IGNORE]
            urls = [u for s in strings for u in CDN_RE.findall(s)]
            if urls:
                chosen = pick_full_size_heuristic(urls)
        if chosen:
            out.append(MediaItem(
                url=chosen, key=stable_key(chosen),
                when=here or date_in_url(chosen) or fallback, tab=tab))
        for v in node.values():
            items_from_json(v, tab, out, fallback, here)
    elif isinstance(node, list):
        for v in node:
            items_from_json(v, tab, out, fallback, inherited_date)


# --------------------------------------------------------------------------- #
# Shared state
# --------------------------------------------------------------------------- #
class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.data: dict[str, dict[str, int]] = {}
        self.bytes = 0

    def bump(self, tab: str, field_: str, n: int = 1) -> int:
        with self._lock:
            d = self.data.setdefault(tab, {})
            d[field_] = d.get(field_, 0) + n
            return d[field_]

    def add_bytes(self, n: int) -> None:
        with self._lock:
            self.bytes += n

    def get(self, tab: str, field_: str) -> int:
        with self._lock:
            return self.data.get(tab, {}).get(field_, 0)


class Store:
    """Output directory: naming, de-duplication, progress, manifest."""

    def __init__(self, out_dir: str):
        self.dir = os.path.abspath(os.path.expanduser(out_dir))
        os.makedirs(self.dir, exist_ok=True)
        self.manifest = os.path.join(self.dir, MANIFEST_NAME)
        self._lock = threading.Lock()
        self._hashes: set[str] = set()
        self._done: dict[str, set[str]] = {}
        self._rows: list[tuple] = []
        self._scan()

    def _scan(self) -> None:
        """Index existing files and any per-source progress written before."""
        for name in os.listdir(self.dir):
            m = re.match(r"\d{8}_([0-9a-f]{12})", name)
            if m:
                self._hashes.add(m.group(1))
                continue
            m = re.match(r"\.progress_(\w+)\.json$", name)
            if m:
                try:
                    with open(os.path.join(self.dir, name)) as fh:
                        self._done[m.group(1)] = set(json.load(fh))
                except Exception:
                    pass

    def _progress_path(self, tab: str) -> str:
        return os.path.join(self.dir, f".progress_{tab}.json")

    def existing_count(self) -> int:
        return len(self._hashes)

    def is_done(self, tab: str, key: str) -> bool:
        """
        Has this file been fetched by ANY source?

        The gallery and the feed serve the same photo under the same file UUID,
        so checking every source's progress avoids re-downloading a file just to
        discover by content hash that we already have it. Progress is still
        recorded per source.
        """
        with self._lock:
            self._done.setdefault(tab, set())
            return any(key in done for done in self._done.values())

    def mark_done(self, tab: str, key: str) -> None:
        with self._lock:
            self._done.setdefault(tab, set()).add(key)

    def save_progress(self, tab: str) -> None:
        with self._lock:
            keys = sorted(self._done.setdefault(tab, set()))
        try:
            with open(self._progress_path(tab), "w") as fh:
                json.dump(keys, fh)
        except Exception:
            pass

    @staticmethod
    def _ext(url: str, content_type: str, is_video: bool) -> str:
        ext = os.path.splitext(urlparse(url).path)[1].lower()
        if ext in IMAGE_EXTS + VIDEO_EXTS:
            return ext
        ct = (content_type or "").lower()
        for key, e in (("mp4", ".mp4"), ("quicktime", ".mov"), ("webm", ".webm"),
                       ("jpeg", ".jpg"), ("png", ".png"), ("webp", ".webp"),
                       ("gif", ".gif"), ("heic", ".heic")):
            if key in ct:
                return e
        return ".mp4" if is_video else ".jpg"

    def save(self, data: bytes, item: MediaItem, content_type: str):
        """Write named by content hash. Returns (filename, 'saved'|'dup')."""
        digest = hashlib.sha1(data).hexdigest()[:12]
        with self._lock:
            if digest in self._hashes:
                return None, "dup"
            self._hashes.add(digest)
        name = f"{item.when.strftime('%Y%m%d')}_{digest}" \
               f"{self._ext(item.url, content_type, item.is_video)}"
        path = os.path.join(self.dir, name)
        if os.path.exists(path):
            return name, "dup"
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        with self._lock:
            self._rows.append((name, item.when.isoformat(), item.week, item.url))
        return name, "saved"

    def write_manifest(self) -> None:
        with self._lock:
            rows = sorted(self._rows)
        if not rows:
            return
        new = not os.path.exists(self.manifest)
        with open(self.manifest, "a") as fh:
            if new:
                fh.write("filename,date,week,source_url\n")
            for r in rows:
                fh.write('"{}","{}","{}","{}"\n'.format(*r))

    def summary(self) -> tuple[int, int]:
        total = size = 0
        for name in os.listdir(self.dir):
            if name.startswith(".") or name == MANIFEST_NAME:
                continue
            total += 1
            try:
                size += os.path.getsize(os.path.join(self.dir, name))
            except OSError:
                pass
        return total, size


# --------------------------------------------------------------------------- #
# Download pool
# --------------------------------------------------------------------------- #
class DownloadPool:
    """Fetches signed CDN URLs over plain HTTP — no browser involved."""

    def __init__(self, store: Store, stats: Stats, threads: int):
        self.store, self.stats = store, stats
        self.q: queue.Queue = queue.Queue()
        self.threads = [threading.Thread(target=self._worker, args=(i + 1,),
                                         daemon=True) for i in range(threads)]

    def start(self) -> None:
        for t in self.threads:
            t.start()

    def submit(self, item: MediaItem) -> None:
        self.q.put(item)

    def pending(self) -> int:
        return self.q.qsize()

    def drain(self) -> None:
        self.q.join()
        for _ in self.threads:
            self.q.put(None)

    @staticmethod
    def _session() -> requests.Session:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        s.mount("https://", adapter)
        s.headers.update({"User-Agent": UA, "Referer": BASE})
        return s

    def _worker(self, idx: int) -> None:
        session = self._session()
        while True:
            item = self.q.get()
            if item is None:
                self.q.task_done()
                return
            try:
                data, ct = None, ""
                for attempt in range(4):
                    try:
                        r = session.get(item.url, timeout=180)
                        if r.ok:
                            data, ct = r.content, r.headers.get("Content-Type", "")
                            break
                        if r.status_code not in (403, 408, 429, 500, 502, 503, 504):
                            break
                    except Exception:
                        pass
                    time.sleep(1.0 * (2 ** attempt))

                if not data:
                    self.stats.bump(item.tab, "misses")
                    log(f"dl{idx}", f"FAILED {item.url[:70]}")
                    continue

                name, status = self.store.save(data, item, ct)
                self.store.mark_done(item.tab, item.key)
                if status == "saved":
                    self.stats.add_bytes(len(data))
                    n = self.stats.bump(item.tab, "saved")
                    if n % 50 == 0:
                        log(f"dl{idx}", f"{item.tab}: {n} saved")
                else:
                    self.stats.bump(item.tab, "dups")
            finally:
                self.q.task_done()


# --------------------------------------------------------------------------- #
# Gallery — one browser tab
# --------------------------------------------------------------------------- #
class PayloadSource:
    """
    Base for pages whose data arrives as JSON.

    Captures every JSON response containing Procare URLs, so callers can read
    items from the API payload rather than scraping the rendered DOM. `seq`
    also doubles as a "something loaded" signal for waiting on navigation.
    """

    def __init__(self, page):
        self.page = page
        self._payloads: list = []
        self._seq = 0
        self._lock = threading.Lock()
        page.on("response", self._on_response)

    def _on_response(self, resp) -> None:
        try:
            if "json" not in resp.headers.get("content-type", "").lower():
                return
            body = resp.text()
            if not body or len(body) > 6_000_000:
                return
            with self._lock:
                self._seq += 1
                if "procareconnect.com" in body:
                    self._payloads.append(json.loads(body))
        except Exception:
            pass

    def take_payloads(self) -> list:
        with self._lock:
            out, self._payloads = self._payloads, []
            return out

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def _pump(self) -> None:
        """Let Playwright deliver queued events."""
        try:
            self.page.evaluate("1")
        except Exception:
            pass


class Gallery(PayloadSource):
    """
    The Photos/Videos gallery, paged by its weekly date filter.

    Week changes are detected from the network: a click on the < arrow triggers
    a JSON payload, and that payload is both the completion signal AND the data.
    That avoids polling the DOM for tiles to settle, which was the slow part.
    """

    def __init__(self, page, tab: str):
        super().__init__(page)
        self.tab = tab

    # -- navigation -------------------------------------------------------- #
    def open(self, gid: str) -> None:
        self.page.goto(f"{BASE}/dashboard/gallery/{gid}/{self.tab}",
                       wait_until="domcontentloaded")
        self.page.wait_for_selector(SEL_DATE_TITLE, timeout=30000)
        self.set_weekly()

    def set_weekly(self) -> bool:
        try:
            cur = self.page.eval_on_selector(SEL_DROPDOWN, "e => e.innerText.trim()") or ""
            if "week" in cur.lower():
                return True
            self.page.click(SEL_DROPDOWN, timeout=5000)
            time.sleep(0.6)
            self.page.evaluate(r"""
            () => {
                for (const e of document.querySelectorAll('div, li, span, button, [role=option]')) {
                    if (e.children.length) continue;
                    if ((e.innerText || '').trim().toLowerCase() === 'weekly') {
                        (e.closest('[role=option], li, div') || e).click(); return;
                    }
                }
            }""")
            time.sleep(1.5)
            return True
        except Exception:
            return False

    def title(self) -> str | None:
        try:
            return self.page.eval_on_selector(SEL_DATE_TITLE, "e => e.innerText.trim()")
        except Exception:
            return None

    def prev_week(self) -> bool:
        """Click the < arrow, excluding the period dropdown's chevron."""
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
            return bool(self.page.evaluate(js))
        except Exception:
            return False

    def await_week(self, old_title: str | None, timeout: float = WEEK_TIMEOUT) -> bool:
        """
        Wait for the next week: label changes, then its JSON payload lands.
        Network-driven, so it returns as soon as the data is actually there.
        """
        t0 = time.time()
        before_seq = self.seq
        changed = False
        while time.time() - t0 < timeout:
            if self.title() != old_title:
                changed = True
                break
            time.sleep(0.08)
        while time.time() - t0 < timeout:
            self._pump()
            if self.seq > before_seq:
                time.sleep(PAYLOAD_GRACE)   # let a second page of JSON arrive
                self._pump()
                break
            time.sleep(0.08)
        return changed

    # -- tiles (fallback path) --------------------------------------------- #
    def tiles(self) -> list[dict]:
        js = r"""
        () => {
            document.querySelectorAll('[data-tile]').forEach(e => e.removeAttribute('data-tile'));
            const root = document.querySelector('div.gallery') || document.body;
            const out = [];
            let n = 0;
            root.querySelectorAll('div.gallery__item').forEach(el => {
                let src = '';
                const b = getComputedStyle(el).backgroundImage;
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
            return self.page.evaluate(js)
        except Exception:
            return []

    def _modal_gone(self, timeout: float = 4.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self.page.evaluate("() => !document.querySelector('div.modal')"):
                    return True
            except Exception:
                return True
            time.sleep(0.08)
        return False

    def href_via_viewer(self, idx: int, last_href: str | None = None) -> str | None:
        """Fallback: open the tile's viewer and read its download link."""
        href = None
        try:
            self._modal_gone(3.0)
            el = self.page.query_selector(f'[data-tile="{idx}"]')
            if not el:
                return None
            el.scroll_into_view_if_needed(timeout=4000)
            try:
                el.click(timeout=6000)
            except Exception:
                self.page.keyboard.press("Escape")
                self._modal_gone(3.0)
                el.click(timeout=6000, force=True)
            deadline = time.time() + VIEWER_TIMEOUT
            while time.time() < deadline:
                try:
                    h = self.page.eval_on_selector(SEL_DL_ANCHOR,
                                                   "a => a.getAttribute('href')")
                except Exception:
                    h = None
                if h and h != last_href:      # guard against a stale modal
                    href = h
                    break
                time.sleep(0.1)
        except Exception:
            href = None
        finally:
            try:
                self.page.keyboard.press("Escape")
                self._modal_gone(4.0)
            except Exception:
                pass
        return href


class Feed(PayloadSource):
    """
    The dashboard activity feed.

    Some photos are posted as activities and never appear in the gallery, so
    this is a second, independent source. The feed is an infinite scroll that
    loads ~30 activities at a time inside a nested container (`section.section`,
    NOT the window), so scrolling has to target that container. Items are read
    from the JSON payloads, same as the gallery.
    """

    def open(self) -> None:
        self.page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded")
        try:
            self.page.wait_for_selector(SEL_ACTIVITY, timeout=30000)
        except Exception:
            pass
        time.sleep(1.5)

    def activity_count(self) -> int:
        try:
            return self.page.evaluate(
                "(s) => document.querySelectorAll(s).length", SEL_ACTIVITY)
        except Exception:
            return 0

    def oldest_date(self) -> dt.date | None:
        """Oldest label in the feed. Labels are 'Aug 12, 2026', 'Today', ..."""
        try:
            labels = self.page.eval_on_selector_all(
                SEL_ACTIVITY_DATE, "els => els.map(e => e.textContent.trim())")
        except Exception:
            return None
        dates = []
        for text in labels:
            m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", text or "")
            if not m:
                continue
            month = MONTHS.get(m.group(1)[:3].lower())
            if not month:
                continue
            try:
                dates.append(dt.date(int(m.group(3)), month, int(m.group(2))))
            except ValueError:
                pass
        return min(dates) if dates else None

    def _scroll(self) -> None:
        """
        Scroll the feed's real container.

        Confirmed by measurement: the feed lives in `section.section`
        (overflow-y:auto). The window does not scroll, and div.activity-list
        has scrollHeight == clientHeight, so neither of those works.
        """
        js = r"""
        (listSel) => {
            const scrolls = el => el && el.scrollHeight > el.clientHeight + 50 &&
                /(auto|scroll|overlay)/.test(getComputedStyle(el).overflowY);
            let el = document.querySelector(listSel), target = null;
            while (el) { if (scrolls(el)) { target = el; break; } el = el.parentElement; }
            if (!target && scrolls(document.querySelector('section.section')))
                target = document.querySelector('section.section');
            if (!target) target = [...document.querySelectorAll('*')].find(scrolls);
            if (target) { target.scrollTop = target.scrollHeight; return true; }
            window.scrollTo(0, document.body.scrollHeight);
            return false;
        }"""
        try:
            self.page.evaluate(js, SEL_ACTIVITY_LIST)
        except Exception:
            pass

    def _press_end(self) -> None:
        """Second scroll technique, also measured to work, used when stuck."""
        try:
            box = self.page.query_selector(SEL_ACTIVITY_LIST)
            if box:
                b = box.bounding_box()
                if b:
                    self.page.mouse.click(b["x"] + 5, b["y"] + 5)
            self.page.keyboard.press("End")
        except Exception:
            pass

    def load_more(self, timeout: float = 12.0) -> bool:
        """Scroll once and wait for more activities. True if the feed grew."""
        before = self.activity_count()
        self._scroll()
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.25)
            self._pump()
            if self.activity_count() > before:
                time.sleep(PAYLOAD_GRACE)
                self._pump()
                return True
        self._press_end()
        deadline = time.time() + 4.0
        while time.time() < deadline:
            time.sleep(0.25)
            self._pump()
            if self.activity_count() > before:
                self._pump()
                return True
        return False


def walk_feed(target: dt.date, headless: bool, store: Store | None,
              pool: DownloadPool | None, stats: Stats,
              dry_run: bool = False) -> WalkResult:
    """Scroll the activity feed back to the target date, queueing media."""
    who = "feed"
    res = WalkResult()
    seen: set[str] = set()
    stale = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=SESSION_FILE,
                                      viewport={"width": 1500, "height": 950})
        feed = Feed(context.new_page())
        feed.open()

        for round_no in range(MAX_FEED_SCROLLS):
            oldest = feed.oldest_date()
            if oldest:
                res.oldest = oldest
                res.newest = res.newest or oldest

            found: list[MediaItem] = []
            for payload in feed.take_payloads():
                items_from_json(payload, "feed", found, oldest or dt.date.today())

            queued = 0
            for it in found:
                if it.key in seen:
                    continue
                seen.add(it.key)
                if store and store.is_done("feed", it.key):
                    continue
                res.items += 1
                if pool:
                    pool.submit(MediaItem(it.url, it.key, it.when, "feed",
                                          it.when.isoformat()))
                    queued += 1
                    stats.bump("feed", "fast")

            n = feed.activity_count()
            res.weeks = round_no + 1
            if round_no % 5 == 0 or queued:
                log(who, f"activities={n}  oldest={oldest}  "
                         f"media={res.items}" +
                    (f"  queued={queued}  [q={pool.pending()}]" if pool else ""))
            if store:
                store.save_progress("feed")

            if oldest and oldest <= target:
                res.reached = True
                log(who, f"reached target {oldest} <= {target}")
                break
            if not feed.load_more():
                stale += 1
                if stale >= 3:
                    log(who, f"no more activities load (oldest {oldest}) — stopping")
                    break
            else:
                stale = 0

        browser.close()

    log(who, f"done: {res.weeks} scroll(s), {res.items} media item(s)")
    return res


# --------------------------------------------------------------------------- #
# Direct API access
# --------------------------------------------------------------------------- #
API_BASE = "https://api-school.procareconnect.com"
EP_KIDS = "/api/web/parent/kids/"
EP_ACTIVITIES = "/api/web/parent/daily_activities/"
EP_PHOTOS = "/api/web/parent/photos/"


class Api:
    """
    Talks to Procare's API the way the web app does.

    Far more reliable than scraping: the feed's own endpoint is paged and
    date-filtered, so nothing depends on scroll timing or what happens to be
    rendered. Auth is the Bearer token captured from the logged-in browser and
    kept only in memory.
    """

    def __init__(self, headers: dict):
        self.s = requests.Session()
        self.s.headers.update(headers)

    def get(self, path: str, params: dict | None = None) -> dict:
        r = self.s.get(API_BASE + path, params=params or {}, timeout=90)
        r.raise_for_status()
        return r.json()

    def kids(self) -> list[dict]:
        try:
            return self.get(EP_KIDS).get("kids", [])
        except Exception:
            return []

    @staticmethod
    def _rows(payload) -> list:
        """Find the list of records in a response, whatever it's called."""
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("daily_activities", "activities", "photos", "results",
                    "data", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
        for val in payload.values():         # fall back to the longest list
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
        return []

    def activities(self, kid_id: str, date_to: dt.date, page: int) -> list:
        payload = self.get(EP_ACTIVITIES, {
            "kid_id": kid_id,
            "filters[daily_activity][date_to]": date_to.isoformat(),
            "page": page,
        })
        return self._rows(payload)

    def photos(self, kid_id: str, page: int) -> list:
        payload = self.get(EP_PHOTOS, {"kid_id": kid_id, "page": page})
        return self._rows(payload)


def walk_api(kid_id: str, kid_name: str, api: Api, target: dt.date,
             store: Store | None, pool: DownloadPool | None, stats: Stats,
             tab: str = "api") -> WalkResult:
    """
    Page the activity API back to the target date.

    This is the authoritative source: the same endpoint the feed uses, but read
    directly, so no photo can be missed because a scroll didn't render it.
    """
    who = f"api:{kid_name[:6]}" if kid_name else "api"
    res = WalkResult()
    seen: set[str] = set()
    date_to = dt.date.today()
    page = 1
    empty_pages = 0

    while page <= 500:
        try:
            rows = api.activities(kid_id, date_to, page)
        except Exception as exc:
            log(who, f"API error on page {page}: {str(exc)[:70]}")
            break
        if not rows:
            empty_pages += 1
            if empty_pages >= 2:
                log(who, f"no more activities (page {page})")
                break
            page += 1
            continue
        empty_pages = 0

        found: list[MediaItem] = []
        items_from_json(rows, tab, found, date_to)

        oldest = None
        for row in rows:
            d = item_date(row) if isinstance(row, dict) else None
            if d and (oldest is None or d < oldest):
                oldest = d
        if oldest:
            res.oldest = oldest
            res.newest = res.newest or oldest

        queued = 0
        for it in found:
            if it.key in seen:
                continue
            seen.add(it.key)
            if store and store.is_done(tab, it.key):
                continue
            res.items += 1
            if pool:
                pool.submit(MediaItem(it.url, it.key, it.when, tab,
                                      it.when.isoformat()))
                queued += 1
                stats.bump(tab, "fast")

        res.weeks = page
        if queued or page % 10 == 0:
            log(who, f"page {page}: {len(rows)} activities, oldest={oldest}, "
                     f"media={res.items}" +
                (f", queued={queued} [q={pool.pending()}]" if pool else ""))
        if store:
            store.save_progress(tab)

        if oldest and oldest <= target:
            res.reached = True
            log(who, f"reached target {oldest} <= {target}")
            break
        page += 1

    log(who, f"done: {res.weeks} page(s), {res.items} media item(s)")
    return res


# --------------------------------------------------------------------------- #
# Walking a tab
# --------------------------------------------------------------------------- #
@dataclass
class WalkResult:
    weeks: int = 0
    items: int = 0
    oldest: dt.date | None = None
    newest: dt.date | None = None
    gaps: list[str] = field(default_factory=list)
    reached: bool = False


def make_gallery(p, tab: str, gid: str, headless: bool):
    browser = p.chromium.launch(headless=headless)
    context = browser.new_context(storage_state=SESSION_FILE,
                                  viewport={"width": 1500, "height": 950})
    gallery = Gallery(context.new_page(), tab)
    gallery.open(gid)
    return browser, gallery


def walk_tab(tab: str, gid: str, target: dt.date, headless: bool,
             store: Store | None, pool: DownloadPool | None, stats: Stats,
             worker: int = 0, skip_weeks: int = 0, max_weeks: int = MAX_WEEKS,
             dry_run: bool = False) -> WalkResult:
    """
    Step back week by week. With a pool, queue every new item for download;
    without one (dry run / nav test), just count what's there.
    """
    who = tab if worker == 0 else f"{tab[:3]}#{worker}"
    res = WalkResult()
    cursor = dt.date.today()
    prev_start: dt.date | None = None

    with sync_playwright() as p:
        browser, g = make_gallery(p, tab, gid, headless)

        if skip_weeks:
            log(who, f"skipping back {skip_weeks} week(s) to my slice...")
            for _ in range(skip_weeks):
                before = g.title()
                if not g.prev_week():
                    break
                g.await_week(before)
            log(who, f"slice starts at {g.title()!r}")
        g.take_payloads()   # discard anything gathered while skipping

        for _ in range(max_weeks):
            title = g.title()
            week_start = parse_week_start(title, cursor)
            if week_start:
                cursor = week_start
            when_fallback = week_start or dt.date.today()

            # Items come from the JSON the page just fetched.
            found: list[MediaItem] = []
            for payload in g.take_payloads():
                items_from_json(payload, tab, found, when_fallback)

            # De-duplicate within the week and drop anything already downloaded.
            unique: dict[str, MediaItem] = {}
            for it in found:
                unique.setdefault(it.key, MediaItem(it.url, it.key, it.when,
                                                    tab, title or ""))
            tiles = g.tiles()

            # Fallback: tiles the JSON didn't explain (rare) get the viewer path.
            if len(unique) < len(tiles) and not dry_run:
                last = None
                for t in tiles:
                    m = UUID_RE.search(t["src"])
                    tile_key = m.group(0) if m else t["src"]
                    if any(tile_key in k or k in tile_key for k in unique):
                        continue
                    href = None
                    for attempt in range(TILE_RETRIES):
                        href = g.href_via_viewer(t["idx"], last)
                        if href:
                            break
                        time.sleep(0.6 * (attempt + 1))
                    if not href:
                        stats.bump(tab, "misses")
                        continue
                    last = href
                    stats.bump(tab, "clicked")
                    k = stable_key(href)
                    unique.setdefault(k, MediaItem(
                        href, k, date_in_url(href) or when_fallback, tab, title or ""))

            queued = 0
            for it in unique.values():
                if store and store.is_done(tab, it.key):
                    continue
                res.items += 1
                if pool:
                    pool.submit(it)
                    queued += 1
                    stats.bump(tab, "fast")

            res.weeks += 1
            res.newest = res.newest or week_start
            res.oldest = week_start or res.oldest
            if prev_start and week_start and (prev_start - week_start).days != 7:
                res.gaps.append(f"{prev_start} -> {week_start} "
                                f"({(prev_start - week_start).days}d)")
            prev_start = week_start

            suffix = f"queued={queued}  [q={pool.pending()}]" if pool else \
                     f"items={len(unique)}"
            log(who, f"{title!r} ({week_start})  tiles={len(tiles)}  {suffix}")
            if store:
                store.save_progress(tab)

            if week_start and week_start <= target:
                res.reached = True
                log(who, f"reached target {week_start} <= {target}")
                break
            before = title
            if not g.prev_week():
                log(who, "could not click the previous-week arrow — stopping")
                break
            if not g.await_week(before):
                log(who, "week label did not change — stopping")
                break

        browser.close()

    log(who, f"done: {res.weeks} weeks, {res.items} items")
    return res


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
@dataclass
class Auth:
    gallery_id: str | None = None
    headers: dict = field(default_factory=dict)   # incl. the Bearer token
    kids: list = field(default_factory=list)


def ensure_session(force_relogin: bool = False) -> Auth:
    """
    Reuse the saved session if it works, else log in once and save it.

    Also captures the API request headers (the Bearer token the web app uses),
    which stay in memory only — they are never written to disk or logged.
    """
    auth = Auth()
    have = os.path.exists(SESSION_FILE) and not force_relogin
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            storage_state=SESSION_FILE if have else None,
            viewport={"width": 1500, "height": 950})
        page = context.new_page()

        def on_request(req):
            try:
                if "api-school.procareconnect.com" not in req.url or auth.headers:
                    return
                h = req.headers
                if not h.get("authorization"):
                    return
                auth.headers = {k: v for k, v in h.items()
                                if k.lower() in ("authorization", "accept",
                                                 "requested-from", "user-agent",
                                                 "referer", "origin")}
            except Exception:
                pass

        page.on("request", on_request)
        page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded")
        time.sleep(4.0)

        def gallery_id() -> str | None:
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
            print("\n" + "=" * 72)
            print("  Please LOG IN in the browser window (one time only).")
            print(f"  The session is saved to {SESSION_FILE} and reused after this.")
            print("=" * 72)
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

        # Nudge the app into an API call if we haven't seen one yet.
        if not auth.headers:
            page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded")
            time.sleep(4.0)

        context.storage_state(path=SESSION_FILE)
        try:
            os.chmod(SESSION_FILE, 0o600)
        except Exception:
            pass
        browser.close()

    auth.gallery_id = gid
    if auth.headers:
        auth.kids = Api(auth.headers).kids()
    return auth


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_jobs(tabs: list[str], workers: int, target: dt.date) -> list[tuple]:
    """(tab, worker_no, skip_weeks, max_weeks) — photos split across workers."""
    total = max(1, ((dt.date.today() - target).days // 7) + 2)
    jobs = []
    for tab in tabs:
        n = workers if (tab == "photos" and workers > 1) else 1
        if n == 1:
            jobs.append((tab, 0, 0, MAX_WEEKS))
            continue
        chunk = -(-total // n)
        for i in range(n):
            if i * chunk >= total:
                break
            jobs.append((tab, i + 1, i * chunk, chunk))
    return jobs


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Download all photos and videos from the Procare gallery.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  %(prog)s --nav-test                     check the range, download nothing
  %(prog)s                                download to ./procare_media
  %(prog)s -o ~/Pictures/Daycare          download somewhere else
  %(prog)s --target-date 2025-07-01 -w 3  go further back, 3 browsers
""")
    ap.add_argument("-o", "--out", metavar="DIR", default=DEFAULT_OUT,
                    help=f"Download folder (default: ./{DEFAULT_OUT}). "
                         "Created if missing; ~ is expanded.")
    ap.add_argument("--target-date", metavar="YYYY-MM-DD",
                    help=f"Stop once this week is reached (default {DEFAULT_TARGET})")
    ap.add_argument("--nav-test", action="store_true",
                    help="Verify the whole range is reachable; download nothing.")
    ap.add_argument("--source", choices=["all", "api", "gallery", "feed"],
                    default="all",
                    help="Where to look. 'api' reads the activity endpoint "
                         "directly (fastest and most complete), 'gallery' and "
                         "'feed' scrape the pages. Default 'all' does the API "
                         "plus the gallery, which together cover everything.")
    ap.add_argument("--kid", metavar="ID",
                    help="Limit to one child (id prefix). Default: all children "
                         "on the account.")
    ap.add_argument("--photos-only", action="store_true")
    ap.add_argument("--videos-only", action="store_true")
    ap.add_argument("--relogin", action="store_true",
                    help="Ignore the saved session and log in again.")
    ap.add_argument("--show", action="store_true",
                    help="Show the browser windows (default: hidden).")
    ap.add_argument("-w", "--workers", type=int, default=2, metavar="N",
                    help="Browsers splitting the photo date range (default 2).")
    ap.add_argument("-d", "--dl-threads", type=int, default=12, metavar="M",
                    help="Parallel download threads (default 12).")
    args = ap.parse_args()

    target = (dt.date.fromisoformat(args.target_date)
              if args.target_date else DEFAULT_TARGET)
    tabs = ["photos", "videos"]
    if args.photos_only:
        tabs = ["photos"]
    if args.videos_only:
        tabs = ["videos"]
    if args.source in ("feed", "api"):
        tabs = []
    use_feed = args.source == "feed"
    use_api = args.source in ("all", "api")
    sources = tabs + (["feed"] if use_feed else []) + (["api"] if use_api else [])

    store = Store(args.out)
    stats = Stats()
    headless = not args.show

    print()
    print("=" * 72)
    print(f"  Download folder : {store.dir}")
    print(f"  Already there   : {store.existing_count()} file(s)")
    print(f"  Going back to   : {target}")
    print(f"  Sources         : {', '.join(sources)}")
    print("=" * 72 + "\n")

    auth = ensure_session(args.relogin)
    gid = auth.gallery_id
    api = Api(auth.headers) if auth.headers else None

    if auth.kids:
        print(f"  Children on the account: "
              f"{', '.join(k.get('name') or k.get('first_name') or k.get('id', '?')[:8] for k in auth.kids)}")
    if api:
        print("  API access: OK (reading the feed's own endpoint directly)")
    else:
        print("  API access: unavailable — falling back to browser scraping")

    kid_ids = [k["id"] for k in auth.kids if k.get("id")]
    if args.kid and args.kid != "all":
        kid_ids = [k for k in kid_ids if k.startswith(args.kid)]
    if not kid_ids and gid:
        kid_ids = [gid]

    if not gid and not kid_ids:
        print("!! Could not determine the gallery/kid id. Open Photos/Videos, then re-run.")
        sys.exit(1)

    t0 = time.time()

    # ------------------------- nav test ------------------------- #
    if args.nav_test:
        print("\n  NAV TEST — walking the range, downloading nothing.\n")
        results: dict[str, WalkResult] = {}

        def nav(tab):
            results[tab] = walk_tab(tab, gid, target, headless, None, None,
                                    stats, dry_run=True)

        def nav_feed():
            results["feed"] = walk_feed(target, headless, None, None, stats,
                                        dry_run=True)

        def nav_api():
            results["api"] = walk_api(kid_ids[0], "", api, target, None, None, stats)

        threads = [threading.Thread(target=nav, args=(t,)) for t in tabs]
        if use_feed:
            threads.append(threading.Thread(target=nav_feed))
        if use_api and api and kid_ids:
            threads.append(threading.Thread(target=nav_api))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        print("\n" + "=" * 72)
        ok = True
        for tab in sources:
            r = results.get(tab)
            if not r:
                continue
            ok &= r.reached and not r.gaps
            unit = "scrolls" if tab == "feed" else "weeks  "
            print(f"  {'OK ' if r.reached else '!! '}{tab:<7} "
                  f"{r.weeks:>3} {unit}  {r.items:>4} items  "
                  f"{r.newest} -> {r.oldest}  {len(r.gaps)} gap(s)")
            for g in r.gaps[:5]:
                print(f"        gap: {g}")
        print(f"  {time.time() - t0:.0f}s")
        print("=" * 72)
        print("  Range fully reachable — safe to download." if ok else
              "  Range incomplete — see above before downloading.")
        return

    # ------------------------- real run ------------------------- #
    pool = DownloadPool(store, stats, args.dl_threads)
    pool.start()
    print(f"  Saving to {store.dir}")
    print(f"  {args.dl_threads} download threads, "
          f"{args.workers} browser worker(s) on photos\n")

    # The gallery URL is /dashboard/gallery/<kid_id>/..., so every child needs
    # its own walk — otherwise siblings' videos are silently never visited.
    threads = []
    for kid_id in (kid_ids or [gid]):
        for tab, wk, skip, mx in build_jobs(tabs, args.workers, target):
            threads.append(threading.Thread(
                target=walk_tab,
                args=(tab, kid_id, target, headless, store, pool, stats,
                      wk, skip, mx)))
    if use_feed:
        threads.append(threading.Thread(
            target=walk_feed, args=(target, headless, store, pool, stats)))
    if use_api and api:
        for kid in (auth.kids or [{"id": k, "name": ""} for k in kid_ids]):
            if kid.get("id") not in kid_ids:
                continue
            threads.append(threading.Thread(
                target=walk_api,
                args=(kid["id"], kid.get("name") or kid.get("first_name") or "",
                      api, target, store, pool, stats)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    left = pool.pending()
    if left:
        print(f"\n  Browsing done — finishing {left} download(s)...")
    pool.drain()

    for tab in sources:
        store.save_progress(tab)
    store.write_manifest()

    count, size = store.summary()
    print("\n" + "=" * 72)
    for tab in sources:
        print(f"  {tab:<7}: {stats.get(tab, 'saved'):>4} new  "
              f"{stats.get(tab, 'dups'):>4} dup  "
              f"{stats.get(tab, 'misses'):>3} miss  "
              f"(viewer fallbacks: {stats.get(tab, 'clicked')})")
    print("-" * 72)
    print(f"  Downloaded this run : {human_size(stats.bytes)} "
          f"in {(time.time() - t0) / 60:.1f} min")
    print(f"  Folder now holds    : {count} file(s), {human_size(size)}")
    print(f"  FILES ARE IN        : {store.dir}")
    print(f"  Manifest            : {store.manifest}")
    print("=" * 72)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted — downloaded files and progress are kept. "
              "Re-run to resume.")
        sys.exit(0)
