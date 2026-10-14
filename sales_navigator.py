"""
sales_navigator.py -- format Island Engine leads as a LinkedIn Sales Navigator
account-list upload CSV, straight from whatever show is loaded.

LinkedIn's own account-upload template has exactly these columns (order
matters for the import). Only fields the floor plan or Apollo actually
published are filled in; anything the directory never gave us (street
address, ZIP, stock symbol, the numeric LinkedIn company id) is left blank
rather than guessed -- same "never invent a value" rule as the rest of v3.
"""

from __future__ import annotations

import pandas as pd

SN_COLUMNS = [
    "Account Name (REQUIRED)",
    "Website URL (OPTIONAL)",
    "Country (OPTIONAL)",
    "City (OPTIONAL)",
    "Street (OPTIONAL)",
    "Stock symbol (OPTIONAL)",
    "Zip/Postal Code (OPTIONAL)",
    "Industry (OPTIONAL)",
    "LinkedIn Company ID (OPTIONAL)",
    "LinkedIn Company URL (OPTIONAL)",
]


def _first(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    """Per row, the first non-blank value among `cols` (left to right)."""
    present = [c for c in cols if c in df.columns]
    if not present:
        return pd.Series([""] * len(df), index=df.index, dtype="object")
    sub = df[present].fillna("").astype(str)
    out = sub[present[0]]
    for c in present[1:]:
        out = out.where(out.str.strip() != "", sub[c])
    return out


def build(df: pd.DataFrame) -> pd.DataFrame:
    """`df` is the island-qualified target rows (any subset of the normal
    exhibitor schema works; missing columns just come back blank). HQ city/
    state/country prefer Apollo (`hq_city`/`hq_country`) when it ran, else
    fall back to what the show directory listed (`city`/`country`) -- the
    same preference `build_targets()` already applies on screen. One row per
    company: if the same frame is fed in twice (e.g. a merged multi-year
    file) duplicates by name are dropped rather than uploaded twice.
    """
    if df.empty:
        return pd.DataFrame(columns=SN_COLUMNS)
    out = pd.DataFrame({
        "Account Name (REQUIRED)": df["exhibitor_name"].fillna(""),
        "Website URL (OPTIONAL)": _first(df, ["website"]),
        "Country (OPTIONAL)": _first(df, ["hq_country", "country"]),
        "City (OPTIONAL)": _first(df, ["hq_city", "city"]),
        "Street (OPTIONAL)": "",
        "Stock symbol (OPTIONAL)": "",
        "Zip/Postal Code (OPTIONAL)": "",
        "Industry (OPTIONAL)": _first(df, ["industry", "categories"]),
        "LinkedIn Company ID (OPTIONAL)": "",
        "LinkedIn Company URL (OPTIONAL)": _first(df, ["company_linkedin"]),
    })
    out = out[out["Account Name (REQUIRED)"].str.strip() != ""]
    out = out.drop_duplicates(subset=["Account Name (REQUIRED)"])
    return out[SN_COLUMNS]


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return build(df).to_csv(index=False).encode("utf-8")
