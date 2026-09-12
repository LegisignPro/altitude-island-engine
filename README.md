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
| `delta_engine.py` | Prior-year CSV join, sq-ft delta, `CRITICAL: NEWBORN ISLAND` flag |
| `freight.py` | HQ state -> freight arbitrage tag |
| `apollo.py` | Organisation enrichment (1 credit), people search (0 credits), new-hire flag, mock mode |
| `pitch_generator.py` | Trigger hierarchy A > B > C > D, intro lines, Instantly/Smartlead CSV |
| `tests/` | Mock MapYourShow server + module tests + Streamlit AppTest flow |

## Workflow

1. Sidebar: paste any URL on the show's `mapyourshow.com` host, set the sq-ft
   range, click **Run Extraction**. Pavilions, associations, "State of" and
   "Department" listings are removed. Websites are fetched from the exhibitor
   detail pages for in-range exhibitors only.
2. **Tab 2**: upload last year's extraction (the CSV this app exports, or any
   CSV with a name column and sq-ft or width/length columns). Companies that
   jumped from under 200 sq ft to 400+ are flagged NEWBORN ISLAND.
3. **Tab 4**: run Apollo. Employee count, HQ and industry per company
   (>1,000 employees dropped); buyer contacts by title with months-in-role
   (< 6 months = NEW HIRE). Budget-capped: Apollo Free is 75 credits/month.
4. **Tab 3**: freight arbitrage by HQ region (needs Tab 4).
5. **Tab 5**: one trigger and pitch angle per company, preview, CSV export
   with `email, first_name, company_name, booth_sqft, trigger_badge,
   pitch_angle, custom_intro_line` and supporting columns.

Emails: Apollo people search never reveals addresses (the Free plan blocks
`people/match`), so `email` is blank unless Apollo returned a real one. Let
Instantly / Smartlead's finder fill it, or wire `apollo.reveal_email()` on a
paid plan.

## Tests

    python tests/test_modules.py   # scraper vs mock MapYourShow server, delta, freight, pitch, Apollo mock
    python tests/test_app.py       # headless Streamlit flow: extraction -> delta -> Apollo -> pitch -> export
