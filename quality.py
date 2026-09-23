"""
quality.py -- Extraction Quality checks. Runs on every extraction and every loaded CSV.

check(rows, meta) -> {"grade": "PASS" | "WARN" | "FAIL", "findings": [{check, level, note}], "notes": str}

    Check               FAIL if                                   WARN if
    Row count           < 10 companies                            10-49
    Sized share         < 40% of rows have sqft > 0               40-70%
    Area vs dimensions  -                                         > 5% of single-booth rows: w x l off by > 5%
    Standard sizes      -                                         < 50% of single-booth rows a multiple of 50 sq ft
    Implausible sizes   a single booth > 50,000 sq ft             a single booth < 50 sq ft
    Coverage            < 80% of the platform's own count         80-95%
    Duplicates          -                                         same normalised name on > 1 row
    Demo leakage        ANY demo / generated row                  -
    Year consistency    -                                         chosen year != year the source states
    Island sanity       -                                         0 companies >= 400 sq ft with 200+ companies

Demo leakage is always FAIL and can never be overridden: generated data must not reach a comparison
or an email list (Woz, 2026-09-23). It catches size_source "demo", exhid "demo...", and the
invented company names that the old fallback dataset and fallback_harvester.py produced.
"""

from __future__ import annotations

import re
from collections import Counter

from extractors.common import name_key

LEVELS = {"PASS": 0, "WARN": 1, "FAIL": 2}

# Invented names used by v1/v2's fallback dataset and by fallback_harvester.py. Never real exhibitors.
KNOWN_FAKE_NAMES = {name_key(n) for n in [
    "Lumen Audio Labs", "Vantage Robotics Systems", "Kestrel Aerial Cinema", "Northbridge Signal Systems",
    "Helios Studio Lighting", "Orbit Wireless Video", "Clearwave Networking", "Summit Streaming Platforms",
    "Aurora Display Technologies", "Pinnacle Newsroom Software", "Redrock Podcast Gear",
    "Tidewater Satellite Uplink", "Beacon Intercom Systems", "Stratos Media AI", "Copperline Audio",
    "Evergreen Battery Co.", "Nimbus Cloud Cameras", "Ironwood Rugged Computing", "Skyline Virtual Production",
    "Meridian Captioning", "Helix Broadcast Equipment", "Spectrum Digital Audio",
]}
_FAKE_SUFFIX = re.compile(r"\((?:[A-Z0-9&]+) Division\)$")   # fallback_harvester.py's "Name (SEMA Division)" pattern


def demo_rows(rows: list[dict]) -> list[str]:
    """Names of rows that are generated / demo data."""
    bad = []
    for r in rows:
        name = str(r.get("exhibitor_name") or "")
        if (str(r.get("size_source") or "").lower() == "demo"
                or str(r.get("exhid") or "").lower().startswith("demo")
                or name_key(re.sub(r"\s*\([^)]*Division\)$", "", name)) in KNOWN_FAKE_NAMES
                or _FAKE_SUFFIX.search(name)):
            bad.append(name)
    return bad


def _single(rows):
    return [r for r in rows if (r.get("booth_count") in (1, "1", None, "") and not r.get("shared_booth"))
            and (r.get("sqft") or 0) > 0]


def check(rows: list[dict], meta: dict | None = None, chosen_year: int | None = None) -> dict:
    meta = meta or {}
    findings = []

    def add(check_name, level, note):
        findings.append({"check": check_name, "level": level, "note": note})

    n = len(rows)
    add("Row count", "FAIL" if n < 10 else "WARN" if n < 50 else "PASS", f"{n} companies")

    sized = sum(1 for r in rows if (r.get("sqft") or 0) > 0)
    share = sized / n if n else 0
    add("Sized share", "FAIL" if share < 0.4 else "WARN" if share < 0.7 else "PASS",
        f"{sized} of {n} ({share:.0%}) have a booth size")

    single = _single(rows)
    off = 0
    for r in single:
        try:
            w, l = float(r.get("width") or 0), float(r.get("length") or 0)
        except (TypeError, ValueError):
            continue
        if w and l and abs(w * l - r["sqft"]) > 0.05 * r["sqft"]:
            off += 1
    pct = off / len(single) if single else 0
    add("Area vs dimensions", "WARN" if pct > 0.05 else "PASS", f"{off} single-booth rows where width x length disagrees with sq ft")

    std = sum(1 for r in single if round(r["sqft"]) % 50 == 0)
    std_pct = std / len(single) if single else 1
    add("Standard sizes", "WARN" if single and std_pct < 0.5 else "PASS",
        f"{std_pct:.0%} of single booths are a multiple of 50 sq ft" + (" (unit error?)" if std_pct < 0.5 else ""))

    booth_sizes = [(r.get("booth_sqft") or r.get("sqft") or 0) for r in single]
    huge = sum(1 for s in booth_sizes if s > 50000)
    tiny = sum(1 for s in booth_sizes if 0 < s < 50)
    add("Implausible sizes", "FAIL" if huge else "WARN" if tiny else "PASS",
        f"{huge} booths over 50,000 sq ft, {tiny} under 50 sq ft")

    pc = meta.get("platform_count") or 0
    if pc:
        cov = n / pc
        add("Coverage", "FAIL" if cov < 0.8 else "WARN" if cov < 0.95 else "PASS",
            f"{n} of the platform's {pc} listed exhibitors ({cov:.0%})")
    else:
        add("Coverage", "PASS", "platform count not available (CSV or older export)")

    dup = [k for k, c in Counter(name_key(r.get("exhibitor_name")) for r in rows).items() if k and c > 1]
    add("Duplicates", "WARN" if dup else "PASS", f"{len(dup)} names on more than one row")

    fake = demo_rows(rows)
    add("Demo leakage", "FAIL" if fake else "PASS",
        f"{len(fake)} generated/demo rows (e.g. {', '.join(fake[:3])})" if fake else "no generated rows")

    stated = meta.get("show_year")
    if chosen_year and stated and int(chosen_year) != int(stated):
        add("Year consistency", "WARN", f"you chose {chosen_year}, the source says {stated}")
    else:
        add("Year consistency", "PASS", f"source states {stated}" if stated else "source states no year")

    islands = sum(1 for r in rows if (r.get("sqft") or 0) >= 400)
    add("Island sanity", "WARN" if n >= 200 and islands == 0 else "PASS", f"{islands} companies at 400+ sq ft")

    grade = max((f["level"] for f in findings), key=lambda lv: LEVELS[lv]) if findings else "PASS"
    notes = "; ".join(f"{f['check']}: {f['note']}" for f in findings if f["level"] != "PASS")
    return {"grade": grade, "findings": findings, "notes": notes, "demo": bool(fake)}
