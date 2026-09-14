"""
app.py -- Altitude Exhibits Island Lead Engine (Streamlit).

    pip install -r requirements.txt
    streamlit run app.py

Modules
    scraper.py          MapYourShow JSON extraction + demo fallback
    delta_engine.py     year-over-year footprint delta ("Newborn Island")
    freight.py          HQ-based freight arbitrage tagging
    apollo.py           Apollo organisation enrichment + people search (live or mock)
    pitch_generator.py  trigger hierarchy, intro lines, Instantly/Smartlead export

Every number on screen is either an observed MapYourShow fact, an Apollo
firmographic, or a value computed from those two. Nothing is modelled.
"""

from __future__ import annotations

import re
from datetime import datetime

import pandas as pd
import streamlit as st

import apollo
import delta_engine
import freight
import pitch_generator as pg
import platforms
import scraper

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
]

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
.ax-demo   { background: rgba(245,158,11,.18); color: #d97706; border-color: rgba(245,158,11,.55); }
.ax-mock   { background: rgba(139,92,246,.16); color: #7c3aed; border-color: rgba(139,92,246,.5); }
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
    label = f"{base} {int(year)}" if year not in (None, "") else base
    if meta.get("source") == "demo":
        label += " (DEMO DATA)"
    return label


def show_file_stub() -> str:
    """
    Filename-safe '{show_base}_{show_year}' (year omitted when unknown), built from the
    same editable fields as current_show_label() but WITHOUT the "(DEMO DATA)" suffix --
    that belongs on screen, not baked into a file a person might rename and forward.
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
    mode = st.session_state.get(f"slot_mode_{slot_id}", "CSV upload")
    slot["error"] = ""
    if not year:
        slot["error"] = "Set a year first."
        return
    slot["year"] = int(year)
    if mode == "CSV upload":
        up = st.session_state.get(f"slot_csv_{slot_id}")
        if up is None:
            slot["error"] = "Choose a CSV file first."
            return
        try:
            prior = delta_engine.load_prior_csv(up)
            slot["df"] = prior.rename(columns={"prior_name": "exhibitor_name", "prior_sqft": "sqft"}) \
                               [["exhibitor_name", "sqft"]]
            slot["label"] = up.name
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
            rows, meta = platforms.extract_directory(url)
            slot["df"] = pd.DataFrame(rows, columns=scraper.EXHIBITOR_COLUMNS)[["exhibitor_name", "sqft"]]
            slot["label"] = meta.get("show_name") or url
        except scraper.ScrapeError as exc:
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


# =============================================================================
# Sidebar
# =============================================================================

def render_sidebar() -> dict:
    with st.sidebar:
        st.markdown("### Altitude Exhibits")
        st.caption("Island Lead Engine: MapYourShow facts + Apollo firmographics. No modelled data.")

        st.markdown("**API configuration**")
        secret_key = ""
        try:
            secret_key = st.secrets.get("APOLLO_API_KEY", "")
        except Exception:
            secret_key = ""
        api_key = st.text_input("Apollo API key", value=secret_key, type="password",
                                help="Master API key from app.apollo.io > Settings > Integrations > API. "
                                     "Leave blank to run the UI on clearly-labelled mock responses.")
        api_key = (api_key or "").strip()
        mock_default = not bool(api_key)
        use_mock = st.checkbox("Use mock Apollo responses", value=mock_default,
                               help="Mock data is deterministic placeholder output for demoing the UI. "
                                    "It is labelled MOCK everywhere it appears and is never real firmographics.")
        if not api_key and not use_mock:
            st.warning("No Apollo key: enrichment will return nothing. Add a key or enable mock mode.")
        budget = st.number_input("Max Apollo org lookups per run (1 credit each)", min_value=1, max_value=500,
                                 value=DEFAULT_APOLLO_BUDGET, step=5,
                                 help="Apollo Free = 75 credits/month. People search is free.")

        st.divider()
        st.markdown("**Target show**")
        url = st.text_input("Directory / floor-plan URL (MapYourShow, A2Z, EXPOCAD, ExpoFP)",
                            placeholder="https://ces2026.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm",
                            help="Any page on a mapyourshow.com, a2zinc.net/mya2zevents.com, expocad(web).com, "
                                 "or expofp.com host. MapYourShow is read via its JSON endpoints; the other "
                                 "three are read by intercepting the JSON their floor plan loads in the "
                                 "background, which needs a browser and can take longer.")
        allow_demo = st.checkbox("Load demo dataset if the URL fails", value=True,
                                 help="20 fictional exhibitors so the UI can be demonstrated offline. "
                                      "Clearly banded as DEMO DATA whenever shown.")

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

    return {"api_key": api_key, "use_mock": use_mock, "budget": int(budget), "url": (url or "").strip(),
            "allow_demo": allow_demo, "min_sqft": int(min_sqft), "max_sqft": int(max(max_sqft, min_sqft)),
            "run": run}


# =============================================================================
# Extraction
# =============================================================================

def run_extraction(url: str, allow_demo: bool, min_sqft: int, max_sqft: int) -> None:
    log: list[str] = []
    rows, meta = None, None
    with st.status("Extracting directory...", expanded=True) as status:
        def _log(msg: str) -> None:
            log.append(msg)
            st.write(msg)

        if url:
            if not url.startswith("http"):
                url = "https://" + url
            platform = platforms.platform_for(url)
            _log(f"Detected platform: {platform or 'unrecognised'} for `{url}`")
            try:
                rows, meta = platforms.extract_directory(url, log=_log)
                if meta.get("platform") == "mapyourshow":
                    # The other platforms already carry a website straight from their own JSON;
                    # this extra per-exhibitor detail-page fetch is MapYourShow-specific.
                    targets = [r for r in rows if min_sqft <= r["sqft"] <= max_sqft]
                    _log(f"Fetching websites from detail pages for {len(targets)} in-range exhibitors")
                    scraper.enrich_websites(targets, url, log=_log)
            except scraper.ScrapeError as exc:
                _log(f"Live extraction failed: {exc}")
                rows = None
            except Exception as exc:  # the demo must not crash on an unexpected shape
                _log(f"Unexpected error during extraction: {exc.__class__.__name__}: {exc}")
                rows = None
        else:
            _log("No URL entered.")

        if rows is None:
            if allow_demo:
                _log("Loading the DEMO fallback dataset (20 fictional exhibitors).")
                rows, meta = scraper.load_fallback_dataset()
            else:
                status.update(label="Extraction failed", state="error", expanded=True)
                st.session_state["extract_log"] = log
                return

        df = pd.DataFrame(rows, columns=scraper.EXHIBITOR_COLUMNS)
        df["sqft"] = pd.to_numeric(df["sqft"], errors="coerce").fillna(0).astype(int)
        meta["extracted_at"] = datetime.now().strftime("%b %d, %Y %I:%M %p")
        st.session_state["exhibitors"] = df
        st.session_state["meta"] = meta
        st.session_state["extract_log"] = log
        # Pre-fill the editable show name/year from this extraction (Task 1: year
        # is a fact from THIS scrape, never carried over from a prior run or guessed).
        st.session_state["show_base"] = meta.get("show_base") or ""
        st.session_state["show_year"] = meta.get("show_year")
        reset_apollo()
        platform_txt = f" (live {meta.get('platform', 'mapyourshow')})" if meta["source"] == "live" else ""
        status.update(label=f"Extracted {len(df)} exhibitors from {meta['show_name']}{platform_txt}",
                      state="complete", expanded=False)
    # Rerun once so the sidebar's Show name/year inputs (rendered before this
    # function runs) immediately reflect the freshly extracted values instead
    # of lagging one interaction behind. Tab selection survives via key="main_tabs".
    st.rerun()


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

    org_cols = {"employees": [], "hq_city": [], "hq_state": [], "hq_country": [], "industry": [],
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
        org_cols["hq_city"].append(org.get("hq_city", ""))
        org_cols["hq_state"].append(org.get("hq_state", ""))
        org_cols["hq_country"].append(org.get("hq_country", ""))
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
    if meta["source"] == "demo":
        st.warning("DEMO DATA: the directory could not be read (or no URL was entered), so the 20 fictional "
                   "exhibitors below are placeholders for the UI walk-through. Nothing here is a real company.")
    platform_label = {"mapyourshow": "MapYourShow", "a2z": "A2Z", "expocad": "EXPOCAD",
                      "expofp": "ExpoFP"}.get(meta.get("platform"), "MapYourShow")
    pills = (f"<span class='ax-pill ax-live'>Live {platform_label}</span>" if meta["source"] == "live"
             else "<span class='ax-pill ax-demo'>Demo data</span>")
    pills += f"<span class='ax-pill ax-info'>{current_show_label()}</span>"
    if meta.get("halls"):
        pills += f"<span class='ax-pill ax-info'>{meta['halls']} halls</span>"
    st.markdown(pills, unsafe_allow_html=True)
    note = f"{meta['total']} exhibitors, {meta['sized']} with floor-plan geometry, extracted {meta['extracted_at']}"
    if meta.get("hall_errors"):
        note += f", {meta['hall_errors']} hall(s) failed to load"
    st.markdown(f"<div class='ax-muted'>{note}</div>", unsafe_allow_html=True)


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
    "detail_url": st.column_config.LinkColumn("MYS listing", display_text="open", width="small"),
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
    st.caption("Every column is an observed MapYourShow fact. Rows dropped for >1,000 employees (Apollo) are "
               "listed separately below.")
    live = targets[~targets["oversized"]]
    cols = ["exhibitor_name", "booth_number", "width", "length", "sqft", "hall", "website",
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
        show_table(all_df, cols, int(all_df["sqft"].max() or 1), key="grid_all", height=420)
        with_size = int((all_df["sqft"] > 0).sum())
        st.caption(f"{with_size:,} of {len(all_df):,} exhibitors have floor-plan geometry. "
                   f"Exhibitors without geometry show 0 sq ft and never enter the target list.")
    st.download_button("Download full extraction CSV (use as next year's prior-year file)",
                       data=all_df.to_csv(index=False).encode("utf-8"),
                       file_name=f"{show_file_stub()}_exhibitors.csv", mime="text/csv")


TRAJ_STYLES = {
    delta_engine.TRAJ_NEWBORN: "background-color: #ef4444; color: white; font-weight: 700",
    delta_engine.TRAJ_PEAK_RETREAT: f"background-color: {pg.TRIGGERS[pg.TRIGGER_E]['hex']}; color: white; font-weight: 700",
    delta_engine.TRAJ_STEADY_GROWTH: "background-color: rgba(34,197,94,.3); font-weight: 600",
    delta_engine.TRAJ_SHRINKING: "background-color: rgba(100,116,139,.3)",
    delta_engine.TRAJ_VOLATILE: "background-color: rgba(139,92,246,.28); font-weight: 600",
    delta_engine.TRAJ_UPGRADE: "background-color: rgba(34,197,94,.18)",
    delta_engine.TRAJ_DOWNSIZE: "background-color: rgba(100,116,139,.18)",
    delta_engine.TRAJ_NEW: "background-color: rgba(59,130,246,.2)",
}


def render_timeline_slot(slot: dict) -> None:
    sid = slot["id"]
    with st.container(border=True):
        c1, c2, c3 = st.columns([1, 2, 1])
        c1.number_input("Year", min_value=2015, max_value=2035, step=1,
                        value=slot["year"] or 2025, key=f"slot_year_{sid}")
        mode = c2.radio("Source", ["CSV upload", "MapYourShow URL"], key=f"slot_mode_{sid}", horizontal=True)
        c3.markdown("&nbsp;", unsafe_allow_html=True)   # align the Remove button with the row above
        c3.button("Remove slot", key=f"slot_remove_{sid}", on_click=remove_timeline_slot, args=(sid,),
                  width="stretch")
        if mode == "CSV upload":
            st.file_uploader("Prior-year CSV (this app's export, or any file with a name + sq-ft column)",
                             type=["csv"], key=f"slot_csv_{sid}")
        else:
            st.text_input("MapYourShow directory URL for that year's show", key=f"slot_url_{sid}",
                          placeholder="https://nab26.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm")
        lc1, lc2 = st.columns([1, 3])
        lc1.button("Load", key=f"slot_load_{sid}", type="primary", on_click=load_timeline_slot, args=(sid,))
        if slot.get("error"):
            lc2.error(slot["error"])
        elif slot.get("df") is not None:
            lc2.success(f"{slot.get('label') or 'Loaded'}: {len(slot['df'])} companies, year {slot['year']}")
        else:
            lc2.caption("Not loaded yet.")


def tab_timeline(params: dict, targets: pd.DataFrame) -> None:
    st.markdown("#### Multi-year show timeline")
    st.caption(md(
        "Add prior years as a CSV (this app's own export, or any file with a name and sq-ft column) or a "
        "MapYourShow directory URL for that year's show -- up to 6 years total. With three or more years on "
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
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Newborn islands", s.get("newborn", 0))
    m2.metric("Peak retreats", s.get("peak_retreat", 0))
    m3.metric("Steady growth", s.get("steady_growth", 0))
    m4.metric("Shrinking", s.get("shrinking", 0))
    m5.metric("New to show", s.get("new", 0))

    years = sorted(int(y["year"]) for y in all_slots)
    year_cols = [f"sqft_{y}" for y in years]
    cols = ["exhibitor_name"] + year_cols + ["peak_sqft", "peak_year", "current_sqft", "delta_sqft", "trajectory"]
    target_names = set(targets["exhibitor_name"]) if not targets.empty else set()
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


def tab_freight(params: dict, targets: pd.DataFrame) -> None:
    st.markdown("#### Home-field freight arbitrage")
    st.caption("HQ state comes from Apollo organisation enrichment (Tab 4). Exhibitors headquartered outside "
               "Nevada and the West Coast pay cross-country freight, round-trip drayage and out-of-town I&D "
               "for every Las Vegas show. Nevada HQs get the local storage / asset-takeover angle instead.")
    live = targets[~targets["oversized"]]
    if not st.session_state["orgs"]:
        st.info("Run Apollo enrichment in Tab 4 first. HQ location is never guessed.")
        return
    known = live[live["region"] != "Unknown"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("HQ known", f"{len(known)} / {len(live)}")
    m2.metric("High freight savings", int(live["freight_tag"].map(freight.is_high_freight).sum()))
    m3.metric("Vegas local", int(live["freight_tag"].map(freight.is_vegas_local).sum()))
    m4.metric("West Coast / Mountain", int(live["region"].isin(["West Coast", "Mountain West"]).sum()))

    regions = sorted(live["region"].unique())
    pick = st.multiselect("Regions", regions, default=[r for r in regions if r != "Unknown"] or regions)
    view = live[live["region"].isin(pick)].sort_values(["priority", "sqft"], ascending=[True, False])
    cols = ["exhibitor_name", "sqft", "hq_city", "hq_state", "hq_country", "region", "freight_tag", "apollo_source", "website"]
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
    mock = params["use_mock"]
    log: list[str] = []
    done = 0
    for _, row in todo.iterrows():
        if not mock and st.session_state["credits_used"] >= budget:
            log.append(f"Budget of {budget} org lookups reached; remaining companies left unenriched.")
            break
        domain = row["domain"]
        org = apollo.get_organization(domain, params["api_key"], mock=mock)
        st.session_state["orgs"][domain] = org
        if not mock:
            st.session_state["credits_used"] += 1
        if org.get("found") and org.get("employees") and org["employees"] > apollo.MAX_EMPLOYEES:
            st.session_state["people"][domain] = []
            log.append(f"{row['exhibitor_name']}: {org['employees']:,} employees, dropped")
        else:
            ppl = apollo.search_people(domain, params["api_key"], mock=mock)
            st.session_state["people"][domain] = ppl
            nh = sum(1 for p in ppl if p["new_hire"])
            log.append(f"{row['exhibitor_name']}: {org.get('employees') or '?'} employees, "
                       f"HQ {org.get('hq_state') or org.get('hq_country') or '?'}, {len(ppl)} contacts"
                       + (f", {nh} NEW HIRE" if nh else "")
                       + (f" [{org['error']}]" if org.get("error") else ""))
        done += 1
    st.session_state["apollo_log"] = [f"Apollo pass complete: {done} companies ({'MOCK' if mock else 'live'})"] + log
    st.toast(f"Apollo pass complete: {done} companies ({'MOCK' if mock else 'live'})")


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
    c4.metric("Mode", "MOCK" if params["use_mock"] else "Live Apollo")
    if params["use_mock"]:
        st.markdown("<span class='ax-pill ax-mock'>Mock responses</span> <span class='ax-muted'>placeholder "
                    "firmographics for the UI walk-through, labelled MOCK in every table and the export</span>",
                    unsafe_allow_html=True)

    b1, b2, _ = st.columns([1.6, 1.2, 3])
    label = f"Run Apollo enrichment ({min(pending, params['budget']) if not params['use_mock'] else pending} companies)"
    b1.button(label, type="primary", disabled=pending == 0, width="stretch", on_click=on_run_apollo)
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
    n_with_email = int((export_df["email"] != "").sum()) if not export_df.empty else 0
    st.caption(f"{len(export_df)} rows ({n_with_email} with an email). Columns: {', '.join(pg.EXPORT_COLUMNS[:11])}. "
               "Import into Instantly or Smartlead and map custom_intro_line to a custom variable.")
    st.download_button("Download campaign CSV (Instantly / Smartlead)",
                       data=export_df.to_csv(index=False).encode("utf-8"),
                       file_name=f"{show_file_stub()}_campaign.csv", mime="text/csv", type="primary")


# =============================================================================
# Main
# =============================================================================

def render_empty_state() -> None:
    with st.container(border=True):
        st.markdown("**How this works**")
        st.markdown(md(
            "1. Paste a MapYourShow directory URL in the sidebar (any page on the show's `mapyourshow.com` host) "
            "and click **Run Extraction**. The app reads the show's JSON endpoints: halls, the full exhibitor "
            "gallery and the floor-plan booth geometry, then joins them for exact booth footprints.\n"
            "2. The sq-ft filter keeps island exhibitors (default 400 to 3,000 sq ft). Pavilions, associations "
            "and government stands are removed.\n"
            "3. Tab 2: add one or more prior years (CSV or URL) to flag companies that just jumped from an "
            "inline to an island, or that grew to a peak and pulled back -- likely shopping for a new partner.\n"
            "4. Tab 4: Apollo adds employee count, HQ state and buyer contacts (new hires flagged).\n"
            "5. Tab 3 and Tab 5: freight arbitrage tags, one pitch angle per company, CSV for Instantly / Smartlead."
        ))


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="expanded")
    init_state()
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(
        "<div class='ax-header'><h1>Altitude Exhibits &middot; Island Lead Engine</h1>"
        "<p>Low-volume, high-intent island exhibitors from MapYourShow facts and Apollo firmographics. "
        "No modelled revenue, no guessed websites, no synthetic scores.</p></div>",
        unsafe_allow_html=True,
    )
    params = render_sidebar()

    if params["run"]:
        run_extraction(params["url"], params["allow_demo"], params["min_sqft"], params["max_sqft"])

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


if __name__ == "__main__":
    main()
