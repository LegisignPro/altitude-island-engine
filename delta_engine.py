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


# =============================================================================
# Multi-year timeline ("Show Timeline") -- several prior-year slots, not just one.
#
# A single prior-year upload only ever sees a one-step delta. With three or more
# years on the table the SHAPE of a company's booth history becomes visible:
# a small island that grew every year is a different lead than one that peaked
# two years ago and has been shrinking since. Woz's hypothesis, encoded below as
# PEAK RETREAT: small -> large -> medium means they went big once, pulled back,
# and are likely shopping for a new exhibit partner to make a splash again.
# =============================================================================

TRAJ_NEWBORN = STATUS_NEWBORN              # prior < 200, current >= 400 -- unchanged, highest priority
TRAJ_PEAK_RETREAT = "PEAK RETREAT"         # 3+ pts, peaked earlier, now 50-90% of that peak, still >= 400
TRAJ_STEADY_GROWTH = "STEADY GROWTH"       # 3+ pts, non-decreasing with >=1 increase, current >= 400
TRAJ_SHRINKING = "SHRINKING"               # 3+ pts, non-increasing with >=1 decrease
TRAJ_VOLATILE = "VOLATILE"                 # 3+ pts, direction changes more than once
TRAJ_UPGRADE = STATUS_UPGRADE              # exactly 2 pts, grew (existing single-step semantics)
TRAJ_DOWNSIZE = STATUS_DOWNSIZE            # exactly 2 pts, shrank
TRAJ_STAGNANT = STATUS_STAGNANT            # exactly 2 pts, unchanged
TRAJ_NEW = STATUS_NEW                      # only ever seen in the current year
TRAJ_NONE = STATUS_NONE                    # timeline has only one slot -- nothing to compare at all

PEAK_RETREAT_MIN_PCT = 0.50    # current must be within 50-90% of the historical peak
PEAK_RETREAT_MAX_PCT = 0.90


def classify_trajectory(points: list[tuple[int, float]], total_slots: int) -> str:
    """
    `points`: this company's (year, sqft) pairs, ascending by year, for years where it had a
    booth (years it was absent are simply not included). `total_slots`: how many years are
    loaded in the timeline overall -- a company can have fewer points than that if it skipped
    a year. Returns one of the TRAJ_* labels above. At 3+ points the classification looks at
    the whole shape; at exactly 2 it reduces to the original single-step classify().
    """
    if total_slots <= 1:
        return TRAJ_NONE
    n = len(points)
    if n <= 1:
        return TRAJ_NEW
    sqfts = [s for _, s in points]
    prior_sqft, current_sqft = sqfts[-2], sqfts[-1]
    if n == 2:
        return classify(prior_sqft, current_sqft)

    # n >= 3: NEWBORN still wins outright -- a first-time island is the strongest signal
    # regardless of what happened in earlier years.
    if prior_sqft < INLINE_MAX_SQFT and current_sqft >= ISLAND_MIN_SQFT:
        return TRAJ_NEWBORN

    peak_sqft = max(sqfts)
    peak_year = next(y for y, s in points if s == peak_sqft)   # first year the peak was hit
    first_year, last_year = points[0][0], points[-1][0]
    # The peak must sit strictly between the first and last points -- there has to be growth
    # INTO the peak (small -> large) and a retreat FROM it (large -> medium). A peak that is
    # simply the earliest point on file is a plain decline (SHRINKING below), not a retreat.
    if (first_year < peak_year < last_year and current_sqft >= ISLAND_MIN_SQFT
            and PEAK_RETREAT_MIN_PCT * peak_sqft <= current_sqft <= PEAK_RETREAT_MAX_PCT * peak_sqft):
        return TRAJ_PEAK_RETREAT

    diffs = [b - a for a, b in zip(sqfts, sqfts[1:])]
    if all(d >= 0 for d in diffs) and any(d > 0 for d in diffs) and current_sqft >= ISLAND_MIN_SQFT:
        return TRAJ_STEADY_GROWTH
    if all(d <= 0 for d in diffs) and any(d < 0 for d in diffs):
        return TRAJ_SHRINKING

    signs = [1 if d > 0 else (-1 if d < 0 else 0) for d in diffs]
    nonzero = [s for s in signs if s != 0]
    direction_changes = sum(1 for i in range(1, len(nonzero)) if nonzero[i] != nonzero[i - 1])
    if direction_changes > 1:
        return TRAJ_VOLATILE

    # A 3+ point series that is not a clean peak-retreat, monotonic run, or multi-reversal
    # (e.g. flat, or a single up/down tick that misses the peak-retreat percentage band):
    # fall back to the plain last-step read rather than inventing another category.
    return classify(prior_sqft, current_sqft)


