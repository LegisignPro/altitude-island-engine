"""
app.py -- Altitude Exhibits Island Lead Engine (Streamlit).

    pip install -r requirements.txt
    streamlit run app.py

Modules
    platforms.py        URL -> platform router (MapYourShow incl. custom domains, A2Z, EXPOCAD, ExpoFP)
    extractors/         one module per platform: extract() live, normalise() pure (tested on real payloads)
    quality.py          PASS / WARN / FAIL checks on every extraction and every loaded CSV
    showfile.py         loads Map Grabber CSVs / app exports; refuses generated data
    grabber/            the in-browser Map Grabber bookmark (same row logic as extractors/)
    scraper.py          MapYourShow HTTP client
    delta_engine.py     year-over-year footprint delta ("Newborn Island")
    freight.py          HQ-based freight arbitrage tagging
    apollo.py           Apollo organisation enrichment + people search (live key only, no mock)
    pitch_generator.py  trigger hierarchy, intro lines, Instantly/Smartlead export
    show_finder.py      Tavily search + content verification: show name -> directory URL(s), by year

Every number on screen is either an observed floor-plan fact, an Apollo
firmographic, or a value computed from those two. Nothing is modelled and
there is no demo or mock data anywhere in v3.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import apollo
import quality
import showfile
from extractors.common import ROW_COLUMNS, ExtractionError
import delta_engine
import freight
import pitch_generator as pg
import platforms
import scraper
import show_finder

# =============================================================================
# Config
# =============================================================================

APP_TITLE = "Altitude Exhibits | Island Lead Engine"
DEFAULT_MIN_SQFT = 400
DEFAULT_MAX_SQFT = 3000
DEFAULT_APOLLO_BUDGET = 25

TAB_LABELS = [
    "🎯 Lead Scanner & Directory Extractor",
    "📈 Show Timeline & Newborn Islands",
    "🚚 Vegas Freight & Local Storage Arbitrage",
    "👤 Apollo Contact Enrichment & New Hire Finder",
    "✉️ Pitch Angle Generator & Export",
    "🧭 Map Grabber & Platform Check",
]
GRADE_PILL = {"PASS": "ax-pass", "WARN": "ax-warn", "FAIL": "ax-fail", "FAIL-OVERRIDDEN": "ax-fail"}

CSS = """
<style>
:root { --ax-navy: #0b1f3a; --ax-blue: #1f6fb5; --ax-amber: #f59e0b; --ax-green: #22c55e; --ax-red: #ef4444; }
.ax-header { background: linear-gradient(135deg, #0b1f3a 0%, #123c6e 55%, #1f6fb5 100%); color: #fff;
             padding: 1.2rem 1.5rem; border-radius: 12px; margin-bottom: .8rem; }
.ax-header h1 { color: #fff; font-size: 1.55rem; margin: 0 0 .2rem 0; line-height: 1.2; }
.ax-header p { color: rgba(255,255,255,.85); margin: 0; font-size: .92rem; }
.ax-pill { display: inline-block; padding: .2rem .65rem; border-radius: 999px; font-size: .74rem; font-weight: 700;
           letter-spacing: .05em; text-transform: uppercase; margin: 0 .35rem .35rem 0; border: 1px solid transparent; }
.ax-live   { background: rgba(34,197,94,.16);  color: #16a34a; border-color: rgba(34,197,94,.5); }
.ax-pass   { background: rgba(34,197,94,.16);  color: #16a34a; border-color: rgba(34,197,94,.5); }
.ax-warn   { background: rgba(245,158,11,.18); color: #d97706; border-color: rgba(245,158,11,.55); }
.ax-fail   { background: rgba(239,68,68,.16);  color: #dc2626; border-color: rgba(239,68,68,.55); }
.ax-info   { background: rgba(59,130,246,.14); color: #2563eb; border-color: rgba(59,130,246,.45); }
.ax-badge  { display: inline-block; padding: .18rem .6rem; border-radius: 6px; font-size: .74rem; font-weight: 700;
             color: #fff; letter-spacing: .04em; margin-right: .4rem; }
.ax-muted { opacity: .72; font-size: .86rem; }
div[data-testid="stMetric"] { background: rgba(31,111,181,.08); border: 1px solid rgba(31,111,181,.25);
                               border-radius: 10px; padding: .55rem .8rem; }
div[data-testid="stMetric"] label { font-weight: 600; }
.stTabs [data-baseweb="tab"] { font-weight: 600; }
</style>
"""


def md(text: str) -> str:
    """Escape $ so Streamlit's markdown does not treat '$150' as LaTeX."""
    return text.replace("$", "\\$")


def fmt_money(v: float) -> str:
    if v >= 1e6:
        return f"${v / 1e6:,.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:,.0f}K"
    return f"${v:,.0f}"


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower() or "export"


def current_show_label() -> str:
    """
    Single source of truth for the show's display name and year, everywhere the
    UI, filenames and exports need it. Reads the editable sidebar fields
    (session_state["show_base"]/["show_year"]), falling back to whatever the
    scraper inferred from the URL when the user hasn't overridden them. The
    year is NEVER re-derived from today's date or a target-show calendar --
    that was the source of the old "+1 year" bug.
    """
    meta = st.session_state.get("meta") or {}
    base = (st.session_state.get("show_base") or "").strip() or (meta.get("show_base") or "Trade Show")
    year = st.session_state.get("show_year")
    if year in (None, ""):
        year = meta.get("show_year")
    return f"{base} {int(year)}" if year not in (None, "") else base


def show_file_stub() -> str:
    """
    Filename-safe '{show_base}_{show_year}' (year omitted when unknown), built from the
    same editable fields as current_show_label().
    """
    meta = st.session_state.get("meta") or {}
    base = (st.session_state.get("show_base") or "").strip() or (meta.get("show_base") or "Trade Show")
    year = st.session_state.get("show_year")
    if year in (None, ""):
        year = meta.get("show_year")
    return f"{slug(base)}_{int(year)}" if year not in (None, "") else slug(base)


# =============================================================================
# State
# =============================================================================

def init_state() -> None:
    st.session_state.setdefault("exhibitors", None)      # DataFrame: every exhibitor extracted
    st.session_state.setdefault("meta", None)            # dict from scraper
    st.session_state.setdefault("extract_log", [])       # progress lines
    st.session_state.setdefault("show_base", "")          # editable show name (Task 1: year fix)
    st.session_state.setdefault("show_year", None)        # editable show year, int or None
    st.session_state.setdefault("timeline_slots", [])     # list of prior-year slot dicts (Task 2)
    st.session_state.setdefault("_slot_seq", 0)           # stable id generator for slots
    st.session_state.setdefault("orgs", {})              # domain -> apollo.get_organization()
    st.session_state.setdefault("people", {})            # domain -> apollo.search_people()
    st.session_state.setdefault("credits_used", 0)
    st.session_state.setdefault("apollo_log", [])
    st.session_state.setdefault("finder", None)          # show_finder.find_show_urls() result
    st.session_state.setdefault("finder_error", "")
    st.session_state.setdefault("tavily_key", "")
    st.session_state.setdefault("quality", None)         # quality.check() of the current show
    st.session_state.setdefault("raw", None)             # canonical raw payload of the last live extraction
    st.session_state.setdefault("csv_errors", [])


def reset_apollo() -> None:
    st.session_state["orgs"] = {}
    st.session_state["people"] = {}
    st.session_state["credits_used"] = 0
    st.session_state["apollo_log"] = []


# Widget callbacks run before the script body, so state changes land in the same
# rerun and the user stays on the tab they clicked (an explicit st.rerun() would
# bounce them back to the first tab).

MAX_TIMELINE_SLOTS = 6


def add_timeline_slot() -> None:
    slots = st.session_state["timeline_slots"]
    if len(slots) >= MAX_TIMELINE_SLOTS:
        return
    st.session_state["_slot_seq"] += 1
    slots.append({"id": st.session_state["_slot_seq"], "year": None, "df": None, "label": "", "error": ""})


def remove_timeline_slot(slot_id: int) -> None:
    st.session_state["timeline_slots"] = [s for s in st.session_state["timeline_slots"] if s["id"] != slot_id]


def load_timeline_slot(slot_id: int) -> None:
    """Load (or reload) one timeline slot from whatever its widgets currently hold."""
    slot = next((s for s in st.session_state["timeline_slots"] if s["id"] == slot_id), None)
    if slot is None:
        return
    year = st.session_state.get(f"slot_year_{slot_id}")
    mode = st.session_state.get(f"slot_mode_{slot_id}", SLOT_MODE_CSV)
    slot["error"] = ""
    if not year:
        slot["error"] = "Set a year first."
        return
    slot["year"] = int(year)
    if mode == SLOT_MODE_CSV:
        up = st.session_state.get(f"slot_csv_{slot_id}")
        if up is None:
            slot["error"] = "Choose a CSV file first."
            return
        try:
            df, fmeta = showfile.load(up)
            q = quality.check(df.to_dict("records"), fmeta, chosen_year=int(year))
            if q["grade"] == "FAIL" and not st.session_state.get(f"slot_force_{slot_id}"):
                slot["df"] = None
                slot["error"] = f"Quality FAIL: {q['notes']}"
                return
            slot["df"] = df[["exhibitor_name", "sqft"]]
            slot["label"] = up.name
            slot["quality"] = q["grade"]
        except ValueError as exc:
            slot["df"] = None
            slot["error"] = str(exc)
    else:
        url = (st.session_state.get(f"slot_url_{slot_id}") or "").strip()
        if not url:
            slot["error"] = "Paste a directory URL first."
            return
        if not url.startswith("http"):
            url = "https://" + url
        try:
            # facts only: no website fetch, no Apollo -- any supported platform works here
            rows, meta, _raw = platforms.extract(url)
            q = quality.check(rows, meta, chosen_year=int(year))
            if q["grade"] == "FAIL" and (q["demo"] or not st.session_state.get(f"slot_force_{slot_id}")):
                slot["df"] = None
                slot["error"] = f"Quality FAIL: {q['notes']}"
                return
            slot["df"] = pd.DataFrame(rows, columns=ROW_COLUMNS)[["exhibitor_name", "sqft"]]
            slot["label"] = meta.get("show_name") or url
            slot["quality"] = q["grade"]
        except (ExtractionError, scraper.ScrapeError) as exc:
            slot["df"] = None
            slot["error"] = f"Extraction failed: {exc}"
        except Exception as exc:  # a bad year/site must not crash the whole app
            slot["df"] = None
            slot["error"] = f"Unexpected error: {exc.__class__.__name__}: {exc}"


def timeline_slots_for_trajectories() -> list[dict]:
    """
    Every loaded prior slot plus the live extraction as the newest slot, ready for
    delta_engine.build_trajectories(). The live year is the same editable show_year used
    everywhere else (current_show_label()) -- never re-derived from today's date.
    """
    out = []
    for s in st.session_state["timeline_slots"]:
        if s.get("df") is not None and s.get("year"):
            out.append({"year": int(s["year"]), "label": s.get("label") or str(s["year"]),
                       "source": "prior", "df": s["df"]})
    live_df = st.session_state.get("exhibitors")
    if live_df is not None:
        meta = st.session_state.get("meta") or {}
        year = st.session_state.get("show_year")
        if year in (None, ""):
            year = meta.get("show_year")
        if year not in (None, ""):
            out.append({"year": int(year), "label": current_show_label(), "source": "live",
                       "df": live_df[["exhibitor_name", "sqft"]]})
    return out


def on_run_apollo() -> None:
    params = st.session_state.get("_params")
    targets = st.session_state.get("_targets")
    if params is None or targets is None:
        return
    run_apollo(params, targets)


# --- "Find it for me" (show_finder) -------------------------------------------------------------
# The finder only ever offers URLs whose page content has been extracted and verified as an
# exhibitor listing (show_finder.looks_like_directory); a search hit that failed verification is
# not shown at all. These callbacks fill widgets, so they must run before the widgets render.

SLOT_MODE_CSV, SLOT_MODE_URL = "CSV upload", "Directory URL"


def use_found_url(url: str) -> None:
    st.session_state["url_box"] = url


def send_found_to_slot(year: int, url: str) -> None:
    """Put a verified prior-year URL into the timeline slot for that year (creating one if needed)."""
    slots = st.session_state["timeline_slots"]
    slot = next((sl for sl in slots if st.session_state.get(f"slot_year_{sl['id']}", sl.get("year")) == year), None)
    if slot is None:
        if len(slots) >= MAX_TIMELINE_SLOTS:
            return
        add_timeline_slot()
        slot = slots[-1]
        slot["year"] = year
    sid = slot["id"]
    st.session_state[f"slot_year_{sid}"] = year
    st.session_state[f"slot_mode_{sid}"] = SLOT_MODE_URL
    st.session_state[f"slot_url_{sid}"] = url


def run_show_finder(show: str, years: list[int]) -> None:
    """Search + verify; the result (or the plain-English failure) lands in session state."""
    st.session_state["finder_error"] = ""
    try:
        st.session_state["finder"] = show_finder.find_show_urls(
            show, years=years, api_key=st.session_state.get("tavily_key", ""))
    except show_finder.ShowFinderError as exc:
        st.session_state["finder_error"] = str(exc)
    except Exception as exc:   # never let a lookup take the page down
        st.session_state["finder_error"] = f"Unexpected error: {exc.__class__.__name__}: {exc}"


def run_show_finder_year(show: str, year: int) -> None:
    """Probe ONE prior year from a timeline slot and merge it into the existing finder result."""
    st.session_state["finder_error"] = ""
    try:
        res = show_finder.find_show_urls(show, years=[year], api_key=st.session_state.get("tavily_key", ""))
    except show_finder.ShowFinderError as exc:
        st.session_state["finder_error"] = str(exc)
        return
    except Exception as exc:
        st.session_state["finder_error"] = f"Unexpected error: {exc.__class__.__name__}: {exc}"
        return
    cur = st.session_state.get("finder") or {"show": res["show"], "current": [], "by_year": {},
                                              "years_with_maps": [], "years_probed": [], "log": []}
    cur["by_year"][year] = res["by_year"].get(year, [])
    cur["years_probed"] = sorted(set(cur.get("years_probed", [])) | {year}, reverse=True)
    cur["years_with_maps"] = sorted({y for y in cur["years_probed"] if cur["by_year"].get(y)}, reverse=True)
    cur["log"] = (cur.get("log") or []) + res["log"]
    st.session_state["finder"] = cur


def finder_hit_label(hit: dict) -> str:
    platform = {"mapyourshow": "MapYourShow", "a2z": "A2Z", "expocad": "EXPOCAD", "expofp": "ExpoFP"}.get(
        hit.get("platform"), "verified listing, unsupported platform")
    return f"[{platform}] {hit.get('title') or hit['url']}"


def render_finder_hits(hits: list[dict], key: str, action_label: str, on_pick, extra_args=()) -> None:
    """A selectbox of verified hits + one action button; unsupported hits are shown as plain links."""
    supported = [h for h in hits if h.get("supported")]
    others = [h for h in hits if not h.get("supported")]
    if supported:
        labels = [finder_hit_label(h) for h in supported]
        idx = st.selectbox("Verified directory URL", range(len(labels)), format_func=lambda i: labels[i],
                           key=f"{key}_pick", label_visibility="collapsed")
        chosen = supported[idx]
        st.caption(chosen["url"])
        st.button(action_label, key=f"{key}_use", on_click=on_pick, args=tuple(extra_args) + (chosen["url"],),
                  width="stretch")
    for h in others:
        st.markdown(f"- Verified exhibitor listing on a platform this engine cannot extract "
                    f"(open it by hand): [{h.get('title') or h['url']}]({h['url']})")


# =============================================================================
# Sidebar
# =============================================================================

def render_sidebar() -> dict:
    with st.sidebar:
        st.markdown("### Altitude Exhibits")
        st.caption("Island Lead Engine v3: floor-plan facts + Apollo firmographics. No modelled, demo or mock data.")

        st.markdown("**API configuration**")
        secret_key = ""
        try:
            secret_key = st.secrets.get("APOLLO_API_KEY", "")
        except Exception:
            secret_key = ""
        api_key = st.text_input("Apollo API key", value=secret_key, type="password",
                                help="Master API key from app.apollo.io > Settings > Integrations > API. "
                                     "Without a key the Apollo tab stays empty (there is no mock mode).")
        api_key = (api_key or "").strip()
        budget = st.number_input("Max Apollo org lookups per run (1 credit each)", min_value=1, max_value=500,
                                 value=DEFAULT_APOLLO_BUDGET, step=5,
                                 help="Apollo Free = 75 credits/month. People search is free.")
        tavily_secret = ""
        try:
            tavily_secret = st.secrets.get("TAVILY_API_KEY", "")
        except Exception:
            tavily_secret = ""
        tavily_key = st.text_input("Tavily API key (optional: enables 'Find it for me')", value=tavily_secret,
                                   type="password",
                                   help="Free tier at tavily.com (~1,000 searches/month). Used only to search for and "
                                        "verify a show's exhibitor-directory URL. Leave blank to skip that feature.")
        st.session_state["tavily_key"] = (tavily_key or "").strip()

        st.divider()
        st.markdown("**Target show**")
        st.file_uploader("Map Grabber CSVs (one or more years of the same show)", type=["csv"],
                         accept_multiple_files=True, key="show_csvs",
                         help="Download each year's floor plan with the Map Grabber bookmark (last tab), then drop "
                              "the files here. The newest year becomes the current show; older years fill the "
                              "Show Timeline automatically. Files with generated/demo rows are refused.")
        load_csvs = st.button("Load CSVs", width="stretch", disabled=not st.session_state.get("show_csvs"))
        for err in st.session_state.get("csv_errors") or []:
            st.error(err)
        st.caption("...or extract live from a URL:")
        url = st.text_input("Directory / floor-plan URL (MapYourShow, A2Z, EXPOCAD, ExpoFP)", key="url_box",
                            placeholder="https://ces2026.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm",
                            help="MapYourShow (also white-label hosts like directory.imts.com), A2Z EventMap.aspx, "
                                 "ExpoFP <show>.expofp.com, EXPOCAD exfx.html. Some shows block cloud servers; "
                                 "the Map Grabber bookmark in your own browser always works.")
        render_show_finder()

        # Editable show name/year (Task 1: the "+1 year" fix). Re-seeded from the freshly
        # extracted meta only when a NEW extraction lands (its extracted_at stamp changes);
        # otherwise these boxes keep whatever the user last typed, across unrelated reruns
        # (e.g. a filter change). The year is a plain text field, never a number_input pinned
        # to today's date -- it is ALWAYS either what this extraction observed or what's typed
        # here, never derived from the calendar or a target-show list. That derivation was the
        # source of the old bug.
        meta_now = st.session_state.get("meta") or {}
        stamp = meta_now.get("extracted_at", "")
        if st.session_state.get("_show_fields_synced_at") != stamp:
            st.session_state["show_base_box"] = st.session_state.get("show_base") or ""
            prev_year = st.session_state.get("show_year")
            st.session_state["show_year_box"] = str(prev_year) if prev_year not in (None, "") else ""
            st.session_state["_show_fields_synced_at"] = stamp
        sc1, sc2 = st.columns([2, 1])
        sc1.text_input("Show name", key="show_base_box", placeholder="Inferred after extraction",
                       help="Editable. Feeds the banner, filenames, export and pitch lines.")
        sc2.text_input("Show year", key="show_year_box", placeholder="Inferred",
                       help="Editable 4-digit year. Blank uses the year inferred from the URL for this "
                            "extraction -- never today's date or a show calendar.")
        show_base_val = (st.session_state["show_base_box"] or "").strip()
        year_text = (st.session_state["show_year_box"] or "").strip()
        show_year_val = None
        if year_text:
            if re.fullmatch(r"\d{4}", year_text) and 2015 <= int(year_text) <= 2035:
                show_year_val = int(year_text)
            else:
                st.error("Show year must be a 4-digit year between 2015 and 2035.")
        st.session_state["show_base"] = show_base_val
        st.session_state["show_year"] = show_year_val

        st.divider()
        st.markdown("**Global filters**")
        c1, c2 = st.columns(2)
        min_sqft = c1.number_input("Min booth sq ft", min_value=0, max_value=20000, value=DEFAULT_MIN_SQFT, step=50)
        max_sqft = c2.number_input("Max booth sq ft", min_value=100, max_value=50000, value=DEFAULT_MAX_SQFT, step=100)
        if max_sqft < min_sqft:
            st.error("Max sq ft must be at least the min.")

        st.divider()
        run = st.button("Run Extraction", type="primary", width="stretch")

    return {"api_key": api_key, "budget": int(budget), "url": (url or "").strip(),
            "min_sqft": int(min_sqft), "max_sqft": int(max(max_sqft, min_sqft)),
            "run": run, "load_csvs": load_csvs}


def render_show_finder() -> None:
    """Sidebar expander: show name -> searched, content-verified directory URLs (current + prior years)."""
    have_key = bool(st.session_state.get("tavily_key"))
    finder = st.session_state.get("finder")
    title = "Find the directory URL for me"
    if finder:
        title += f" ({len(finder['current'])} current, {len(finder['years_with_maps'])} prior year(s) verified)"
    with st.expander(title, expanded=bool(finder) or bool(st.session_state.get("finder_error"))):
        if not have_key:
            st.caption("Add a Tavily API key above (or TAVILY_API_KEY in secrets) to search for a show's "
                       "directory by name. Every hit is content-verified before it is offered.")
        # Seed from the editable show name once it is known; the user can still overtype it.
        if not st.session_state.get("finder_show") and st.session_state.get("show_base"):
            st.session_state["finder_show"] = st.session_state["show_base"]
        show = st.text_input("Show to find", key="finder_show", placeholder="e.g. NAB Show, SEMA Show, CES",
                             disabled=not have_key)
        back = st.number_input("Prior editions to probe", min_value=0, max_value=show_finder.MAX_YEARS_BACK,
                               value=show_finder.DEFAULT_YEARS_BACK, step=1, disabled=not have_key,
                               help="Each probed year costs one search + one extract call. Answers 'how many "
                                    "years of maps exist' from what search can still find AND verify.")
        if st.button("Search & verify", key="finder_run", disabled=not have_key, width="stretch"):
            # Prior years are counted back from the show year in play: the sidebar override, else the
            # year THIS extraction observed, and only with neither does today's calendar year stand in.
            anchor = st.session_state.get("show_year") or (st.session_state.get("meta") or {}).get("show_year")
            years = show_finder.years_to_probe(anchor, int(back))
            with st.spinner("Searching and verifying page content..."):
                run_show_finder((show or "").strip(), years)
        if st.session_state.get("finder_error"):
            st.error(st.session_state["finder_error"])
        finder = st.session_state.get("finder")
        if not finder:
            return
        st.markdown(f"**{finder['show']}: current edition**")
        if finder["current"]:
            render_finder_hits(finder["current"], key="finder_cur", action_label="Use as target URL",
                               on_pick=use_found_url)
        else:
            st.caption("No search result passed content verification for the current edition.")
        if finder["years_probed"]:
            st.markdown(show_finder.summarize_years(finder))
            for y in finder["years_probed"]:
                hits = finder["by_year"].get(y) or []
                if hits:
                    st.markdown(f"**{y}**")
                    render_finder_hits(hits, key=f"finder_y{y}", action_label=f"Send to a {y} timeline slot",
                                       on_pick=send_found_to_slot, extra_args=(y,))
        with st.expander("Finder log"):
            for line in finder.get("log") or []:
                st.write(line)


# =============================================================================
# Extraction
# =============================================================================

def set_current_show(df: pd.DataFrame, meta: dict, raw: dict | None, log: list[str]) -> dict:
    """Make `df` the current show everywhere. Returns its quality report."""
    df = df.copy()
    for c in ROW_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    df["sqft"] = pd.to_numeric(df["sqft"], errors="coerce").fillna(0).astype(int)
    meta.setdefault("extracted_at", datetime.now().strftime("%b %d, %Y %I:%M %p"))
    meta["extracted_at"] = meta["extracted_at"] or datetime.now().strftime("%b %d, %Y %I:%M %p")
    meta.setdefault("total", len(df))
    meta.setdefault("sized", int((df["sqft"] > 0).sum()))
    q = quality.check(df.to_dict("records"), meta)
    st.session_state["exhibitors"] = df
    st.session_state["meta"] = meta
    st.session_state["quality"] = q
    st.session_state["raw"] = raw
    st.session_state["extract_log"] = log
    st.session_state["show_base"] = meta.get("show_base") or ""
    st.session_state["show_year"] = meta.get("show_year")
    st.session_state["use_anyway"] = False
    reset_apollo()
    return q


def run_extraction(url: str) -> None:
    log: list[str] = []
    with st.status("Extracting floor plan...", expanded=True) as status:
        def _log(msg: str) -> None:
            log.append(msg)
            st.write(msg)

        if not url:
            _log("No URL entered.")
            status.update(label="No URL", state="error")
            st.session_state["extract_log"] = log
            return
        if not url.startswith("http"):
            url = "https://" + url
        try:
            rows, meta, raw = platforms.extract(url, log=_log)
        except (ExtractionError, scraper.ScrapeError) as exc:
            _log(f"Extraction failed: {exc}")
            status.update(label="Extraction failed (nothing loaded)", state="error", expanded=True)
            st.session_state["extract_log"] = log
            return
        except Exception as exc:  # never crash the page on an unexpected shape
            _log(f"Unexpected error during extraction: {exc.__class__.__name__}: {exc}")
            status.update(label="Extraction failed (nothing loaded)", state="error", expanded=True)
            st.session_state["extract_log"] = log
            return
        q = set_current_show(pd.DataFrame(rows, columns=ROW_COLUMNS), meta, raw, log)
        status.update(label=f"Extracted {len(rows)} companies from {meta.get('show_name')} "
                            f"({meta.get('platform')}) -- quality {q['grade']}",
                      state="complete" if q["grade"] != "FAIL" else "error", expanded=False)
    st.rerun()


def load_show_csvs(files) -> None:
    """Newest year -> current show; every older year -> a timeline slot (replacing a slot of the same year)."""
    errors, loaded = [], []
    for f in files or []:
        try:
            df, meta = showfile.load(f)
        except showfile.ShowFileError as exc:
            errors.append(str(exc))
            continue
        if not meta.get("show_year"):
            errors.append(f"{meta['filename']}: no show year in the file or its name. Rename it with the year "
                          f"(e.g. kbis_2025.csv) and load again.")
            continue
        loaded.append((df, meta))
    st.session_state["csv_errors"] = errors
    if not loaded:
        return
    loaded.sort(key=lambda t: t[1]["show_year"])
    cur_df, cur_meta = loaded[-1]
    set_current_show(cur_df, cur_meta, None, [f"Loaded {cur_meta['filename']} ({len(cur_df)} companies)"])
    slots = [s_ for s_ in st.session_state["timeline_slots"]
             if s_.get("year") not in {m["show_year"] for _, m in loaded}]
    for df, meta in loaded[:-1]:
        if meta["show_year"] == cur_meta["show_year"]:
            errors.append(f"{meta['filename']}: same year as {cur_meta['filename']}; skipped.")
            continue
        q = quality.check(df.to_dict("records"), meta)
        if q["grade"] == "FAIL":
            errors.append(f"{meta['filename']}: quality FAIL ({q['notes']}); not added to the timeline.")
            continue
        st.session_state["_slot_seq"] += 1
        sid = st.session_state["_slot_seq"]
        slots.append({"id": sid, "year": meta["show_year"], "df": df[["exhibitor_name", "sqft"]],
                      "label": meta["filename"], "error": "", "quality": q["grade"]})
        st.session_state[f"slot_year_{sid}"] = meta["show_year"]
        st.session_state[f"slot_mode_{sid}"] = SLOT_MODE_CSV
    st.session_state["timeline_slots"] = sorted(slots, key=lambda s_: s_.get("year") or 0)[-MAX_TIMELINE_SLOTS:]
    st.session_state["csv_errors"] = errors


# =============================================================================
# Pipeline assembly (pure, re-run every rerun from session state)
# =============================================================================

def build_targets(params: dict) -> pd.DataFrame:
    """Filtered islands with delta, Apollo org, primary contact, freight tag, and pitch columns."""
    df: pd.DataFrame = st.session_state["exhibitors"]
    targets = df[(df["sqft"] >= params["min_sqft"]) & (df["sqft"] <= params["max_sqft"])].copy()

    # Year-over-year delta + multi-year trajectory (Task 2), fed by the timeline slots with the
    # live extraction always the newest one. With zero or one slot loaded every company falls
    # back to NO PRIOR DATA, identical to the old single-upload behaviour.
    traj = delta_engine.build_trajectories(timeline_slots_for_trajectories())
    targets["name_key"] = targets["exhibitor_name"].map(delta_engine.name_key)
    traj_cols = ["name_key", "prior_sqft", "delta_sqft", "trajectory", "yoy_status", "newborn_island",
                "peak_sqft", "peak_year"]
    if not traj.empty:
        targets = targets.merge(traj[traj_cols], on="name_key", how="left")
    else:
        for c in traj_cols[1:]:
            targets[c] = pd.NA
    targets = targets.drop(columns=["name_key"])
    # A target absent from every timeline slot (shouldn't happen -- the live extraction IS a
    # slot -- but stay defensive) reads the same as "no prior data" rather than blank/NaN.
    targets["yoy_status"] = targets["yoy_status"].fillna(delta_engine.STATUS_NONE)
    targets["trajectory"] = targets["trajectory"].fillna(delta_engine.STATUS_NONE)
    targets["newborn_island"] = targets["newborn_island"].fillna(False).astype(bool)

    # Apollo organisation + primary contact
    orgs: dict = st.session_state["orgs"]
    people: dict = st.session_state["people"]
    targets["domain"] = targets["website"].map(apollo.domain_from_website)

    org_cols = {"employees": [], "hq_city": [], "hq_state": [], "hq_country": [], "hq_source": [], "industry": [],
                "revenue_usd": [], "apollo_source": [], "apollo_status": []}
    contact_cols = {"first_name": [], "last_name": [], "contact_name": [], "contact_title": [], "email": [],
                    "linkedin": [], "months_in_role": [], "new_hire": [], "contacts_found": []}
    for _, row in targets.iterrows():
        org = orgs.get(row["domain"])
        if org is None:
            status = "not enriched" if row["domain"] else "no website in directory"
            org = {"employees": None, "hq_city": "", "hq_state": "", "hq_country": "", "industry": "",
                   "revenue_usd": None, "source": "", "error": ""}
        else:
            status = "enriched" if org.get("found") else f"no data ({org.get('error') or 'no match'})"
        org_cols["employees"].append(org.get("employees"))
        # HQ: Apollo when it has one, else what the exhibitor listed in the show directory
        # (MapYourShow / A2Z / EXPOCAD detail data). Never guessed.
        use_dir = not (org.get("hq_state") or org.get("hq_country"))
        org_cols["hq_city"].append(org.get("hq_city", "") if not use_dir else (row.get("city") or ""))
        org_cols["hq_state"].append(org.get("hq_state", "") if not use_dir else (row.get("state") or ""))
        org_cols["hq_country"].append(org.get("hq_country", "") if not use_dir else (row.get("country") or ""))
        org_cols["hq_source"].append("Apollo" if not use_dir else ("directory" if (row.get("state") or row.get("country")) else ""))
        org_cols["industry"].append(org.get("industry", ""))
        org_cols["revenue_usd"].append(org.get("revenue_usd"))
        org_cols["apollo_source"].append(org.get("source", "") if org.get("found") else "")
        org_cols["apollo_status"].append(status)

        plist = people.get(row["domain"]) or []
        primary = None
        if plist:
            new_hires = [p for p in plist if p.get("new_hire")]
            primary = min(new_hires, key=lambda p: p["months_in_role"]) if new_hires else plist[0]
        contact_cols["first_name"].append(primary["first_name"] if primary else "")
        contact_cols["last_name"].append(primary["last_name"] if primary else "")
        contact_cols["contact_name"].append(primary["name"] if primary else "")
        contact_cols["contact_title"].append(primary["title"] if primary else "")
        contact_cols["email"].append(primary["email"] if primary else "")
        contact_cols["linkedin"].append(primary["linkedin"] if primary else "")
        contact_cols["months_in_role"].append(primary["months_in_role"] if primary else None)
        contact_cols["new_hire"].append(bool(primary and primary["new_hire"]))
        contact_cols["contacts_found"].append(len(plist))
    for k, v in {**org_cols, **contact_cols}.items():
        targets[k] = v

    targets["employees"] = pd.to_numeric(targets["employees"], errors="coerce")
    targets["oversized"] = targets["employees"].fillna(0) > apollo.MAX_EMPLOYEES
    targets["hq_state"] = [apollo.normalise_state(s, c) if s else s for s, c in zip(targets["hq_state"], targets["hq_country"])]
    targets["region"] = [freight.region_of(s, c) for s, c in zip(targets["hq_state"], targets["hq_country"])]
    targets["freight_tag"] = [freight.freight_tag(s, c) for s, c in zip(targets["hq_state"], targets["hq_country"])]

    targets = pg.assign_pitches(targets, current_show_label())
    return targets


def contact_level(targets: pd.DataFrame, show_name: str) -> pd.DataFrame:
    """One row per Apollo contact (companies without contacts keep one row with blank contact fields)."""
    people: dict = st.session_state["people"]
    records = []
    for _, row in targets.iterrows():
        base = row.to_dict()
        plist = people.get(row["domain"]) or []
        if not plist:
            records.append(base)
            continue
        for p in plist:
            rec = {**base, "first_name": p["first_name"], "last_name": p["last_name"], "contact_name": p["name"],
                   "contact_title": p["title"], "email": p["email"], "linkedin": p["linkedin"],
                   "months_in_role": p["months_in_role"], "new_hire": bool(p["new_hire"])}
            records.append(rec)
    out = pd.DataFrame(records)
    if out.empty:
        return out
    return pg.assign_pitches(out.drop(columns=[c for c in ("trigger", "trigger_badge", "pitch_angle",
                                                             "custom_intro_line", "priority", "est_value")
                                                if c in out.columns]), show_name)


# =============================================================================
# Shared UI pieces
# =============================================================================

def source_banner(meta: dict) -> None:
    platform_label = {"mapyourshow": "MapYourShow", "a2z": "A2Z", "expocad": "EXPOCAD",
                      "expofp": "ExpoFP", "csv": "CSV"}.get(meta.get("platform"), meta.get("platform") or "?")
    q = st.session_state.get("quality") or {"grade": "PASS", "findings": [], "notes": ""}
    how = "Live" if meta.get("source") == "live" else "File"
    pills = f"<span class='ax-pill ax-live'>{how} &middot; {platform_label}</span>"
    pills += f"<span class='ax-pill ax-info'>{current_show_label()}</span>"
    pills += f"<span class='ax-pill {GRADE_PILL[q['grade']]}'>Quality {q['grade']}</span>"
    if meta.get("halls"):
        pills += f"<span class='ax-pill ax-info'>{meta['halls']} halls</span>"
    st.markdown(pills, unsafe_allow_html=True)
    note = f"{meta.get('total', 0)} companies, {meta.get('sized', 0)} with a booth size"
    if meta.get("filename"):
        note += f", from {meta['filename']}"
    if meta.get("extracted_at"):
        note += f", extracted {meta['extracted_at']}"
    st.markdown(f"<div class='ax-muted'>{md(note)}</div>", unsafe_allow_html=True)
    render_quality(q)


def render_quality(q: dict, key: str = "main") -> None:
    icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "⛔"}
    with st.expander(f"Extraction quality: {q['grade']}", expanded=q["grade"] == "FAIL"):
        st.dataframe(pd.DataFrame([{"": icon[f["level"]], "Check": f["check"], "Result": f["note"]}
                                   for f in q.get("findings", [])]),
                     hide_index=True, width="stretch", key=f"quality_{key}")
        if q["grade"] == "FAIL":
            if q.get("demo"):
                st.error("Generated/demo rows found. This data can't be exported or compared, with no override.")
            elif key == "main":
                st.warning("Exports are blocked on FAIL. Tick below only if you've checked the floor plan yourself; "
                           "every exported row is then stamped FAIL-OVERRIDDEN.")
                st.checkbox("Use anyway", key="use_anyway")


def export_gate() -> tuple[bool, str, str]:
    """(allowed, quality stamp, notes) for any CSV export of the current show."""
    q = st.session_state.get("quality") or {"grade": "PASS", "notes": "", "demo": False}
    if q["grade"] == "FAIL":
        if q.get("demo") or not st.session_state.get("use_anyway"):
            return False, "FAIL", q["notes"]
        return True, "FAIL-OVERRIDDEN", q["notes"]
    return True, q["grade"], q["notes"]


def metric_row(all_df: pd.DataFrame, targets: pd.DataFrame) -> None:
    live = targets[~targets["oversized"]]
    high = int(live["trigger"].isin(pg.HIGH_PRIORITY).sum()) if not live.empty else 0
    pipeline = float(live["sqft"].sum()) * pg.PRICE_PER_SQFT
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Exhibitors", f"{len(all_df):,}")
    k2.metric("Target Islands", f"{len(live):,}",
              delta=f"{int(targets['oversized'].sum())} dropped (>1,000 employees)" if targets["oversized"].any() else None,
              delta_color="off")
    k3.metric("High-Priority Triggers", f"{high:,}", help="Targets carrying trigger A, B or C.")
    k4.metric("Estimated Pipeline Value", fmt_money(pipeline),
              help=md(f"Total target sq ft x ${pg.PRICE_PER_SQFT}/sq ft. A planning figure, not a forecast."))


def badge_legend() -> None:
    cols = st.columns(len(pg.TRIGGERS))
    for col, (code, t) in zip(cols, pg.TRIGGERS.items()):
        with col:
            st.markdown(f"<span class='ax-badge' style='background:{t['hex']}'>{code} &middot; {t['badge']}</span>",
                        unsafe_allow_html=True)
            st.caption(t["pitch_angle"])


def style_triggers(df: pd.DataFrame, extra_cols: dict | None = None):
    """Styler that paints the trigger badge cell (and optional other cells) with the trigger colour."""
    hex_by_badge = {t["badge"]: t["hex"] for t in pg.TRIGGERS.values()}

    def _paint(col: pd.Series):
        return [f"background-color: {hex_by_badge.get(v, '#64748b')}; color: white; font-weight: 700"
                for v in col]

    styler = df.style
    if "trigger_badge" in df.columns:
        styler = styler.apply(_paint, subset=["trigger_badge"])
    for col, mapping in (extra_cols or {}).items():
        if col in df.columns:
            styler = styler.apply(lambda s, m=mapping: [m.get(v, "") for v in s], subset=[col])
    return styler


def sqft_column(max_sqft: int):
    return st.column_config.ProgressColumn("Sq ft", min_value=0, max_value=max(max_sqft, 1), format="%d")


BASE_COLUMN_CONFIG = {
    "exhibitor_name": st.column_config.TextColumn("Exhibitor", width="medium"),
    "booth_number": st.column_config.TextColumn("Booth", width="small"),
    "width": st.column_config.NumberColumn("W (ft)", width="small", format="%g"),
    "length": st.column_config.NumberColumn("L (ft)", width="small", format="%g"),
    "hall": st.column_config.TextColumn("Hall", width="small"),
    "website": st.column_config.LinkColumn("Website", display_text=r"https?://(?:www\.)?([^/]+)", width="medium"),
    "is_sponsor": st.column_config.CheckboxColumn("Sponsor", width="small"),
    "has_video_listing": st.column_config.CheckboxColumn("Video", width="small"),
    "detail_url": st.column_config.LinkColumn("Listing", display_text="open", width="small"),
    "shared_booth": st.column_config.CheckboxColumn("Shared", width="small",
                                                    help="On a shared/pavilion booth: sq ft is this company's share."),
    "booth_sqft": st.column_config.NumberColumn("Whole booth", format="%d", width="small"),
    "city": st.column_config.TextColumn("City", width="small"),
    "state": st.column_config.TextColumn("State", width="small"),
    "platform": st.column_config.TextColumn("Platform", width="small"),
    "raw_size": st.column_config.TextColumn("Size as listed", width="small"),
    "employees": st.column_config.NumberColumn("Employees", format="%d", width="small"),
    "hq_state": st.column_config.TextColumn("HQ state", width="small"),
    "hq_country": st.column_config.TextColumn("HQ country", width="small"),
    "linkedin": st.column_config.LinkColumn("LinkedIn", display_text="profile", width="small"),
    "months_in_role": st.column_config.NumberColumn("Months in role", format="%d", width="small"),
    "new_hire": st.column_config.CheckboxColumn("New hire", width="small"),
    "prior_sqft": st.column_config.NumberColumn("Prior sq ft", format="%d", width="small"),
    "delta_sqft": st.column_config.NumberColumn("Delta sq ft", format="%+d", width="small"),
    "est_value": st.column_config.NumberColumn("Est. value", format="$%d", width="small"),
    "trigger_badge": st.column_config.TextColumn("Trigger", width="medium"),
    "apollo_source": st.column_config.TextColumn("Source", width="small"),
    "size_source": st.column_config.TextColumn("Size source", width="small",
                                               help="Where the size came from: floorplan (MapYourShow geometry), a2z-map, "
                                                    "expocad-fx, expofp-svg (booth shape on the map); unknown = the "
                                                    "platform publishes no size for this company (0 sq ft)."),
}


def show_table(df: pd.DataFrame, cols: list[str], max_sqft: int, key: str, styled=None, height: int | None = None):
    cfg = {c: BASE_COLUMN_CONFIG[c] for c in cols if c in BASE_COLUMN_CONFIG}
    cfg["sqft"] = sqft_column(max_sqft)
    data = styled if styled is not None else df[cols]
    st.dataframe(data, column_config=cfg, hide_index=True, width="stretch", key=key,
                 height=height or min(60 + 35 * max(len(df), 1), 560))


# =============================================================================
# Tabs
# =============================================================================

def tab_scanner(params: dict, all_df: pd.DataFrame, targets: pd.DataFrame, meta: dict) -> None:
    source_banner(meta)
    metric_row(all_df, targets)
    st.markdown(f"#### Target islands ({params['min_sqft']:,} to {params['max_sqft']:,} sq ft)")
    st.caption("Every column is an observed floor-plan fact. Shared/pavilion booths count only this company's "
               "share of the stand. Rows dropped for >1,000 employees (Apollo) are listed separately below.")
    live = targets[~targets["oversized"]]
    cols = ["exhibitor_name", "booth_number", "raw_size", "sqft", "shared_booth", "hall", "city", "state", "website",
            "is_sponsor", "has_video_listing", "detail_url"]
    if live.empty:
        st.info("No exhibitors in the current sq-ft range. Widen the filters in the sidebar.")
    else:
        show_table(live, cols, params["max_sqft"], key="grid_targets")
    if targets["oversized"].any():
        with st.expander(f"Dropped: {int(targets['oversized'].sum())} exhibitors over {apollo.MAX_EMPLOYEES:,} employees"):
            show_table(targets[targets["oversized"]], cols[:6] + ["employees", "apollo_source"], params["max_sqft"],
                       key="grid_oversized")

    with st.expander(f"Full directory ({len(all_df):,} exhibitors, all sizes)"):
        show_table(all_df, cols + ["booth_sqft", "size_source"], int(all_df["sqft"].max() or 1), key="grid_all", height=420)
        with_size = int((all_df["sqft"] > 0).sum())
        st.caption(f"{with_size:,} of {len(all_df):,} exhibitors have a booth size. "
                   f"Exhibitors without one show 0 sq ft and never enter the target list.")
    ok, stamp, notes = export_gate()
    out = all_df.copy()
    out["show_name"], out["show_year"] = (st.session_state.get("show_base") or meta.get("show_base") or ""), \
        (st.session_state.get("show_year") or meta.get("show_year") or "")
    out["quality"], out["quality_notes"] = stamp, notes
    st.download_button("Download full extraction CSV (keep it: it's next year's comparison file)",
                       data=out.to_csv(index=False).encode("utf-8"), disabled=not ok,
                       file_name=f"{show_file_stub()}_exhibitors.csv", mime="text/csv")
    if not ok:
        st.caption("Download blocked: extraction quality FAIL (see the quality panel above).")


TRAJ_STYLES = {
    delta_engine.TRAJ_NEWBORN: "background-color: #ef4444; color: white; font-weight: 700",
    delta_engine.TRAJ_PEAK_RETREAT: f"background-color: {pg.TRIGGERS[pg.TRIGGER_E]['hex']}; color: white; font-weight: 700",
    delta_engine.TRAJ_STEADY_GROWTH: "background-color: rgba(34,197,94,.3); font-weight: 600",
    delta_engine.TRAJ_SHRINKING: "background-color: rgba(100,116,139,.3)",
    delta_engine.TRAJ_VOLATILE: "background-color: rgba(139,92,246,.28); font-weight: 600",
    delta_engine.TRAJ_UPGRADE: "background-color: rgba(34,197,94,.18)",
    delta_engine.TRAJ_DOWNSIZE: "background-color: rgba(100,116,139,.18)",
    delta_engine.TRAJ_NEW: "background-color: rgba(59,130,246,.2)",
    delta_engine.TRAJ_DROPPED: "background-color: rgba(100,116,139,.35); font-weight: 600",
}


def render_timeline_slot(slot: dict) -> None:
    sid = slot["id"]
    with st.container(border=True):
        c1, c2, c3 = st.columns([1, 2, 1])
        # A default `value` only when nothing (e.g. the finder's send-to-slot) has seeded the key.
        year_kw = {} if f"slot_year_{sid}" in st.session_state else {"value": slot["year"] or 2025}
        c1.number_input("Year", min_value=2015, max_value=2035, step=1, key=f"slot_year_{sid}", **year_kw)
        mode = c2.radio("Source", [SLOT_MODE_CSV, SLOT_MODE_URL], key=f"slot_mode_{sid}", horizontal=True)
        c3.markdown("&nbsp;", unsafe_allow_html=True)   # align the Remove button with the row above
        c3.button("Remove slot", key=f"slot_remove_{sid}", on_click=remove_timeline_slot, args=(sid,),
                  width="stretch")
        if mode == SLOT_MODE_CSV:
            st.file_uploader("Prior-year CSV (this app's export, or any file with a name + sq-ft column)",
                             type=["csv"], key=f"slot_csv_{sid}")
        else:
            st.text_input("Directory URL for that year's show (MapYourShow, A2Z, EXPOCAD, ExpoFP)",
                          key=f"slot_url_{sid}",
                          placeholder="https://nab26.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm")
            render_slot_finder(slot)
        lc1, lc2 = st.columns([1, 3])
        lc1.button("Load", key=f"slot_load_{sid}", type="primary", on_click=load_timeline_slot, args=(sid,))
        if slot.get("error"):
            lc2.error(slot["error"])
            if slot["error"].startswith("Quality FAIL") and "demo" not in slot["error"]:
                lc2.checkbox("Use anyway (I've checked this floor plan)", key=f"slot_force_{sid}")
        elif slot.get("df") is not None:
            lc2.success(f"{slot.get('label') or 'Loaded'}: {len(slot['df'])} companies, year {slot['year']}"
                        + (f", quality {slot['quality']}" if slot.get("quality") else ""))
        else:
            lc2.caption("Not loaded yet.")


def render_slot_finder(slot: dict) -> None:
    """Inside a URL-mode timeline slot: offer already-verified URLs for this slot's year, or probe it."""
    sid = slot["id"]
    year = st.session_state.get(f"slot_year_{sid}") or slot.get("year")
    finder = st.session_state.get("finder") or {}
    hits = (finder.get("by_year") or {}).get(year) or []
    if hits:
        st.caption(f"Verified by the finder for {year}:")
        render_finder_hits(hits, key=f"slot_finder_{sid}", action_label=f"Use this URL for {year}",
                           on_pick=send_found_to_slot, extra_args=(year,))
        return
    show = (st.session_state.get("finder_show") or st.session_state.get("show_base") or "").strip()
    if not st.session_state.get("tavily_key"):
        st.caption("Tip: add a Tavily API key in the sidebar and the engine can search for this year's "
                   "directory URL for you (content-verified before it is offered).")
        return
    if not show:
        st.caption("Set the show name in the sidebar to search for this year's directory.")
        return
    if year in (finder.get("years_probed") or []):
        st.caption(f"The finder probed {year} for {finder.get('show')} and found no verifiable directory. "
                   f"Use your own saved CSV/URL for that year.")
    if st.button(f"Find the {year} directory for {show}", key=f"slot_find_{sid}"):
        with st.spinner(f"Searching for {show} {year} and verifying page content..."):
            run_show_finder_year(show, int(year))
        st.rerun()


def tab_timeline(params: dict, targets: pd.DataFrame) -> None:
    st.markdown("#### Multi-year show timeline")
    st.caption(md(
        "Fastest: drop several years of Map Grabber CSVs in the sidebar and they land here automatically. "
        "Or add prior years one by one as a CSV (Map Grabber file, this app's export, or any file with a name and "
        "sq-ft column) or a "
        "directory URL for that year's show (any supported platform; with a Tavily key the engine can search "
        "for and verify prior-year URLs for you) -- up to 6 years total. With three or more years on "
        "file the engine sees the SHAPE of a company's booth history, not just one delta: a booth that grew "
        "to a peak and pulled back (small -> large -> medium) is flagged PEAK RETREAT, likely shopping for a "
        "new exhibit partner to make a splash again. A jump from under 200 to 400+ sq ft is still flagged "
        "CRITICAL: NEWBORN ISLAND, the strongest single signal in the engine."
    ))
    slots = st.session_state["timeline_slots"]
    for slot in slots:
        render_timeline_slot(slot)
    st.button(f"+ Add prior year ({len(slots)}/{MAX_TIMELINE_SLOTS})", on_click=add_timeline_slot,
             disabled=len(slots) >= MAX_TIMELINE_SLOTS)

    all_slots = timeline_slots_for_trajectories()
    if len(all_slots) <= 1:
        st.info("Add at least one prior year above (and load it) to unlock trajectory flags like "
               "NEWBORN ISLAND and PEAK RETREAT.")
        return

    traj = delta_engine.build_trajectories(all_slots)
    cur = traj[traj["is_current_year"]]
    s = delta_engine.trajectory_summary(traj)
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Newborn islands", s.get("newborn", 0))
    m2.metric("Peak retreats", s.get("peak_retreat", 0))
    m3.metric("Steady growth", s.get("steady_growth", 0))
    m4.metric("Shrinking", s.get("shrinking", 0))
    m5.metric("New to show", s.get("new", 0))
    m6.metric("Dropped out", s.get("dropped_out", 0), help="On the floor last edition, not this one.")

    years = sorted(int(y["year"]) for y in all_slots)
    year_cols = [f"sqft_{y}" for y in years]
    cols = ["exhibitor_name"] + year_cols + ["peak_sqft", "peak_year", "current_sqft", "delta_sqft", "trajectory"]
    target_names = set(targets["exhibitor_name"]) if not targets.empty else set()
    render_dropped_out(traj, years, params)
    view = cur[cur["exhibitor_name"].isin(target_names)].copy() if target_names else cur.iloc[0:0].copy()
    if view.empty:
        st.info("No target islands (within the sidebar's sq-ft filter) have timeline data yet.")
        return
    view = view.sort_values(["trajectory", "current_sqft"], ascending=[True, False])

    # Streamlit 1.63's column_config.NumberColumn renders a missing value as the literal
    # text "None" -- confirmed to happen for a NaN float, a pandas-nullable Int64 pd.NA, and
    # even a blank string coerced through NumberColumn's own numeric parsing; the Styler's
    # na_rep is bypassed entirely once a column has a NumberColumn. A company absent from a
    # given year is the COMMON case here (most exhibitors don't appear in every slot), so
    # that literal "None" would be the single most visible thing on this tab. The only
    # rendering path that shows a true blank is a pre-formatted string column under
    # TextColumn, so year sq-ft and the prior-year delta are formatted here by hand; peak/
    # current sq ft and peak year are never NaN for a current-year row and keep NumberColumn.
    disp = view[cols].copy()
    for yc in year_cols:
        disp[yc] = disp[yc].map(lambda v: "" if pd.isna(v) else f"{v:,.0f}")
    disp["delta_sqft"] = disp["delta_sqft"].map(lambda v: "" if pd.isna(v) else f"{v:+,.0f}")

    cfg = {
        "exhibitor_name": st.column_config.TextColumn("Exhibitor", width="medium"),
        "peak_sqft": st.column_config.NumberColumn("Peak sq ft", format="%d", width="small"),
        "peak_year": st.column_config.NumberColumn("Peak year", format="%d", width="small"),
        "current_sqft": st.column_config.NumberColumn("Current sq ft", format="%d", width="small"),
        "delta_sqft": st.column_config.TextColumn("Delta vs prior", width="small"),
        "trajectory": st.column_config.TextColumn("Trajectory", width="medium"),
    }
    fmt_map = {c: "{:,.0f}" for c in ["peak_sqft", "current_sqft"]}
    fmt_map["peak_year"] = "{:.0f}"
    for yc in year_cols:
        cfg[yc] = st.column_config.TextColumn(yc.replace("sqft_", ""), width="small")
    styled = disp.style.apply(lambda ser: [TRAJ_STYLES.get(v, "") for v in ser], subset=["trajectory"]) \
        .format(fmt_map, na_rep="")
    st.dataframe(styled, column_config=cfg, hide_index=True, width="stretch", key="grid_timeline",
                height=min(60 + 35 * max(len(view), 1), 560))


def render_dropped_out(traj: pd.DataFrame, years: list[int], params: dict) -> None:
    gone = traj[(traj["trajectory"] == delta_engine.TRAJ_DROPPED)].copy()
    if gone.empty:
        return
    prev = years[-2]
    gone["last_sqft"] = pd.to_numeric(gone[f"sqft_{prev}"], errors="coerce").fillna(0)
    big = gone[gone["last_sqft"] >= params["min_sqft"]].sort_values("last_sqft", ascending=False)
    with st.expander(f"Dropped out: {len(gone)} companies exhibited in {prev} but not this year "
                     f"({len(big)} had {params['min_sqft']:,}+ sq ft)"):
        st.caption("Former islands that skipped this edition: they may be re-thinking their show plan or their "
                   "exhibit partner. Check whether they are booked at a competing show.")
        st.dataframe(big[["exhibitor_name", "last_sqft", "peak_sqft", "peak_year", "first_year"]],
                     column_config={"exhibitor_name": "Exhibitor",
                                    "last_sqft": st.column_config.NumberColumn(f"{prev} sq ft", format="%d"),
                                    "peak_sqft": st.column_config.NumberColumn("Peak sq ft", format="%d"),
                                    "peak_year": st.column_config.NumberColumn("Peak year", format="%d"),
                                    "first_year": st.column_config.NumberColumn("First seen", format="%d")},
                     hide_index=True, width="stretch", key="grid_dropped")
        ok, stamp, _ = export_gate()
        st.download_button("Download dropped-out list", data=big.assign(quality=stamp).to_csv(index=False).encode("utf-8"),
                           file_name=f"{show_file_stub()}_dropped_out.csv", mime="text/csv", disabled=not ok)


def tab_freight(params: dict, targets: pd.DataFrame) -> None:
    st.markdown("#### Home-field freight arbitrage")
    st.caption("HQ comes from Apollo (Tab 4) or, when Apollo has none, from the exhibitor's own show-directory "
               "listing (MapYourShow / A2Z / EXPOCAD). Exhibitors headquartered outside "
               "Nevada and the West Coast pay cross-country freight, round-trip drayage and out-of-town I&D "
               "for every Las Vegas show. Nevada HQs get the local storage / asset-takeover angle instead.")
    live = targets[~targets["oversized"]]
    known = live[live["region"] != "Unknown"]
    if known.empty:
        st.info("No HQ locations yet: the directory didn't list them and Apollo hasn't run (Tab 4). "
                "HQ location is never guessed.")
        return
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("HQ known", f"{len(known)} / {len(live)}")
    m2.metric("High freight savings", int(live["freight_tag"].map(freight.is_high_freight).sum()))
    m3.metric("Vegas local", int(live["freight_tag"].map(freight.is_vegas_local).sum()))
    m4.metric("West Coast / Mountain", int(live["region"].isin(["West Coast", "Mountain West"]).sum()))

    regions = sorted(live["region"].unique())
    pick = st.multiselect("Regions", regions, default=[r for r in regions if r != "Unknown"] or regions)
    view = live[live["region"].isin(pick)].sort_values(["priority", "sqft"], ascending=[True, False])
    cols = ["exhibitor_name", "sqft", "hq_city", "hq_state", "hq_country", "hq_source", "region", "freight_tag", "website"]
    tag_styles = {
        freight.TAG_HIGH: "background-color: #3b82f6; color: white; font-weight: 700",
        freight.TAG_HIGH_INTL: "background-color: #2563eb; color: white; font-weight: 700",
        freight.TAG_LOCAL: "background-color: #22c55e; color: white; font-weight: 700",
        freight.TAG_MOUNTAIN: "background-color: rgba(245,158,11,.3)",
    }
    styled = view[cols].style.apply(lambda s_: [tag_styles.get(v, "") for v in s_], subset=["freight_tag"])
    show_table(view, cols, params["max_sqft"], key="grid_freight", styled=styled)


def run_apollo(params: dict, targets: pd.DataFrame) -> None:
    live = targets[~targets["oversized"]]
    todo = live[(live["domain"] != "") & ~live["domain"].isin(st.session_state["orgs"].keys())]
    todo = todo.sort_values(["priority", "sqft"], ascending=[True, False])
    budget = params["budget"]
    log: list[str] = []
    done = 0
    for _, row in todo.iterrows():
        if st.session_state["credits_used"] >= budget:
            log.append(f"Budget of {budget} org lookups reached; remaining companies left unenriched.")
            break
        domain = row["domain"]
        org = apollo.get_organization(domain, params["api_key"])
        st.session_state["orgs"][domain] = org
        st.session_state["credits_used"] += 1
        if org.get("found") and org.get("employees") and org["employees"] > apollo.MAX_EMPLOYEES:
            st.session_state["people"][domain] = []
            log.append(f"{row['exhibitor_name']}: {org['employees']:,} employees, dropped")
        else:
            ppl = apollo.search_people(domain, params["api_key"])
            st.session_state["people"][domain] = ppl
            nh = sum(1 for p in ppl if p["new_hire"])
            log.append(f"{row['exhibitor_name']}: {org.get('employees') or '?'} employees, "
                       f"HQ {org.get('hq_state') or org.get('hq_country') or '?'}, {len(ppl)} contacts"
                       + (f", {nh} NEW HIRE" if nh else "")
                       + (f" [{org['error']}]" if org.get("error") else ""))
        done += 1
    st.session_state["apollo_log"] = [f"Apollo pass complete: {done} companies"] + log
    st.toast(f"Apollo pass complete: {done} companies")


def tab_apollo(params: dict, targets: pd.DataFrame) -> None:
    st.markdown("#### Apollo firmographics and buyer contacts")
    st.caption(md(f"Organisation enrichment (1 credit each) returns employee count, HQ and industry; companies over "
                  f"{apollo.MAX_EMPLOYEES:,} employees are dropped. People search (0 credits) finds "
                  f"{', '.join(apollo.TARGET_TITLES)}; under {apollo.NEW_HIRE_MONTHS} months in role = NEW HIRE TRIGGER. "
                  f"Emails are not revealed by search; the export leaves them blank for Instantly/Smartlead's finder."))
    live = targets[~targets["oversized"]]
    with_domain = int((live["domain"] != "").sum())
    pending = int(((live["domain"] != "") & ~live["domain"].isin(st.session_state["orgs"].keys())).sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Targets with a website", f"{with_domain} / {len(live)}")
    c2.metric("Enriched", len(st.session_state["orgs"]))
    c3.metric("Credits used this session", st.session_state["credits_used"])
    c4.metric("Key", "set" if params["api_key"] else "missing")
    if not params["api_key"]:
        st.info("Add your Apollo API key in the sidebar (or APOLLO_API_KEY in Streamlit secrets) to enrich. "
                "There is no mock mode: without a key this tab stays empty.")

    b1, b2, _ = st.columns([1.6, 1.2, 3])
    label = f"Run Apollo enrichment ({min(pending, params['budget'])} companies)"
    b1.button(label, type="primary", disabled=pending == 0 or not params["api_key"], width="stretch",
              on_click=on_run_apollo)
    b2.button("Clear Apollo results", width="stretch", disabled=not st.session_state["orgs"], on_click=reset_apollo)
    if with_domain < len(live):
        st.caption(f"{len(live) - with_domain} targets have no website in the directory and are skipped: "
                   "Apollo is keyed on the domain and the app never guesses one.")
    if st.session_state["apollo_log"]:
        with st.expander(st.session_state["apollo_log"][0], expanded=False):
            for line in st.session_state["apollo_log"][1:]:
                st.write(line)
    if not st.session_state["orgs"]:
        return

    st.markdown("##### Organisations")
    org_cols = ["exhibitor_name", "sqft", "domain", "employees", "industry", "hq_city", "hq_state", "hq_country",
                "apollo_status", "apollo_source"]
    show_table(targets.sort_values("sqft", ascending=False), org_cols, params["max_sqft"], key="grid_orgs")

    st.markdown("##### Contacts")
    people = st.session_state["people"]
    rows = []
    for _, r in live.iterrows():
        for p in people.get(r["domain"]) or []:
            rows.append({"exhibitor_name": r["exhibitor_name"], "sqft": r["sqft"], "contact_name": p["name"],
                         "contact_title": p["title"], "months_in_role": p["months_in_role"],
                         "new_hire": bool(p["new_hire"]), "email": p["email"] or "", "linkedin": p["linkedin"],
                         "apollo_source": p["source"]})
    if not rows:
        st.info("No contacts with the target titles were returned yet.")
        return
    cdf = pd.DataFrame(rows).sort_values(["new_hire", "months_in_role"], ascending=[False, True])
    n_new = int(cdf["new_hire"].sum())
    st.markdown(f"<span class='ax-badge' style='background:{pg.TRIGGERS['B']['hex']}'>NEW HIRE TRIGGER</span> "
                f"<span class='ax-muted'>{n_new} of {len(cdf)} contacts under {apollo.NEW_HIRE_MONTHS} months in role</span>",
                unsafe_allow_html=True)
    ccols = ["exhibitor_name", "sqft", "contact_name", "contact_title", "months_in_role", "new_hire", "email",
             "linkedin", "apollo_source"]
    styled = cdf[ccols].style.apply(
        lambda s_: [f"background-color: {pg.TRIGGERS['B']['hex']}; color: white; font-weight: 700" if v else ""
                    for v in s_], subset=["new_hire"])
    show_table(cdf, ccols, params["max_sqft"], key="grid_contacts", styled=styled)


def tab_pitch(params: dict, targets: pd.DataFrame, meta: dict) -> None:
    st.markdown("#### Pitch angles by trigger")
    badge_legend()
    live = targets[~targets["oversized"]]
    if live.empty:
        st.info("No target islands to pitch. Run an extraction first.")
        return
    counts = live["trigger_badge"].value_counts()
    m = st.columns(len(pg.TRIGGERS))
    for col, code in zip(m, pg.TRIGGERS):
        col.metric(pg.TRIGGERS[code]["badge"], int(counts.get(pg.TRIGGERS[code]["badge"], 0)))

    picks = st.multiselect("Show triggers", [t["badge"] for t in pg.TRIGGERS.values()],
                           default=[t["badge"] for t in pg.TRIGGERS.values()])
    view = live[live["trigger_badge"].isin(picks)]
    cols = ["trigger_badge", "exhibitor_name", "sqft", "contact_name", "contact_title", "hq_state",
            "yoy_status", "pitch_angle", "custom_intro_line", "est_value"]
    cfg_extra = {
        "contact_name": st.column_config.TextColumn("Contact", width="small"),
        "contact_title": st.column_config.TextColumn("Title", width="small"),
        "yoy_status": st.column_config.TextColumn("YoY", width="small"),
        "pitch_angle": st.column_config.TextColumn("Pitch angle", width="medium"),
        "custom_intro_line": st.column_config.TextColumn("Custom intro line", width="large"),
    }
    cfg = {c: BASE_COLUMN_CONFIG[c] for c in cols if c in BASE_COLUMN_CONFIG}
    cfg.update(cfg_extra)
    cfg["sqft"] = sqft_column(params["max_sqft"])
    st.dataframe(style_triggers(view[cols]), column_config=cfg, hide_index=True, width="stretch",
                 key="grid_pitch", height=min(60 + 35 * max(len(view), 1), 560))

    st.markdown("##### Preview one pitch")
    pick = st.selectbox("Company", list(view["exhibitor_name"]), index=0 if len(view) else None)
    if pick:
        row = view[view["exhibitor_name"] == pick].iloc[0]
        t = pg.TRIGGERS[row["trigger"]]
        st.markdown(f"<span class='ax-badge' style='background:{t['hex']}'>{row['trigger']} &middot; {t['badge']}</span>"
                    f"<span class='ax-muted'>{row['pitch_angle']}</span>", unsafe_allow_html=True)
        # key includes the content hash so the preview refreshes when enrichment changes the line
        st.text_area("Intro line", value=row["custom_intro_line"], height=120,
                     key=f"intro_{slug(pick)}_{abs(hash(row['custom_intro_line'])) % 10**8}")
        facts = [f"Booth {row['booth_number'] or 'n/a'}, {pg.dims_label(row['width'], row['length'], row['sqft'])} "
                 f"({int(row['sqft']):,} sq ft), {row['hall'] or 'hall n/a'}"]
        if row["contact_name"]:
            facts.append(f"Contact: {row['contact_name']}, {row['contact_title']}"
                         + (f" ({int(row['months_in_role'])} months in role)" if pd.notna(row["months_in_role"]) else ""))
        if row["hq_state"] or row["hq_country"]:
            facts.append(f"HQ: {', '.join(x for x in (row['hq_city'], row['hq_state'], row['hq_country']) if x)} "
                         f"-> {row['freight_tag']}")
        if row["yoy_status"] not in (delta_engine.STATUS_NONE,):
            facts.append(f"YoY: {row['yoy_status']}"
                         + (f" ({int(row['prior_sqft']):,} -> {int(row['sqft']):,} sq ft)" if pd.notna(row["prior_sqft"]) else ""))
        if row.get("trajectory") and row["trajectory"] not in (row["yoy_status"], delta_engine.STATUS_NONE):
            peak_txt = (f" (peak {int(row['peak_sqft']):,} sq ft in {int(row['peak_year'])})"
                       if pd.notna(row.get("peak_sqft")) and pd.notna(row.get("peak_year")) else "")
            facts.append(f"Multi-year trajectory: {row['trajectory']}{peak_txt}")
        if row["apollo_source"]:
            facts.append(f"Firmographics source: {row['apollo_source']}")
        st.markdown("\n".join(f"- {md(f)}" for f in facts))

    st.markdown("##### Export")
    export_df = pg.build_export(contact_level(view, current_show_label()), current_show_label())
    ok, stamp, notes = export_gate()
    export_df["quality"], export_df["quality_notes"] = stamp, notes
    n_with_email = int((export_df["email"] != "").sum()) if not export_df.empty else 0
    st.caption(f"{len(export_df)} rows ({n_with_email} with an email). Columns: {', '.join(pg.EXPORT_COLUMNS[:11])}. "
               "Import into Instantly or Smartlead and map custom_intro_line to a custom variable.")
    st.download_button("Download campaign CSV (Instantly / Smartlead)",
                       data=export_df.to_csv(index=False).encode("utf-8"),
                       file_name=f"{show_file_stub()}_campaign.csv", mime="text/csv", type="primary", disabled=not ok)
    if not ok:
        st.caption("Export blocked: extraction quality FAIL (see the quality panel on the first tab).")


# =============================================================================
# Main
# =============================================================================

GRABBER_PATH = Path(__file__).parent / "grabber" / "grabber.min.js"


def bookmarklet() -> str:
    try:
        return "javascript:" + quote(GRABBER_PATH.read_text(encoding="utf-8"), safe="")
    except OSError:
        return ""


def render_grabber_install() -> None:
    st.markdown("#### Map Grabber bookmark")
    st.markdown(md(
        "Open any floor plan in your own browser (MapYourShow, A2Z/Personify, EXPOCAD FX, ExpoFP), click the "
        "bookmark, and it downloads that show's exhibitor list as a CSV: company, booth, exact size, shared-booth "
        "flag, and (for booths 400+ sq ft) website and HQ city/state from the listing. It reads the same data the "
        "page itself loads, so it works on shows that block cloud servers. Nothing is sent anywhere."
    ))
    bm = bookmarklet()
    if not bm:
        st.error("grabber/grabber.min.js is missing from this deployment.")
        return
    embed = st.iframe if hasattr(st, "iframe") else (lambda html, height: components.html(html, height=height))
    embed(
        f"""<div style="font:14px -apple-system,Segoe UI,Roboto,sans-serif;color:#94a3b8">
        <a href="{bm.replace('"', '&quot;')}" onclick="return false"
           style="display:inline-block;padding:10px 16px;border-radius:8px;background:#1f6fb5;color:#fff;
                  font-weight:700;text-decoration:none;cursor:grab">&#x1F9ED; Altitude Map Grabber</a>
        <span style="margin-left:12px">&larr; drag this button onto your bookmarks bar</span></div>""",
        height=60)
    st.markdown(md(
        "**Use it**\n"
        "1. Open the show's floor plan or exhibitor list (e.g. `kbis.a2zinc.net/.../EventMap.aspx`, "
        "`imexamerica26.expofp.com`, an EXPOCAD `exfx.html` map, or a MapYourShow gallery).\n"
        "2. Wait for the map to finish loading, then click the bookmark. A panel shows progress, counts and a "
        "quality grade, then the CSV downloads (FAIL grades don't download).\n"
        "3. Do the same for last year's (and older) editions of the show.\n"
        "4. Drop all the CSVs into **Map Grabber CSVs** in the sidebar and click **Load CSVs**."
    ))
    with st.expander("Can't drag? Create the bookmark by hand"):
        st.caption("Make a new bookmark named 'Altitude Map Grabber' and paste this whole line as its URL.")
        st.code(bm, language=None, wrap_lines=True)


def render_platform_check() -> None:
    st.markdown("#### Platform Check")
    st.caption("Debug a new show: detects the platform, extracts it, and shows the count at each stage, sample rows "
               "next to their raw source records, and the quality report. Or check a CSV from the Map Grabber.")
    c1, c2 = st.columns([3, 1])
    url = c1.text_input("Floor-plan URL", key="pc_url", placeholder="https://kbis.a2zinc.net/kbis2026/Public/EventMap.aspx?shMode=E")
    go = c2.button("Check URL", key="pc_go", width="stretch")
    up = st.file_uploader("...or a CSV to grade", type=["csv"], key="pc_csv")
    if go and url.strip():
        u = url.strip() if url.strip().startswith("http") else "https://" + url.strip()
        st.write(f"Detected platform: **{platforms.platform_for(u, probe=True) or platforms.roadmap_name(u) or 'not recognised'}**")
        log: list[str] = []
        try:
            with st.spinner("Extracting..."):
                rows, meta, raw = platforms.extract(u, log=log.append)
        except Exception as exc:  # show every failure plainly -- this tab is for debugging
            st.error(f"{exc.__class__.__name__}: {exc}")
            if log:
                st.code("\n".join(log))
            return
        stages = pd.DataFrame([
            {"Stage": "Records fetched from the platform", "Count": meta.get("fetched", "")},
            {"Stage": "Records with a company name", "Count": meta.get("named", "")},
            {"Stage": "Companies after grouping + name filter", "Count": len(rows)},
            {"Stage": "Companies with a booth size", "Count": sum(1 for r in rows if r["sqft"] > 0)},
            {"Stage": "Companies at 400+ sq ft (own share)", "Count": sum(1 for r in rows if r["sqft"] >= 400)},
            {"Stage": "Companies on shared booths", "Count": sum(1 for r in rows if r["shared_booth"])},
            {"Stage": "Platform's own exhibitor count", "Count": meta.get("platform_count", "")},
        ])
        st.dataframe(stages, hide_index=True, width="stretch", key="pc_stages")
        st.write(f"Show: **{meta.get('show_base') or '?'}**, year stated by the source: "
                 f"**{meta.get('show_year') or 'none'}** ({meta.get('source_year_text') or 'no year text'})")
        st.markdown("**Sample normalised rows**")
        st.dataframe(pd.DataFrame(rows[:5], columns=ROW_COLUMNS), hide_index=True, width="stretch", key="pc_rows")
        st.markdown("**Raw source records (first 5)**")
        sample = (raw.get("records") or raw.get("booths") or (raw.get("data") or {}).get("booths")
                  or raw.get("gallery") or [])[:5]
        st.code(json.dumps(sample, indent=1, default=str)[:6000], language="json")
        render_quality(quality.check(rows, meta), key="pc")
        with st.expander("Extraction log"):
            st.code("\n".join(log) or "(empty)")
    elif up is not None:
        try:
            df, fmeta = showfile.load(up)
        except showfile.ShowFileError as exc:
            st.error(str(exc))
            return
        st.write(f"{len(df)} companies, show **{fmeta.get('show_base') or '?'} {fmeta.get('show_year') or ''}**, "
                 f"platform {fmeta.get('platform')}")
        render_quality(quality.check(df.to_dict("records"), fmeta), key="pc_csv")


def tab_grabber() -> None:
    render_grabber_install()
    st.divider()
    render_platform_check()


def render_empty_state() -> None:
    with st.container(border=True):
        st.markdown("**How this works**")
        st.markdown(md(
            "1. Get the data: click the **Map Grabber** bookmark on a show's floor plan (below) for this year "
            "and past years, then load the CSVs in the sidebar. Or paste a floor-plan URL and click "
            "**Run Extraction** (MapYourShow, A2Z, EXPOCAD, ExpoFP).\n"
            "2. The sq-ft filter keeps island exhibitors (default 400 to 3,000 sq ft). Pavilions, associations "
            "and government stands are removed; companies on shared booths count only their share.\n"
            "3. Show Timeline: companies that jumped from inline to island (NEWBORN ISLAND), peaked and pulled "
            "back (PEAK RETREAT), grew steadily, or dropped out.\n"
            "4. Apollo adds employee count, HQ and buyer contacts (new hires flagged).\n"
            "5. Freight tags and one pitch angle per company, exported for Instantly / Smartlead.\n\n"
            "Every extraction gets a PASS / WARN / FAIL quality grade; FAIL blocks exports. There is no demo or "
            "mock data anywhere in this app."
        ))
    tab_grabber()


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="expanded")
    init_state()
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(
        "<div class='ax-header'><h1>Altitude Exhibits &middot; Island Lead Engine</h1>"
        "<p>v3 &middot; Island exhibitors from MapYourShow, A2Z, EXPOCAD and ExpoFP floor plans plus Apollo "
        "firmographics. No modelled revenue, no guessed websites, no demo data.</p></div>",
        unsafe_allow_html=True,
    )
    params = render_sidebar()

    if params["load_csvs"]:
        load_show_csvs(st.session_state.get("show_csvs"))
        st.rerun()
    if params["run"]:
        run_extraction(params["url"])

    if st.session_state["exhibitors"] is None:
        render_empty_state()
        if st.session_state["extract_log"]:
            with st.expander("Last extraction log", expanded=True):
                for line in st.session_state["extract_log"]:
                    st.write(line)
        return

    all_df: pd.DataFrame = st.session_state["exhibitors"]
    meta: dict = st.session_state["meta"]
    targets = build_targets(params)
    st.session_state["_params"] = params      # read by on_run_apollo() on the next click
    st.session_state["_targets"] = targets

    tabs = st.tabs(TAB_LABELS, key="main_tabs", on_change="rerun")  # stateful: selection survives reruns
    with tabs[0]:
        tab_scanner(params, all_df, targets, meta)
    with tabs[1]:
        tab_timeline(params, targets)
    with tabs[2]:
        tab_freight(params, targets)
    with tabs[3]:
        tab_apollo(params, targets)
    with tabs[4]:
        tab_pitch(params, targets, meta)
    with tabs[5]:
        tab_grabber()


if __name__ == "__main__":
    main()
