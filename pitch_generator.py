"""
pitch_generator.py -- trigger hierarchy, pitch angles and the Instantly /
Smartlead export.

Trigger hierarchy (first match wins):

    A  NEWBORN ISLAND   prior year < 200 sq ft, this year >= 400 sq ft (delta engine)
                        Pitch: height limits, rigging panic, drayage optimisation.
    B  NEW HIRE         Apollo contact with < 6 months in the current role
                        Pitch: refresh the incumbent agency's stale design; make their mark.
    C  FREIGHT / LOCAL  HQ outside the West (East Coast, Midwest, South, international)
                        or HQ in Nevada
                        Pitch: local Vegas storage and zero-freight asset takeover.
    D  AOR FRICTION     every remaining 400+ sq ft island with no other trigger
                        Pitch: fixed-price guarantee, no $10k+ post-show change orders,
                        no national-agency drayage markup.

Every merge field in the intro line is an observed fact: the exhibitor name
and booth footprint from MapYourShow, the prior footprint from the uploaded
CSV, the contact's name and title from Apollo, the HQ state from Apollo.
"""

from __future__ import annotations

import pandas as pd

import freight

TRIGGER_A = "A"
TRIGGER_B = "B"
TRIGGER_C = "C"
TRIGGER_D = "D"

TRIGGERS = {
    TRIGGER_A: {
        "badge": "NEWBORN ISLAND",
        "color": "red",          # st.badge colour
        "hex": "#ef4444",
        "priority": 1,
        "pitch_angle": "First-island survival: height limits, rigging panic, drayage optimisation",
    },
    TRIGGER_B: {
        "badge": "NEW HIRE",
        "color": "violet",
        "hex": "#8b5cf6",
        "priority": 2,
        "pitch_angle": "New decision-maker: refresh the incumbent agency's stale booth and make your mark",
    },
    TRIGGER_C: {
        "badge": "FREIGHT ARBITRAGE",
        "color": "blue",
        "hex": "#3b82f6",
        "priority": 3,
        "pitch_angle": "Home-field: local Vegas storage and zero-freight asset takeover",
    },
    TRIGGER_D: {
        "badge": "AOR FRICTION",
        "color": "orange",
        "hex": "#f59e0b",
        "priority": 4,
        "pitch_angle": "Billing shock: fixed-price guarantee, no post-show change orders, no agency drayage markup",
    },
}
HIGH_PRIORITY = {TRIGGER_A, TRIGGER_B, TRIGGER_C}

PRICE_PER_SQFT = 150   # pipeline estimate only: total target sq ft x $150


def dims_label(width, length, sqft) -> str:
    def _f(v):
        v = float(v)
        return f"{int(v)}" if v.is_integer() else f"{v:g}"
    if width and length and not pd.isna(width) and not pd.isna(length):
        return f"{_f(width)}x{_f(length)}"
    return f"{int(sqft or 0):,} sq ft"


def _first_name(row: pd.Series) -> str:
    fn = str(row.get("first_name") or "").strip()
    return fn


def choose_trigger(row: pd.Series) -> str:
    if bool(row.get("newborn_island")):
        return TRIGGER_A
    if bool(row.get("new_hire")):
        return TRIGGER_B
    tag = str(row.get("freight_tag") or "")
    if freight.is_high_freight(tag) or freight.is_vegas_local(tag):
        return TRIGGER_C
    return TRIGGER_D