def build_trajectories(slots: list[dict]) -> pd.DataFrame:
    """
    `slots`: list of {"year": int, "label": str, "source": str, "df": DataFrame with
    exhibitor_name + sqft columns}. Any order in; sorted ascending by year internally, and the
    highest-year slot is treated as "this year" (the live extraction is always added as the
    newest slot by the caller).

    Returns one row per company (union across every slot) with: exhibitor_name (latest known
    spelling), name_key, one sqft_<year> column per slot, years_present, first_year, last_year,
    peak_sqft, peak_year, current_sqft, prior_sqft (nearest earlier year present), delta_sqft,
    trajectory, yoy_status (single-step, kept compatible with the existing STATUS_* logic),
    newborn_island, and is_current_year (False for a company that exhibited in a past slot but
    not this year's -- yoy_status/newborn_island/trigger logic never fire for those rows).
    """
    slots = sorted(slots, key=lambda s: s["year"])
    years = [s["year"] for s in slots]
    base_cols = (["name_key", "exhibitor_name"] + [f"sqft_{y}" for y in years] +
                 ["years_present", "first_year", "last_year", "peak_sqft", "peak_year", "current_sqft",
                  "prior_sqft", "delta_sqft", "trajectory", "yoy_status", "newborn_island", "is_current_year"])
    if not slots:
        return pd.DataFrame(columns=base_cols)

    per_year: dict[int, pd.DataFrame] = {}
    for s in slots:
        d = s["df"][["exhibitor_name", "sqft"]].copy()
        d["sqft"] = pd.to_numeric(d["sqft"], errors="coerce")
        d["name_key"] = d["exhibitor_name"].map(name_key)
        d = d.dropna(subset=["name_key"])
        d = d[d["name_key"] != ""]
        # a company with more than one booth in a single year's file: keep the total
        d = d.groupby("name_key", as_index=False).agg(exhibitor_name=("exhibitor_name", "first"),
                                                        sqft=("sqft", "sum"))
        per_year[s["year"]] = d

    name_by_key: dict[str, str] = {}
    for y in years:   # ascending -- a later year's spelling overwrites an earlier one
        for _, row in per_year[y].iterrows():
            name_by_key[row["name_key"]] = row["exhibitor_name"]

    current_year = years[-1]
    records = []
    for key, ename in name_by_key.items():
        rec = {"name_key": key, "exhibitor_name": ename}
        points: list[tuple[int, float]] = []
        for y in years:
            sub = per_year[y]
            hit = sub.loc[sub["name_key"] == key, "sqft"]
            sqft = float(hit.iloc[0]) if len(hit) and pd.notna(hit.iloc[0]) else None
            rec[f"sqft_{y}"] = sqft
            if sqft is not None:
                points.append((y, sqft))

        rec["years_present"] = len(points)
        rec["first_year"] = points[0][0] if points else None
        rec["last_year"] = points[-1][0] if points else None
        if points:
            peak_sqft = max(s for _, s in points)
            rec["peak_sqft"] = peak_sqft
            rec["peak_year"] = next(y for y, s in points if s == peak_sqft)
            rec["current_sqft"] = points[-1][1]
            rec["prior_sqft"] = points[-2][1] if len(points) >= 2 else None
            rec["delta_sqft"] = (points[-1][1] - points[-2][1]) if len(points) >= 2 else None
        else:
            rec["peak_sqft"] = rec["peak_year"] = rec["current_sqft"] = None
            rec["prior_sqft"] = rec["delta_sqft"] = None

        is_current = bool(points) and points[-1][0] == current_year
        rec["is_current_year"] = is_current
        rec["trajectory"] = classify_trajectory(points, len(slots))
        if not is_current or len(slots) <= 1:
            rec["yoy_status"] = STATUS_NONE
        elif len(points) >= 2:
            rec["yoy_status"] = classify(points[-2][1], points[-1][1])
        else:
            rec["yoy_status"] = STATUS_NEW
        rec["newborn_island"] = is_current and rec["yoy_status"] == STATUS_NEWBORN
        records.append(rec)

    return pd.DataFrame(records, columns=base_cols)


def trajectory_summary(df: pd.DataFrame) -> dict:
    """Counts per trajectory, for companies present in the current year only."""
    if "trajectory" not in df or "is_current_year" not in df:
        return {}
    cur = df[df["is_current_year"]]
    counts = cur["trajectory"].value_counts().to_dict()
    return {
        "newborn": int(counts.get(TRAJ_NEWBORN, 0)),
        "peak_retreat": int(counts.get(TRAJ_PEAK_RETREAT, 0)),
        "steady_growth": int(counts.get(TRAJ_STEADY_GROWTH, 0)),
        "shrinking": int(counts.get(TRAJ_SHRINKING, 0)),
        "volatile": int(counts.get(TRAJ_VOLATILE, 0)),
        "upgrade": int(counts.get(TRAJ_UPGRADE, 0)),
        "downsize": int(counts.get(TRAJ_DOWNSIZE, 0)),
        "stagnant": int(counts.get(TRAJ_STAGNANT, 0)),
        "new": int(counts.get(TRAJ_NEW, 0)),
    }
