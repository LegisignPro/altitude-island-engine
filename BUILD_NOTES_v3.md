# Island Engine v3 build notes (2026-09-23)

## Scope (Woz, 2026-09-23: "option C", "never ever fake data")
- A: rebuild v3 properly on branch `v3` (from `v2`), separate Streamlit app; v1 (`main`) and v2 (`v2`) untouched.
- B: Map Grabber bookmarklet that extracts any supported floor plan from inside the user's own browser.
- No generated data anywhere: demo dataset, Apollo mock mode and v2's guessed-format fixtures removed;
  demo/generated rows are refused on load and FAIL quality with no override.

## What replaced what
- The earlier local-only v3 (Haiku commit d72c55c, never pushed) was discarded: its A2Z extractor crashed
  on an undefined variable, its EXPOCAD and ExpoFP extractors read fields that don't exist, and its
  "tested against real shows" claim was copied from the spec. Only its idea of CSV comparison survives,
  as multi-file CSV loading in the sidebar.
- v2's browser_scraper field-guessing engine -> extractors/ built from the formats inspected live.
- v2's scraper.load_fallback_dataset and apollo mock -> deleted.

## Verified live (all 2026-09-23, in Woz's Chrome, via the Map Grabber)
| Show | Platform | Companies | 400+ sq ft | Python vs browser parity |
|---|---|---|---|---|
| IMEX America 2025 | ExpoFP | 3,654 | 54 | full show, 0 differences |
| IMEX America 2026 | ExpoFP | 3,622 | 58 | full show, 0 differences |
| Data Center World 2026 | EXPOCAD FX | 480 | 66 | full show, 0 differences |
| KBIS 2026 | A2Z | 586 (of 609 listed, 96%) | 341 | see fixtures |
| IMTS 2026 | MapYourShow (white-label) | 1,756 listed | - | see fixtures |

Real YoY run, IMEX 2025 -> 2026: 5 NEWBORN ISLANDS (Explore Edmonton 157 -> 495, Blue Delta Jeans 100 -> 450,
Shangri-la Hotels & Resorts 50 -> 400, The Expo Group 100 -> 400, University Village 80 -> 400), biggest
upgrades Together by Hyatt 1,000 -> 2,400, Show Gear Productions 400 -> 800, Stova 300 -> 600.

## Findings that changed the design
- **Shared booths.** IMEX: 272 of 469 linked booths are shared, up to 109 exhibitors on one stand (hotel and
  destination pavilions). The spec's "give each company the full booth area" would have created ~3,000 fake
  islands. v3 credits each company area / companies (`sqft`), keeps `booth_sqft`, flags `shared_booth`.
- **ExpoFP layer discovery** is solved without a browser: `fp.svg.js` declares `__fpLayers`; each layer is
  `fp.svg.<name>.js`. Irregular booths are `<g id="b...">` whose path points into `__fpPaths<layer>`.
- **ExpoFP duplicate listings**: the same company can have several exhibitor ids; rows are grouped by name.
- **A2Z's booth API has no CORS**: the grabber uses JSONP like the site; the server uses requests.
- **MapYourShow detail pages** carry `addressValues` (city/state/country), website, LinkedIn, phone. They are
  ~630 KB each, so detail pages are read only for 400+ sq ft booths.
- **Browser downloads**: Chrome blocks a second automatic download from the same page; the grabber saves
  one file and shows a "Download CSV again" button.

## Still open
- EXPOCAD `<org>.expocad.com/Events/<code>/index.html` URLs: routed to EXPOCAD, not inspected.
- A2Z `customTileBaseUrl`: handled, never seen on a live show.
- Server-side EXPOCAD needs Chromium on Streamlit Cloud (best effort); the grabber always works.
- Some ExpoFP "booths" are show features (Media Zone, IMEX Press Center, Silent Seminars) rather than
  companies; they are not filtered yet.
- Roadmap platforms: Coconnex, Swapcard, ExpoPlatform, Map Dynamics, RX in-house directories.
