# dayCareDownload

Bulk-download **your own** photos and videos from the [Procare](https://www.procaresoftware.com/) parent portal (`schools.procareconnect.com`).

Procare's web gallery only lets you save one file at a time, a week at a time — and some photos never appear there at all, only in the dashboard activity feed. This script walks both, back to whatever date you choose, and saves every photo and video at full resolution — named by date, de-duplicated, and resumable.

You log in yourself, in a real browser window. Nothing here bypasses authentication, and your password is never stored.

---

## What it does

- Reads Procare's **own API** (the endpoint the app itself uses) plus the
  Photos/Videos gallery — together they cover photos the gallery alone omits
- Covers **every child on the account**, not just the first one
- Grabs the **full-size original** of every item (not the gallery thumbnail)
- Saves everything **flat** into a folder you choose as `YYYYMMDD_<contenthash>.jpg` / `.mp4`
- **Never duplicates**: filenames are a hash of the file's contents
- **Resumable**: interrupt with Ctrl-C any time and re-run; it picks up where it stopped
- **Logs in once**: the browser session is saved and reused on later runs
- Writes `procare_manifest.csv` (inside the download folder) listing every file with its date, week, and source URL
- Prints the **full download path** before starting and again when finished

---

## Requirements

- **Python 3.9+** (macOS ships with 3.9 — `python3 --version`)
- A Procare parent account with access to your child's gallery
- ~1 GB free disk for a year of photos and videos (videos are the bulk)

---

## Install (self-contained)

Everything lives inside a virtual environment in the project folder — nothing is installed system-wide.

```bash
git clone https://github.com/WhiteFoxMax/dayCareDownload.git
cd dayCareDownload
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

`playwright install chromium` downloads a private ~170 MB browser build into `~/Library/Caches/ms-playwright` (macOS) or `~/.cache/ms-playwright` (Linux). It does not touch your everyday Chrome.

On Windows, activate with `venv\Scripts\activate` instead.

**Every later session** starts with:

```bash
cd dayCareDownload
source venv/bin/activate
```

To remove everything: delete the project folder and `~/.cache/ms-playwright`.

---

## Usage

### 1. Check the range first (no downloads)

```bash
python procare_download.py --nav-test --target-date 2025-07-01
```

Walks back to the target date and reports how many weeks and how many photos/videos it found, plus any **gaps** (a week that got skipped). Takes a couple of minutes and downloads nothing. A browser window opens for you to log in the first time.

Confirm it prints `REACHED TARGET` and `0 gaps` before committing to a long run.

### 2. Download everything

```bash
python procare_download.py --target-date 2025-07-01
```

The full path being written to is printed **before** downloading starts and again **when it finishes**, so there's never any doubt where the files went.

Re-run the same command any time to pick up newly posted photos — existing files are skipped.

### Where photos hide

Not every photo reaches the Photos/Videos gallery — some are only ever posted as
activities in the dashboard feed. Both are scraped by default. To pick one:

```bash
python procare_download.py --source gallery   # gallery only
python procare_download.py --source feed      # activity feed only
```

A photo appearing in both is downloaded once: sources share progress, so the
second one skips it before fetching anything.

### Choosing where files go

By default everything lands in `./procare_media`. Point it anywhere with `-o`:

```bash
python procare_download.py -o ~/Pictures/Daycare
```

The folder is created if it doesn't exist, `~` is expanded, and the manifest and resume state live inside it — so a folder is fully self-describing and can be moved or backed up as a unit.

### Options

| Flag | Meaning |
|---|---|
| `-o, --out DIR` | Download folder (default `./procare_media`) |
| `--target-date YYYY-MM-DD` | How far back to go (default `2025-08-18`) |
| `--nav-test` | Verify the date range is reachable; download nothing |
| `-w, --workers N` | Browsers splitting the photo date range (default 2) |
| `-d, --dl-threads M` | Parallel download threads (default 12) |
| `--source all\|api\|gallery\|feed` | Where to look (default `all` = API + gallery) |
| `--kid ID` | Limit to one child (id prefix); default is all children |
| `--photos-only` / `--videos-only` | Restrict the gallery to one tab |
| `--show` | Show the browser windows instead of running hidden |
| `--relogin` | Ignore the saved session and log in again |

The defaults are tuned for a normal laptop. More workers means more browsers, so more RAM and CPU; past ~4 you're mostly adding load, not speed.

---

## How it works

Three pieces, none of which waits on anything it doesn't have to:

**Gallery** drives one browser tab. Changing week is *network*-driven: it waits for the gallery's own JSON payload rather than polling the DOM for tiles to settle, and it reads the items straight out of that payload. The photo viewer is never opened in the normal path.

**API** pages `/api/web/parent/daily_activities/` directly, using the Bearer token captured from the logged-in browser (kept in memory, never written to disk). This is the authoritative source and the fastest — a year of history is ~100 pages in well under a minute — and unlike scraping it cannot miss an item because something failed to render.

**Feed** (`--source feed`) scrolls the same activities in the DOM. Superseded by the API path; kept as a fallback.

Renditions are chosen **by key**, not guessed from the URL: `main_url` for photos, `video_file_url` for videos, ignoring `thumb_url`/`medium_url`. Dates inherit the enclosing activity's `activity_time`, so a photo taken Monday but uploaded Wednesday files under Monday.

**Walker** steps back week by week, turns payloads into items, and pushes them onto a queue. Only items the JSON doesn't explain fall back to clicking a tile and reading the viewer's download link.

**Download pool** fetches over plain HTTP. The CDN links are CloudFront *signed URLs* — the signature is in the query string — so they need neither browser nor cookies and parallelise freely.

Dates come from the item's own timestamp in the API when available, then from a date embedded in the CDN filename, and failing both, the Monday of the week it appeared in.

De-duplication is by **content hash**, and resume state is keyed by the URL path (never the query string, since the CloudFront signature changes on every page load).

---

## Login and privacy

The first run opens a browser window for you to log in manually — this handles 2FA and bot checks that a script can't.

Afterwards, the browser session is written to **`procare_session.json`** (mode `600`) and reused, so you don't retype anything. **Your email and password are never stored or seen by the script** — only the session cookie Procare itself issued, the same thing a "stay signed in" checkbox saves. Delete the file (or pass `--relogin`) to sign out.

`.gitignore` excludes `procare_session.json` and every media folder and file type, so **your session and your child's photos can never be committed**. Keep it that way if you fork this.

---

## Troubleshooting

**"Could not determine the gallery id"** — open Photos/Videos in the browser window manually, then re-run.

**It stops before the target date** — run `--nav-test` to see where. The week pager stops when the date label doesn't change, which usually means the page was still loading; the timeouts near the top of the script (`WEEK_TIMEOUT`) can be raised.

**Lots of `miss` in the summary** — the viewer fallback is failing. Run with `--show` to watch, and check whether the modal opens on click.

**It suddenly finds nothing** — Procare changed their markup. The selectors are grouped near the top of `procare_download.py`:

```python
SEL_DROPDOWN   = "div.date-filter .dropdown-portal__header"   # Daily/Weekly picker
SEL_DATE_TITLE = '[data-testid="datepicker-title"]'           # "Aug 10 - Aug 16"
SEL_TILE       = "div.gallery__item"                          # a photo tile
SEL_DL_ANCHOR  = "a.action-button[href], a[download][href]"   # the download link
SEL_ACTIVITY   = "div.activity"                               # a feed activity
SEL_ACTIVITY_DATE = "div.activity-date"                       # "Aug 12, 2026"
```

Open the gallery, inspect the element, and update the matching line.

**Session expired** — run with `--relogin`.

---

## Notes

Written for personal use, to get my own kid's photos out of a portal that has no bulk export. It fetches only what your account can already see, at a human-ish pace. Don't point it at an account that isn't yours.

No affiliation with Procare Software.