def intro_line(row: pd.Series, trigger: str, show_name: str) -> str:
    company = row["exhibitor_name"]
    sqft = int(row.get("sqft") or 0)
    dims = dims_label(row.get("width"), row.get("length"), sqft)
    booth = str(row.get("booth_number") or "").strip()
    booth_txt = f" (Booth {booth})" if booth else ""
    first = _first_name(row)
    greet = f"Hi {first}, " if first else ""

    if trigger == TRIGGER_A:
        prior = row.get("prior_sqft")
        prior_txt = f"from {int(prior):,} sq ft last year " if prior is not None and not pd.isna(prior) else ""
        return (f"{greet}saw {company} moved up {prior_txt}to a {dims} island at {show_name}{booth_txt}. "
                f"First island on the floor usually means first run-in with hall height limits, rigging quotes "
                f"and drayage per crate. We build islands in Las Vegas that are engineered around all three.")
    if trigger == TRIGGER_B:
        title = str(row.get("contact_title") or "your new role")
        return (f"{greet}congratulations on the move into {title} at {company}. Your {dims} at {show_name}"
                f"{booth_txt} is the first booth with your name on it. If the incumbent agency is rolling out "
                f"last year's design again, we can put a fresh concept next to it in a week, no obligation.")
    if trigger == TRIGGER_C:
        tag = str(row.get("freight_tag") or "")
        state = str(row.get("hq_state") or row.get("hq_country") or "")
        if freight.is_vegas_local(tag):
            return (f"{greet}{company} and Altitude are both Las Vegas companies. Your {dims} at {show_name}"
                    f"{booth_txt} could live in our warehouse a few miles from the hall, with zero freight and "
                    f"the same crew that built it doing the install.")
        origin = f" from {state}" if state else ""
        return (f"{greet}noticed {company} is shipping a {dims} into {show_name}{booth_txt}{origin}. "
                f"We build and store exhibits in Las Vegas, so a locally built asset takes cross-country freight, "
                f"round-trip drayage and an out-of-town I&D crew off the invoice.")
    return (f"{greet}{company}'s {dims} at {show_name}{booth_txt} is the footprint where national-agency "
            f"change orders start landing after the show. We quote islands at a fixed price: no post-show "
            f"surprises, no drayage markup, built and installed by the same Las Vegas team.")


def _cap(text: str) -> str:
    """Capitalise the first character (lines without a 'Hi <name>,' greeting start mid-sentence)."""
    return text[:1].upper() + text[1:] if text else text


def assign_pitches(df: pd.DataFrame, show_name: str) -> pd.DataFrame:
    """Add trigger, trigger_badge, pitch_angle, custom_intro_line, priority, est_value to the frame."""
    out = df.copy()
    if out.empty:
        for col in ("trigger", "trigger_badge", "pitch_angle", "custom_intro_line", "priority", "est_value"):
            out[col] = pd.Series(dtype="object")
        return out
    triggers = out.apply(choose_trigger, axis=1)
    out["trigger"] = triggers
    out["trigger_badge"] = triggers.map(lambda t: TRIGGERS[t]["badge"])
    out["pitch_angle"] = triggers.map(lambda t: TRIGGERS[t]["pitch_angle"])
    out["priority"] = triggers.map(lambda t: TRIGGERS[t]["priority"])
    out["custom_intro_line"] = [_cap(intro_line(row, t, show_name)) for (_, row), t in zip(out.iterrows(), triggers)]
    out["est_value"] = out["sqft"].fillna(0).astype(float) * PRICE_PER_SQFT
    return out.sort_values(["priority", "sqft"], ascending=[True, False]).reset_index(drop=True)


EXPORT_COLUMNS = ["email", "first_name", "last_name", "contact_title", "company_name", "website", "booth_number",
                  "booth_sqft", "trigger_badge", "pitch_angle", "custom_intro_line", "show_name",
                  "hq_state", "freight_tag", "yoy_status", "linkedin", "data_source"]


def build_export(df: pd.DataFrame, show_name: str) -> pd.DataFrame:
    """Instantly / Smartlead-ready frame. One row per contact (or per company when no contact was found)."""
    if df.empty:
        return pd.DataFrame(columns=EXPORT_COLUMNS)
    out = pd.DataFrame({
        "email": df.get("email", pd.Series([""] * len(df))).fillna(""),
        "first_name": df.get("first_name", pd.Series([""] * len(df))).fillna(""),
        "last_name": df.get("last_name", pd.Series([""] * len(df))).fillna(""),
        "contact_title": df.get("contact_title", pd.Series([""] * len(df))).fillna(""),
        "company_name": df["exhibitor_name"],
        "website": df.get("website", pd.Series([""] * len(df))).fillna(""),
        "booth_number": df.get("booth_number", pd.Series([""] * len(df))).fillna(""),
        "booth_sqft": df["sqft"].fillna(0).astype(int),
        "trigger_badge": df["trigger_badge"],
        "pitch_angle": df["pitch_angle"],
        "custom_intro_line": df["custom_intro_line"],
        "show_name": show_name,
        "hq_state": df.get("hq_state", pd.Series([""] * len(df))).fillna(""),
        "freight_tag": df.get("freight_tag", pd.Series([""] * len(df))).fillna(""),
        "yoy_status": df.get("yoy_status", pd.Series([""] * len(df))).fillna(""),
        "linkedin": df.get("linkedin", pd.Series([""] * len(df))).fillna(""),
        "data_source": df.get("apollo_source", pd.Series([""] * len(df))).fillna(""),
    })
    return out[EXPORT_COLUMNS]
