"""
test_click_download.py

Focused test: week navigation already works, so this only proves we can
OPEN a tile and DOWNLOAD the original.

Why v2 failed to open anything: the gallery tiles are divs with a CSS
background-image (the grid contains zero <img> elements), but the click helper
only clicked <img>/<video>. This version marks the real tile elements with
data-tile="N" and clicks them with a genuine mouse click.

If no download anchor appears, it dumps every modal/viewer-ish element it can
find so we can see the actual markup instead of guessing.

Saves to ./test_downloads/ as YYYYMMDD_<hash>.<ext>
"""

import datetime as dt
import hashlib
import os
import re
import time

from playwright.sync_api import sync_playwright

START_URL = "https://schools.procareconnect.com/"
OUT_DIR = "test_downloads"

SEL_DROPDOWN_HEADER = "div.date-filter .dropdown-portal__header"
SEL_DATE_TITLE = '[data-testid="datepicker-title"]'
SEL_DL_ANCHOR = ("a.action-button[href], a[download][href], "
                 "div.modal a[href*='procareconnect']")

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_title_start(title, not_after):
    if not title:
        return None
    m = re.search(r'([A-Za-z]{3,9})\.?\s+(\d{1,2})', title)
    if not m:
        return None
    mon = MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return None
    day = int(m.group(2))
    for year in (not_after.year, not_after.year - 1):
        try:
            d = dt.date(year, mon, day)
        except ValueError:
            continue
        if d <= not_after:
            return d
    return None


def set_period(page, wanted="Weekly"):
    try:
        cur = page.eval_on_selector(SEL_DROPDOWN_HEADER, "e => e.innerText.trim()")
        if wanted.lower() in (cur or "").lower():
            return True
        page.click(SEL_DROPDOWN_HEADER, timeout=4000)
        time.sleep(0.8)
        page.evaluate(r"""
        (want) => {
            for (const e of document.querySelectorAll('div, li, span, button, [role=option]')) {
                if (e.children.length) continue;
                if ((e.innerText || '').trim().toLowerCase() === want.toLowerCase()) {
                    (e.closest('[role=option], li, div') || e).click(); return true;
                }
            }
            return false;
        }""", wanted)
        time.sleep(2.0)
        return True
    except Exception:
        return False


