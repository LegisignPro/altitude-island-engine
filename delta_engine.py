"""
delta_engine.py -- "Newborn Island" year-over-year footprint tracker.

Upload last year's extraction for the same show (the CSV this app exports, or
any CSV with an exhibitor-name column and either a sq-ft column or width +
length columns). The engine joins it to the current extraction on a
normalised company name and computes:

    delta_sqft   = current sqft - prior sqft
    yoy_status   = CRITICAL: NEWBORN ISLAND   prior < 200 sq ft  and  current >= 400 sq ft
                   UPGRADE                    grew, but did not cross the inline -> island line
                   DOWNSIZE                   shrank
                   STAGNANT                   same footprint as last year
                   NEW TO SHOW                not in the prior-year file
                   NO PRIOR DATA              no prior file loaded, or prior sqft blank

A company that just jumped from a 10x10 to a 20x20 has almost certainly never
built a custom island: they are about to discover rigging, height limits and
drayage for the first time. That is the strongest observed buying signal in
the whole engine.
"""

from __future__ import annotations

import io
import re

import pandas as pd

INLINE_MAX_SQFT = 200      # prior footprint below this = "inline"
ISLAND_MIN_SQFT = 400      # current footprint at or above this = "island"

STATUS_NEWBORN = "CRITICAL: NEWBORN ISLAND"
STATUS_UPGRADE = "UPGRADE"
STATUS_DOWNSIZE = "DOWNSIZE"
STATUS_STAGNANT = "STAGNANT"
STATUS_NEW = "NEW TO SHOW"
STATUS_NONE = "NO PRIOR DATA"

LEGAL_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|gmbh|ag|sa|srl|plc|lp|llp|group|holdings)\b\.?",
    re.I,
)

NAME_COLUMNS = ["exhibitor_name", "exhibitor", "company", "company_name", "name", "exhname"]
SQFT_COLUMNS = ["sqft", "sq_ft", "square_feet", "area", "booth_sqft", "sq ft", "size_sqft"]
WIDTH_COLUMNS = ["width", "booth_width", "w"]
LENGTH_COLUMNS = ["length", "depth", "booth_length", "booth_depth", "l", "d"]


def name_key(name: str) -> str:
    """'3Play Media, Inc.' -> '3playmedia' so two years' spellings still join."""
    return re.sub(r"[^a-z0-9]", "", LEGAL_SUFFIX_RE.sub("", str(name or "").lower()))


def _find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    lower = {c.lower().strip(): c for c in df.columns}
    for cand in candidates:
        if cand in lower:
            return lower[cand]
    # loose match: any column containing the candidate
    for cand in candidates:
        for k, original in lower.items():
            if cand in k:
                return original
    return None


def load_prior_csv(file) -> pd.DataFrame:
    """
    Read an uploaded prior-year CSV into a two-column frame:
        prior_name, prior_sqft
    Raises ValueError with a clear message if the CSV has no usable columns.
    """
    raw = file.read() if hasattr(file, "read") else file
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig", errors="replace")
    df = pd.read_csv(io.StringIO(raw))
    if df.empty:
        raise ValueError("The prior-year CSV is empty.")

    name_col = _find_column(df, NAME_COLUMNS)
    if not name_col:
        raise ValueError(f"No exhibitor-name column found. Columns seen: {', '.join(map(str, df.columns))}")

    sqft_col = _find_column(df, SQFT_COLUMNS)
    if sqft_col:
        sqft = pd.to_numeric(df[sqft_col], errors="coerce")
    else:
        w_col, l_col = _find_column(df, WIDTH_COLUMNS), _find_column(df, LENGTH_COLUMNS)
        if not (w_col and l_col):
            raise ValueError("No sq-ft column and no width/length pair found in the prior-year CSV.")
        sqft = pd.to_numeric(df[w_col], errors="coerce") * pd.to_numeric(df[l_col], errors="coerce")

    out = pd.DataFrame({"prior_name": df[name_col].astype(str).str.strip(), "prior_sqft": sqft})
    out = out[out["prior_name"] != ""]
    out["name_key"] = out["prior_name"].map(name_key)
    # A company with several booths last year: keep the total.
    out = out.groupby("name_key", as_index=False).agg(prior_name=("prior_name", "first"),
                                                       prior_sqft=("prior_sqft", "sum"))
    return out


def classify(prior_sqft, current_sqft) -> str:
    if prior_sqft is None or pd.isna(prior_sqft):
        return STATUS_NONE
    prior, current = float(prior_sqft), float(current_sqft or 0)
    if prior < INLINE_MAX_SQFT and current >= ISLAND_MIN_SQFT:
        return STATUS_NEWBORN
    if current > prior:
        return STATUS_UPGRADE
    if current < prior:
        return STATUS_DOWNSIZE
    return STATUS_STAGNANT


def compute_delta(current: pd.DataFrame, prior: pd.DataFrame | None) -> pd.DataFrame:
    """
    Add prior_sqft, delta_sqft, yoy_status, newborn_island to the current-year frame.
    `current` must carry exhibitor_name and sqft. Works with prior=None (all NO PRIOR DATA).
    """
    out = current.copy()
    out["name_key"] = out["exhibitor_name"].map(name_key)
    if prior is None or prior.empty:
        out["prior_sqft"] = pd.NA
        out["delta_sqft"] = pd.NA
        out["yoy_status"] = STATUS_NONE
        out["newborn_island"] = False
        return out.drop(columns=["name_key"])

    merged = out.merge(prior[["name_key", "prior_sqft"]], on="name_key", how="left")
    in_prior = merged["name_key"].isin(set(prior["name_key"]))
    merged["prior_sqft"] = pd.to_numeric(merged["prior_sqft"], errors="coerce")
    merged["delta_sqft"] = merged["sqft"].astype(float) - merged["prior_sqft"]
    merged["yoy_status"] = [
        classify(p, c) if known else STATUS_NEW
        for p, c, known in zip(merged["prior_sqft"], merged["sqft"], in_prior)
    ]
    merged["newborn_island"] = merged["yoy_status"] == STATUS_NEWBORN
    return merged.drop(columns=["name_key"])


def delta_summary(df: pd.DataFrame) -> dict:
    counts = df["yoy_status"].value_counts().to_dict() if "yoy_status" in df else {}
    return {
        "newborn": int(counts.get(STATUS_NEWBORN, 0)),
        "upgrade": int(counts.get(STATUS_UPGRADE, 0)),
        "downsize": int(counts.get(STATUS_DOWNSIZE, 0)),
        "stagnant": int(counts.get(STATUS_STAGNANT, 0)),
        "new": int(counts.get(STATUS_NEW, 0)),
        "matched": int(sum(v for k, v in counts.items() if k not in (STATUS_NEW, STATUS_NONE))),
    }
