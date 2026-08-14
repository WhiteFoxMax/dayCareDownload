"""
diag_api.py — inspect Procare's own API.

The DOM has been the flaky part all along; the app talks to
api-school.procareconnect.com and that is the real source of truth.

This dumps:
  1. every API endpoint the dashboard + feed call (with paging params)
  2. how each media item is shaped — ALL its URLs, so we can see why an item
     with two non-thumbnail URLs gets skipped
  3. whether Aug 11/12 media appears in any payload
  4. what auth the requests carry (presence and scheme only, never the value)

Downloads nothing. Secrets are not printed. One sample payload is written to
tools/sample_payload.json for inspection — it contains signed media URLs, so it
is gitignored; delete it when done.

Run from the project root:  python tools/diag_api.py
"""

import json
import re
import time
from collections import defaultdict

from playwright.sync_api import sync_playwright

BASE = "https://schools.procareconnect.com"
SESSION = "procare_session.json"
API_HOST = "api-school.procareconnect.com"
CDN_RE = re.compile(r"https://private\.cdn\.procareconnect\.com/[^\"'\\\s]+")
INTEREST = ("2026-08-11", "2026-08-12")
SAMPLE_OUT = "tools/sample_payload.json"


def main():
    calls = []                      # (method, url, has_auth, auth_scheme)
    bodies = []                     # (url, body)
    header_names = defaultdict(int)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(storage_state=SESSION,
                                  viewport={"width": 1500, "height": 950})
        page = ctx.new_page()

        def on_request(req):
            try:
                if API_HOST not in req.url:
                    return
                h = req.headers
                auth = h.get("authorization", "")
                for k in h:
                    header_names[k] += 1
                calls.append((req.method, req.url,
                              bool(auth), auth.split(" ")[0] if auth else ""))
            except Exception:
                pass

        def on_response(resp):
            try:
                if API_HOST not in resp.url:
                    return
                if "json" not in resp.headers.get("content-type", "").lower():
                    return
                body = resp.text()
                if body and len(body) < 8_000_000:
                    bodies.append((resp.url, body))
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

        # Hard load of the dashboard so nothing is served from cache.
        page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded")
        time.sleep(3)
        page.reload(wait_until="domcontentloaded")
        time.sleep(6)

        # Scroll the feed twice to trigger the paged activity fetches.
        for _ in range(2):
            page.evaluate(r"""() => {
                const scrolls = el => el && el.scrollHeight > el.clientHeight + 50 &&
                    /(auto|scroll|overlay)/.test(getComputedStyle(el).overflowY);
                let el = document.querySelector('div.activity-list'), t = null;
                while (el) { if (scrolls(el)) { t = el; break; } el = el.parentElement; }
                if (t) t.scrollTop = t.scrollHeight;
            }""")
            time.sleep(3)

        labels = page.evaluate(
            """() => [...document.querySelectorAll('div.activity-date')]
                       .map(e => e.textContent.trim())""")
        n_act = page.evaluate("() => document.querySelectorAll('div.activity').length")
        print(f"\n=== FEED date labels on screen: {labels[:8]}")
        print(f"=== activities in DOM: {n_act}")

        # ---------------- endpoints ---------------- #
        print(f"\n=== API ENDPOINTS ({len(calls)} call(s)) ===")
        seen = {}
        for method, url, has_auth, scheme in calls:
            path = url.split("?")[0].replace(f"https://{API_HOST}", "")
            params = url.split("?")[1][:120] if "?" in url else ""
            key = (method, path)
            if key in seen:
                continue
            seen[key] = True
            print(f"  {method:<5} {path}")
            if params:
                print(f"        params: {params}")
        print(f"\n  auth header present on API calls: "
              f"{any(c[2] for c in calls)}  scheme={ {c[3] for c in calls if c[3]} }")
        print(f"  request header names seen: {sorted(header_names)}")

        # ---------------- payload shapes ---------------- #
        print(f"\n=== PAYLOADS ({len(bodies)}) ===")
        sample_saved = False
        for url, body in bodies:
            path = url.split("?")[0].replace(f"https://{API_HOST}", "")
            n_media = len(CDN_RE.findall(body))
            hits = [s for s in INTEREST if s in body]
            print(f"  {path:<45} {len(body):>8}B  cdn_urls={n_media:<4} "
                  f"aug11/12={hits}")
            if n_media and not sample_saved:
                try:
                    with open(SAMPLE_OUT, "w") as fh:
                        json.dump(json.loads(body), fh, indent=2)
                    sample_saved = True
                except Exception:
                    pass

        # ---------------- item shape ---------------- #
        print("\n=== ITEM SHAPE: all URLs per media item ===")
        shown = 0
        for url, body in bodies:
            if shown >= 4:
                break
            try:
                payload = json.loads(body)
            except Exception:
                continue

            def walk(node):
                nonlocal shown
                if shown >= 4:
                    return
                if isinstance(node, dict):
                    strings = {k: v for k, v in node.items() if isinstance(v, str)}
                    urls = {k: v for k, v in strings.items() if CDN_RE.match(v or "")}
                    if urls:
                        shown += 1
                        print(f"\n  item keys: {sorted(node.keys())}")
                        for k, v in urls.items():
                            kind = ("THUMB" if "/thumb/" in v else
                                    "MAIN" if "/main/" in v else
                                    "ATTACH" if "/attachments/" in v else "OTHER")
                            print(f"    {k:<22} [{kind}] {v[:95]}")
                        for k in ("created_at", "date", "activity_time", "type",
                                  "activity_type", "kind"):
                            if node.get(k):
                                print(f"    {k:<22} = {str(node[k])[:60]}")
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)

            walk(payload)

        print(f"\n  sample payload written to {SAMPLE_OUT}"
              if sample_saved else "\n  (no sample payload captured)")
        print("\nPaste this output back to Claude.")
        input("\nPress ENTER to close... ")
        browser.close()


if __name__ == "__main__":
    main()
