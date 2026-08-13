"""
diagnose_scroll.py

Figures out WHY the feed isn't scrolling, by inspecting the live page and then
testing each plausible scroll technique to see which one actually loads more
activities.

Run it, log in, navigate to the activity feed, press ENTER, and paste the output
back. It changes nothing and downloads nothing — it only looks and reports.
"""

import time

from playwright.sync_api import sync_playwright

START_URL = "https://schools.procareconnect.com/"


def report_containers(page):
    """List every element on the page that is actually scrollable."""
    js = r"""
    () => {
        const out = [];
        // The window itself
        out.push({
            what: 'WINDOW',
            scrollHeight: document.documentElement.scrollHeight,
            clientHeight: window.innerHeight,
            scrollTop: window.scrollY,
            overflowY: 'n/a',
        });
        document.querySelectorAll('*').forEach(el => {
            const s = getComputedStyle(el);
            const scrollable = /(auto|scroll|overlay)/.test(s.overflowY);
            const overflows = el.scrollHeight > el.clientHeight + 50;
            if (!overflows) return;
            const cls = (typeof el.className === 'string' ? el.className : '').slice(0, 60);
            out.push({
                what: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
                      (cls ? '.' + cls.trim().split(/\s+/).join('.') : ''),
                scrollHeight: el.scrollHeight,
                clientHeight: el.clientHeight,
                scrollTop: el.scrollTop,
                overflowY: s.overflowY,
                scrollableByCSS: scrollable,
            });
        });
        return out;
    }
    """
    rows = page.evaluate(js)
    print("\n--- SCROLLABLE ELEMENTS (scrollHeight > clientHeight) ---")
    for r in rows:
        flag = "  <-- CSS-scrollable" if r.get("scrollableByCSS") else ""
        print(f"  {r['what'][:70]:<70} h={r['scrollHeight']:>7} "
              f"view={r['clientHeight']:>5} top={r['scrollTop']:>6} "
              f"overflowY={r['overflowY']}{flag}")
    if len(rows) <= 1:
        print("  (nothing overflows — the feed may be virtualized or not loaded)")
    return rows


def feed_stats(page):
    """Snapshot of the feed: activity count, first/last dates, loader presence."""
    js = r"""
    () => {
        const acts = document.querySelectorAll('div.activity');
        const dates = [...document.querySelectorAll('div.activity-date')]
                        .map(e => e.textContent.trim());
        const list = document.querySelector('div.activity-list');
        const loaders = [...document.querySelectorAll(
            '[class*=loading i], [class*=spinner i], [class*=loader i], [class*=sentinel i]')]
            .map(e => (typeof e.className === 'string' ? e.className : '')).slice(0, 5);
        // Is the list virtualized? (absolutely-positioned / transformed children)
        let virtualized = false;
        if (list) {
            for (const c of list.children) {
                const s = getComputedStyle(c);
                if (s.position === 'absolute' || (s.transform && s.transform !== 'none')) {
                    virtualized = true; break;
                }
            }
        }
        return {
            activities: acts.length,
            dateCount: dates.length,
            firstDate: dates[0] || null,
            lastDate: dates[dates.length - 1] || null,
            hasList: !!list,
            listHeight: list ? list.scrollHeight : 0,
            listClient: list ? list.clientHeight : 0,
            listParent: list && list.parentElement
                        ? (typeof list.parentElement.className === 'string'
                           ? list.parentElement.className : '') : '',
            loaders, virtualized,
            bodyScrollHeight: document.body.scrollHeight,
            windowScrollY: window.scrollY,
        };
    }
    """
    return page.evaluate(js)


def try_technique(page, name, fn, settle=3.0):
    """Run one scroll technique and report whether the feed grew."""
    before = feed_stats(page)
    try:
        fn()
    except Exception as exc:
        print(f"  {name:<34} ERROR: {str(exc)[:50]}")
        return False
    time.sleep(settle)
    after = feed_stats(page)
    grew = after["activities"] - before["activities"]
    moved = after["windowScrollY"] != before["windowScrollY"] or \
        after["bodyScrollHeight"] != before["bodyScrollHeight"]
    verdict = "WORKS" if grew > 0 else ("moved, no new items" if moved else "no effect")
    print(f"  {name:<34} activities {before['activities']:>4} -> {after['activities']:>4} "
          f"({grew:+d})  last date: {after['lastDate']}   [{verdict}]")
    return grew > 0


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1500, "height": 950})
        page = context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")

        print("\n" + "=" * 70)
        print("  LOG IN, go to the activity feed with the photos, then press ENTER.")
        print("=" * 70)
        input("\nPress ENTER when the feed is visible... ")

        print("\n=== BASELINE ===")
        st = feed_stats(page)
        for k, v in st.items():
            print(f"  {k}: {v}")

        report_containers(page)

        print("\n=== TESTING SCROLL TECHNIQUES (3s settle each) ===")

        # 1. Plain window scroll
        try_technique(page, "1. window.scrollTo(bottom)",
                      lambda: page.evaluate("window.scrollTo(0, document.body.scrollHeight)"))

        # 2. scrollTop on the activity list and its ancestors
        try_technique(page, "2. activity-list ancestors", lambda: page.evaluate(r"""
            () => { let el = document.querySelector('div.activity-list');
                    while (el) { if (el.scrollHeight > el.clientHeight + 50)
                                    el.scrollTop = el.scrollHeight; el = el.parentElement; } }
        """))

        # 3. Mouse wheel with the pointer physically over the feed
        def wheel():
            box = page.query_selector("div.activity-list")
            if box:
                b = box.bounding_box()
                if b:
                    page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
            page.mouse.wheel(0, 3000)
        try_technique(page, "3. mouse wheel over feed", wheel)

        # 4. Scroll the LAST activity into view (works with virtualized lists)
        try_technique(page, "4. last activity into view", lambda: page.evaluate(r"""
            () => { const a = document.querySelectorAll('div.activity');
                    if (a.length) a[a.length-1].scrollIntoView({block:'end'}); }
        """))

        # 5. Keyboard End key on the focused feed
        def keyboard_end():
            box = page.query_selector("div.activity-list")
            if box:
                box.click(position={"x": 5, "y": 5})
            page.keyboard.press("End")
        try_technique(page, "5. click feed + End key", keyboard_end)

        # 6. Incremental scroll (some loaders need gradual movement, not a jump)
        def incremental():
            page.evaluate(r"""
                () => new Promise(res => {
                    let n = 0;
                    const step = () => {
                        window.scrollBy(0, 800);
                        document.querySelectorAll('*').forEach(el => {
                            const s = getComputedStyle(el);
                            if (/(auto|scroll)/.test(s.overflowY) &&
                                el.scrollHeight > el.clientHeight + 50) el.scrollTop += 800;
                        });
                        if (++n < 12) setTimeout(step, 250); else res();
                    };
                    step();
                })
            """)
        try_technique(page, "6. incremental scrollBy x12", incremental, settle=4.0)

        print("\n=== AFTER ALL TESTS ===")
        st2 = feed_stats(page)
        for k in ("activities", "firstDate", "lastDate", "virtualized", "loaders"):
            print(f"  {k}: {st2[k]}")

        report_containers(page)

        print("\nPaste this whole output back to Claude.")
        input("\nPress ENTER to close... ")
        browser.close()


if __name__ == "__main__":
    main()
