"""
test_gallery_nav.py  (v2)

TEST ONLY — steps back a few weeks and downloads ONE photo + ONE video to prove
the whole pipeline works before the long run.

Fixes from v1's output:
  * Week arrows are div.datepicker__arrow (x=412 prev / x=615 next). v1 grabbed
    the period dropdown's chevron (icon dropdown-portal__header-arrow, x=386),
    so the week never changed and every step hit the 15s timeout.
  * v1 found 0 media URLs despite 8 tiles, so this version DUMPS the real tile
    markup instead of assuming <img src="https://private.cdn...">.

Dates: the picker title ("May 4 - May 10") has no year, so the year is inferred
by walking backwards from today — going back a week at a time, whenever the
parsed date would land in the future, the year decrements.

Test downloads land in ./test_downloads/ named YYYYMMDD_<hash>.<ext>.
"""

import datetime as dt
import hashlib
import os
import re
import time

from playwright.sync_api import sync_playwright

START_URL = "https://schools.procareconnect.com/"
WEEKS_TO_TEST = 3
OUT_DIR = "test_downloads"

SEL_DATE_FILTER = "div.date-filter"
SEL_DROPDOWN_HEADER = "div.date-filter .dropdown-portal__header"
SEL_DATE_TITLE = '[data-testid="datepicker-title"], [data-cy="date-filter-date-picker-title"]'
SEL_ARROWS = "div.date-filter .datepicker__arrow"       # [0]=prev, [-1]=next
SEL_DL_ANCHOR = "div.modal a.action-button[href], div.modal a[download][href]"

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def parse_title_start(title: str, not_after: dt.date):
    """
    'May 4 - May 10' -> date(2026, 5, 4). The title carries no year, so pick the
    most recent year that is not in the future relative to `not_after`.
    """
    if not title:
        return None
    m = re.search(r'([A-Za-z]{3,9})\.?\s+(\d{1,2})', title)
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
        if d <= not_after:
            return d
    return None


# --------------------------------------------------------------------------- #
# Gallery plumbing
# --------------------------------------------------------------------------- #
def find_gallery_id(page):
    m = re.search(r"/dashboard/gallery/([0-9a-f-]{36})", page.url)
    if m:
        return m.group(1)
    href = page.evaluate(
        """() => { const a = document.querySelector('a[href*="/dashboard/gallery/"]');
                   return a ? a.getAttribute('href') : null; }""")
    if href:
        m = re.search(r"/dashboard/gallery/([0-9a-f-]{36})", href)
        if m:
            return m.group(1)
    page.evaluate(r"""
    () => { for (const b of document.querySelectorAll('button, a')) {
        const t = (b.innerText || '').trim().toLowerCase();
        if (t.includes('photos/videos')) { b.click(); return; } } }
    """)
    for _ in range(20):
        time.sleep(0.5)
        m = re.search(r"/dashboard/gallery/([0-9a-f-]{36})", page.url)
        if m:
            return m.group(1)
    return None


def read_period(page):
    try:
        return page.eval_on_selector(SEL_DROPDOWN_HEADER, "e => e.innerText.trim()")
    except Exception:
        return "?"


def read_date_title(page):
    try:
        return page.eval_on_selector(SEL_DATE_TITLE, "e => e.innerText.trim()")
    except Exception:
        return None


def set_period(page, wanted="Weekly") -> bool:
    if wanted.lower() in (read_period(page) or "").lower():
        return True
    try:
        page.click(SEL_DROPDOWN_HEADER, timeout=4000)
        time.sleep(0.8)
        page.evaluate(r"""
        (want) => {
            for (const e of document.querySelectorAll('div, li, span, button, [role=option]')) {
                if (e.children.length) continue;
                if ((e.innerText || '').trim().toLowerCase() === want.toLowerCase()) {
                    (e.closest('[role=option], li, div') || e).click();
                    return true;
                }
            }
            return false;
        }
        """, wanted)
        time.sleep(1.5)
        return wanted.lower() in (read_period(page) or "").lower()
    except Exception:
        return False


