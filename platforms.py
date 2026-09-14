"""
platforms.py -- routes a directory URL to the right scraper by host. app.py and the Task 2
timeline slots both call extract_directory() instead of reaching into scraper.py / browser_scraper.py
directly, so adding a fifth platform later only means adding one more elif here.
"""

from __future__ import annotations

from urllib.parse import urlparse

import browser_scraper
import scraper

SUPPORTED = "MapYourShow, A2Z/Personify, EXPOCAD, ExpoFP"


def platform_for(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if "mapyourshow.com" in host:
        return "mapyourshow"
    if "a2zinc.net" in host or "mya2zevents.com" in host:
        return "a2z"
    if "expocadweb.com" in host or "expocad.com" in host:
        return "expocad"
    if "expofp.com" in host:
        return "expofp"
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
