"""
platforms.py -- the single router: URL -> platform -> extractor.

Detection order: host first (mapyourshow.com, a2zinc.net / mya2zevents.com, expocad.com,
expofp.com), then path fingerprints for white-label copies (/Public/EventMap.aspx for A2Z,
exfx.html for EXPOCAD), then a live probe for MapYourShow on a custom domain (any URL with a
/<n>_<n>/ path whose remote-proxy.cfm answers getBoothHalls, e.g. directory.imts.com/8_0/...).

Every extractor returns (rows, meta, raw):
    rows  one dict per company in extractors.common.ROW_COLUMNS
    meta  platform, show_base, show_year (None when the source doesn't state it -- never guessed),
          platform_count (the platform's own exhibitor count, for the coverage check), fetched, named
    raw   the untouched source payload in canonical form, re-parseable with extractors.<x>.normalise()
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import scraper
from extractors import a2z, expocad, expofp, mys
from extractors.common import ExtractionError

SUPPORTED = "MapYourShow (incl. custom domains), A2Z/Personify, EXPOCAD FX, ExpoFP"

# Common platforms nobody has inspected yet. URLs on these hosts get a clear "not yet
# supported" message instead of a failed scrape.
ROADMAP = {
    "coconnex.com": "Coconnex (e.g. Money20/20)",
    "swapcard.com": "Swapcard (Informa / Emerald events)",
    "expoplatform.com": "ExpoPlatform",
    "mapdynamics.com": "Map Dynamics",
}

_A2Z_PATH = re.compile(r"/Public/(EventMap|Exhibitors|eBooth)\.aspx", re.I)
_EXPOCAD_PATH = re.compile(r"exfx\.html$", re.I)
_MYS_PATH = re.compile(r"/\d+_\d+/", re.I)


class UnsupportedPlatform(ExtractionError):
    pass


def platform_for(url: str, probe: bool = False) -> str | None:
    """Host / path detection. With probe=True, also tries the MapYourShow proxy on custom domains."""
    parsed = urlparse(url if "://" in url else "https://" + url)
    host, path = parsed.netloc.lower(), parsed.path
    if host.endswith("mapyourshow.com"):
        return "mapyourshow"
    if host.endswith("a2zinc.net") or host.endswith("mya2zevents.com"):
        return "a2z"
    if host.endswith("expocad.com") or host.endswith("expocadweb.com"):
        return "expocad"
    if host.endswith("expofp.com"):
        return "expofp"
    if _A2Z_PATH.search(path):
        return "a2z"
    if _EXPOCAD_PATH.search(path):
        return "expocad"
    if _MYS_PATH.search(path):
        if not probe or mys.probe(url):
            return "mapyourshow"
    return None


def roadmap_name(url: str) -> str | None:
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    return next((name for dom, name in ROADMAP.items() if host.endswith(dom)), None)


_EXTRACTORS = {"mapyourshow": mys.extract, "a2z": a2z.extract, "expocad": expocad.extract, "expofp": expofp.extract}


def extract(url: str, log=None) -> tuple[list[dict], dict, dict]:
    log = log or (lambda m: None)
    if not url.startswith("http"):
        url = "https://" + url
    road = roadmap_name(url)
    if road:
        raise UnsupportedPlatform(f"{road} isn't supported yet. Supported: {SUPPORTED}.")
    platform = platform_for(url, probe=True)
    if platform is None:
        host = urlparse(url).netloc or url
        raise UnsupportedPlatform(f"Couldn't recognise a floor-plan platform at {host}. Supported: {SUPPORTED}. "
                                  "Tip: the Map Grabber bookmark detects the platform from inside the page.")
    try:
        rows, meta, raw = _EXTRACTORS[platform](url, log=log)
    except scraper.ScrapeError as exc:
        raise ExtractionError(str(exc)) from exc
    meta.update({"platform": platform, "url": url, "source": "live", "total": len(rows),
                 "sized": sum(1 for r in rows if r.get("sqft", 0) > 0)})
    if not meta.get("show_base"):
        meta["show_base"] = scraper.infer_show_base(url) if platform == "mapyourshow" else None
    if meta.get("show_year") is None and platform == "mapyourshow":
        meta["show_year"] = scraper.infer_show_year(url)
    meta["show_name"] = " ".join(str(x) for x in (meta.get("show_base"), meta.get("show_year")) if x) or url
    return rows, meta, raw


def extract_directory(url: str, log=None) -> tuple[list[dict], dict]:
    """v2-compatible wrapper: (rows, meta)."""
    rows, meta, _ = extract(url, log=log)
    return rows, meta
