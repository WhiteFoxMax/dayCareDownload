# tools/

Diagnostic and historical scripts. **None of these are needed for normal use** —
run `procare_download.py` in the project root instead.

They're kept because Procare changes their markup from time to time, and these
are how the working selectors were found in the first place. Each one opens a
visible browser, waits for you to log in, and prints what it discovers.

Run them from the project root with the venv active, e.g.:

```bash
source venv/bin/activate
python tools/test_click_download.py
```

| Script | What it's for |
|---|---|
| `test_gallery_nav.py` | Walks the gallery's weekly pager and reports the period dropdown, the week arrows, and the tiles found per week. Use this first if week navigation breaks. |
| `test_click_download.py` | Opens a single tile and downloads it, dumping the viewer's markup if no download link appears. Use this if the download link moves. |
| `diagnose_scroll.py` | Tries six different scrolling techniques on the dashboard activity feed and reports which one actually loads more content. Only relevant to the old feed-based approach. |
| `download_procare_photos.py` | The **superseded** first approach: scraped the dashboard activity feed by infinite-scrolling it. Replaced by the gallery's weekly pager, which is faster and complete. Kept for reference. |

## Why the feed approach was replaced

The dashboard feed loads 30 activities at a time inside a nested scroll
container, mixed in with naps, meals and sign-ins. Reaching a year of history
meant ~130 scroll rounds, and photos had to be picked out of unrelated activity
cards.

The Photos/Videos gallery exposes the same media behind a weekly date filter,
with a clean tile grid and an explicit download link per item — far fewer page
loads and no ambiguity about what is a photo.