def click_prev_week(page):
    js = r"""
    () => {
        const arrows = [...document.querySelectorAll('div.date-filter .datepicker__arrow')]
            .filter(a => !String(a.className).includes('dropdown-portal'));
        const title = document.querySelector('[data-testid="datepicker-title"]');
        const tx = title ? title.getBoundingClientRect().x : Infinity;
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
    """
    Tag every real media tile with data-tile="N".

    Tiles are background-image divs (that's the bit v2 missed), but <img> and
    <video poster> are handled too. Nested duplicates are skipped so we mark the
    outermost tile element, which is what carries the click handler.
    """
    js = r"""
    () => {
        document.querySelectorAll('[data-tile]').forEach(e => e.removeAttribute('data-tile'));

        // Must be a media file on the private CDN. Plain /procareconnect/ also
        // matched schools.procareconnect.com/assets/images/logo.svg (the topbar
        // logo), which is what tile 0 used to be.
        const ok = src => src &&
            /private\.cdn\.procareconnect\.com/.test(src) &&
            !/\.svg(\?|$)/i.test(src) &&
            !/profile_pic|avatar|logo/i.test(src);

        // Search inside the gallery grid only, so chrome/nav can't sneak in.
        const root = document.querySelector('div.gallery') || document.body;
        const marked = [];
        let n = 0;

        const consider = (el, src) => {
            if (!ok(src)) return;
            const r = el.getBoundingClientRect();
            if (r.width < 50 || r.height < 50) return;
            for (const m of marked) if (m.contains(el) || el.contains(m)) return;
            el.setAttribute('data-tile', String(n++));
            el.setAttribute('data-tilesrc', src);
            marked.push(el);
        };

        root.querySelectorAll('div, a, span, li').forEach(el => {
            const b = getComputedStyle(el).backgroundImage;
            if (b && b !== 'none' && b.includes('url(')) {
                const m = b.match(/url\(["']?(.*?)["']?\)/);
                if (m) consider(el, m[1]);
            }
        });
        root.querySelectorAll('img').forEach(i => consider(i, i.currentSrc || i.src || ''));
        root.querySelectorAll('video').forEach(v => consider(v, v.poster || v.src || ''));
        return n;
    }"""
    try:
        return page.evaluate(js)
    except Exception:
        return 0


def dump_tile_context(page, idx=0):
    """Show the tile's own HTML and its parent — looking for a per-item date."""
    js = r"""
    (i) => {
        const el = document.querySelector(`[data-tile="${i}"]`);
        if (!el) return null;
        const p = el.parentElement;
        return {
            tagCls: el.tagName + '.' + String(el.className).slice(0, 50),
            tileHTML: el.outerHTML.slice(0, 400),
            parentHTML: p ? p.outerHTML.slice(0, 700) : null,
            parentText: p ? (p.innerText || '').replace(/\s+/g, ' ').slice(0, 160) : null,
            src: el.getAttribute('data-tilesrc'),
        };
    }"""
    try:
        return page.evaluate(js, idx)
    except Exception:
        return None


def dump_overlays(page):
    """When no anchor appears, show every modal/viewer-ish element present."""
    js = r"""
    () => {
        const out = [];
        document.querySelectorAll('*').forEach(el => {
            const c = String(typeof el.className === 'string' ? el.className : '');
            if (!/modal|viewer|lightbox|overlay|dialog|popup/i.test(c)) return;
            const r = el.getBoundingClientRect();
            if (r.width < 100 || r.height < 100) return;
            out.push({ cls: c.slice(0, 60), w: Math.round(r.width), h: Math.round(r.height),
                       html: el.outerHTML.slice(0, 500) });
        });
        const anchors = [...document.querySelectorAll('a')].map(a => ({
            cls: String(a.className).slice(0, 40),
            href: (a.getAttribute('href') || '').slice(0, 90),
            dl: a.hasAttribute('download'),
        })).filter(a => a.href.includes('procareconnect') || a.dl ||
                        a.cls.includes('action-button'));
        return { overlays: out.slice(0, 4), anchors: anchors.slice(0, 6) };
    }"""
    try:
        return page.evaluate(js)
    except Exception:
        return {"overlays": [], "anchors": []}


def open_tile(page, idx):
    """Real mouse click on the tile, then wait for the download anchor."""
    sel = f'[data-tile="{idx}"]'
    try:
        el = page.query_selector(sel)
        if not el:
            return False, "tile not found"
        el.scroll_into_view_if_needed(timeout=4000)
        time.sleep(0.3)
        el.click(timeout=5000)              # genuine mouse event, not el.click() in JS
    except Exception as exc:
        try:
            page.evaluate(f'() => document.querySelector(\'{sel}\')?.click()')
        except Exception:
            return False, f"click failed: {str(exc)[:50]}"
    try:
        page.wait_for_selector(SEL_DL_ANCHOR, timeout=10000, state="attached")
        return True, "anchor found"
    except Exception:
        return False, "no download anchor appeared"


def viewer_info(page):
    js = r"""
    () => {
        const a = document.querySelector("a.action-button[href], a[download][href]");
        const m = document.querySelector('div.modal') ||
                  document.querySelector('[class*=viewer i]');
        return {
            href: a ? a.getAttribute('href') : null,
            text: m ? (m.innerText || '').replace(/\s+/g, ' ').slice(0, 260) : null,
        };
    }"""
    try:
        return page.evaluate(js)
    except Exception:
        return {"href": None, "text": None}


def download_one(context, href, when, referer, label):
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
    name = f"{when.strftime('%Y%m%d')}_{label}_{hashlib.sha1(data).hexdigest()[:10]}{ext}"
    with open(os.path.join(OUT_DIR, name), "wb") as fh:
        fh.write(data)
    return f"SAVED {name}  ({len(data) // 1024} KB, {ct})"


def test_tab(page, context, gid, tab):
    print("\n" + "=" * 72)
    print(f"  TAB: {tab.upper()}")
    print("=" * 72)
    page.goto(f"https://schools.procareconnect.com/dashboard/gallery/{gid}/{tab}",
              wait_until="domcontentloaded")
    time.sleep(3.0)
    set_period(page, "Weekly")
    time.sleep(2.0)

    cursor = dt.date.today()
    # Step back until we find a week with tiles (up to 6 weeks).
    for attempt in range(6):
        n = mark_tiles(page)
        title = page.eval_on_selector(SEL_DATE_TITLE, "e => e.innerText.trim()")
        wk = parse_title_start(title, cursor)
        if wk:
            cursor = wk
        print(f"\n  week {title!r} (start {wk}): marked {n} tile(s)")
        if n:
            break
        if not click_prev_week(page):
            print("  !! could not step back")
            return False
        time.sleep(3.0)
    if not n:
        print("  no tiles found in 6 weeks — nothing to test")
        return False

    ctx = dump_tile_context(page, 0)
    if ctx:
        print(f"    tile element : {ctx['tagCls']}")
        print(f"    tile src     : {str(ctx['src'])[:100]}")
        print(f"    parent text  : {ctx['parentText']!r}")
        print(f"    parent HTML  : {str(ctx['parentHTML'])[:300]}")

    print("\n  clicking tile 0...")
    ok, why = open_tile(page, 0)
    print(f"    open result: {why}")

    if not ok:
        d = dump_overlays(page)
        print("    --- what IS on screen ---")
        for o in d["overlays"]:
            print(f"      overlay {o['cls'][:45]!r} {o['w']}x{o['h']}")
            print(f"        {o['html'][:260]}")
        for a in d["anchors"]:
            print(f"      anchor cls={a['cls']!r} dl={a['dl']} href={a['href']}")
        if not d["overlays"] and not d["anchors"]:
            print("      (nothing modal-like found — the click may not have registered)")
        page.keyboard.press("Escape")
        return False

    info = viewer_info(page)
    print(f"    viewer text : {str(info['text'])[:200]!r}")
    print(f"    href        : {str(info['href'])[:100]}")

    when = cursor or dt.date.today()
    res = download_one(context, info["href"], when, page.url, tab[:-1])
    print(f"    DOWNLOAD    : {res}")
    page.keyboard.press("Escape")
    time.sleep(0.8)
    return True


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1500, "height": 950},
                                      accept_downloads=True)
        page = context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")
        print("\n" + "=" * 72)
        print("  LOG IN, then press ENTER.")
        print("=" * 72)
        input("\nPress ENTER when logged in... ")

        gid = None
        m = re.search(r"/dashboard/gallery/([0-9a-f-]{36})", page.url)
        if m:
            gid = m.group(1)
        else:
            href = page.evaluate(
                """() => { const a = document.querySelector('a[href*="/dashboard/gallery/"]');
                           return a ? a.getAttribute('href') : null; }""")
            if href:
                gid = re.search(r"gallery/([0-9a-f-]{36})", href).group(1)
        if not gid:
            page.evaluate(r"""() => { for (const b of document.querySelectorAll('button, a')) {
                const t = (b.innerText || '').trim().toLowerCase();
                if (t.includes('photos/videos')) { b.click(); return; } } }""")
            for _ in range(20):
                time.sleep(0.5)
                m = re.search(r"/dashboard/gallery/([0-9a-f-]{36})", page.url)
                if m:
                    gid = m.group(1)
                    break
        if not gid:
            print("!! no gallery id")
            input("ENTER to close...")
            browser.close()
            return
        print(f"\n  gallery id = {gid}")

        p_ok = test_tab(page, context, gid, "photos")
        v_ok = test_tab(page, context, gid, "videos")

        print("\n" + "=" * 72)
        print(f"  photos download: {'OK' if p_ok else 'FAILED'}")
        print(f"  videos download: {'OK' if v_ok else 'FAILED'}")
        print(f"  files in ./{OUT_DIR}/")
        print("=" * 72)
        input("\nPress ENTER to close... ")
        browser.close()


if __name__ == "__main__":
    main()
