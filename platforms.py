"""
platforms.py -- routes a directory URL to the right scraper by host. app.py and the Task 2
timeline slots both call extract_directory() instead of reaching into scraper.py / browser_scraper.py
directly, so adding a fifth platform later only means adding one more elif here.

Host matching alone misses white-labelled deployments, where a show organiser puts one of
these platforms on its OWN domain instead of the vendor's (confirmed live: CES's MapYourShow
gallery is at exhibitors.ces.tech, SEMA's EXPOCAD floor plan is at semashow.com/floorplan, and
Woz found a real A2Z show at a2z.aafp.org -- none of those hostnames contain a2zinc.net /
mapyourshow.com / expocad.com). For A2Z specifically, every real-world URL seen so far --
white-labelled or not -- uses the same event-map path from A2Z/Personify's own software:
`/Public/EventMap.aspx` (case-insensitive). That path is checked as a fallback whenever the
host doesn't already say a2z.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import browser_scraper
import scraper

SUPPORTED = "MapYourShow, A2Z/Personify, EXPOCAD, ExpoFP"

# Path signature fallback for platforms that get white-labelled onto a show's own domain, so a
# host-only check would miss them. Checked in order; first match wins. Each entry is a compiled
# regex tested against the URL's path (case-insensitive).
_PATH_SIGNATURES = [
    ("a2z", re.compile(r"/Public/eventmap\.aspx", re.I)),
    # MapYourShow / Map Dynamics keeps this exact path shape on white-labelled domains too
    # (confirmed live on exhibitors.ces.tech, which is not a mapyourshow.com host).
    ("mapyourshow", re.compile(r"/8_0/(explore/exhibitor-gallery|floorplan|sitemap)", re.I)),
]


def platform_for(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "mapyourshow.com" in host:
        return "mapyourshow"
    if "a2zinc.net" in host or "mya2zevents.com" in host:
        return "a2z"
    if "expocadweb.com" in host or "expocad.com" in host:
        return "expocad"
    if "expofp.com" in host:
        return "expofp"
    for platform, pattern in _PATH_SIGNATURES:
        if pattern.search(parsed.path):
            return platform
    return None


def extract_directory(url: str, log=None) -> tuple[list[dict], dict]:
    """
    (rows, meta) in the same EXHIBITOR_COLUMNS shape regardless of platform. Website enrichment
    (scraper.enrich_websites) is MapYourShow-specific -- the other platforms already carry a
    website field straight from their own JSON, so callers should only run that extra step when
    meta["platform"] == "mapyourshow" (or the field is simply blank elsewhere, as scraper.py's
    columns always default to "").
    """
    log = log or (lambda msg: None)
    platform = platform_for(url)
    if platform == "mapyourshow":
        rows, meta = scraper.scrape_mapyourshow(url, log=log)
        meta["platform"] = "mapyourshow"
        return rows, meta
    if platform == "a2z":
        return browser_scraper.extract_a2z(url, log=log)
    if platform == "expocad":
        return browser_scraper.extract_expocad(url, log=log)
    if platform == "expofp":
        return browser_scraper.extract_expofp(url, log=log)
    host = urlparse(url).netloc or url
    raise scraper.ScrapeError(f"Unsupported platform for host '{host}'. Supported: {SUPPORTED}.")
