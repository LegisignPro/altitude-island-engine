"""
A2Z / Personify event maps (*.a2zinc.net, mya2zevents.com, white-label .../Public/EventMap.aspx).

Verified on KBIS 2026 (kbis.a2zinc.net/kbis2026/Public/EventMap.aspx?shMode=E), 2026-09-23.

1. EventMap.aspx HTML carries intRootEventID, strRootApplicationID and one data-mapId per hall.
   customTileBaseUrl, when present, replaces the default API host https://img14.a2zinc.net.
2. <base>/api/exhibitor?mapId&eventId&appId&floorplanViewType=View4&langId=1&boothId=&shMode=E
   answers per map with one record per booth (JSONP when a callback is passed; the site's own
   page uses JSONP because the API sends no CORS headers). Calling it without a mapId gives 503.
       {name, label: {text: booth}, id, status (2 assigned / 0 open), size, dimension: "20 x 30",
        unit: "sq ft", coExhs: [...], enhanced, videoCount, hyperLinkFieldValue (booth id), ...}
   There is no company id, so booths are grouped by normalised name.
3. eBooth.aspx?BoothID=<id> is the public booth page: BoothContactCity / State / Country / Url.
   Read only for booths at or above DETAIL_MIN_SQFT.
Coverage reference: the number of a.boothLabel rows in the EventMap.aspx exhibitor list.
KBIS 2026: 632 booth records on 3 maps, 609 booth labels.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .common import (SQM_TO_SQFT, ExtractionError, clean, dims, finish, group_booths, num, year_from)

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"}
TIMEOUT = 45
DETAIL_MIN_SQFT = 400
WORKERS = 4
DEFAULT_API = "https://img14.a2zinc.net"


# ------------------------------------------------------------------ parsing (pure)

def parse_event_map(html: str) -> dict:
    ev = re.search(r"intRootEventID\s*=\s*['\"]?(\d+)", html)
    app = re.search(r"strRootApplicationID\s*=\s*['\"]([^'\"]+)['\"]", html)
    maps = []
    for m in re.finditer(r"data-mapId=[\"'](\d+)[\"']", html, re.I):
        if m.group(1) not in maps:
            maps.append(m.group(1))
    tile = re.search(r"customTileBaseUrl\s*=\s*['\"]([^'\"]+)['\"]", html)
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return {"event_id": ev.group(1) if ev else None, "app_id": app.group(1) if app else None, "map_ids": maps,
            "api_base": (tile.group(1) if tile else DEFAULT_API).rstrip("/"),
            "label_count": len(soup.select("a.boothLabel")), "title": title}


def strip_jsonp(text: str):
    """'cb([...]);' or bare '[...]' -> parsed JSON."""
    t = text.strip()
    m = re.match(r"^[\w$.]+\s*\((.*)\)\s*;?\s*$", t, re.S)
    return json.loads(m.group(1) if m else t)


def normalise(raw: dict) -> tuple[list[dict], dict]:
    """raw = {"records": [...], "details": {booth_id: {...}}, "label_count": int, "title": str}. No network."""
    details = raw.get("details") or {}
    booths, sqm_seen = [], False
    for b in raw.get("records") or []:
        name = clean(b.get("name"))
        if not name or b.get("status") == 0:
            continue
        unit = str(b.get("unit") or "sq ft").lower()
        sqm = "m" in unit and "ft" not in unit
        sqm_seen = sqm_seen or sqm
        area = num(b.get("size")) * (SQM_TO_SQFT if sqm else 1)
        w, l = dims(b.get("dimension"))
        booth_id = str(b.get("hyperLinkFieldValue") or b.get("id") or "")
        label = b.get("label")
        extra = {"exhid": booth_id, "is_sponsor": bool(b.get("enhanced")),
                 "has_video_listing": (b.get("videoCount") or 0) > 0}
        extra.update({k: v for k, v in (details.get(booth_id) or {}).items() if v})
        booths.append({"name": name, "booth": clean(label.get("text") if isinstance(label, dict) else label or b.get("boothName")),
                       "hall": "", "area": round(area), "w": w, "l": l,
                       "raw": clean(b.get("dimension")) or (f"{b.get('size')} {b.get('unit') or ''}" if b.get("size") else ""),
                       "shared": 1 + len(b.get("coExhs") or []), "extra": extra})
    rows = group_booths(booths, "a2z", "a2z-map-sqm" if sqm_seen else "a2z-map")
    # a company on several booths keeps the first booth's id; fold in details found under any of its booths
    for r in rows:
        if r.get("detail_url") == "" and r.get("exhid") and raw.get("booth_url_base"):
            r["detail_url"] = f"{raw['booth_url_base']}eBooth.aspx?BoothID={r['exhid']}"
    title = clean(re.sub(r"-\s*Event Map.*$", "", raw.get("title") or "", flags=re.I))
    meta = {"platform": "a2z", "show_base": clean(re.sub(r"\b20\d{2}\b", "", title)) or None,
            "show_year": year_from(title), "source_year_text": title,
            "platform_count": raw.get("label_count") or 0,
            "fetched": len(raw.get("records") or []), "named": len(booths)}
    return finish(rows), meta


# ------------------------------------------------------------------ live

def event_map_url(url: str) -> str:
    p = urlparse(url)
    if re.search(r"EventMap\.aspx$", p.path, re.I):
        return url
    folder = p.path.rsplit("/", 1)[0]
    return f"{p.scheme or 'https'}://{p.netloc}{folder}/EventMap.aspx?shMode=E"


def _get(session: requests.Session, url: str, params: dict | None = None) -> requests.Response:
    for attempt in range(3):
        r = session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code in (429, 503):
            time.sleep(1.5 * (attempt + 1))
            continue
        return r
    return r


def _detail(session: requests.Session, url: str) -> dict:
    try:
        r = _get(session, url)
        if r.status_code != 200:
            return {}
    except requests.RequestException:
        return {}
    soup = BeautifulSoup(r.text, "html.parser")

    def t(cls: str) -> str:
        el = soup.find(class_=cls)
        return clean(el.get_text(" ", strip=True)).rstrip(",") if el else ""
    site = t("BoothContactUrl")
    if site and not site.lower().startswith("http"):
        site = "https://" + site
    return {"city": t("BoothContactCity"), "state": t("BoothContactState"), "country": t("BoothContactCountry"),
            "website": site}


def fetch_raw(url: str, log=None) -> dict:
    log = log or (lambda m: None)
    session = requests.Session()
    em_url = event_map_url(url)
    r = _get(session, em_url)
    if r.status_code != 200:
        raise ExtractionError(f"A2Z event map returned HTTP {r.status_code}.")
    info = parse_event_map(r.text)
    if not (info["event_id"] and info["app_id"] and info["map_ids"]):
        raise ExtractionError("This A2Z page has no event-map ids. Use the show's EventMap.aspx floor-plan URL.")
    log(f"A2Z: event {info['event_id']}, {len(info['map_ids'])} map(s)")
    records = []
    for map_id in info["map_ids"]:
        params = {"mapId": map_id, "eventId": info["event_id"], "appId": info["app_id"], "floorplanViewType": "View4",
                  "langId": "1", "boothId": "", "shMode": "E", "callback": "aig"}
        resp = _get(session, f"{info['api_base']}/api/exhibitor", params)
        if resp.status_code != 200:
            raise ExtractionError(f"A2Z booth API returned HTTP {resp.status_code} for map {map_id}.")
        data = strip_jsonp(resp.text)
        data = data if isinstance(data, list) else (data or {}).get("data") or []
        log(f"  map {map_id}: {len(data)} booths")
        records.extend(data)
        time.sleep(0.3)
    booth_url_base = em_url.split("?")[0].rsplit("/", 1)[0] + "/"
    raw = {"event_id": info["event_id"], "map_ids": info["map_ids"], "label_count": info["label_count"],
           "title": info["title"], "records": records, "details": {}, "booth_url_base": booth_url_base}
    rows, _ = normalise(raw)
    todo = [r for r in rows if r["sqft"] >= DETAIL_MIN_SQFT and r.get("exhid")]
    log(f"A2Z: reading {len(todo)} booth pages ({DETAIL_MIN_SQFT}+ sq ft) for website and HQ city/state")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        found = list(pool.map(lambda row: _detail(session, f"{booth_url_base}eBooth.aspx?BoothID={row['exhid']}"), todo))
    raw["details"] = {row["exhid"]: d for row, d in zip(todo, found) if d}
    return raw


def extract(url: str, log=None) -> tuple[list[dict], dict, dict]:
    raw = fetch_raw(url, log)
    rows, meta = normalise(raw)
    return rows, meta, raw
