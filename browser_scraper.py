"""
browser_scraper.py -- Playwright network-interception scraper for the floor-plan platforms that
render exhibitor/booth data as canvas or SVG instead of HTML: A2Z (Personify), EXPOCAD, and
ExpoFP. Because the data never appears in the page's markup, this captures the JSON the page's
own JavaScript fetches from the network layer, before it gets drawn.

IMPORTANT -- field names are hypotheses, not verified facts. Woz's brief named plausible field
names for each platform (AreaSqFt, BoothNumber, CompanyId, ...), but this container's egress is
restricted to package registries and GitHub -- it cannot reach a2zinc.net, expocad.com, or
expofp.com to confirm them against a real show. Every normaliser below is alias-tolerant (see
`pick()`) so it can absorb small naming differences, but it may still need tuning once run against
a real payload. That is what the `capture` CLI at the bottom of this file is for:

    python browser_scraper.py capture <url> --out captures/<slug>.json
    python browser_scraper.py capture <url> --normalise a2z

Run that from a machine that CAN reach the target site (not this container), then adjust the
alias lists in pick(...) calls below to match what actually came back.

Nothing here invents a show name or footprint: a booth whose area cannot be determined is marked
sqft=0 / size_source="unknown" rather than guessed, exactly like scraper.py's MapYourShow client.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import scraper  # ScrapeError, apply_name_filter, EXHIBITOR_COLUMNS

CAPTURE_TIMEOUT_S = 45
SETTLE_EXTRA_S = 3   # extra wait after "networkidle" for lazy/late API calls

# Case-insensitive signature keys: a JSON response body must contain at least one of these
# (as a raw substring, before parsing) to be kept as a candidate booth/exhibitor payload.
SIGNATURE_KEYS = ["areasqft", "boothnumber", "companyid", "exhibitorid", "boothid", "exhibitors",
                  "booths", "\"area\"", "\"width\"", "\"length\""]


class BrowserUnavailable(scraper.ScrapeError):
    """Playwright or Chromium could not be launched on this host."""


# =============================================================================
# Network interception
# =============================================================================

def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Browser scraping is unavailable on this host -- run the app locally or use the "
            "capture CLI (python browser_scraper.py capture <url>)."
        ) from exc


def _is_jsonish_response(response) -> bool:
    try:
        ctype = (response.headers or {}).get("content-type", "")
    except Exception:
        ctype = ""
    if "json" in ctype.lower():
        return True
    url = response.url.lower()
    return bool(re.search(r"\.(json|ashx)(\?|$)", url)) or "/api/" in url


def capture_json_responses(url: str, timeout_s: int = CAPTURE_TIMEOUT_S, log=None) -> list[dict]:
    """
    Launch headless Chromium, attach a response listener BEFORE navigating, and return every JSON
    response whose body contains one of SIGNATURE_KEYS. Each entry: {"url", "status", "json"}.

    Runs Playwright's sync API inside a worker thread: Streamlit's script thread has no asyncio
    event loop, and a fresh thread sidesteps that instead of fighting it. Always closes the
    browser, even on error, so a failed capture never leaks a Chromium process.
    """
    log = log or (lambda msg: None)
    sync_playwright = _import_playwright()

    def _run() -> list[dict]:
        captures: list[dict] = []
        seen = 0
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()

                def _on_response(response):
                    nonlocal seen
                    seen += 1
                    if not _is_jsonish_response(response):
                        return
                    try:
                        body = response.text()
                    except Exception:
                        return
                    if not body:
                        return
                    low = body.lower()
                    if not any(k in low for k in SIGNATURE_KEYS):
                        return
                    try:
                        parsed = json.loads(body)
                    except ValueError:
                        return
                    captures.append({"url": response.url, "status": response.status, "json": parsed})

                page.on("response", _on_response)
                page.goto(url, wait_until="networkidle", timeout=timeout_s * 1000)
                page.wait_for_timeout(SETTLE_EXTRA_S * 1000)
            finally:
                browser.close()
        log(f"Captured {len(captures)} matching JSON response(s) out of {seen} total requests")
        return captures

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            return pool.submit(_run).result(timeout=timeout_s + SETTLE_EXTRA_S + 15)
        except BrowserUnavailable:
            raise
        except Exception as exc:
            raise scraper.ScrapeError(f"Browser capture failed: {exc.__class__.__name__}: {exc}") from exc


def ensure_browser() -> bool:
    """
    Best-effort one-time Chromium check/install for a host (e.g. Streamlit Community Cloud) that
    may not ship it. No-ops quickly once a marker file confirms Chromium already launches (the
    common case here and on Woz's own machine, both pre-installed). Never raises -- returns
    whether a browser is usable; capture_json_responses() raises its own clear error if not.
    """
    marker = os.path.join(tempfile.gettempdir(), ".island_engine_playwright_checked")
    if os.path.exists(marker):
        return open(marker).read().strip() == "ok"
    ok = False
    try:
        sync_playwright = _import_playwright()
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            b.close()
        ok = True
    except Exception:
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                           check=False, timeout=180, capture_output=True)
            sync_playwright = _import_playwright()
            with sync_playwright() as pw:
                b = pw.chromium.launch(headless=True)
                b.close()
            ok = True
        except Exception:
            ok = False
    try:
        with open(marker, "w") as f:
            f.write("ok" if ok else "failed")
    except OSError:
        pass
    return ok


# =============================================================================
# Field-matching helpers shared by every normaliser
# =============================================================================

def pick(record, *aliases: str):
    """Case-insensitive, alias-tolerant field lookup on one JSON record (dict)."""
    if not isinstance(record, dict):
        return None
    lower = {str(k).lower(): v for k, v in record.items()}
    for a in aliases:
        if a.lower() in lower:
            return lower[a.lower()]
    return None


def num(v, default=None):
    """Parse a number that may arrive as an int, float, or a string like '400 sq ft'."""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        m = re.search(r"[-+]?\d*\.?\d+", str(v))
        return float(m.group()) if m else default


def polygon_area(points) -> float:
    """Shoelace formula. `points`: a list of (x, y) pairs, [x, y] lists, or {'x':..,'y':..} dicts."""
    pts = []
    for p in points or []:
        if isinstance(p, dict):
            x, y = pick(p, "x"), pick(p, "y")
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            x, y = p[0], p[1]
        else:
            continue
        x, y = num(x), num(y)
        if x is not None and y is not None:
            pts.append((x, y))
    if len(pts) < 3:
        return 0.0
    area = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _iter_record_arrays(obj):
    """Yield every non-empty list-of-dicts found anywhere in a nested JSON structure."""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            yield obj
        for item in obj:
            yield from _iter_record_arrays(item)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_record_arrays(v)


def all_record_arrays(captures: list[dict]) -> list[list[dict]]:
    out = []
    for cap in captures:
        out.extend(_iter_record_arrays(cap.get("json")))
    return out


def _largest_matching(arrays: list[list[dict]], *required_alias_groups: tuple[str, ...]) -> list[dict]:
    """The largest record array whose first record has a hit for every alias group given."""
    best: list[dict] = []
    for arr in arrays:
        sample = arr[0]
        if all(pick(sample, *grp) is not None for grp in required_alias_groups) and len(arr) > len(best):
            best = arr
    return best


# =============================================================================
# A2Z / Personify
# =============================================================================

def normalize_a2z(captures: list[dict]) -> list[dict]:
    """
    Booth records: BoothNumber + (AreaSqFt | Width/Length | Sqft) + CompanyId/ExhibitorId.
    Exhibitor records: CompanyId/ExhibitorId + CompanyName (+ Website). Joined on the company id;
    a company with several booths has its area summed, width/length taken from the largest booth.
    """
    arrays = all_record_arrays(captures)
    booth_records = _largest_matching(
        arrays, ("boothnumber", "booth_number", "booth"), ("companyid", "exhibitorid", "exhid"))
    exh_records = _largest_matching(
        arrays, ("companyname", "exhibitorname", "exhname"), ("companyid", "exhibitorid", "exhid"))

    exh_by_id = {}
    for r in exh_records:
        cid = str(pick(r, "companyid", "exhibitorid", "exhid") or "").strip()
        if cid:
            exh_by_id[cid] = {"name": str(pick(r, "companyname", "exhibitorname", "exhname") or "").strip(),
                              "website": str(pick(r, "website", "websiteurl", "url") or "").strip()}

    booths_by_company: dict[str, list[dict]] = {}
    for r in booth_records:
        cid = str(pick(r, "companyid", "exhibitorid", "exhid") or "").strip()
        if not cid:
            continue
        area = num(pick(r, "areasqft", "area", "sqft"))
        width = num(pick(r, "width", "boothwidth"))
        length = num(pick(r, "length", "depth", "boothlength", "boothdepth"))
        if area is None and width and length:
            area = width * length
        booths_by_company.setdefault(cid, []).append({
            "booth_number": str(pick(r, "boothnumber", "booth_number", "booth") or "").strip(),
            "area": area or 0.0, "width": width, "length": length,
            "hall": str(pick(r, "hall", "hallname") or "").strip(),
        })

    rows = []
    for cid, booths in booths_by_company.items():
        name = exh_by_id.get(cid, {}).get("name", "")
        if not name:
            continue
        biggest = max(booths, key=lambda b: b["area"])
        rows.append({
            "exhibitor_name": name,
            "booth_number": ", ".join(sorted({b["booth_number"] for b in booths if b["booth_number"]})),
            "width": biggest["width"], "length": biggest["length"],
            "sqft": int(round(sum(b["area"] for b in booths))),
            "hall": "; ".join(sorted({b["hall"] for b in booths if b["hall"]})),
            "website": exh_by_id.get(cid, {}).get("website", ""),
            "is_sponsor": False, "has_video_listing": False,
            "exhid": cid, "detail_url": "",
            "size_source": "a2z-json" if any(b["area"] for b in booths) else "unknown",
        })
    return scraper.apply_name_filter(rows)


# =============================================================================
# EXPOCAD
# =============================================================================

def normalize_expocad(captures: list[dict]) -> list[dict]:
    """
    Booth records: BoothID + (Area, a string like "400 sq ft" or a bare number | Width/Length |
    polygon vertices under Points/Vertices/Polygon). Units: used as declared if the payload says
    feet; if the polygon result is implausible as square feet (<20 or >20000), retried as inches
    (/144) before falling back to "unknown units" rather than guessing. Joined to the exhibitor
    payload on BoothID/BoothNumber.
    """
    arrays = all_record_arrays(captures)
    booth_records = _largest_matching(
        arrays, ("boothid", "booth_id", "boothnumber"),
    )
    booth_records = [r for r in booth_records
                     if pick(r, "area", "width", "points", "vertices", "polygon") is not None] or booth_records
    exh_records = _largest_matching(
        arrays, ("boothid", "booth_id", "boothnumber"), ("companyname", "exhibitorname", "name"))

    exh_by_booth = {}
    for r in exh_records:
        bid = str(pick(r, "boothid", "booth_id", "boothnumber", "booth") or "").strip()
        if bid:
            exh_by_booth[bid] = {"name": str(pick(r, "companyname", "exhibitorname", "name") or "").strip(),
                                 "website": str(pick(r, "website", "websiteurl", "url") or "").strip()}

    rows = []
    for r in booth_records:
        bid = str(pick(r, "boothid", "booth_id", "boothnumber", "booth") or "").strip()
        exh = exh_by_booth.get(bid, {})
        name = exh.get("name", "")
        if not bid or not name:
            continue
        width = num(pick(r, "width", "boothwidth"))
        length = num(pick(r, "length", "depth", "boothlength", "boothdepth"))
        area = num(pick(r, "area"))
        size_source = "expocad-json"
        if area is None and width and length:
            area = width * length
        if area is None:
            pts = pick(r, "points", "vertices", "polygon")
            if pts:
                raw = polygon_area(pts)
                if raw and 20 <= raw <= 20000:
                    area = raw
                elif raw and 20 <= raw / 144 <= 20000:
                    area = raw / 144   # payload was likely in inches
                elif raw:
                    area, size_source = 0, "expocad-unknown-units"
        rows.append({
            "exhibitor_name": name,
            "booth_number": str(pick(r, "boothnumber", "booth_number", "booth") or bid),
            "width": width, "length": length,
            "sqft": int(round(area)) if area else 0,
            "hall": str(pick(r, "hall", "hallname") or "").strip(),
            "website": exh.get("website", ""),
            "is_sponsor": False, "has_video_listing": False,
            "exhid": bid, "detail_url": "",
            "size_source": size_source if area else "unknown",
        })
    return scraper.apply_name_filter(rows)


# =============================================================================
# ExpoFP
# =============================================================================

def normalize_expofp(captures: list[dict]) -> list[dict]:
    """
    One monolithic config JSON with root arrays `booths` and `exhibitors`. Each booth carries a
    PRE-COMPUTED `area` (used as-is, never recomputed) and an exhibitor id or id list. Booth area
    is summed per exhibitor for exhibitors holding more than one booth.
    """
    booths, exhibitors = [], []
    for cap in captures:
        obj = cap.get("json")
        if isinstance(obj, dict):
            b, e = pick(obj, "booths"), pick(obj, "exhibitors")
            if isinstance(b, list) and len(b) > len(booths):
                booths = b
            if isinstance(e, list) and len(e) > len(exhibitors):
                exhibitors = e
    if not booths or not exhibitors:
        arrays = all_record_arrays(captures)
        if not booths:
            booths = _largest_matching(arrays, ("area",), ("exhibitors", "exhibitorid", "exhibitorids"))
        if not exhibitors:
            exhibitors = _largest_matching(arrays, ("name", "companyname"), ("id", "exhibitorid"))

    exh_by_id = {}
    for r in exhibitors:
        eid = str(pick(r, "id", "exhibitorid") or "").strip()
        if eid:
            exh_by_id[eid] = {"name": str(pick(r, "name", "companyname") or "").strip(),
                              "website": str(pick(r, "website", "url") or "").strip()}

    per_exh: dict[str, list[dict]] = {}
    for b in booths:
        area = num(pick(b, "area"))   # pre-computed by ExpoFP -- never recompute from geometry
        ids = pick(b, "exhibitors", "exhibitorids")
        if ids is None:
            single = pick(b, "exhibitorid", "exhibitor_id")
            ids = [single] if single is not None else []
        if not isinstance(ids, list):
            ids = [ids]
        for eid in ids:
            eid = str(eid).strip()
            if not eid:
                continue
            per_exh.setdefault(eid, []).append({
                "area": area or 0.0,
                "booth_number": str(pick(b, "number", "boothnumber", "name") or "").strip(),
                "hall": str(pick(b, "hall", "zone") or "").strip(),
            })

    rows = []
    for eid, blist in per_exh.items():
        name = exh_by_id.get(eid, {}).get("name", "")
        if not name:
            continue
        rows.append({
            "exhibitor_name": name,
            "booth_number": ", ".join(sorted({b["booth_number"] for b in blist if b["booth_number"]})),
            "width": None, "length": None,
            "sqft": int(round(sum(b["area"] for b in blist))),
            "hall": "; ".join(sorted({b["hall"] for b in blist if b["hall"]})),
            "website": exh_by_id.get(eid, {}).get("website", ""),
            "is_sponsor": False, "has_video_listing": False,
            "exhid": eid, "detail_url": "", "size_source": "expofp-json",
        })
    return scraper.apply_name_filter(rows)


NORMALIZERS = {"a2z": normalize_a2z, "expocad": normalize_expocad, "expofp": normalize_expofp}


# =============================================================================
# Per-platform extraction entry points (used by platforms.py)
# =============================================================================

def _extract(url: str, platform: str, log=None) -> tuple[list[dict], dict]:
    log = log or (lambda msg: None)
    ensure_browser()
    captures = capture_json_responses(url, log=log)
    if not captures:
        raise scraper.ScrapeError(
            f"No {platform.upper()} booth/exhibitor JSON was captured from this page. It may render "
            f"after a longer delay, or the field-name hypotheses in browser_scraper.py may not match "
            f"this show -- run `python browser_scraper.py capture {url}` from a machine that can "
            f"reach this site to inspect the real payloads."
        )
    rows = NORMALIZERS[platform](captures)
    if not rows:
        raise scraper.ScrapeError(
            f"{platform.upper()} JSON was captured ({len(captures)} response(s)) but no rows matched "
            f"the expected field names. Run the capture CLI with --normalise {platform} to see what "
            f"came back and tune the aliases in browser_scraper.py."
        )
    rows.sort(key=lambda r: (-r["sqft"], r["exhibitor_name"].lower()))
    log(f"{platform.upper()}: {len(rows)} exhibitors normalised")
    meta = {
        # Unknown until real captures reveal where (if anywhere) each platform states the show's
        # own name -- never guessed. The sidebar / timeline slot's own explicit year field is the
        # source of truth for this extraction's year, same as MapYourShow (Task 1).
        "show_name": "Trade Show", "show_base": "Trade Show", "show_year": None,
        "source": "live", "platform": platform, "url": url,
        "halls": len({r["hall"] for r in rows if r["hall"]}), "hall_errors": 0,
        "sized": sum(1 for r in rows if r["sqft"]), "total": len(rows),
    }
    return rows, meta


def extract_a2z(url: str, log=None) -> tuple[list[dict], dict]:
    return _extract(url, "a2z", log=log)


def extract_expocad(url: str, log=None) -> tuple[list[dict], dict]:
    return _extract(url, "expocad", log=log)


def extract_expofp(url: str, log=None) -> tuple[list[dict], dict]:
    return _extract(url, "expofp", log=log)


# =============================================================================
# Capture-first CLI -- run this against a REAL show from a machine that can reach it, since this
# container's egress does not allow a2zinc.net / expocad.com / expofp.com.
# =============================================================================

def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Capture-first workflow for tuning the A2Z/EXPOCAD/ExpoFP normalisers against a real show.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cap = sub.add_parser("capture", help="Capture every matching JSON response from a URL.")
    cap.add_argument("url")
    cap.add_argument("--out", default=None, help="Write the raw captures to this JSON file.")
    cap.add_argument("--normalise", choices=list(NORMALIZERS), default=None,
                     help="Also run this platform's normaliser and print a row-count summary.")
    args = parser.parse_args()

    if args.cmd == "capture":
        captures = capture_json_responses(args.url, log=print)
        print(f"\n{len(captures)} matching response(s):")
        for c in captures:
            body = c["json"]
            shape = list(body.keys()) if isinstance(body, dict) else f"<list of {len(body)}>"
            first = (body[0] if isinstance(body, list) and body else
                    (next(iter(body.values())) if isinstance(body, dict) and body else None))
            print(f"- {c['url']}  [{c['status']}]  top-level: {shape}")
            if first is not None:
                print(f"    first record sample: {json.dumps(first, default=str)[:300]}")
        if args.out:
            os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
            with open(args.out, "w") as f:
                json.dump(captures, f, indent=2, default=str)
            print(f"\nWrote {args.out}")
        if args.normalise:
            rows = NORMALIZERS[args.normalise](captures)
            print(f"\n{args.normalise} normaliser: {len(rows)} rows")
            for r in rows[:5]:
                print(" ", {k: r[k] for k in ("exhibitor_name", "booth_number", "sqft", "size_source")})


if __name__ == "__main__":
    _cli()
