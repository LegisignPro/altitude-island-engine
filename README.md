# Altitude Exhibits — Island Lead Engine v3

Finds trade-show exhibitors worth pitching for a custom exhibit build: companies on island-size
booths (400+ sq ft), and above all companies whose booth **changed shape year over year**:
NEWBORN ISLAND (inline under 200 sq ft to 400+), PEAK RETREAT, STEADY GROWTH, DROPPED OUT.

Branch `v3` of `LegisignPro/altitude-island-engine`, deployed separately from v1 (`main`) and v2 (`v2`).

**No generated data, anywhere.** No demo dataset, no Apollo mock, no fallback. Any file containing
demo/generated rows (including anything written by the old `fallback_harvester.py`) is refused with
no override. Every size is read from the platform; a size the platform doesn't publish stays 0.

## Two ways to get a show's data

1. **Map Grabber bookmark (recommended).** Last tab of the app: drag the button to your bookmarks
   bar. Open a floor plan in your own browser, click it, and it downloads
   `<show>_<year>_<platform>.csv`. Works on every supported platform, including shows that block
   cloud servers, because it reads what the page itself loads. Do it for this year and prior years,
   then drop all the CSVs into **Map Grabber CSVs** in the sidebar: the newest year becomes the
   current show and the older years fill the Show Timeline.
2. **Live URL extraction** in the sidebar (Run Extraction). Same extractors, run on the server.

| Platform | Example | How it's read | Verified live (2026-09-23) |
|---|---|---|---|
| MapYourShow (incl. white-label, e.g. directory.imts.com) | `<show>.mapyourshow.com/8_0/...` | 3 JSON endpoints + detail pages | IMTS 2026 |
| A2Z / Personify | `kbis.a2zinc.net/kbis2026/Public/EventMap.aspx` | EventMap ids + JSONP booth API + eBooth pages | KBIS 2026 |
| EXPOCAD FX | `expocad.com/host/fx/<org>/<code>/exfx.html` | the page's own `window.data` (needs a browser) | Data Center World 2026 |
| ExpoFP | `<show>.expofp.com` | `/data/data.js` + per-layer SVG booth shapes | IMEX America 2025 + 2026 |

Not supported yet (clear message, no scrape): Coconnex, Swapcard, ExpoPlatform, Map Dynamics,
RX in-house directories.

## What each row carries

`exhibitor_name, booth_number, width, length, sqft, hall, website, is_sponsor, has_video_listing,
exhid, detail_url, size_source` (v2 schema) plus `platform, booth_count, shared_booth, shared_with,
booth_sqft, raw_size, city, state, country, company_linkedin, phone, description, categories`.

- **Shared booths** (pavilions, co-exhibitors): `sqft` is the company's share of the stand
  (`booth_sqft / shared_with`), so a pavilion member never looks like an island, and a company
  that leaves a pavilion for its own island shows up as a NEWBORN ISLAND.
- Website and HQ city/state come from the exhibitor's own listing (MapYourShow, A2Z, EXPOCAD)
  for island-size booths; freight tags use them when Apollo hasn't run.

## Extraction quality

Every extraction and every loaded CSV is graded PASS / WARN / FAIL (`quality.py`): row count,
sized share, width x length vs sq ft, standard sizes, implausible sizes, coverage vs the platform's
own exhibitor count, duplicates, demo leakage, year consistency, island sanity. FAIL blocks every
export ("Use anyway" stamps rows FAIL-OVERRIDDEN; demo leakage can never be overridden). Exports
carry `quality` and `quality_notes` columns. The **Platform Check** tab shows stage counts,
sample rows beside raw records, and the report for any URL or CSV.

## Run

    pip install -r requirements.txt
    streamlit run app.py

Apollo key: sidebar or `APOLLO_API_KEY` in `.streamlit/secrets.toml`. Tavily key (optional,
"Find the directory URL for me"): `TAVILY_API_KEY`.

## Files

    app.py              Streamlit UI (6 tabs)
    platforms.py        URL -> platform router, roadmap list
    extractors/         common.py (row schema, booth grouping), mys.py, a2z.py, expocad.py, expofp.py
                        each: extract(url, log) -> (rows, meta, raw); normalise(raw) pure, no network
    grabber/            grabber.js (source) and grabber.min.js (the bookmarklet); same row logic
    quality.py          quality checks
    showfile.py         CSV loader (grabber files, app exports, any name + sq-ft CSV); refuses demo rows
    delta_engine.py     year-over-year and multi-year trajectories (NEWBORN ISLAND ... DROPPED OUT)
    apollo.py, freight.py, pitch_generator.py, show_finder.py   as in v2 (Apollo mock removed)
    browser_scraper.py  ensure_browser() for EXPOCAD on the server
    scraper.py          MapYourShow HTTP client

## Tests

    python -m pytest -q tests/test_v3.py     # extractor mechanics, quality, demo refusal, real-capture parity
    python tests/test_modules.py             # delta engine, freight, pitch, Apollo (no key), router
    python tests/test_app.py                 # whole app via AppTest on real grabber CSVs

`tests/fixtures/real/` holds payloads captured by the Map Grabber on live shows (trimmed to keep
the repo small) with the browser's own CSV for the same companies; the parity test checks the
Python normalisers reproduce the browser's rows exactly.
