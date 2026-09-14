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

Without a key the sidebar defaults to **mock Apollo responses**: deterministic
placeholder firmographics labelled MOCK in every table and in the export.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI: sidebar, 5 tabs, metrics, tables, export |
| `scraper.py` | MapYourShow JSON endpoints (halls, gallery, floor-plan geometry, detail-page websites) + demo fallback |
| `platforms.py` | Routes a directory URL to the right scraper by host (MapYourShow, A2Z, EXPOCAD, ExpoFP) |
| `browser_scraper.py` | Playwright network-interception extraction for A2Z/EXPOCAD/ExpoFP (canvas/SVG floor plans) + capture-first CLI |
| `delta_engine.py` | Prior-year CSV/URL join, sq-ft delta, `CRITICAL: NEWBORN ISLAND` flag, multi-year trajectory classification (PEAK RETREAT, STEADY GROWTH, ...) |
| `freight.py` | HQ state -> freight arbitrage tag |
| `apollo.py` | Organisation enrichment (1 credit), people search (0 credits), new-hire flag, mock mode |
| `pitch_generator.py` | Trigger hierarchy A > E > B > C > D, intro lines, Instantly/Smartlead CSV |
| `tests/` | Mock MapYourShow server + module tests + Streamlit AppTest flow + browser-scraper tests |

## Workflow

1. Sidebar: paste any directory / floor-plan URL (MapYourShow, A2Z, EXPOCAD or
   ExpoFP), set the sq-ft range, click **Run Extraction**. Pavilions,
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

## Adding a new floor-plan platform

A2Z, EXPOCAD and ExpoFP render exhibitor/booth data via canvas or SVG, not
HTML, so `browser_scraper.py` intercepts the JSON their own JavaScript fetches
from the network layer instead of parsing markup. The field names used in each
normaliser (`normalize_a2z`, `normalize_expocad`, `normalize_expofp`) are
**hypotheses**, not verified against a real show -- this container's egress
cannot reach a2zinc.net / expocad.com / expofp.com. To tune them against a
real show from a machine that can reach it:

    python browser_scraper.py capture <url> --out captures/<slug>.json
    python browser_scraper.py capture <url> --normalise a2z

The first command dumps every captured JSON response's shape and a sample
record; the second also runs the normaliser and prints a row-count summary so
the alias lists in `pick(...)` calls can be adjusted to match what actually
came back.

## Tests

    python tests/test_modules.py         # scraper vs mock MapYourShow server, show year, delta, trajectories, freight, pitch, Apollo mock
    python tests/test_app.py             # headless Streamlit flow: extraction -> timeline -> Apollo -> pitch -> export
    python tests/test_browser_scraper.py # A2Z/EXPOCAD/ExpoFP normalisers against fixtures + a real local Playwright capture
