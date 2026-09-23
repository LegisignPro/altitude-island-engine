"""
ExpoFP floor plans (<slug>.expofp.com). Plain HTTP, no browser.

Verified on IMEX America 2026 (imexamerica26.expofp.com), 2026-09-23:

  /data/data.js        `var __data = {...};`  title, startDate, exhibitors[], booths[]
                       booths[].exhibitors is a LIST of exhibitor ids (the same company can have several ids across
                       pavilions, so rows are grouped by normalised name); booths[].special marks
                       points of interest (food court, stage...). Booths carry no area.
  /data/fp.svg.js      the default layer's SVG as a JS template literal, plus
                       `var __fpLayers = [{name: ...}, ...]` naming every layer file:
  /data/fp.svg.<name>.js   one file per level/layer (IMEX: AM, LL, 1, 2)
                       Booth shapes have id "b" + booth name. Rectangles give width x height.
                       Irregular booths are <g id="b..."> whose <path data-index="N"> points at
                       window['__fpPaths<layer>'][N].positions, a polygon -> shoelace area.
                       The root <svg units="ft|m"> gives the unit.

Pages at expofp.com/<venue>/<show> are ExpoFP marketing/calendar pages with no exhibitor data;
they raise a clear error instead of returning zero rows.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse

import requests

from .common import (SQM_TO_SQFT, ExtractionError, clean, finish, group_booths, name_key, num, shoelace,
                     year_from)

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"}
TIMEOUT = 45


def is_venue_page(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host in ("expofp.com", "www.expofp.com")


def _get(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code != 200:
        raise ExtractionError(f"ExpoFP returned HTTP {r.status_code} for {urlparse(url).path}")
    return r.text


# ------------------------------------------------------------------ parsing (pure)

def parse_data_js(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ExtractionError("ExpoFP data.js did not contain a data object.")
    return json.loads(text[start:end + 1])


def layer_names(fp_svg_js: str) -> list[str]:
    m = re.search(r"__fpLayers\s*=\s*(\[.*?\]);\s*\r?\n", fp_svg_js, re.S)
    if not m:
        return []
    try:
        return [str(layer.get("name")) for layer in json.loads(m.group(1)) if layer.get("name")]
    except ValueError:
        return []


def _template(js: str) -> str:
    a, b = js.find("`"), js.rfind("`")
    return js[a + 1:b] if a >= 0 and b > a else ""


def _paths(js: str) -> list:
    m = re.search(r"window\['__fpPaths[^']*'\]\s*=\s*(\[.*?\]);\s*\r?\n", js, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(1))
    except ValueError:
        return []


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_layer(name: str, js: str) -> dict:
    """One fp.svg*.js file -> {"name", "units", "shapes": [{id, tag, width, height} | {id, tag, positions}]}."""
    svg = _template(js)
    out = {"name": name, "units": "", "shapes": []}
    if not svg:
        return out
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise ExtractionError(f"ExpoFP layer {name!r} is not valid SVG: {exc}") from exc
    out["units"] = root.get("units") or ""
    paths = _paths(js)
    for el in root.iter():
        el_id = el.get("id") or ""
        if not el_id.startswith("b"):
            continue
        tag = _local(el.tag)
        if tag == "rect":
            out["shapes"].append({"id": el_id, "tag": "rect", "width": num(el.get("width")), "height": num(el.get("height"))})
            continue
        positions = []
        for child in el.iter():
            if _local(child.tag) == "path" and child.get("data-index") is not None:
                idx = int(num(child.get("data-index")))
                if 0 <= idx < len(paths) and isinstance(paths[idx], dict):
                    positions = paths[idx].get("positions") or []
                break
        out["shapes"].append({"id": el_id, "tag": tag, "positions": positions})
    return out


def normalise(raw: dict) -> tuple[list[dict], dict]:
    """
    raw = {"data": {title, startDate, booths[], exhibitors[]}, "layers": [parse_layer(...), ...]}
    (the same shape the browser grabber saves in debug mode). No network.
    """
    data = raw.get("data") or {}
    area: dict[str, float] = {}
    units_ft = True
    for layer in raw.get("layers") or []:
        units = layer.get("units") or ""
        if units and units != "ft":
            units_ft = False
        mult = SQM_TO_SQFT if units == "m" else 1.0
        for s in layer.get("shapes") or []:
            a = (s.get("width") or 0) * (s.get("height") or 0) if s.get("tag") == "rect" else shoelace(s.get("positions"))
            bid = str(s.get("id") or "")[1:]
            if a > 0 and bid not in area:
                area[bid] = a * mult
    ex_by_id = {e.get("id"): e for e in data.get("exhibitors") or []}
    booths, linked = [], set()
    for b in data.get("booths") or []:
        if b.get("special") or not b.get("exhibitors"):
            continue
        ids = [i for i in b["exhibitors"] if i in ex_by_id and clean(ex_by_id[i].get("name"))]
        for i in ids:
            linked.add(name_key(ex_by_id[i].get("name")))
            e = ex_by_id[i]
            a = area.get(str(b.get("name")), 0.0)
            desc = clean(re.sub(r"<[^>]+>", " ", str(e.get("description") or "")))[:500]
            booths.append({"key": name_key(e.get("name")), "name": clean(e.get("name")), "booth": clean(b.get("name")), "hall": "",
                           "area": round(a), "raw": f"{round(a)} sq ft (from floor-plan shape)" if a else "",
                           "shared": len(ids),
                           "extra": {"exhid": str(i), "is_sponsor": bool(e.get("featured")), "description": desc}})
    rows = finish(group_booths(booths, "expofp", "expofp-svg" if units_ft else "expofp-svg-sqm"))
    title = clean(data.get("title"))
    meta = {"platform": "expofp", "show_base": clean(re.sub(r"\b20\d{2}\b", "", title)) or None,
            "show_year": year_from(data.get("startDate")) or year_from(title),
            "source_year_text": data.get("startDate") or title,
            "platform_count": len(linked), "fetched": len(data.get("booths") or []),
            "named": sum(1 for b in data.get("booths") or [] if not b.get("special") and b.get("exhibitors"))}
    return rows, meta


# ------------------------------------------------------------------ live

def fetch_raw(url: str, log=None) -> dict:
    log = log or (lambda m: None)
    if is_venue_page(url):
        raise ExtractionError("That is an ExpoFP calendar/marketing page (expofp.com/<venue>/<show>), not a floor "
                              "plan. Open the show's own map at <show>.expofp.com and use that URL.")
    origin = f"{urlparse(url).scheme or 'https'}://{urlparse(url).netloc}"
    data = parse_data_js(_get(origin + "/data/data.js"))
    base = _get(origin + "/data/fp.svg.js")
    layers = [parse_layer("(default)", base)]
    names = layer_names(base)
    for name in names:
        try:
            layers.append(parse_layer(name, _get(f"{origin}/data/fp.svg.{quote(name)}.js")))
        except ExtractionError:
            pass  # the default layer is inside fp.svg.js itself, so one 404 here is expected
    log(f"ExpoFP: {len(data.get('booths') or [])} booths, {len(data.get('exhibitors') or [])} exhibitor records, "
        f"{len(layers)} layer file(s)")
    keep = {"title", "startDate", "booths", "exhibitors"}
    return {"data": {k: v for k, v in data.items() if k in keep}, "layers": layers}


def extract(url: str, log=None) -> tuple[list[dict], dict, dict]:
    raw = fetch_raw(url, log)
    rows, meta = normalise(raw)
    return rows, meta, raw
