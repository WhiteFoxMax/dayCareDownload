# Changelog

All notable changes to this project, newest first.

This started as a script that scrolled the dashboard feed and clicked every
photo. It ended up reading Procare's own API. The entries below keep the wrong
turns in, because each one explains why the current design is shaped the way it
is.

---

## 0.5.0 — File dates

**Files carry the day the photo was taken.** Created and modified dates are set
from the activity's own date, so photos sort chronologically in Finder and
import into Photos on the right day instead of clustering on the download date.

- `os.utime` sets the modified date everywhere. Finder sorts by the *creation*
  date, which `utime` cannot touch, so `SetFile` is used when present (macOS
  command line tools) and skipped silently otherwise.
- Stamped at **noon**, not midnight — a midnight stamp can roll onto the
  adjacent day under a timezone shift.
- `--fix-dates` stamps an existing folder from the dates in the filenames,
  batching `SetFile` by date: one subprocess per distinct day rather than per
  file, which is seconds instead of minutes across ~1,400 files.

## 0.4.0 — One folder per child

**Downloads land in `<out>/<child>/`**, with the manifest at the top level.

- The child stays in the filename as well as the folder, so a file still names
  its subject if it is moved out or emailed.
- `Store` walks subfolders when indexing, so de-duplication and resume keep
  working across the layout change.
- `--organize` migrates an existing folder using the filenames alone, no
  network required.

## 0.3.0 — Names in filenames

**Files are now `YYYYMMDD_<child>_<contenthash>.<ext>`.** With more than one
child on an account, a flat date-sorted folder didn't say who anything was of.

- A photo of two children is one file on Procare's side, so it is stored once,
  under whichever child's walk reached it first.
- `--rename-existing` migrates older folders: it rebuilds the file-to-child map
  from the API and renames in place, without downloading anything.
- The existing-file scan accepts both the old and new name forms, so
  de-duplication survives the change.

## 0.2.0 — Read the API; cover every child

The release that fixed photos going missing. Three separate defects:

1. **Only one child was ever fetched.** The gallery id found on the first run
   was treated as the whole account, but `/api/web/parent/kids/` lists every
   child. Both the API walk and the gallery walk now run per child — the gallery
   URL is `/dashboard/gallery/<kid_id>/…`, so siblings need their own walk. This
   was 39% of the collection, invisible the entire time.
2. **Items were silently dropped.** `pick_full_size` returned `None` whenever an
   item had two non-thumbnail URLs — which is *every* photo, since they carry
   both `main_url` and `medium_url`. Renditions are now chosen by key
   (`video_file_url`, then `main_url`, then `photo_url`); the heuristic is kept
   only for unrecognised shapes and can no longer bail out.
3. **Dates were upload times.** A photo's `created_at` is when it was uploaded.
   Dates now inherit the enclosing activity's `activity_time`, so a photo taken
   Monday and uploaded Wednesday files under Monday.

**New `--source api`** pages `/api/web/parent/daily_activities/` directly, using
the Bearer token captured from the logged-in browser (memory only, never written
or logged). A year of history is ~100 pages in under a minute, and unlike
scraping it cannot miss an item because something failed to render.

`--kid` limits to one child. `tools/diag_recent.py` and `tools/diag_api.py` are
the diagnostics that found all of this.

## 0.1.0 — The activity feed as a second source

Some photos are only ever posted as feed activities and never reach the
Photos/Videos gallery — 110 of them in a year of history.

- `PayloadSource` base class extracted from `Gallery`; `Feed` reuses the JSON
  capture, sequence counter and event pump.
- The feed scrolls `section.section`. The window does not scroll, and
  `div.activity-list` has `scrollHeight == clientHeight`, so neither of those
  works — this cost an afternoon to discover.
- `--source all|gallery|feed`, defaulting to all.
- Progress is recorded per source but **checked across all sources** before
  queueing: the gallery and the feed serve the same photo under the same file
  UUID, so this skips it before fetching rather than downloading the bytes only
  to discard them by content hash. Turned a ~300 MB pass into 11 MB.

## 0.0.2 — Components, and a much faster walk

Split into `Gallery`, `Walker`, `DownloadPool`, `Store` and `Stats`.

- **Week changes are network-driven.** Previously each week meant clicking `<`
  and polling the DOM until the tile count settled — a guaranteed ~1.5s tax per
  week. Now it waits for the gallery's own JSON payload, which is both the
  completion signal and the data. 59 weeks across both tabs in 104s.
- `-o/--out` sets the download folder; the manifest and resume state live inside
  it, so a folder is self-describing and can be moved as a unit.
- The absolute path is printed before downloading and again at the end, with
  file count and total size.
- **Fixed:** resume keys were derived from the full URL, but the CloudFront
  signature changes on every page load, so keys were unstable across runs. They
  now use the URL path only.

## 0.0.1 — First working downloader

Walks the gallery's weekly date filter back to a target date and saves
full-resolution originals.

- Manual login once; the session is saved and reused, so credentials are never
  stored — only the session Procare itself issued.
- Downloads run through a thread pool over plain HTTP: the CDN links are
  CloudFront signed URLs, so they need neither browser nor cookies.
- Content-hash filenames, so re-runs never duplicate. Per-source progress files
  make an interrupted run resumable.
- `--nav-test` verifies the whole date range is reachable without downloading.

### Before this: the approach that didn't survive

The first version scrolled the dashboard activity feed and opened each photo's
viewer to read its download link. It worked, but 30 activities per scroll and a
modal per photo meant ~30 minutes for a year of history, and it was hostage to
scroll timing. The gallery's weekly pager replaced it; the API replaced that.
Both are kept in `tools/` — they are how the working selectors were found.
