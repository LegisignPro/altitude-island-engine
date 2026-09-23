"""
browser_scraper.py -- headless-Chromium availability check for the one platform that needs it (EXPOCAD).

v2 kept a field-name-guessing engine here for A2Z / EXPOCAD / ExpoFP. On 2026-09-23 it was checked
against real shows and returned zero rows on all three (A2Z serves JSONP, EXPOCAD XML, ExpoFP a .js
file), so v3 replaced it with extractors/ built from the verified formats. Only ensure_browser()
remains.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile


def ensure_browser() -> bool:
    """
    Best-effort one-time Chromium check/install (Streamlit Community Cloud doesn't ship it).
    Never raises; returns whether a headless browser can start. The result is cached in a marker file.
    """
    marker = os.path.join(tempfile.gettempdir(), ".island_engine_playwright_checked")
    if os.path.exists(marker):
        return open(marker).read().strip() == "ok"

    def _launch() -> None:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()

    ok = False
    try:
        _launch()
        ok = True
    except Exception:
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                           check=False, timeout=180, capture_output=True)
            _launch()
            ok = True
        except Exception:
            ok = False
    try:
        with open(marker, "w") as f:
            f.write("ok" if ok else "failed")
    except OSError:
        pass
    return ok
