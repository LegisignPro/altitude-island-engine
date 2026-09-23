"""
MapYourShow, including white-label hosts (directory.imts.com, exhibitors.ces.tech).

Same three JSON endpoints v1/v2 use (see scraper.py), plus the exhibitor detail page, which
carries website, LinkedIn, phone and `addressValues: {"CITY": ..., "STATE": ..., "COUNTRY": ...}`
(verified on IMTS 2026). The show year comes from the floor plan's ShowID ("IMTS26" -> 2026)
before the host name, because custom domains have no year in the host.

normalise() reads the same canonical raw the browser grabber saves in debug mode.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

import scraper

from .common import ExtractionError, clean, finish, group_booths

DETAIL_MIN_SQFT = 400
WORKERS = 4
_TRUTHY = {"1", "true", "yes", "y", "t"}


def mys_root(url: str) -> str | None:
    m = re.search(r"/(\d+_\d+)/", urlparse(url).path)
    return m.group(1) if m else None


def probe(url: str) -> bool:
    """True when <origin>/<ver>/ajax/remote-proxy.cfm answers getBoothHalls with a DATA list."""
    root = mys_root(url)
    if not root:
        return False
    p = urlparse(url)
    try:
        r = requests.get(f"{p.scheme or 'https'}://{p.netloc}/{root}/ajax/remote-proxy.cfm",
                         params={"action": "getsearchoptions", "function": "getBoothHalls"},
                         headers=scraper.JSON_HEADERS, timeout=20)
        return r.status_code == 200 and isinstance(r.json().get("DATA"), list)
    except (requests.RequestException, ValueError, AttributeError):
        return False


def year_from_showid(showid: str) -> int | None:
    m = re.search(r"(\d{2}|\d{4})$", showid or "")
    if not m:
        return None
    y = m.group(1)
    return int(y) if len(y) == 4 else 2000 + int(y)


def _flag(fields: dict, *needles: str) -> bool:
    for k, v in fields.items():
        if any(n in k.lower() for n in needles):
            if isinstance(v, str) and v.strip().lower() in _TRUTHY:
                return True
            if isinstance(v, (list, tuple, dict)) and len(v) > 0:
                return True
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v:
                return True
            if v is True:
                return True
    return False


def _booth_rows(hall_booths: dict, halls: dict) -> list[dict]:
    out = []
    for hall, payload in (hall_booths or {}).items():
        cols = payload.get("COLUMNS") or []
        for raw in payload.get("DATA") or []:
            rec = dict(zip(cols, raw))
            if rec.get("OBJECTTYPE") != "booth" or not rec.get("EXHID"):
                continue
            props = rec.get("FEATUREPROPERTIES") or {}
            if isinstance(props, str):
                try:
                    props = json.loads(props)
                except ValueError:
                    props = {}
            props = (props or {}).get("properties") or {}
            try:
                w = round(float(props["boothWidth"]) / 12, 1) if props.get("boothWidth") else None
                l = round(float(props["boothHeight"]) / 12, 1) if props.get("boothHeight") else None
            except (TypeError, ValueError):
                w = l = None
            try:
                area = float(props.get("area") or 0)
            except (TypeError, ValueError):
                area = 0.0
            if not area and w and l:
                area = w * l
            fmt = lambda v: f"{int(v)}" if float(v).is_integer() else f"{v:g}"
            out.append({"exhid": str(rec["EXHID"]), "name": clean(rec.get("EXHNAME")),
                        "booth": clean(rec.get("BOOTHDISPLAY") or rec.get("BOOTH")), "hall": halls.get(hall, hall),
                        "area": round(area), "w": w, "l": l,
                        "raw": f"{fmt(w)}' x {fmt(l)}'" if w and l else (f"{round(area)} sq ft" if area else "")})
    return out


def normalise(raw: dict) -> tuple[list[dict], dict]:
    """
    raw = {"halls": {id: name}, "gallery": [hits], "hall_booths": {hall: {COLUMNS, DATA}},
           "details": {exhid: {website, linkedin, phone, address}}, "showid": str,
           "origin": str, "root": str, "title": str}.  No network.
    """
    halls = {str(k): v for k, v in (raw.get("halls") or {}).items()}
    origin, root = raw.get("origin") or "", raw.get("root") or "8_0"
    detail = lambda i: f"{origin}/{root}/exhibitor/exhibitor-details.cfm?exhid={i}" if origin else ""
    gallery = {}
    for hit in raw.get("gallery") or []:
        f = hit.get("fields") or {}
        i = str(f.get("exhid_l") or hit.get("id") or "").strip()
        name = clean(f.get("exhname_t"))
        if not i or not name:
            continue
        gallery[i] = {"name": name, "halls": [str(h) for h in f.get("hallid_la") or []],
                      "booth": ", ".join(b for b in (re.sub(r"randomstring$", "", clean(b), flags=re.I)
                                                     for b in (f.get("boothsdisplay_la") or f.get("booths_la") or [])) if b),
                      "sponsor": _flag(f, "sponsor", "featured", "premium"), "video": _flag(f, "video"),
                      "description": clean(f.get("exhdesc_t"))[:500],
                      "categories": "; ".join(clean(t) for t in f.get("exhtags_la") or [])}
    by_exh: dict[str, list] = {}
    for b in _booth_rows(raw.get("hall_booths"), halls):
        by_exh.setdefault(b["exhid"], []).append(b)
    booths = []
    for i, g in gallery.items():
        extra = {"exhid": i, "is_sponsor": g["sponsor"], "has_video_listing": g["video"],
                 "description": g["description"], "categories": g["categories"], "detail_url": detail(i)}
        lst = by_exh.get(i) or []
        if not lst:
            booths.append({"key": f"id:{i}", "name": g["name"], "booth": g["booth"],
                           "hall": "; ".join(halls.get(h, h) for h in g["halls"]), "area": 0, "shared": 1, "extra": extra})
        for b in lst:
            booths.append({**b, "key": f"id:{i}", "name": g["name"], "shared": 1, "extra": extra})
    for i, lst in by_exh.items():
        if i in gallery:
            continue
        name = next((b["name"] for b in lst if b["name"] and b["name"].lower() != "unassigned"), "")
        if name:
            for b in lst:
                booths.append({**b, "key": f"id:{i}", "name": name, "shared": 1,
                               "extra": {"exhid": i, "detail_url": detail(i)}})
    rows = group_booths(booths, "mapyourshow", "floorplan")
    details = raw.get("details") or {}
    for r in rows:
        d = details.get(r["exhid"]) or {}
        if d.get("website"):
            r["website"] = d["website"]
        if d.get("linkedin"):
            r["company_linkedin"] = d["linkedin"]
        if d.get("phone"):
            r["phone"] = d["phone"]
        if d.get("has_video"):
            r["has_video_listing"] = True
        addr = d.get("address")
        if isinstance(addr, str) and addr:
            try:
                addr = json.loads(addr)
            except ValueError:
                addr = {}
        if isinstance(addr, dict):
            r["city"], r["state"], r["country"] = (clean(addr.get("CITY")), clean(addr.get("STATE")),
                                                   clean(addr.get("COUNTRY")))
    showid = raw.get("showid") or ""
    title = clean((raw.get("title") or "").split("|")[0])
    meta = {"platform": "mapyourshow", "showid": showid,
            "show_base": clean(re.sub(r"\b20\d{2}\b", "", title)) or None,
            "show_year": year_from_showid(showid), "source_year_text": showid,
            "platform_count": len(gallery), "fetched": sum(len(v) for v in by_exh.values()),
            "named": len(gallery), "halls": len(halls)}
    return finish(rows), meta


# ------------------------------------------------------------------ live

def _fetch_detail(client: scraper.MapYourShowClient, exhid: str) -> dict:
    try:
        resp = client.session.get(client.detail_url(exhid), headers=scraper.REQUEST_HEADERS, timeout=20)
        if resp.status_code != 200:
            return {}
        html = resp.text
    except requests.RequestException:
        return {}

    def get(key: str) -> str:
        m = re.search(key + r'\s*:\s*"([^"]*)"', html)
        return m.group(1).replace("\\/", "/").strip() if m else ""
    site = get("websiteValue")
    if site and not site.lower().startswith("http"):
        site = "https://" + site
    addr = re.search(r"addressValues\s*:\s*(\{[^}]*\})", html)
    return {"website": site, "linkedin": get("linkedInValue"), "phone": get("phoneValue"),
            "address": addr.group(1) if addr else "",
            "has_video": bool(scraper.MapYourShowClient.VIDEO_RE.search(html))}


def fetch_raw(url: str, log=None) -> dict:
    log = log or (lambda m: None)
    client = scraper.MapYourShowClient(url)
    try:
        halls = client.halls()
        gallery_resp = client._json(client.proxy, {"action": "search", "searchtype": "exhibitorgallery",
                                                   "searchsize": 20000}, client.referer)
    except scraper.ScrapeError as exc:
        raise ExtractionError(str(exc)) from exc
    hits = (((gallery_resp.get("DATA") or {}).get("results") or {}).get("exhibitor") or {}).get("hit") or []
    if not hits:
        raise ExtractionError("MapYourShow returned zero exhibitors. The directory may not be published yet.")
    log(f"MapYourShow: {len(halls)} halls, {len(hits)} exhibitors")
    showid, fpver = client.show_id_and_version()
    hall_booths: dict = {}
    for version in ["02"] + ([fpver] if fpver != "02" else []):
        def one(hall):
            url2 = f"{client.origin}/{client.root}/floorplan/{version}/_remote-proxy.cfm"
            try:
                r = client._get(url2, {"showid": showid, "selectedbooth": "", "hallid": hall,
                                       "action": "GetBoothByHall", "method": "GetBoothByHall", "regid": 0},
                                client.fp_referer)
                return hall, (r.json() if r.status_code == 200 else None)
            except (scraper.ScrapeError, ValueError):
                return hall, None
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            got = {h: d for h, d in pool.map(one, list(halls)) if isinstance(d, dict)}
        if any(d.get("DATA") for d in got.values()):
            hall_booths = got
            break
    try:
        title = client.session.get(client.referer, headers=scraper.REQUEST_HEADERS, timeout=20).text
        m = re.search(r"<title>(.*?)</title>", title, re.S | re.I)
        title = m.group(1) if m else ""
    except requests.RequestException:
        title = ""
    raw = {"halls": halls, "gallery": hits, "hall_booths": hall_booths, "details": {}, "showid": showid,
           "origin": client.origin, "root": client.root, "title": clean(title)}
    rows, _ = normalise(raw)
    todo = [r["exhid"] for r in rows if r["sqft"] >= DETAIL_MIN_SQFT and r.get("exhid")]
    log(f"MapYourShow: reading {len(todo)} detail pages ({DETAIL_MIN_SQFT}+ sq ft) for website and HQ city/state")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        raw["details"] = dict(zip(todo, pool.map(lambda i: _fetch_detail(client, i), todo)))
    return raw


def extract(url: str, log=None) -> tuple[list[dict], dict, dict]:
    raw = fetch_raw(url, log)
    rows, meta = normalise(raw)
    return rows, meta, raw
