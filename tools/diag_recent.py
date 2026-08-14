"""
diag_recent.py — why are Aug 11/12 photos missing?

Checks the most recent days from BOTH sources and dumps exactly what the API
returns, so we can tell the difference between:
  * the media never appearing in any payload (a capture problem), and
  * it appearing but being dated wrong (a naming problem).

Downloads nothing. Run from the project root:  python tools/diag_recent.py
"""

import datetime as dt
import json
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "https://schools.procareconnect.com"
SESSION = "procare_session.json"
CDN_RE = re.compile(r"https://private\.cdn\.procareconnect\.com/[^\"'\\\s]+")
DATE_KEYS = ("captured_at", "taken_at", "created_at", "activity_date", "date",
             "created_on", "uploaded_at", "activity_time", "occurred_at",
             "start_time", "datetime")
INTEREST = ("2026-08-11", "2026-08-12", "20260811", "20260812")


def collect(page):
    """Attach a response listener that keeps raw JSON bodies."""
    bodies = []

    def on_response(resp):
        try:
            if "json" not in resp.headers.get("content-type", "").lower():
                return
            body = resp.text()
            if body and len(body) < 6_000_000:
                bodies.append((resp.url, body))
        except Exception:
            pass

    page.on("response", on_response)
    return bodies


def describe_items(payload, label):
    """List every media-bearing dict with whatever date fields it carries."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            strings = [v for v in node.values() if isinstance(v, str)]
            urls = [u for s in strings for u in CDN_RE.findall(s)]
            if urls:
                dates = {k: node[k] for k in DATE_KEYS if node.get(k)}
                # any date-looking value at all, even under an unexpected key
                other = {k: v for k, v in node.items()
                         if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v)}
                full = [u for u in urls if "/thumb/" not in u]
                found.append({"n_urls": len(urls), "full": len(full),
                              "dates": dates or other,
                              "sample": (full or urls)[0][:100],
                              "keys": sorted(node.keys())[:12]})
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    print(f"\n  --- {label}: {len(found)} media dict(s) ---")
    for f in found[:12]:
        print(f"    urls={f['n_urls']} full={f['full']}  dates={f['dates']}")
        print(f"      {f['sample']}")
    if found and not found[0]["dates"]:
        print(f"    !! no date fields; dict keys were: {found[0]['keys']}")
    return found


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(storage_state=SESSION,
                                  viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        bodies = collect(page)

        # ---------------- 1. GALLERY, current week ---------------- #
        page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded")
        time.sleep(3)
        m = re.search(r"/dashboard/gallery/([0-9a-f-]{36})", page.url)
        if not m:
            href = page.evaluate(
                """() => { const a = document.querySelector('a[href*="/dashboard/gallery/"]');
                           return a ? a.getAttribute('href') : null; }""")
            m = re.search(r"gallery/([0-9a-f-]{36})", href or "")
        gid = m.group(1) if m else None
        print(f"\ngallery id = {gid}")

        bodies.clear()
        page.goto(f"{BASE}/dashboard/gallery/{gid}/photos", wait_until="domcontentloaded")
        time.sleep(4)
        period = page.eval_on_selector("div.date-filter .dropdown-portal__header",
                                       "e => e.innerText.trim()")
        title = page.eval_on_selector('[data-testid="datepicker-title"]',
                                      "e => e.innerText.trim()")
        tiles = page.evaluate("() => document.querySelectorAll('div.gallery__item').length")
        print(f"\n=== GALLERY (as first loaded) period={period!r} "
              f"range={title!r} tiles={tiles}")

        # Switch to Weekly exactly like the downloader does, then re-check.
        page.click("div.date-filter .dropdown-portal__header")
        time.sleep(0.8)
        page.evaluate(r"""() => {
            for (const e of document.querySelectorAll('div, li, span, button, [role=option]')) {
                if (e.children.length) continue;
                if ((e.innerText || '').trim().toLowerCase() === 'weekly') {
                    (e.closest('[role=option], li, div') || e).click(); return; } } }""")
        time.sleep(4)
        title = page.eval_on_selector('[data-testid="datepicker-title"]',
                                      "e => e.innerText.trim()")
        tiles = page.evaluate("() => document.querySelectorAll('div.gallery__item').length")
        print(f"=== GALLERY (Weekly) range={title!r} tiles={tiles}")

        for url, body in bodies[-6:]:
            if "procareconnect.com" in body:
                describe_items(json.loads(body), f"gallery payload {url.split('?')[0][-60:]}")

        hits = [u for u, b in bodies if any(s in b for s in INTEREST)]
        print(f"\n  payloads mentioning Aug 11/12: {len(hits)}")

        # ---------------- 2. FEED, most recent ---------------- #
        bodies.clear()
        page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded")
        time.sleep(5)
        info = page.evaluate(r"""() => {
            const dates = [...document.querySelectorAll('div.activity-date')]
                            .map(e => e.textContent.trim());
            return { activities: document.querySelectorAll('div.activity').length,
                     dates: dates.slice(0, 10),
                     filter: (document.querySelector('.date-filter, .form-date-filter')
                              || {innerText: ''}).innerText.replace(/\s+/g, ' ').slice(0, 120) };
        }""")
        print(f"\n=== FEED  activities={info['activities']}")
        print(f"    date labels: {info['dates']}")
        print(f"    date filter: {info['filter']!r}")

        for url, body in bodies:
            if "procareconnect.com" in body:
                describe_items(json.loads(body), f"feed payload {url.split('?')[0][-60:]}")

        hits = [u for u, b in bodies if any(s in b for s in INTEREST)]
        print(f"\n  feed payloads mentioning Aug 11/12: {len(hits)}")
        for u in hits[:3]:
            print(f"    {u[:110]}")

        print("\nPaste this output back to Claude.")
        input("\nPress ENTER to close... ")
        browser.close()


if __name__ == "__main__":
    main()