def click_prev_week(page) -> bool:
    """
    Click the PREVIOUS-week arrow: div.datepicker__arrow, the one left of the
    title. Explicitly excludes .dropdown-portal__header-arrow (the period
    dropdown's chevron), which v1 clicked by mistake.
    """
    js = r"""
    () => {
        const arrows = [...document.querySelectorAll('div.date-filter .datepicker__arrow')]
            .filter(a => !a.className.includes('dropdown-portal'));
        if (!arrows.length) return { ok: false, reason: 'no .datepicker__arrow found' };
        const title = document.querySelector('[data-testid="datepicker-title"]');
        const tx = title ? title.getBoundingClientRect().x : Infinity;
        // Prefer an explicit left/prev class, else the arrow left of the title.
        let prev = arrows.find(a => /left|prev|back/i.test(a.className));
        if (!prev) prev = arrows.filter(a => a.getBoundingClientRect().x < tx)
                                .sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x)[0];
        if (!prev) return { ok: false, reason: 'no arrow left of title' };
        (prev.closest('button, [role=button], a') || prev).click();
        return { ok: true, cls: prev.className };
    }
    """
    try:
        res = page.evaluate(js)
        if not res.get("ok"):
            print(f"      !! prev-arrow: {res.get('reason')}")
        return bool(res.get("ok"))
    except Exception as exc:
        print(f"      !! prev-arrow error: {exc}")
        return False


def wait_for_week(page, old_title, timeout=20.0):
    """Wait for the title to change, then for tile count to stop growing."""
    t0 = time.time()
    changed = False
    while time.time() - t0 < timeout:
        time.sleep(0.3)
        if read_date_title(page) != old_title:
            changed = True
            break
    stable, last = 0, -1
    while time.time() - t0 < timeout and stable < 3:
        time.sleep(0.4)
        n = len(find_tiles(page))
        stable = stable + 1 if n == last else 0
        last = n
    return changed, time.time() - t0


