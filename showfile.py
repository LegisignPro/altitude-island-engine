"""
showfile.py -- load a show CSV (Map Grabber download, an Island Engine export, or any file
with a company-name and a sq-ft column) into the row schema, with its show name and year.

Generated data is refused outright: a file with any demo/generated row raises ShowFileError.
There is no override (see quality.demo_rows).
"""

from __future__ import annotations

import re

import pandas as pd

import quality
from extractors.common import ROW_COLUMNS, clean, year_from

NAME_COLS = ["exhibitor_name", "company", "company_name", "exhibitor", "name", "organization", "account"]
SQFT_COLS = ["sqft", "booth_sqft", "sq_ft", "square_feet", "booth_size_sqft", "size", "area", "prior_sqft"]


class ShowFileError(ValueError):
    pass


def _find(df: pd.DataFrame, candidates: list[str]) -> str | None:
    norm = {re.sub(r"[^a-z0-9]", "", c.lower()): c for c in df.columns}
    for cand in candidates:
        hit = norm.get(re.sub(r"[^a-z0-9]", "", cand))
        if hit:
            return hit
    return None


def load(file, filename: str | None = None) -> tuple[pd.DataFrame, dict]:
    name = filename or getattr(file, "name", "") or ""
    try:
        df = pd.read_csv(file, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    except Exception as exc:
        raise ShowFileError(f"{name}: couldn't read as CSV ({exc.__class__.__name__}).") from exc
    ncol, scol = _find(df, NAME_COLS), _find(df, SQFT_COLS)
    if not ncol or not scol:
        raise ShowFileError(f"{name}: needs a company-name column and a sq-ft column.")
    out = pd.DataFrame({c: df[c] if c in df.columns else "" for c in ROW_COLUMNS})
    out["exhibitor_name"] = df[ncol].map(clean)
    # A grabber file's `sqft` is the company's credited share; prefer it over booth_sqft.
    out["sqft"] = pd.to_numeric(df[scol].str.replace(",", ""), errors="coerce").fillna(0).round().astype(int)
    for c in ("booth_sqft", "booth_count", "shared_with"):
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).astype(int)
    for c in ("shared_booth", "is_sponsor", "has_video_listing"):
        out[c] = out[c].astype(str).str.lower().isin(["true", "1", "yes"])
    for c in ("width", "length"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out[out["exhibitor_name"] != ""].reset_index(drop=True)
    if out.empty:
        raise ShowFileError(f"{name}: no company rows.")
    fake = quality.demo_rows(out.to_dict("records"))
    if fake:
        raise ShowFileError(f"{name}: refused. It contains {len(fake)} generated/demo rows "
                            f"(e.g. {', '.join(fake[:3])}). Only real extracted data can be compared.")
    first = lambda col: next((v for v in df[col] if str(v).strip()), "") if col in df.columns else ""
    year_txt = first("show_year")
    meta = {
        "show_base": clean(first("show_name")) or None,
        "show_year": int(float(year_txt)) if re.fullmatch(r"\d{4}(\.0)?", str(year_txt)) else year_from(name),
        "platform": clean(first("platform")) or "csv",
        "source": "csv", "url": first("source_url"), "filename": name,
        "extracted_at": first("extracted_at"), "total": len(out),
        "sized": int((out["sqft"] > 0).sum()),
    }
    return out, meta
