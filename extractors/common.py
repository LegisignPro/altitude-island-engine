"""
Shared pieces for every platform extractor.

Each platform module turns its native payload into a list of *booth records* and hands them
to group_booths(), which produces one row per company in the ROW_COLUMNS schema. The same
logic runs in the browser grabber (grabber/grabber.js, groupBooths) so a CSV downloaded from
the grabber and a live server-side extraction give identical rows.

Booth record keys:
    key     grouping key (platform company id when there is one, else the normalised name)
    name    company name as the platform shows it
    booth   booth number
    hall    hall / level name ("" when the platform has none)
    area    booth area in sq ft (0 = the platform does not publish it)
    w, l    width / length in ft when the platform states them, else None
    raw     the size exactly as the source gave it ("20' x 20'", "20 x 30", "400 SqFt")
    shared  number of companies on this booth (1 = own booth)
    extra   dict of row columns to copy (website, city, exhid, ...)

Shared booths (pavilions, co-exhibitors): each company is credited area / companies in
`sqft`, and the whole stand size is kept in `booth_sqft`. A pavilion member therefore never
looks like an island, and a company that leaves a pavilion for its own 400+ sq ft booth
shows up as a Newborn Island in the year-over-year comparison, which is the right answer.
"""

from __future__ import annotations

import re

SQM_TO_SQFT = 10.7639

# The v2 schema every downstream module relies on (timeline, Apollo, freight, pitch) ...
BASE_COLUMNS = [
    "exhibitor_name", "booth_number", "width", "length", "sqft", "hall",
    "website", "is_sponsor", "has_video_listing", "exhid", "detail_url", "size_source",
]
# ... plus the v3 additions. Keep in step with COLUMNS in grabber/grabber.js.
V3_COLUMNS = [
    "platform", "booth_count", "shared_booth", "shared_with", "booth_sqft", "raw_size",
    "city", "state", "country", "company_linkedin", "phone", "description", "categories",
]
ROW_COLUMNS = BASE_COLUMNS + V3_COLUMNS
FILE_COLUMNS = ROW_COLUMNS + ["show_name", "show_year", "source_url", "extracted_at", "grabber_version"]

EXCLUDE_NAME_RE = re.compile(r"\b(pavilion|association|state of|department)\b", re.I)


class ExtractionError(Exception):
    """Plain-English reason an extraction could not run. Shown to the user as-is."""


def clean(text) -> str:
    return re.sub(r"\s+", " ", str("" if text is None else text)).strip()


def name_key(name) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(name).lower()).strip()


def num(value) -> float:
    m = re.search(r"-?\d+(?:\.\d+)?", str("" if value is None else value).replace(",", ""))
    return float(m.group(0)) if m else 0.0


_DIM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*'?\s*[x×]\s*(\d+(?:\.\d+)?)", re.I)


def dims(raw) -> tuple[float | None, float | None]:
    """"20' x 30'" / "20 x 30" -> (20.0, 30.0); anything else -> (None, None)."""
    m = _DIM_RE.search(str(raw or ""))
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def shoelace(points) -> float:
    """Area of a simple polygon given as [[x, y], ...]."""
    pts = [(float(p[0]), float(p[1])) for p in points or []]
    if len(pts) < 3:
        return 0.0
    s = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def year_from(text) -> int | None:
    m = re.search(r"\b(20\d{2})\b", str(text or ""))
    return int(m.group(1)) if m else None


def blank_row() -> dict:
    row = {c: "" for c in ROW_COLUMNS}
    row.update({"sqft": 0, "booth_count": 0, "shared_booth": False, "shared_with": 0, "booth_sqft": 0,
                "is_sponsor": False, "has_video_listing": False, "size_source": "unknown",
                "width": None, "length": None})
    return row


def _uniq(values) -> list:
    out = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


def group_booths(booths: list[dict], platform: str, size_source: str) -> list[dict]:
    """One row per company. See the module docstring for the booth-record shape."""
    groups: dict[str, dict] = {}
    for b in booths:
        key = b.get("key") or name_key(b.get("name"))
        if not key:
            continue
        groups.setdefault(key, {"name": b["name"], "list": []})["list"].append(b)
    rows = []
    for g in groups.values():
        lst = g["list"]
        biggest = max(lst, key=lambda b: b.get("area") or 0)
        shared = any((b.get("shared") or 1) > 1 for b in lst)
        credited = sum((b.get("area") or 0) / max(1, b.get("shared") or 1) for b in lst if (b.get("area") or 0) > 0)
        row = blank_row()
        row.update({
            "exhibitor_name": g["name"],
            "booth_number": ", ".join(_uniq(clean(b.get("booth")) for b in lst)),
            "hall": "; ".join(_uniq(clean(b.get("hall")) for b in lst)),
            "sqft": int(round(credited)),
            "booth_sqft": int(round(sum(b.get("area") or 0 for b in lst))),
            "booth_count": len(lst),
            "shared_booth": shared,
            "shared_with": max((b.get("shared") or 1) for b in lst) if shared else 0,
            "width": biggest.get("w") or None,
            "length": biggest.get("l") or None,
            "raw_size": " + ".join(r for r in (clean(b.get("raw")) for b in lst) if r),
            "platform": platform,
        })
        row["size_source"] = size_source if row["sqft"] > 0 else "unknown"
        for k, v in (lst[0].get("extra") or {}).items():
            if v is not None:
                row[k] = v
        rows.append(row)
    return rows


def apply_name_filter(rows: list[dict]) -> list[dict]:
    """Drop pavilions, associations, government stands (same rule as v1/v2)."""
    return [r for r in rows if not EXCLUDE_NAME_RE.search(r.get("exhibitor_name") or "")]


def finish(rows: list[dict]) -> list[dict]:
    rows = apply_name_filter(rows)
    rows.sort(key=lambda r: (-(r.get("sqft") or 0), (r.get("exhibitor_name") or "").lower()))
    return rows
