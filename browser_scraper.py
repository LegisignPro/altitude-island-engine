"""
browser_scraper.py -- Playwright network-interception scraper for the floor-plan platforms that
render exhibitor/booth data as canvas or SVG instead of HTML: A2Z (Personify), EXPOCAD, and
ExpoFP. Because the data never appears in the page's markup, this captures the JSON the page's
own JavaScript fetches from the network layer, before it gets drawn.

IMPORTANT -- field names are hypotheses, not verified facts. Woz's brief named plausible field
names for each platform (AreaSqFt, BoothNumber, CompanyId, ...), but this container's egress is
restricted to package registries and GitHub -- it cannot reach a2zinc.net, expocad.com, or
expofp.com to confirm them against a real show. The first live run (swe.expocad.com, WE26)
captured five JSON responses and produced ZERO rows because the normaliser demanded an exact
alias hit on every field of one assumed shape. The normalisers are therefore built to BEND:
roles (name, booth number, area, ...) are resolved per record array with fuzzy key matching and
value sanity checks, booth/exhibitor joins are discovered from actual value overlap rather than
assumed, and when only part of the picture can be read the result is a partial, honestly-labelled
row set -- never zero rows without an explanation of what was seen. See the "Field-matching
helpers" section for the design. For deep debugging there is still the capture CLI:

    python browser_scraper.py capture <url> --out captures/<slug>.json
    python browser_scraper.py capture <url> --normalise expocad

Run that from a machine that CAN reach the target site (not this container), then extend
ROLE_ALIASES below if a real payload uses a spelling the fuzzy matcher still misses.

Nothing here invents a show name or footprint: a booth whose area cannot be determined is marked
sqft=0 / size_source="unknown" rather than guessed, exactly like scraper.py's MapYourShow client,
and a footprint read through a fuzzy field match is tagged size_source="<platform>-json-fuzzy".
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field as _field

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
#
# Design (post-WE26): the three platform normalisers used to require an EXACT alias hit on every
# field, on the SAME record, for a join to work at all -- and returned zero rows the moment a real
# show's JSON differed from the assumed shape (confirmed live on swe.expocad.com/Events/we26: five
# JSON responses captured, zero rows). The engine below replaces that with:
#
#   1. Role resolution per record ARRAY, not per record: the union of key paths across a sample of
#      records is matched against ROLE_ALIASES in confidence tiers (exact -> nested leaf ->
#      contains -> fuzzy edit distance), and every candidate is sanity-checked against the actual
#      VALUES (an "area" whose values are not numbers is not an area). Nested objects are searched
#      too (`booth.exhibitor.name`), as are lists of sub-records (`booth.exhibitors[].name`).
#   2. Data-driven join discovery: instead of assuming which id links booths to exhibitors, every
#      id-shaped field on one array is tested for VALUE OVERLAP with every id-shaped field on the
#      other, and the pair that actually shares values wins. A join key that differs from the id
#      used elsewhere in the payload is therefore fine.
#   3. Structural fallbacks: strict join -> single self-contained array (booth + exhibitor fields on
#      one record) -> best-effort (any array with a name field, sqft=0, size_source="unknown").
#   4. Confidence-graded output: size_source is "<platform>-json" only when every field that fed the
#      footprint matched cleanly; "<platform>-json-fuzzy" when a fuzzy/contains match contributed;
#      "unknown" when no footprint could be read. NOTHING is ever invented: an unlabelled numeric
#      column is never treated as an area, and a booth with no readable footprint is 0 sq ft.
#   5. Diagnostics: the normaliser reports what shapes it saw, which roles resolved to which keys,
#      and which strategy it used, through the same log callback the app already displays.
# =============================================================================

MAX_PROFILES = 40          # largest record arrays considered per extraction
SAMPLE_RECORDS = 60        # records inspected per array when resolving roles / sanity-checking values
JOIN_SAMPLE = 2000         # records per side when testing value overlap for join discovery
JOIN_MIN_OVERLAP = 0.3     # share of the smaller id set that must appear on the other side
GENERIC_JOIN_MIN_OVERLAP = 0.5
FUZZY_RATIO = 0.8

# Role -> aliases in PRIORITY order (normalised: lowercase, no separators). The first alias with a
# sane hit wins, so put the most specific spellings first and the generic ones last.
ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("companyname", "exhibitorname", "exhname", "displayname", "organizationname", "orgname",
             "company", "exhibitor", "name", "title"),
    "booth_number": ("boothnumber", "boothno", "boothnum", "boothlabel", "standnumber", "standno",
                     "booth", "stand", "number", "label"),
    "booth_id": ("boothid", "standid", "bid"),
    "company_id": ("companyid", "exhibitorid", "exhid", "exhibitorref", "companyref", "exhref", "exhibitorkey",
                   "companykey", "organizationid", "orgid", "cid", "eid", "id", "key"),
    "exhibitor_ids": ("exhibitorids", "companyids", "exhids", "exhibitors"),
    "area": ("areasqft", "areasqfeet", "sqft", "squarefeet", "squarefootage", "boothsqft", "totalsqft",
             "sqfeet", "availboothsqfeet", "boothsize", "area", "size"),
    "width": ("width", "boothwidth", "widthft", "w"),
    "length": ("length", "depth", "boothlength", "boothdepth", "lengthft", "depthft", "l", "d"),
    "polygon": ("points", "vertices", "polygon", "coords", "coordinates", "outline", "shape", "path"),
    "website": ("website", "websiteurl", "weburl", "webaddress", "homepage", "exhweb", "url", "web"),
    "hall": ("hall", "hallname", "zone", "building", "pavilion", "floor"),
}
# Aliases too generic to trust for join discovery until nothing better exists (a booth's own "id"
# often overlaps numerically with exhibitor ids by coincidence).
GENERIC_ALIASES = {"id", "key", "name", "number", "label", "title", "size", "w", "l", "d", "web", "path", "shape"}
# Short aliases that are still distinctive enough to match as a substring of a longer key.
DISTINCTIVE_SHORT = {"sqft", "sqfeet", "hall"}
ID_ROLES = ("company_id", "booth_id", "booth_number", "exhibitor_ids")
SIZE_ROLES = ("area", "width", "length", "polygon")
DIMS_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:ft|')?\s*[xX×]\s*(\d+(?:\.\d+)?)")


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
    if v is None or v == "" or isinstance(v, bool):
        return default
    if isinstance(v, (list, dict)):
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


def norm_key(k) -> str:
    """'Booth_Number' / 'boothNumber' / 'BOOTH NUMBER' -> 'boothnumber'."""
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


# -----------------------------------------------------------------------------
# Record-array discovery: every list-of-dicts anywhere in the payload, PLUS two wrappers the plain
# walk would miss -- a dict keyed by id whose values are all records ({"B1": {...}, "B2": {...}}),
# and a columns+rows table ({"columns": [...], "rows": [[...], ...]}).
# -----------------------------------------------------------------------------

_TABLE_COLS = ("columns", "fields", "headers", "cols", "keys")
_TABLE_ROWS = ("rows", "data", "values", "items", "records")


def _table_to_records(obj: dict) -> list[dict] | None:
    cols = pick(obj, *_TABLE_COLS)
    rows = pick(obj, *_TABLE_ROWS)
    if not (isinstance(cols, list) and cols and all(isinstance(c, str) for c in cols)):
        return None
    if not (isinstance(rows, list) and rows and all(isinstance(r, list) for r in rows)):
        return None
    if not all(len(r) == len(cols) for r in rows[:20]):
        return None
    return [dict(zip(cols, r)) for r in rows if len(r) == len(cols)]


def _iter_record_arrays_with_paths(obj, path: str = "$"):
    """Yield (json_path, list_of_dicts) for every record array found anywhere in `obj`."""
    if isinstance(obj, list):
        is_records = bool(obj) and all(isinstance(x, dict) for x in obj)
        if is_records:
            yield path, obj
        # Descend into the items of a genuine record array only when it looks like a WRAPPER (a
        # handful of records) -- the nested lists of a 2,000-booth array are reached through key
        # paths during profiling, and treating each as its own array would explode the join search.
        if not is_records or len(obj) <= 3:
            for i, item in enumerate(obj):
                if isinstance(item, (list, dict)):
                    yield from _iter_record_arrays_with_paths(item, f"{path}[{i}]")
    elif isinstance(obj, dict):
        table = _table_to_records(obj)
        if table:
            yield f"{path}{{columns+rows}}", table
        vals = list(obj.values())
        if len(vals) >= 2 and all(isinstance(v, dict) for v in vals):
            # dict keyed by id -> records; keep the key on each record so it can serve as an id
            yield f"{path}{{keyed}}", [dict(v, **{"_key": k}) if "_key" not in v else v for k, v in obj.items()]
        for k, v in obj.items():
            if isinstance(v, (list, dict)):
                yield from _iter_record_arrays_with_paths(v, f"{path}.{k}")


def _iter_record_arrays(obj):
    """Yield every non-empty list-of-dicts found anywhere in a nested JSON structure."""
    for _, arr in _iter_record_arrays_with_paths(obj):
        yield arr


def all_record_arrays(captures: list[dict]) -> list[list[dict]]:
    out = []
    for cap in captures:
        out.extend(_iter_record_arrays(cap.get("json")))
    return out


def _largest_matching(arrays: list[list[dict]], *required_alias_groups: tuple[str, ...]) -> list[dict]:
    """The largest record array whose first record has a hit for every alias group given.
    (Legacy strict matcher, kept for callers/tests; the normalisers now use ArrayProfile.)"""
    best: list[dict] = []
    for arr in arrays:
        sample = arr[0]
        if all(pick(sample, *grp) is not None for grp in required_alias_groups) and len(arr) > len(best):
            best = arr
    return best


# -----------------------------------------------------------------------------
# Key paths inside one record (nested objects and lists of sub-records included)
# -----------------------------------------------------------------------------

MAX_DEPTH = 2


def _leaf_paths(record: dict, prefix: tuple = (), depth: int = 0):
    for k, v in record.items():
        p = prefix + (str(k),)
        if isinstance(v, dict) and v and depth < MAX_DEPTH:
            yield from _leaf_paths(v, p, depth + 1)
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v) and depth < MAX_DEPTH:
            yield p, v      # the list itself (a polygon of {x,y} points, or an id list)
            yield from _leaf_paths(v[0], p + ("[]",), depth + 1)   # ...and its sub-record shape
        else:
            yield p, v


def _ci_get(d: dict, key: str):
    if key in d:
        return d[key]
    lk = key.lower()
    for k, v in d.items():
        if str(k).lower() == lk:
            return v
    return None


def get_path(record, path: tuple):
    """Value at a key path; a "[]" segment maps the remainder over a list of sub-records."""
    cur = record
    for i, seg in enumerate(path):
        if seg == "[]":
            if not isinstance(cur, list):
                return None
            rest = path[i + 1:]
            return [get_path(x, rest) for x in cur if isinstance(x, dict)]
        if not isinstance(cur, dict):
            return None
        cur = _ci_get(cur, seg)
        if cur is None:
            return None
    return cur


def _path_label(path: tuple) -> str:
    return ".".join(path)


def _mapped(fm) -> bool:
    """True when the field is reached THROUGH a list of sub-records (one record -> many values)."""
    return bool(fm) and "[]" in fm.path


def _joined(path: tuple) -> str:
    return norm_key("".join(seg for seg in path if seg != "[]"))


def _leaf(path: tuple) -> str:
    return norm_key(path[-1])


# -----------------------------------------------------------------------------
# Value sanity checks: a key can only claim a role when its VALUES look the part.
# -----------------------------------------------------------------------------

def _flatten(values):
    for v in values:
        if isinstance(v, list):
            yield from v
        else:
            yield v


def _nonnull(values):
    return [v for v in _flatten(values) if v is not None and v != ""]


def _values_ok(role: str, values: list) -> bool:
    vals = _nonnull(values)
    if not vals:
        return False
    n = len(vals)
    if role == "name":
        good = sum(1 for v in vals if isinstance(v, str) and re.search(r"[A-Za-z]", v) and len(v.strip()) >= 2)
        return good >= 0.6 * n
    if role in ("area", "width", "length"):
        good = sum(1 for v in vals if not isinstance(v, (dict, list)) and (num(v) or 0) > 0)
        return good >= 0.5 * n
    if role == "polygon":
        # `vals` was flattened one level: a list-of-lists became its point entries, a list of
        # {x,y} dicts became dicts. Either way each entry must be a 2-D point.
        good = sum(1 for v in vals if (isinstance(v, dict) and num(pick(v, "x")) is not None and num(pick(v, "y")) is not None)
                   or (isinstance(v, (list, tuple)) and len(v) >= 2 and num(v[0]) is not None and num(v[1]) is not None))
        return good >= 0.5 * n
    if role in ("company_id", "booth_id", "booth_number"):
        good = sum(1 for v in vals if isinstance(v, (str, int)) and not isinstance(v, bool))
        return good >= 0.8 * n
    if role == "exhibitor_ids":
        good = sum(1 for v in vals if isinstance(v, (str, int)) and not isinstance(v, bool))
        return good >= 0.8 * n
    if role in ("website", "hall"):
        good = sum(1 for v in vals if isinstance(v, str))
        return good >= 0.8 * n
    return True


# -----------------------------------------------------------------------------
# Role resolution over one record array
# -----------------------------------------------------------------------------

@dataclass
class FieldMatch:
    path: tuple
    alias: str
    method: str        # exact | nested | contains | fuzzy

    @property
    def label(self) -> str:
        return _path_label(self.path)

    @property
    def clean(self) -> bool:
        return self.method in ("exact", "nested")

    @property
    def generic(self) -> bool:
        return self.alias in GENERIC_ALIASES


@dataclass
class ArrayProfile:
    records: list
    where: str
    fields: dict = _field(default_factory=dict)      # role -> FieldMatch
    keys: list = _field(default_factory=list)        # human-readable key labels seen

    def has(self, *roles: str) -> bool:
        return any(r in self.fields for r in roles)

    @property
    def has_name(self) -> bool:
        return "name" in self.fields

    @property
    def has_size(self) -> bool:
        return self.has(*SIZE_ROLES)

    @property
    def has_booth(self) -> bool:
        return self.has("booth_number", "booth_id")

    def get(self, record, role: str):
        fm = self.fields.get(role)
        return get_path(record, fm.path) if fm else None

    def describe(self) -> str:
        roles = ", ".join(f"{r}<-{fm.label} ({fm.method})" for r, fm in self.fields.items()) or "no roles matched"
        keys = ", ".join(self.keys[:14]) + (" ..." if len(self.keys) > 14 else "")
        return f"{self.where} ({len(self.records)} records) keys: [{keys}] -> {roles}"


def _tier_hit(tier: str, path: tuple, alias: str) -> bool:
    joined, leaf = _joined(path), _leaf(path)
    if tier == "exact":
        return joined == alias
    if tier == "nested":
        return len(path) > 1 and leaf == alias
    if tier == "contains":
        return (len(alias) >= 6 or alias in DISTINCTIVE_SHORT) and alias in joined
    if tier == "fuzzy":
        if len(alias) < 5:
            return False
        if difflib.SequenceMatcher(None, joined, alias).ratio() >= FUZZY_RATIO:
            return True
        return len(path) > 1 and difflib.SequenceMatcher(None, leaf, alias).ratio() >= FUZZY_RATIO
    return False


def profile_array(records: list[dict], where: str = "$") -> ArrayProfile:
    """Resolve every role it can on this array, in confidence tiers, with value sanity checks."""
    sample = [r for r in records[:SAMPLE_RECORDS] if isinstance(r, dict)]
    paths: dict[tuple, list] = {}
    for r in sample:
        for p, v in _leaf_paths(r):
            paths.setdefault(p, []).append(v)
    prof = ArrayProfile(records=records, where=where, keys=[_path_label(p) for p in paths])
    claimed: set[tuple] = set()
    # Specific aliases get every tier first; the generic ones ("id", "name", "number", ...) only
    # get an exact pass afterwards, so a fuzzy "exhibitorRef" beats an exact but ambiguous "id".
    passes = [(tier, False) for tier in ("exact", "nested", "contains", "fuzzy")] + [("exact", True), ("nested", True)]
    for tier, generic_pass in passes:
        for role, aliases in ROLE_ALIASES.items():
            if role in prof.fields:
                continue
            for alias in aliases:
                if (alias in GENERIC_ALIASES) != generic_pass:
                    continue
                hit = None
                for p, vals in paths.items():
                    if p in claimed or not _tier_hit(tier, p, alias):
                        continue
                    if _values_ok(role, vals):
                        hit = FieldMatch(p, alias, tier)
                        break
                if hit:
                    prof.fields[role] = hit
                    claimed.add(hit.path)
                    break
    return prof


def profile_captures(captures: list[dict]) -> list[ArrayProfile]:
    found = []
    for i, cap in enumerate(captures):
        short = re.sub(r"^https?://", "", str(cap.get("url", "")))[:60] or f"capture[{i}]"
        for path, arr in _iter_record_arrays_with_paths(cap.get("json")):
            found.append((f"{short} {path}", arr))
    found.sort(key=lambda t: -len(t[1]))
    return [profile_array(arr, where) for where, arr in found[:MAX_PROFILES]]


# -----------------------------------------------------------------------------
# Footprint from one booth record -- never invented
# -----------------------------------------------------------------------------

def _booth_size(rec: dict, prof: ArrayProfile, allow_polygon: bool) -> tuple[float | None, float | None, float | None, str]:
    """(area, width, length, confidence). confidence: clean | fuzzy | unknown-units | none."""
    used: list[FieldMatch] = []
    width = length = area = None
    if "width" in prof.fields:
        width = num(prof.get(rec, "width"))
        used.append(prof.fields["width"])
    if "length" in prof.fields:
        length = num(prof.get(rec, "length"))
        used.append(prof.fields["length"])
    if "area" in prof.fields:
        raw = prof.get(rec, "area")
        dims = DIMS_RE.match(str(raw)) if isinstance(raw, str) else None
        if dims:   # "10x20" is a footprint statement, not an area number
            w, l = float(dims.group(1)), float(dims.group(2))
            width, length = width or w, length or l
            area = w * l
        else:
            area = num(raw)
        used.append(prof.fields["area"])
    if area is None and width and length:
        area = width * length
    conf = "none"
    if area:
        conf = "clean" if all(u.clean for u in used) else "fuzzy"
    elif allow_polygon and "polygon" in prof.fields:
        pts = prof.get(rec, "polygon")
        raw = polygon_area(pts) if isinstance(pts, list) else 0.0
        if raw and 20 <= raw <= 20000:
            area, conf = raw, ("clean" if prof.fields["polygon"].clean else "fuzzy")
        elif raw and 20 <= raw / 144 <= 20000:
            area, conf = raw / 144, ("clean" if prof.fields["polygon"].clean else "fuzzy")   # inches
        elif raw:
            area, conf = None, "unknown-units"
    return area, width, length, conf


def _scalar(v) -> str:
    if v is None or isinstance(v, (dict, list, bool)):
        return ""
    return str(v).strip()


def _key_values(prof: ArrayProfile, role: str, limit: int = JOIN_SAMPLE) -> set[str]:
    out = set()
    for rec in prof.records[:limit]:
        v = prof.get(rec, role)
        for x in (v if isinstance(v, list) else [v]):
            s = _scalar(x).lower()
            if s:
                out.add(s)
    return out


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _generic_scalar_paths(prof: ArrayProfile) -> dict[tuple, set[str]]:
    """Top-level scalar keys with their value sets, for last-resort join discovery."""
    out: dict[tuple, set[str]] = {}
    for rec in prof.records[:JOIN_SAMPLE]:
        if not isinstance(rec, dict):
            continue
        for k, v in rec.items():
            s = _scalar(v).lower()
            if s:
                out.setdefault((str(k),), set()).add(s)
    return {p: vals for p, vals in out.items() if len(vals) >= 3}


@dataclass
class JoinPlan:
    b_roles: list          # roles on the booth side whose values map onto e_role
    e_role: str
    overlap: float
    generic: bool = False

    def describe(self, B: ArrayProfile, E: ArrayProfile) -> str:
        b = " + ".join(B.fields[r].label for r in self.b_roles)
        return f"{b} -> {E.fields[self.e_role].label} (value overlap {self.overlap:.0%}{', generic key' if self.generic else ''})"


def _find_join(B: ArrayProfile, E: ArrayProfile) -> JoinPlan | None:
    e_sets = {r: _key_values(E, r) for r in ID_ROLES if r in E.fields}
    b_sets = {r: _key_values(B, r) for r in ID_ROLES if r in B.fields}
    scored = []
    for br, bv in b_sets.items():
        for er, ev in e_sets.items():
            ov = _overlap(bv, ev)
            if ov >= JOIN_MIN_OVERLAP:
                non_generic = int(not B.fields[br].generic) + int(not E.fields[er].generic)
                scored.append((non_generic, ov, br, er))
    if scored:
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        _, ov, br, er = scored[0]
        # Several booth-side fields may point at the same exhibitor key (ExpoFP: `exhibitors` list
        # on some booths, bare `exhibitorId` on others) -- union them, but never let a GENERIC field
        # ("id") ride along on a specific one's coat-tails: its overlap is often coincidental.
        b_roles = [br] + [r for r, bv in b_sets.items()
                          if r != br and not B.fields[r].generic and _overlap(bv, e_sets[er]) >= JOIN_MIN_OVERLAP]
        return JoinPlan(b_roles=b_roles, e_role=er, overlap=ov)
    # Last resort: any top-level scalar column on either side that shares most of its values.
    best = None
    for bp, bv in _generic_scalar_paths(B).items():
        for ep, ev in _generic_scalar_paths(E).items():
            ov = _overlap(bv, ev)
            if ov >= GENERIC_JOIN_MIN_OVERLAP and (best is None or ov > best[0]):
                best = (ov, bp, ep)
    if best:
        ov, bp, ep = best
        e_role = f"_join:{_path_label(ep)}"       # stable per exhibitor-side column, shared by every booth array
        B.fields["_join"] = FieldMatch(bp, "generic", "overlap")
        E.fields[e_role] = FieldMatch(ep, "generic", "overlap")
        return JoinPlan(b_roles=["_join"], e_role=e_role, overlap=ov, generic=True)
    return None


# -----------------------------------------------------------------------------
# Row assembly
# -----------------------------------------------------------------------------

def _size_source(platform: str, booths: list[dict]) -> str:
    confs = [b["conf"] for b in booths if b["area"]]
    if confs:
        return f"{platform}-json" if all(c == "clean" for c in confs) else f"{platform}-json-fuzzy"
    if booths and all(b["conf"] == "unknown-units" for b in booths):
        return f"{platform}-unknown-units"
    return "unknown"


def _make_row(platform: str, name: str, exhid: str, website: str, booths: list[dict]) -> dict:
    sized = [b for b in booths if b["area"]]
    biggest = max(sized, key=lambda b: b["area"]) if sized else (booths[0] if booths else None)
    return {
        "exhibitor_name": name,
        "booth_number": ", ".join(sorted({b["booth_number"] for b in booths if b["booth_number"]})),
        "width": biggest["width"] if biggest else None,
        "length": biggest["length"] if biggest else None,
        "sqft": int(round(sum(b["area"] for b in sized))) if sized else 0,
        "hall": "; ".join(sorted({b["hall"] for b in booths if b["hall"]})),
        "website": website,
        "is_sponsor": False, "has_video_listing": False,
        "exhid": exhid, "detail_url": "",
        "size_source": _size_source(platform, booths),
    }


def _booth_dict(rec: dict, prof: ArrayProfile, allow_polygon: bool) -> dict:
    area, width, length, conf = _booth_size(rec, prof, allow_polygon)
    bn = _scalar(prof.get(rec, "booth_number")) or _scalar(prof.get(rec, "booth_id"))
    return {"area": area or 0.0, "width": width, "length": length, "conf": conf,
            "booth_number": bn, "hall": _scalar(prof.get(rec, "hall")),
            "id": _scalar(prof.get(rec, "booth_id")) or bn}


def _names_from(rec: dict, prof: ArrayProfile) -> list[str]:
    v = prof.get(rec, "name")
    vals = v if isinstance(v, list) else [v]
    return [s for s in (_scalar(x) for x in vals) if s]


def _booth_fingerprint(b: dict) -> tuple:
    return (b["booth_number"], b["id"], b["area"], b["width"], b["length"], b["hall"])


def _add_booth(entry: dict, booth: dict) -> None:
    """Append a booth to an exhibitor entry unless an identical booth is already there (the same
    endpoint captured twice, or one hall present in two responses, must not double the sq ft)."""
    fp = _booth_fingerprint(booth)
    if fp not in entry.setdefault("_seen", set()):
        entry["_seen"].add(fp)
        entry["booths"].append(booth)


def _rows_self_contained(profiles: list[ArrayProfile], platform: str, allow_polygon: bool) -> list[dict]:
    """Arrays whose records carry both an exhibitor name and booth data (merged across arrays,
    e.g. one response per hall)."""
    grouped: dict[str, dict] = {}
    for prof in profiles:
        for rec in prof.records:
            if not isinstance(rec, dict):
                continue
            names = _names_from(rec, prof)
            if not names:
                continue
            booth = _booth_dict(rec, prof, allow_polygon)
            cid = _scalar(prof.get(rec, "company_id"))
            website = _scalar(prof.get(rec, "website"))
            for name in names:
                key = (cid if cid and len(names) == 1 else name.lower())
                g = grouped.setdefault(key, {"name": name, "exhid": cid or booth["id"], "website": website, "booths": []})
                _add_booth(g, booth)
                if website and not g["website"]:
                    g["website"] = website
    return [_make_row(platform, g["name"], g["exhid"], g["website"], g["booths"]) for g in grouped.values()]


def _rows_joined(plans: list[tuple[ArrayProfile, "JoinPlan"]], E: ArrayProfile, platform: str,
                 allow_polygon: bool, report: list[str]) -> list[dict]:
    """Join one exhibitor array with every booth array that verifiably links to it."""
    exhibitors: dict[str, dict] = {}       # grouped by the exhibitor's own identity (id or name)
    key_index: dict[str, dict[str, dict]] = {}   # e_role -> join value -> entry
    for rec in E.records:
        if not isinstance(rec, dict):
            continue
        names = _names_from(rec, E)
        if not names:
            continue
        name = names[0]
        cid = _scalar(E.get(rec, "company_id"))
        ident = cid or name.lower()
        entry = exhibitors.setdefault(ident, {"name": name, "exhid": cid, "website": _scalar(E.get(rec, "website")),
                                              "booths": []})
        if not entry["website"]:
            entry["website"] = _scalar(E.get(rec, "website"))
        for _, plan in plans:
            kv = E.get(rec, plan.e_role)
            for x in (kv if isinstance(kv, list) else [kv]):
                sv = _scalar(x).lower()
                if sv:
                    key_index.setdefault(plan.e_role, {})[sv] = entry
    unmatched = 0
    for B, plan in plans:
        index = key_index.get(plan.e_role, {})
        for rec in B.records:
            if not isinstance(rec, dict):
                continue
            booth = _booth_dict(rec, B, allow_polygon)
            keys = []
            for r in plan.b_roles:
                v = B.get(rec, r)
                keys.extend(_scalar(x).lower() for x in (v if isinstance(v, list) else [v]))
            hit = False
            for k in keys:
                if k and k in index:
                    _add_booth(index[k], booth)
                    hit = True
            if not hit:
                # Partial fallback: a booth record carrying its own exhibitor name still yields a row.
                own = _names_from(rec, B)
                if own:
                    for name in own:
                        entry = exhibitors.setdefault(name.lower(), {"name": name, "exhid": booth["id"],
                                                                      "website": _scalar(B.get(rec, "website")), "booths": []})
                        _add_booth(entry, booth)
                else:
                    unmatched += 1
    if unmatched:
        report.append(f"{unmatched} booth record(s) had no matching exhibitor and no name of their own (dropped)")
    rows = []
    for e in exhibitors.values():
        rows.append(_make_row(platform, e["name"], e["exhid"] or (e["booths"][0]["id"] if e["booths"] else ""),
                              e["website"], e["booths"]))
    return rows


def _rows_best_effort(prof: ArrayProfile, platform: str) -> list[dict]:
    """Names only (plus a booth number if one exists): sqft=0, size_source='unknown'."""
    seen: dict[str, dict] = {}
    for rec in prof.records:
        if not isinstance(rec, dict):
            continue
        for name in _names_from(rec, prof):
            cid = _scalar(prof.get(rec, "company_id"))
            key = cid or name.lower()
            bn = _scalar(prof.get(rec, "booth_number")) or _scalar(prof.get(rec, "booth_id"))
            g = seen.setdefault(key, {"name": name, "exhid": cid or bn, "website": _scalar(prof.get(rec, "website")),
                                      "booths": []})
            g["booths"].append({"area": 0.0, "width": None, "length": None, "conf": "none",
                                "booth_number": bn, "hall": _scalar(prof.get(rec, "hall")), "id": bn})
    return [_make_row(platform, g["name"], g["exhid"], g["website"], g["booths"]) for g in seen.values()]


def _is_exhibitor_array(p: ArrayProfile) -> bool:
    """One exhibitor per record: a name field that is NOT mapped through a nested list (a hall
    wrapper whose records each hold a LIST of booths/exhibitors is not an exhibitor array)."""
    return p.has_name and not _mapped(p.fields["name"])


def _is_booth_array(p: ArrayProfile) -> bool:
    direct = [p.fields[r] for r in SIZE_ROLES + ("booth_number", "booth_id") if r in p.fields]
    return any(not _mapped(fm) for fm in direct)


# -----------------------------------------------------------------------------
# The generic normaliser every platform wrapper delegates to
# -----------------------------------------------------------------------------

PLATFORM_PROFILES = {
    # allow_polygon: EXPOCAD ships booth outlines, so the shoelace area is a legitimate reading.
    # ExpoFP pre-computes `area` and its geometry is in screen units -- never recompute from it.
    "a2z": {"allow_polygon": False},
    "expocad": {"allow_polygon": True},
    "expofp": {"allow_polygon": False},
}


def normalize_captures(captures: list[dict], platform: str, log=None) -> list[dict]:
    """
    Turn captured JSON into EXHIBITOR_COLUMNS rows for any of the three browser platforms.
    Strategy order: verified join of booth array(s) with an exhibitor array -> self-contained
    array(s) -> best-effort names-only. Every step is explained through `log` so a degraded or
    failed parse says WHY without a separate capture run.
    """
    log = log or (lambda msg: None)
    opts = PLATFORM_PROFILES.get(platform, {"allow_polygon": False})
    allow_polygon = opts["allow_polygon"]
    tag = platform.upper()
    profiles = profile_captures(captures)
    report: list[str] = []

    if not profiles:
        shapes = []
        for cap in captures:
            body = cap.get("json")
            shapes.append(f"{type(body).__name__}" + (f" keys={list(body)[:8]}" if isinstance(body, dict) else ""))
        log(f"{tag}: {len(captures)} JSON response(s) captured but none contained a list of records "
            f"(top-level shapes: {shapes[:5]}).")
        return []

    log(f"{tag}: {len(profiles)} record array(s) found in {len(captures)} response(s):")
    for p in profiles[:8]:
        log(f"  - {p.describe()}")
    found = {r for p in profiles for r in p.fields}
    missing = [r for r in ("name", "booth_number", "booth_id", "company_id", "area", "width", "length", "polygon")
               if r not in found]
    if missing:
        log(f"  roles never found in any array: {', '.join(missing)}")

    rows: list[dict] = []
    strategy = ""
    # 1. Verified join: an exhibitor array (one name per record) x every booth array whose id
    #    values demonstrably overlap with one of its id fields.
    exh_arrays = [p for p in profiles if _is_exhibitor_array(p)]
    booth_arrays = [p for p in profiles if _is_booth_array(p)]
    best: tuple[int, int, list[dict], str, list[str]] | None = None
    for E in exh_arrays:
        plans = []
        for B in booth_arrays:
            if B is E or B.records is E.records:
                continue
            plan = _find_join(B, E)
            if plan:
                plans.append((B, plan))
        if not plans:
            continue
        # A generic-key (last-resort) join is only trusted when no specific id link exists at all;
        # when several booth arrays (one per hall) all link generically, keep only those that agree
        # on the same exhibitor-side column as the strongest one.
        specific = [(B, pl) for B, pl in plans if not pl.generic]
        if specific:
            plans = specific
        else:
            lead = max(plans, key=lambda t: t[1].overlap)[1].e_role
            plans = [(B, pl) for B, pl in plans if pl.e_role == lead]
        sub_report: list[str] = []
        cand = _rows_joined(plans, E, platform, allow_polygon, sub_report)
        sized = sum(1 for r in cand if r["sqft"])
        if cand and (best is None or (sized, len(cand)) > (best[0], best[1])):
            desc = "; ".join(f"{B.where} on {pl.describe(B, E)}" for B, pl in plans)
            best = (sized, len(cand), cand, f"joined exhibitor array {E.where} with booth array(s) {desc}", sub_report)
    if best:
        rows, strategy, report = best[2], best[3], best[4]
    # 2. Self-contained: array(s) whose records carry a name AND booth data (merged).
    if not rows:
        selfc = [p for p in profiles if p.has_name and (p.has_size or p.has_booth)
                 and not any(_mapped(p.fields[r]) for r in SIZE_ROLES + ("booth_number", "booth_id") if r in p.fields)]
        if selfc:
            rows = _rows_self_contained(selfc, platform, allow_polygon)
            if rows:
                strategy = "self-contained array(s) " + "; ".join(p.where for p in selfc)
    # 3. Best effort: names only.
    if not rows:
        for p in profiles:
            if _is_exhibitor_array(p):
                rows = _rows_best_effort(p, platform)
                if rows:
                    strategy = (f"BEST-EFFORT names only from {p.where} -- no booth/footprint fields recognised, "
                                f"every row is 0 sq ft / size_source=unknown")
                    break
    if not rows:
        log(f"{tag}: no array with an exhibitor-name field was found, so no rows could be built. "
            f"If one of the arrays above IS the exhibitor list, add its name key to ROLE_ALIASES['name'].")
        return []

    rows = [r for r in rows if r["exhibitor_name"]]
    rows = scraper.apply_name_filter(rows)
    sized = sum(1 for r in rows if r["sqft"])
    fuzzy = sum(1 for r in rows if r["size_source"].endswith("-fuzzy"))
    log(f"{tag}: strategy = {strategy}")
    for line in report:
        log(f"  {line}")
    log(f"{tag}: {len(rows)} exhibitors, {sized} with a readable footprint"
        + (f", {fuzzy} sized via fuzzy field matches (size_source={platform}-json-fuzzy)" if fuzzy else "")
        + (f", {len(rows) - sized} with no footprint (0 sq ft, size_source=unknown)" if len(rows) - sized else ""))
    return rows


# =============================================================================
# A2Z / Personify
# =============================================================================

def normalize_a2z(captures: list[dict], log=None) -> list[dict]:
    """
    Expected shape: booth records (BoothNumber + AreaSqFt | Width/Length + CompanyId) joined to
    exhibitor records (CompanyId + CompanyName + Website) on the company id, area summed per
    company. Any other shape falls through the generic engine's fallbacks (see normalize_captures).
    """
    return normalize_captures(captures, "a2z", log=log)


# =============================================================================
# EXPOCAD
# =============================================================================

def normalize_expocad(captures: list[dict], log=None) -> list[dict]:
    """
    Expected shape: booth records (BoothID + Area "400 sq ft" | Width/Length | polygon vertices)
    joined to exhibitor records on BoothID/BoothNumber -- or a single array carrying both. Polygon
    areas are used as declared feet when plausible (20..20000 sq ft), retried as inches (/144),
    otherwise marked expocad-unknown-units rather than guessed.
    """
    return normalize_captures(captures, "expocad", log=log)


# =============================================================================
# ExpoFP
# =============================================================================

def normalize_expofp(captures: list[dict], log=None) -> list[dict]:
    """
    Expected shape: one config JSON with root arrays `booths` (pre-computed `area`, exhibitor id or
    id list) and `exhibitors` (id, name, website), area summed per exhibitor. Geometry is never
    used to recompute ExpoFP areas.
    """
    return normalize_captures(captures, "expofp", log=log)


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
    # The normaliser logs what it saw (arrays, keys, which roles resolved, which strategy it used)
    # through the same callback, so a degraded or failed parse explains itself in the app's own
    # extraction log instead of requiring a separate capture run.
    diag: list[str] = []

    def _both(msg: str) -> None:
        diag.append(msg)
        log(msg)

    rows = NORMALIZERS[platform](captures, log=_both)
    if not rows:
        detail = "\n".join(diag[-12:])
        raise scraper.ScrapeError(
            f"{platform.upper()} JSON was captured ({len(captures)} response(s)) but no exhibitor rows could "
            f"be built from it. What the normaliser saw:\n{detail}\n"
            f"For the full payloads run `python browser_scraper.py capture {url} --normalise {platform}` "
            f"from a machine that can reach this site, then extend ROLE_ALIASES in browser_scraper.py."
        )
    rows.sort(key=lambda r: (-r["sqft"], r["exhibitor_name"].lower()))
    low_conf = sum(1 for r in rows if r["size_source"].endswith("-fuzzy"))
    unsized = sum(1 for r in rows if not r["sqft"])
    if unsized == len(rows):
        log(f"{platform.upper()}: WARNING -- names were read but NO footprints; every row is 0 sq ft and "
            f"will fall outside the sq-ft filter. See the field report above for which keys were seen.")
    elif low_conf:
        log(f"{platform.upper()}: {low_conf} row(s) sized via fuzzy field matches -- check a few against the "
            f"live floor plan before trusting them (size_source={platform}-json-fuzzy).")
    meta = {
        # Unknown until real captures reveal where (if anywhere) each platform states the show's
        # own name -- never guessed. The sidebar / timeline slot's own explicit year field is the
        # source of truth for this extraction's year, same as MapYourShow (Task 1).
        "show_name": "Trade Show", "show_base": "Trade Show", "show_year": None,
        "source": "live", "platform": platform, "url": url,
        "halls": len({r["hall"] for r in rows if r["hall"]}), "hall_errors": 0,
        "sized": sum(1 for r in rows if r["sqft"]), "total": len(rows),
        "low_confidence": low_conf, "parse_log": diag,
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
            print()
            rows = NORMALIZERS[args.normalise](captures, log=print)
            print(f"\n{args.normalise} normaliser: {len(rows)} rows")
            for r in rows[:5]:
                print(" ", {k: r[k] for k in ("exhibitor_name", "booth_number", "sqft", "size_source")})


if __name__ == "__main__":
    _cli()