# --------------------------------------------------------------------------- #
# Tile discovery  (v1 assumed the wrong markup — this inspects it)
# --------------------------------------------------------------------------- #
def find_tiles(page):
    """
    Return the gallery's media tiles as {index, src, w, h, cls}.
    Looks at <img src>, CSS background-image, AND <video poster>, on ANY host.
    """
    js = r"""
    () => {
        const out = [];
        const seen = new Set();
        const push = (el, src) => {
            if (!src || seen.has(el)) return;
            const r = el.getBoundingClientRect();
            if (r.width < 50 || r.height < 50) return;
            const s = src.toLowerCase();
            if (/sprite|logo|favicon|\.svg|profile_pic/.test(s)) return;
            seen.add(el);
            out.push({ src: src, w: Math.round(r.width), h: Math.round(r.height),
                       cls: String(typeof el.className === 'string' ? el.className : '').slice(0, 45) });
        };
        document.querySelectorAll('img').forEach(i => push(i, i.currentSrc || i.src || ''));
        document.querySelectorAll('video').forEach(v => push(v, v.poster || v.src || ''));
        document.querySelectorAll('div, a, span, li').forEach(el => {
            const b = getComputedStyle(el).backgroundImage;
            if (b && b !== 'none' && b.includes('url(')) {
                const m = b.match(/url\(["']?(.*?)["']?\)/);
                if (m && m[1] && !m[1].startsWith('data:')) push(el, m[1]);
            }
        });
        return out;
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return []


def dump_tile_markup(page):
    """Print the real structure of the gallery so we stop guessing."""
    js = r"""
    () => {
        const g = document.querySelector('div.gallery') || document.querySelector('section.section');
        if (!g) return { found: false };
        // The container holding the most images = the tile grid
        let grid = null, best = 0;
        g.querySelectorAll('*').forEach(el => {
            const n = el.querySelectorAll('img').length;
            if (n > best && n >= 2) { best = n; grid = el; }
        });
        const target = grid || g;
        const firstTile = target.children[0];
        return {
            found: true,
            gridClass: String(typeof target.className === 'string' ? target.className : ''),
            imgCount: target.querySelectorAll('img').length,
            childCount: target.children.length,
            firstTileHTML: firstTile ? firstTile.outerHTML.slice(0, 900) : null,
            allImgSrcs: [...document.querySelectorAll('img')]
                .map(i => (i.currentSrc || i.src || '').slice(0, 110))
                .filter(Boolean).slice(0, 8),
        };
    }
    """
    try:
        d = page.evaluate(js)
    except Exception as exc:
        print(f"    !! markup dump failed: {exc}")
        return
    if not d.get("found"):
        print("    !! no div.gallery / section.section found")
        return
    print(f"    grid class : {d['gridClass'][:60]!r}")
    print(f"    imgs in grid: {d['imgCount']}   children: {d['childCount']}")
    print("    image srcs on page:")
    for s in d["allImgSrcs"]:
        print(f"      {s}")
    if d["firstTileHTML"]:
        print("    first tile HTML (900 chars):")
        print("      " + d["firstTileHTML"].replace("\n", " ")[:900])


# --------------------------------------------------------------------------- #
# Open one item and download it
# --------------------------------------------------------------------------- #
def open_first_tile(page):
    """Click the first real media tile. Returns True if a viewer opened."""
    js = r"""
    () => {
        const cands = [...document.querySelectorAll('img, video')].filter(el => {
            const r = el.getBoundingClientRect();
            const s = (el.currentSrc || el.src || el.poster || '').toLowerCase();
            return r.width >= 50 && r.height >= 50 &&
                   !/sprite|logo|favicon|\.svg|profile_pic/.test(s);
        });
        if (!cands.length) return false;
        const el = cands[0];
        (el.closest('a, [class*=item i], [class*=tile i], [class*=photo i], [class*=card i]') || el).click();
        return true;
    }
    """
    try:
        if not page.evaluate(js):
            return False
        page.wait_for_selector(SEL_DL_ANCHOR, timeout=10000, state="attached")
        return True
    except Exception:
        return False


def dump_viewer(page):
    """Show what the open viewer contains — text may carry the item's own date."""
    js = r"""
    () => {
        const m = document.querySelector('div.modal');
        if (!m) return null;
        const a = m.querySelector('a.action-button[href], a[download][href]');
        return {
            text: (m.innerText || '').replace(/\s+/g, ' ').slice(0, 300),
            href: a ? a.getAttribute('href') : null,
            headerHTML: (m.querySelector('.modal__header') || {outerHTML: ''})
                        .outerHTML.slice(0, 400),
        };
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return None


def download_one(context, page, href, when: dt.date, referer):
    """Fetch via the browser session and save as YYYYMMDD_<hash>.<ext>."""
    os.makedirs(OUT_DIR, exist_ok=True)
    resp = context.request.get(href, headers={"Referer": referer}, timeout=120000)
    if not resp.ok:
        return f"HTTP {resp.status}"
    data = resp.body()
    ct = (resp.headers or {}).get("content-type", "")
    ext = os.path.splitext(href.split("?")[0])[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
                   ".mp4", ".mov", ".m4v", ".webm"):
        ext = ".mp4" if "video" in ct else ".jpg"
    name = f"{when.strftime('%Y%m%d')}_{hashlib.sha1(data).hexdigest()[:10]}{ext}"
    path = os.path.join(OUT_DIR, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return f"saved {name} ({len(data) // 1024} KB, {ct})"


# --------------------------------------------------------------------------- #
def test_tab(page, context, gallery_id, tab):
    print("\n" + "=" * 72)
    print(f"  TAB: {tab.upper()}")
    print("=" * 72)

    url = f"https://schools.procareconnect.com/dashboard/gallery/{gallery_id}/{tab}"
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(3.0)

    print(f"  period reads {read_period(page)!r}")
    print(f"  set Weekly: {'OK' if set_period(page, 'Weekly') else 'FAILED'} "
          f"-> {read_period(page)!r}")
    time.sleep(2.5)

    n_arrows = page.evaluate(
        "() => document.querySelectorAll('div.date-filter .datepicker__arrow').length")
    print(f"  .datepicker__arrow count: {n_arrows}  (expect 2: prev + next)")

    print("\n  --- TILE MARKUP (week 0) ---")
    dump_tile_markup(page)

    anchor_href, item_date = None, None
    all_srcs, weeks_ok = set(), 0
    cursor = dt.date.today()

    for w in range(WEEKS_TO_TEST + 1):
        title = read_date_title(page)
        wk_start = parse_title_start(title, cursor)
        if wk_start:
            cursor = wk_start
        tiles = find_tiles(page)
        srcs = {t["src"].split("?")[0] for t in tiles}
        new = srcs - all_srcs
        all_srcs |= srcs

        print(f"\n  week {w}: title={title!r}  -> parsed start {wk_start}")
        print(f"    tiles={len(tiles)}  unique_srcs={len(srcs)}  new={len(new)}")
        if tiles:
            print(f"    sample tile: {tiles[0]['w']}x{tiles[0]['h']} "
                  f"{tiles[0]['src'][:88]}")

        # On the first non-empty week, open an item and download it.
        if tiles and anchor_href is None:
            print("    opening first tile...")
            if open_first_tile(page):
                v = dump_viewer(page)
                if v:
                    print(f"      viewer text: {v['text'][:140]!r}")
                    anchor_href = v["href"]
                    print(f"      download href: {str(anchor_href)[:95]}")
                    item_date = wk_start or dt.date.today()
                    res = download_one(context, page, anchor_href, item_date, page.url)
                    print(f"      DOWNLOAD TEST: {res}")
                page.keyboard.press("Escape")
                time.sleep(0.8)
            else:
                print("      !! could not open a viewer for this tile")

        if w < WEEKS_TO_TEST:
            before = title
            if not click_prev_week(page):
                print("    !! prev-week click failed — STOPPING")
                break
            changed, took = wait_for_week(page, before)
            print(f"    -> prev week: title {'CHANGED' if changed else 'DID NOT CHANGE'} "
                  f"in {took:.1f}s")
            if not changed:
                print("       (still stuck — the arrow selector needs another look)")
                break
            weeks_ok += 1

    print(f"\n  {tab}: stepped back {weeks_ok}/{WEEKS_TO_TEST} weeks, "
          f"{len(all_srcs)} unique tile srcs, "
          f"download {'OK' if anchor_href else 'NOT TESTED'}")
    return weeks_ok, len(all_srcs), bool(anchor_href)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1500, "height": 950},
                                      accept_downloads=True)
        page = context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")

        print("\n" + "=" * 72)
        print("  LOG IN, then press ENTER (any page — I'll find the gallery).")
        print("=" * 72)
        input("\nPress ENTER when logged in... ")

        gid = find_gallery_id(page)
        if not gid:
            print("\n!! Could not find the gallery id. Open Photos/Videos manually.")
            input("Press ENTER to close... ")
            browser.close()
            return
        print(f"\n  gallery id = {gid}")

        pw, ps, pd = test_tab(page, context, gid, "photos")
        vw, vs, vd = test_tab(page, context, gid, "videos")

        print("\n" + "=" * 72)
        print("  SUMMARY")
        print(f"    photos: weeks_stepped={pw}/{WEEKS_TO_TEST}  srcs={ps}  "
              f"download={'OK' if pd else 'FAILED'}")
        print(f"    videos: weeks_stepped={vw}/{WEEKS_TO_TEST}  srcs={vs}  "
              f"download={'OK' if vd else 'FAILED'}")
        print(f"    test files in ./{OUT_DIR}/")
        print("  Paste this output back to Claude.")
        print("=" * 72)

        input("\nPress ENTER to close... ")
        browser.close()


if __name__ == "__main__":
    main()
