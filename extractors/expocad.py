"""
EXPOCAD FX floor plans.

Verified on Data Center World 2026 (www.expocad.com/host/fx/informa/26dcw/exfx.html), 2026-09-23.

The page loads static XML files and its own script builds `window.data`:
    window.data.booths[]      {number, status, exhibitorIndex, areaF: "400 SqFt", areaM, dimF: "20' x 20'"}
    window.data.exhibitors[]  {name, exhId, id, website, city, state, country, phone, category, profile, ...}
Join: exhibitors[int(booth.exhibitorIndex)]. Status "0" = open booth.
Show title: attribute eT in config_<code>.xml next to the page.

Exhibitor names in ex_<code>.xml are obfuscated. We do NOT decode that file: we read what
EXPOCAD's own page renders for every visitor (window.data), which needs a real browser.
That makes EXPOCAD the one platform that needs Playwright on the server; the browser grabber
avoids the problem entirely because it runs in your own Chrome.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from .common import (SQM_TO_SQFT, ExtractionError, clean, dims, finish, group_booths, name_key, num,
                     year_from)

WAIT_SECONDS = 45

_SLIM_JS = """() => {
  const D = window.data || {};
  const b = (D.booths || []).map(x => ({number: x.number, status: x.status, exhibitorIndex: x.exhibitorIndex,
                                         areaF: x.areaF, areaM: x.areaM, dimF: x.dimF}));
  const e = (D.exhibitors || []).map(x => ({name: x.name, exhId: x.exhId, id: x.id, website: x.website, city: x.city,
                                             state: x.state, country: x.country, phone: x.phone, category: x.category,
                                             profile: String(x.profile || '').slice(0, 500)}));
  return {booths: b, exhibitors: e};
}"""


def normalise(raw: dict) -> tuple[list[dict], dict]:
    """raw = {"booths": [...], "exhibitors": [...], "config_title": str}. No network."""
    ex = raw.get("exhibitors") or []
    booths = []
    for b in raw.get("booths") or []:
        try:
            e = ex[int(b.get("exhibitorIndex"))]
        except (TypeError, ValueError, IndexError):
            continue
        if not e or not clean(e.get("name")) or str(b.get("status")) == "0":
            continue
        area = num(b.get("areaF"))
        if not area and b.get("areaM"):
            area = num(b.get("areaM")) * SQM_TO_SQFT
        w, l = dims(b.get("dimF"))
        company = e.get("exhId") or e.get("id") or name_key(e.get("name"))
        booths.append({"key": f"id:{company}", "name": clean(e.get("name")), "booth": clean(b.get("number")), "hall": "",
                       "area": round(area), "w": w, "l": l, "raw": clean(b.get("dimF") or b.get("areaF")), "shared": 1,
                       "extra": {"exhid": str(e.get("exhId") or e.get("id") or ""), "website": clean(e.get("website")),
                                 "city": clean(e.get("city")), "state": clean(e.get("state")),
                                 "country": clean(e.get("country")), "phone": clean(e.get("phone")),
                                 "description": clean(e.get("profile"))[:500], "categories": clean(e.get("category"))}})
    rows = finish(group_booths(booths, "expocad", "expocad-fx"))
    title = clean(raw.get("config_title"))
    meta = {"platform": "expocad", "show_base": clean(re.sub(r"\b20\d{2}\b", "", title)) or None,
            "show_year": year_from(title), "source_year_text": title,
            "platform_count": sum(1 for e in ex if clean((e or {}).get("name"))),
            "fetched": len(raw.get("booths") or []), "named": len(booths)}
    return rows, meta


def config_url(url: str) -> str:
    p = urlparse(url)
    folder = p.path.rsplit("/", 1)[0]
    code = folder.rsplit("/", 1)[-1]
    return f"{p.scheme or 'https'}://{p.netloc}{folder}/config_{code}.xml"


def _config_title(url: str) -> str:
    try:
        r = requests.get(config_url(url), timeout=30)
        m = re.search(r'\beT="([^"]+)"', r.text) if r.status_code == 200 else None
        return m.group(1) if m else ""
    except requests.RequestException:
        return ""


def _capture(url: str) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ExtractionError("EXPOCAD needs a browser on the server and Playwright isn't installed here. "
                              "Use the Map Grabber bookmark in your own Chrome instead.") from exc
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_function("() => window.data && window.data.booths && window.data.booths.length > 0",
                                   timeout=WAIT_SECONDS * 1000)
            return page.evaluate(_SLIM_JS)
        finally:
            browser.close()


def fetch_raw(url: str, log=None) -> dict:
    log = log or (lambda m: None)
    try:
        import browser_scraper
        if not browser_scraper.ensure_browser():
            raise ExtractionError("EXPOCAD needs a browser and none can start on this host. "
                                  "Use the Map Grabber bookmark in your own Chrome instead.")
    except ImportError:
        pass
    log("EXPOCAD: loading the floor plan in a headless browser (up to 45 s)...")
    try:
        # Playwright's sync API can't run on Streamlit's script thread (it has an event loop).
        with ThreadPoolExecutor(max_workers=1) as pool:
            data = pool.submit(_capture, url).result(timeout=WAIT_SECONDS + 90)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"EXPOCAD floor plan did not load: {exc.__class__.__name__}. "
                              f"Try the Map Grabber bookmark in your own Chrome.") from exc
    data["config_title"] = _config_title(url)
    log(f"EXPOCAD: {len(data['booths'])} booths, {len(data['exhibitors'])} exhibitor records")
    return data


def extract(url: str, log=None) -> tuple[list[dict], dict, dict]:
    raw = fetch_raw(url, log)
    rows, meta = normalise(raw)
    return rows, meta, raw
