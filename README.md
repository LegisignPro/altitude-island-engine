# Altitude Exhibits — Island Lead Engine

Low-volume, high-intent island exhibitors (400 to 3,000 sq ft by default) from
MapYourShow facts and Apollo firmographics. No modelled revenue, no guessed
websites, no synthetic lead scores.

## Run

    pip install -r requirements.txt
    streamlit run app.py

Optional: put the Apollo master key in `.streamlit/secrets.toml` (locally) or
App settings > Secrets (Streamlit Community Cloud):

    APOLLO_API_KEY = "xxxxxxxx"
    TAVILY_API_KEY = "tvly-xxxxxxxx"   # optional: enables "Find the directory URL for me"

Without an Apollo key the sidebar defaults to **mock Apollo responses**:
deterministic placeholder firmographics labelled MOCK in every table and in
the export. Without a Tavily key the finder is shown disabled with a one-line
hint; nothing else changes.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI: sidebar, 5 tabs, metrics, tables, export |
| `scraper.py` | MapYourShow JSON endpoints (halls, gallery, floor-plan geometry, detail-page websites) + demo fallback |
| `platforms.py` | Routes a directory URL to the right scraper by host (MapYourShow, A2Z, EXPOCAD, ExpoFP) |
| `browser_scraper.py` | Playwright network-interception extraction for A2Z/EXPOCAD/ExpoFP (canvas/SVG floor plans): fuzzy field-role resolution, value-overlap join discovery, graceful partial results + capture-first CLI |
| `show_finder.py` | "Find it for me": Tavily search for a show's directory URL(s) by name, current + prior years, every hit content-verified before it is offered |
| `delta_engine.py` | Prior-year CSV/URL join, sq-ft delta, `CRITICAL: NEWBORN ISLAND` flag, multi-year trajectory classification (PEAK RETREAT, STEADY GROWTH, ...) |
| `freight.py` | HQ state -> freight arbitrage tag |
| `apollo.py` | Organisation enrichment (1 credit), people search (0 credits), new-hire flag, mock mode |
| `pitch_generator.py` | Trigger hierarchy A > E > B > C > D, intro lines, Instantly/Smartlead CSV |
| `tests/` | Mock MapYourShow server + module tests + Streamlit AppTest flow + browser-scraper tests |

## Workflow

1. Sidebar: paste any directory / floor-plan URL (MapYourShow, A2Z, EXPOCAD or
   ExpoFP) -- or, with a Tavily key, open **Find the directory URL for me**,
   type the show's name and let the engine search for the current edition's
   directory and up to 5 prior editions. Only URLs whose page content was
   extracted and verified as an exhibitor listing are offered (a same-pattern
   guess like nab24.mapyourshow.com returns HTTP 200 with the marketing
   homepage -- a 200 is never treated as proof). The per-year summary is the
   honest answer to "how many years of maps exist": whatever search can still
   find AND verify. Set the sq-ft range, click **Run Extraction**. Pavilions,
   associations, "State of" and "Department" listings are removed. Websites
   are fetched from the exhibitor detail pages for in-range MapYourShow
   exhibitors only (the other platforms carry a website in their own JSON).
   The show name and year are editable right below the URL field -- always
   either what this extraction observed or what's typed there, never derived
   from today's date or a show calendar.
2. **Tab 2 (Show Timeline)**: add up to 6 prior-year slots, each a CSV (this
   app's own export, or any file with a name + sq-ft column) or a directory
   URL for that year's show. With 3+ years on file the engine sees the SHAPE
   of a company's booth history: NEWBORN ISLAND (jumped from under 200 to
   400+ sq ft), PEAK RETREAT (grew to a peak, pulled back to 50-90% of it --
   likely shopping for a new exhibit partner), STEADY GROWTH, SHRINKING,
   VOLATILE, or the original two-point UPGRADE/DOWNSIZE/STAGNANT read.
3. **Tab 4**: run Apollo. Employee count, HQ and industry per company
   (>1,000 employees dropped); buyer contacts by title with months-in-role
   (< 6 months = NEW HIRE). Budget-capped: Apollo Free is 75 credits/month.
4. **Tab 3**: freight arbitrage by HQ region (needs Tab 4).
5. **Tab 5**: one trigger and pitch angle per company (A NEWBORN ISLAND > E
   PEAK RETREAT > B NEW HIRE > C FREIGHT ARBITRAGE > D AOR FRICTION),
   preview, CSV export with `email, first_name, company_name, booth_sqft,
   trigger_badge, pitch_angle, custom_intro_line, trajectory, peak_sqft,
   peak_year` and supporting columns.

Emails: Apollo people search never reveals addresses (the Free plan blocks
`people/match`), so `email` is blank unless Apollo returned a real one. Let
Instantly / Smartlead's finder fill it, or wire `apollo.reveal_email()` on a
paid plan.

## Adding a new floor-plan platform / tuning the browser normalisers

A2Z, EXPOCAD and ExpoFP render exhibitor/booth data via canvas or SVG, not
HTML, so `browser_scraper.py` intercepts the JSON their own JavaScript fetches
from the network layer instead of parsing markup. The field names each
platform uses are **hypotheses** -- this container's egress cannot reach
a2zinc.net / expocad.com / expofp.com -- and the first live EXPOCAD run
(swe.expocad.com, WE26) captured five JSON responses and produced zero rows
because the normaliser then demanded an exact alias hit on every field of one
assumed shape. The normalisers are now built to bend instead of break:

- **Roles, not fixed keys.** Each record array is profiled once: every key
  path (nested objects and sub-record lists included) is matched against
  `ROLE_ALIASES` (name, booth number, booth id, company id, area, width,
  length, polygon, website, hall) in confidence tiers -- exact, nested leaf,
  substring, fuzzy edit distance -- and every hit is sanity-checked against
  the actual values (an "Area" holding "North Hall" is not an area).
- **Joins are discovered, not assumed.** Booth arrays and exhibitor arrays
  are linked by whichever id-shaped fields actually share values; several
  per-hall booth arrays that link to the same exhibitor list are merged.
- **Partial beats nothing.** Strict join -> single self-contained array ->
  best-effort names-only. An exhibitor with no readable footprint is a row
  with 0 sq ft and `size_source=unknown`; a footprint read through a fuzzy
  field match is tagged `<platform>-json-fuzzy` so it is visibly lower
  confidence in the table and the export. Nothing is ever invented.
- **Failures explain themselves.** The extraction log (and the error when
  zero rows result) lists the arrays seen, their keys, which roles resolved
  to which key and which strategy was used.

For deep debugging against a real show from a machine that can reach it:

    python browser_scraper.py capture <url> --out captures/<slug>.json
    python browser_scraper.py capture <url> --normalise expocad

If a real payload uses a spelling the fuzzy matcher still misses, add it to
`ROLE_ALIASES` and a fixture under `tests/fixtures/` modelling that shape.

## Tests

    python tests/test_modules.py         # scraper vs mock MapYourShow server, show year, delta, trajectories, freight, pitch, Apollo mock, show finder (mocked Tavily)
    python tests/test_app.py             # headless Streamlit flow: extraction -> timeline -> Apollo -> pitch -> export
    python tests/test_browser_scraper.py # A2Z/EXPOCAD/ExpoFP normalisers against happy-path AND weird-shape fixtures + a real local Playwright capture

The live Tavily network path (api.tavily.com) is unreachable from the build
container, so `show_finder.py`'s decision logic is tested against a mocked
client; its SDK call shape follows tavily-python's documented API.
