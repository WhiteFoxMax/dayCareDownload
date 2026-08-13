# dayCareDownload

Bulk-download **your own** photos and videos from the [Procare](https://www.procaresoftware.com/) parent portal (`schools.procareconnect.com`).

Procare's web gallery only lets you save one file at a time, a week at a time. This script walks the gallery week by week, back to whatever date you choose, and saves every photo and video at full resolution — named by date, de-duplicated, and resumable.

You log in yourself, in a real browser window. Nothing here bypasses authentication, and your password is never stored.

---

## What it does

- Walks the **Photos** and **Videos** tabs of the gallery week by week, back to a target date
- Grabs the **full-size original** of every item (not the gallery thumbnail)
- Saves everything **flat** into `procare_media/` as `YYYYMMDD_<contenthash>.jpg` / `.mp4`
- **Never duplicates**: filenames are a hash of the file's contents
- **Resumable**: interrupt with Ctrl-C any time and re-run; it picks up where it stopped
- **Logs in once**: the browser session is saved and reused on later runs
- Writes `procare_manifest.csv` listing every file with its date, week, and source URL

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
python procare_download.py --target-date 2025-07-01 --workers 3 --dl-threads 12
```

Photos land in `procare_media/`. Re-run the same command any time to pick up newly posted photos — existing files are skipped.

### Options

| Flag | Meaning |
|---|---|
| `--target-date YYYY-MM-DD` | How far back to go (default `2025-08-18`) |
| `--nav-test` | Verify the date range is reachable; download nothing |
| `--workers N` | Split the photo date range across N parallel browsers (default 1) |
| `--dl-threads M` | Parallel download threads (default 8) |
| `--photos-only` / `--videos-only` | Restrict to one tab |
| `--show` | Show the browser windows instead of running hidden |
| `--relogin` | Ignore the saved session and log in again |

Start with `--workers 3 --dl-threads 12`. More workers means more browsers, so more RAM and CPU; past ~4 you're mostly adding load, not speed.

---

## How it works

Two stages that run at the same time and never block each other:

**Browsers** walk the gallery week by week. Most full-size URLs come straight out of the gallery's own JSON API responses, so no photo viewer needs to be opened at all. Tiles the JSON doesn't cover fall back to clicking the tile and reading the download link from the viewer.

**Downloaders** pull those URLs off a queue and fetch them over plain HTTP. The CDN links are CloudFront *signed URLs* — the signature is in the query string — so they authenticate themselves and can be fetched in parallel without a browser.

Dates come from the item's own timestamp in the API when available, then from a date embedded in the CDN filename, and failing both, the Monday of the week it appeared in.

---

## Login and privacy

The first run opens a browser window for you to log in manually — this handles 2FA and bot checks that a script can't.

Afterwards, the browser session is written to **`procare_session.json`** (mode `600`) and reused, so you don't retype anything. **Your email and password are never stored or seen by the script** — only the session cookie Procare itself issued, the same thing a "stay signed in" checkbox saves. Delete the file (or pass `--relogin`) to sign out.

`.gitignore` excludes `procare_session.json`, `procare_media/`, and the manifest, so **your session and your child's photos can never be committed**. Keep it that way if you fork this.

---

## Troubleshooting

**"Could not determine the gallery id"** — open Photos/Videos in the browser window manually, then re-run.

**It stops before the target date** — run `--nav-test` to see where. The week pager stops when the date label doesn't change, which usually means the page was still loading; the timeouts near the top of the script (`WEEK_LOAD_TIMEOUT`) can be raised.

**Lots of `miss` in the summary** — the viewer fallback is failing. Run with `--show` to watch, and check whether the modal opens on click.

**It suddenly finds nothing** — Procare changed their markup. The selectors are grouped near the top of `procare_download.py`:

```python
SEL_DROPDOWN   = "div.date-filter .dropdown-portal__header"   # Daily/Weekly picker
SEL_DATE_TITLE = '[data-testid="datepicker-title"]'           # "Aug 10 - Aug 16"
SEL_TILE       = "div.gallery__item"                          # a photo tile
SEL_DL_ANCHOR  = "a.action-button[href], a[download][href]"   # the download link
```

Open the gallery, inspect the element, and update the matching line.

**Session expired** — run with `--relogin`.

---

## Notes

Written for personal use, to get my own kid's photos out of a portal that has no bulk export. It fetches only what your account can already see, at a human-ish pace. Don't point it at an account that isn't yours.

No affiliation with Procare Software.
