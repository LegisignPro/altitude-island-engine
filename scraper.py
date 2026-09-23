"""
scraper.py -- MapYourShow directory extractor for Altitude Exhibits.

MapYourShow directories (CES, NAB Show, World of Concrete, PACK EXPO, AHR, TISE,
IMTS ...) are Vue apps: the HTML carries no exhibitor rows. The data lives
behind three JSON endpoints that answer a plain GET with an
`X-Requested-With: XMLHttpRequest` header and no authentication:

  1. /8_0/ajax/remote-proxy.cfm?action=getsearchoptions&function=getBoothHalls
        -> every hall: {fieldvalue: "C", fielddisplay: "Central Hall"}
  2. /8_0/ajax/remote-proxy.cfm?action=search&searchtype=exhibitorgallery&searchsize=20000
        -> the whole exhibitor list in one call (name, exhid, booths, halls,
           featured / sponsor / video flags, description)
  3. /8_0/floorplan/02/_remote-proxy.cfm?showid=NAB27&hallid=C&action=GetBoothByHall
        -> every booth polygon in a hall with boothWidth / boothHeight (inches)
           and area (sq ft), plus the EXHID that holds it.

Joining 2 and 3 on the exhibitor id gives an exact booth footprint for the
whole show. Websites come from the server-rendered exhibitor detail page
(`websiteValue: "https://..."`), fetched only for the exhibitors that pass the
square-footage filter so a 2,000-exhibitor show costs ~100 detail requests,
not 2,000.

Every field returned here is an observed MapYourShow fact. Nothing is modelled.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable
from urllib.parse import urlparse

import requests

REQUEST_TIMEOUT = 15
MAX_WORKERS = 6

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
JSON_HEADERS = {
    **REQUEST_HEADERS,
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/plain, */*",
}

# Exhibitor names that are not companies (shared stands, trade bodies, government).
EXCLUDE_NAME_RE = re.compile(r"\b(pavilion|association|state of|department)\b", re.I)

# Column order every downstream module relies on.
EXHIBITOR_COLUMNS = [
    "exhibitor_name", "booth_number", "width", "length", "sqft", "hall",
    "website", "is_sponsor", "has_video_listing", "exhid", "detail_url", "size_source",
]

# Hostname prefix -> show name, for the convention label in pitches.
KNOWN_SHOWS = {
    "ces": "CES", "nab": "NAB Show", "sema": "SEMA Show", "aapex": "AAPEX", "shot": "SHOT Show",
    "ibs": "NAHB International Builders' Show", "woc": "World of Concrete", "g2e": "Global Gaming Expo",
    "mjbiz": "MJBizCon", "nada": "NADA Show", "conexpo": "CONEXPO-CON/AGG", "iscwest": "ISC West",
    "isc": "ISC West", "asd": "ASD Market Week", "awfs": "AWFS Fair", "kbis": "KBIS",
    "infocomm": "InfoComm", "packexpo": "PACK EXPO", "imts": "IMTS", "ahr": "AHR Expo",
    "tise": "TISE", "iwce": "IWCE", "wasteexpo": "WasteExpo", "hdexpo": "HD Expo",
}


class ScrapeError(Exception):
    """Any condition that stops a live extraction. The message is shown to the user."""


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def is_mapyourshow(url: str) -> bool:
    """Host check only. White-label MapYourShow hosts are detected by platforms.platform_for(probe=True)."""
    return "mapyourshow.com" in urlparse(url).netloc.lower()


def mys_base(url: str) -> tuple[str, str]:
    """'https://ces2026.mapyourshow.com/8_0/explore/...' -> ('https://ces2026.mapyourshow.com', '8_0')."""
    p = urlparse(url)
    m = re.search(r"/(\d+_\d+)/", p.path)
    return f"{p.scheme or 'https'}://{p.netloc}", (m.group(1) if m else "8_0")


_HOST_LABEL_RE = re.compile(r"^([a-z]+?)[-_]?(\d{2}|\d{4})?$")


def infer_show_base(url: str) -> str:
    """ces2026.mapyourshow.com -> 'CES'; nab27 -> 'NAB Show'; unrecognised host -> 'Trade Show'."""
    label = urlparse(url).netloc.lower().split(".")[0] if urlparse(url).netloc else ""
    m = _HOST_LABEL_RE.match(label)
    if not m:
        return "Trade Show"
    base = m.group(1)
    return KNOWN_SHOWS.get(base, base.upper())


def infer_show_year(url: str) -> int | None:
    """ces2026.mapyourshow.com -> 2026; nab27 -> 2027; nab (no digits in host) -> None."""
    label = urlparse(url).netloc.lower().split(".")[0] if urlparse(url).netloc else ""
    m = _HOST_LABEL_RE.match(label)
    if not m or not m.group(2):
        return None
    year = m.group(2)
    return int(year) if len(year) == 4 else 2000 + int(year)


def infer_show_name(url: str) -> str:
    """ces2026.mapyourshow.com -> 'CES 2026'; nab27 -> 'NAB Show 2027'. Thin wrapper kept for compatibility."""
    base = infer_show_base(url)
    year = infer_show_year(url)
    return f"{base} {year}" if year else base


def _clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _strip_booth(value) -> str:
    """Gallery booth numbers arrive as 'C8520randomstring'; the floor plan has the clean value."""
    return re.sub(r"randomstring$", "", _clean(value), flags=re.I)


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y", "t"}
    if isinstance(v, (list, tuple, dict)):
        return len(v) > 0
    return bool(v)


def _flag_from_fields(fields: dict, *needles: str) -> bool:
    """True if any gallery field whose name contains a needle is truthy (e.g. featured_b, sponsor_b)."""
    for key, value in fields.items():
        k = key.lower()
        if any(n in k for n in needles) and _truthy(value):
            return True
    return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class MapYourShowClient:
    """One requests.Session per extraction so the site's cookies carry across calls."""

    def __init__(self, url: str):
        self.origin, self.root = mys_base(url)
        self.proxy = f"{self.origin}/{self.root}/ajax/remote-proxy.cfm"
        self.referer = f"{self.origin}/{self.root}/explore/exhibitor-gallery.cfm"
        self.fp_referer = f"{self.origin}/{self.root}/floorplan/"
        self.session = requests.Session()

    def _get(self, url: str, params: dict, referer: str) -> requests.Response:
        headers = {**JSON_HEADERS, "Referer": referer, "Origin": self.origin,
                   "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
        try:
            return self.session.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT * 3)
        except requests.exceptions.RequestException as exc:
            raise ScrapeError(f"MapYourShow request failed: {exc.__class__.__name__}") from exc

    def _json(self, url: str, params: dict, referer: str) -> dict:
        resp = self._get(url, params, referer)
        if resp.status_code != 200:
            raise ScrapeError(f"MapYourShow returned HTTP {resp.status_code} for {params.get('action', 'request')}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ScrapeError("MapYourShow returned a non-JSON response (has the show code changed?)") from exc
        return data if isinstance(data, dict) else {}

    # -- 1. halls ------------------------------------------------------------
    def halls(self) -> dict[str, str]:
        data = self._json(self.proxy, {"action": "getsearchoptions", "function": "getBoothHalls"}, self.referer)
        return {str(h.get("fieldvalue")): _clean(h.get("fielddisplay") or h.get("fieldvalue"))
                for h in (data.get("DATA") or []) if h.get("fieldvalue")}

    # -- 2. exhibitor gallery -----------------------------------------------
    def gallery(self) -> dict[str, dict]:
        data = self._json(self.proxy, {"action": "search", "searchtype": "exhibitorgallery", "searchsize": 20000},
                          self.referer)
        try:
            hits = data["DATA"]["results"]["exhibitor"]["hit"]
        except (KeyError, TypeError) as exc:
            raise ScrapeError("MapYourShow gallery search returned an unexpected shape.") from exc
        out: dict[str, dict] = {}
        for hit in hits:
            f = hit.get("fields") or {}
            exhid = str(f.get("exhid_l") or hit.get("id") or "").strip()
            name = _clean(f.get("exhname_t"))
            if not exhid or not name:
                continue
            booths = f.get("boothsdisplay_la") or f.get("booths_la") or []
            out[exhid] = {
                "exhid": exhid,
                "exhibitor_name": name,
                "booth_number": ", ".join(b for b in (_strip_booth(b) for b in booths) if b),
                "halls": [str(h) for h in (f.get("hallid_la") or [])],
                "is_sponsor": _flag_from_fields(f, "sponsor", "featured", "premium"),
                "has_video_listing": _flag_from_fields(f, "video"),
                "website": _clean(f.get("website_t") or f.get("exhwebsite_t") or ""),
            }
        return out

    # -- 3. floor plan -------------------------------------------------------
    def show_id_and_version(self) -> tuple[str, str]:
        showid = urlparse(self.origin).netloc.split(".")[0].upper()
        fpver = "02"
        try:
            html = self.session.get(self.fp_referer, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT).text
            m = re.search(r'ShowID\s*=\s*"([^"]+)"', html)
            if m:
                showid = m.group(1)
            m = re.search(r"floorplan/(\d{2})/", html)
            if m:
                fpver = m.group(1)
        except requests.exceptions.RequestException:
            pass
        return showid, fpver

    def booths_for_hall(self, version: str, showid: str, hall: str) -> list[dict]:
        url = f"{self.origin}/{self.root}/floorplan/{version}/_remote-proxy.cfm"
        resp = self._get(url, {"showid": showid, "selectedbooth": "", "hallid": hall,
                               "action": "GetBoothByHall", "method": "GetBoothByHall", "regid": 0}, self.fp_referer)
        if resp.status_code != 200:
            raise ScrapeError(f"Floor plan returned HTTP {resp.status_code} for hall {hall}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        cols = data.get("COLUMNS") or []
        out = []
        for raw in data.get("DATA") or []:
            rec = dict(zip(cols, raw))
            if rec.get("OBJECTTYPE") != "booth" or not rec.get("EXHID"):
                continue
            props = rec.get("FEATUREPROPERTIES") or {}
            if isinstance(props, str):
                try:
                    props = json.loads(props)
                except ValueError:
                    props = {}
            props = props.get("properties") or {}
            try:
                area = float(props.get("area") or 0)
            except (TypeError, ValueError):
                area = 0.0
            width_in, depth_in = props.get("boothWidth"), props.get("boothHeight")
            try:
                width_ft = round(float(width_in) / 12, 1) if width_in else None
                depth_ft = round(float(depth_in) / 12, 1) if depth_in else None
            except (TypeError, ValueError):
                width_ft = depth_ft = None
            if not area and width_ft and depth_ft:
                area = width_ft * depth_ft
            out.append({
                "exhid": str(rec["EXHID"]),
                "name": _clean(rec.get("EXHNAME")),
                "booth": _clean(rec.get("BOOTHDISPLAY") or rec.get("BOOTH")),
                "hall": hall,
                "area": round(area),
                "width_ft": width_ft,
                "depth_ft": depth_ft,
            })
        return out

    # -- 4. exhibitor detail page (website) ---------------------------------
    WEBSITE_RE = re.compile(r'websiteValue:\s*"([^"]*)"')
    VIDEO_RE = re.compile(r'(?:videoValue|videoUrl|youtubeValue|vimeoValue)\s*:\s*"([^"]+)"', re.I)

    def detail_url(self, exhid: str) -> str:
        return f"{self.origin}/{self.root}/exhibitor/exhibitor-details.cfm?exhid={exhid}"

    def fetch_detail(self, exhid: str) -> dict:
        """{'website': ..., 'has_video': bool} from the server-rendered detail page. Never raises."""
        try:
            resp = self.session.get(self.detail_url(exhid), headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                return {}
            html = resp.text
        except requests.exceptions.RequestException:
            return {}
        out: dict = {}
        m = self.WEBSITE_RE.search(html)
        if m:
            site = m.group(1).replace("\\/", "/").strip()
            if site:
                out["website"] = site if site.startswith("http") else f"https://{site}"
        if self.VIDEO_RE.search(html):
            out["has_video"] = True
        return out


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _fmt_ft(v) -> str:
    if v is None:
        return ""
    v = float(v)
    return f"{int(v)}" if v.is_integer() else f"{v:g}"


def _join_rows(gallery: dict[str, dict], booths_by_exh: dict[str, list[dict]], halls: dict[str, str],
               client: MapYourShowClient) -> list[dict]:
    rows = []
    for exhid, ex in gallery.items():
        booths = booths_by_exh.get(exhid, [])
        if booths:
            biggest = max(booths, key=lambda b: b["area"])
            total = int(round(sum(b["area"] for b in booths)))
            width, length = biggest["width_ft"], biggest["depth_ft"]
            hall = "; ".join(sorted({halls.get(b["hall"], b["hall"]) for b in booths}))
            booth_no = ", ".join(sorted({b["booth"] for b in booths if b["booth"]})) or ex["booth_number"]
            source = "floorplan"
        else:
            total, width, length, source = 0, None, None, "unknown"
            hall = "; ".join(halls.get(h, h) for h in ex["halls"])
            booth_no = ex["booth_number"]
        rows.append({
            "exhibitor_name": ex["exhibitor_name"],
            "booth_number": booth_no,
            "width": width,
            "length": length,
            "sqft": total,
            "hall": hall,
            "website": ex.get("website", ""),
            "is_sponsor": bool(ex["is_sponsor"]),
            "has_video_listing": bool(ex["has_video_listing"]),
            "exhid": exhid,
            "detail_url": client.detail_url(exhid),
            "size_source": source,
        })
    # Booths on the floor plan whose exhibitor is not (yet) in the public gallery.
    for exhid, booths in booths_by_exh.items():
        if exhid in gallery:
            continue
        name = next((b["name"] for b in booths if b["name"] and b["name"].lower() != "unassigned"), "")
        if not name:
            continue
        biggest = max(booths, key=lambda b: b["area"])
        rows.append({
            "exhibitor_name": name,
            "booth_number": ", ".join(sorted({b["booth"] for b in booths if b["booth"]})),
            "width": biggest["width_ft"],
            "length": biggest["depth_ft"],
            "sqft": int(round(sum(b["area"] for b in booths))),
            "hall": "; ".join(sorted({halls.get(b["hall"], b["hall"]) for b in booths})),
            "website": "",
            "is_sponsor": False,
            "has_video_listing": False,
            "exhid": exhid,
            "detail_url": client.detail_url(exhid),
            "size_source": "floorplan",
        })
    return rows


def apply_name_filter(rows: Iterable[dict]) -> list[dict]:
    """Drop pavilions, associations, government stands."""
    return [r for r in rows if not EXCLUDE_NAME_RE.search(r["exhibitor_name"])]


def scrape_mapyourshow(url: str, log=None) -> tuple[list[dict], dict]:
    """
    Full extraction. Returns (rows, meta). `log(msg)` receives progress lines.
    Raises ScrapeError with a plain-English reason when the directory cannot be read.
    """
    log = log or (lambda msg: None)
    if not is_mapyourshow(url):
        raise ScrapeError("Only MapYourShow directories are supported (host must contain mapyourshow.com).")

    client = MapYourShowClient(url)
    halls = client.halls()
    log(f"Halls: {len(halls)}")

    gallery = client.gallery()
    if not gallery:
        raise ScrapeError("MapYourShow returned zero exhibitors. The directory may not be published yet.")
    log(f"Gallery exhibitors: {len(gallery)}")

    showid, fpver = client.show_id_and_version()
    wanted = sorted({h for ex in gallery.values() for h in ex["halls"] if h in halls}) or list(halls)

    booths_by_exh: dict[str, list[dict]] = {}
    hall_errors = 0
    # The legacy "02" proxy answers for every show seen so far, including shows whose
    # public floor plan already runs the newer "03" app, so it goes first.
    for version in ["02"] + ([fpver] if fpver != "02" else []):
        booths_by_exh, hall_errors = {}, 0

        def _safe(hall: str):
            try:
                return client.booths_for_hall(version, showid, hall)
            except ScrapeError:
                return None

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for result in pool.map(_safe, wanted):
                if result is None:
                    hall_errors += 1
                    continue
                for b in result:
                    booths_by_exh.setdefault(b["exhid"], []).append(b)
        if booths_by_exh:
            break
    log(f"Floor plan: {sum(len(v) for v in booths_by_exh.values())} booths across {len(wanted)} halls "
        f"({hall_errors} hall errors)")

    rows = _join_rows(gallery, booths_by_exh, halls, client)
    before = len(rows)
    rows = apply_name_filter(rows)
    log(f"Name filter removed {before - len(rows)} pavilion / association / government listings")
    rows.sort(key=lambda r: (-r["sqft"], r["exhibitor_name"].lower()))

    show_base = infer_show_base(url)
    show_year = infer_show_year(url)
    meta = {
        "show_name": infer_show_name(url),   # kept for compatibility; prefer show_base/show_year
        "show_base": show_base,
        "show_year": show_year,
        "source": "live",
        "platform": "mapyourshow",
        "url": url,
        "showid": showid,
        "halls": len(wanted),
        "hall_errors": hall_errors,
        "sized": sum(1 for r in rows if r["size_source"] == "floorplan"),
        "total": len(rows),
    }
    return rows, meta


def enrich_websites(rows: list[dict], url: str, log=None) -> list[dict]:
    """
    Fill `website` (and `has_video_listing`) from each exhibitor's detail page.
    Call this with the FILTERED rows only. Never raises; blanks stay blank.
    """
    log = log or (lambda msg: None)
    client = MapYourShowClient(url)
    todo = [r for r in rows if r.get("exhid") and not r.get("website")]
    if not todo:
        return rows
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        details = list(pool.map(lambda r: client.fetch_detail(r["exhid"]), todo))
    found = 0
    for row, detail in zip(todo, details):
        if detail.get("website"):
            row["website"] = detail["website"]
            found += 1
        if detail.get("has_video"):
            row["has_video_listing"] = True
    log(f"Websites found on detail pages: {found}/{len(todo)}")
    return rows
