"""
show_finder.py -- "Find it for me": given a trade show's name, search the web (Tavily) for its
exhibitor-directory / floor-plan URLs on the platforms this engine can already read (MapYourShow,
A2Z, EXPOCAD, ExpoFP) and VERIFY each candidate's actual page content before offering it.

Grown from a standalone prototype (experiments/find_show_urls.py, since retired) whose query design and
verification heuristic were validated live against real shows through Tavily's search + extract
API. What that validation established, and what this module preserves:

  1. A plain query -- "<show name> exhibitor directory floor plan" -- surfaces the CURRENT
     edition's real directory URL directly (e.g. nab26.mapyourshow.com/.../exhview.cfm). No domain
     restriction is needed; the platform reveals itself in the result URL.
  2. Adding a year ("<show> 2025 exhibitor directory floor plan") sometimes turns up a genuine
     ARCHIVED directory, often hosted somewhere other than the platform's own domain (NAB 2025 was
     a third-party flipbook PDF with real booth numbers). For NAB, 2024 and 2023 produced nothing
     directory-shaped. So "how many years of maps exist" has an honest, per-show answer: however
     far back search + verification still confirms a hit -- reported here as `years_with_maps`,
     never assumed or extrapolated.
  3. A same-pattern URL GUESS for an older year (nab24.mapyourshow.com, extrapolated from nab26)
     can return HTTP 200 while silently serving the CURRENT marketing homepage. A 200 is never
     verification. Every candidate is extracted and content-checked (`looks_like_directory`)
     before it is called good, and nothing unverified is ever returned to the caller.

Network reality: the container this was written in cannot reach api.tavily.com at all (egress is
limited to package registries and GitHub), so the live SDK path is exercised only where the app
is deployed (Streamlit Community Cloud) or on a developer's own machine. Everything that can be
tested offline is: query construction, the verification heuristic, platform labelling, result
assembly and every failure mode, all against a mocked client (tests/test_modules.py).

Key: TAVILY_API_KEY via st.secrets / the sidebar field (same pattern as APOLLO_API_KEY). Free
tier is ~1,000 searches a month; one show lookup costs 1 search + 1 extract call per year probed.
"""

from __future__ import annotations

import re
from datetime import date

import platforms

DEFAULT_YEARS_BACK = 2          # prior editions probed by default (each costs one search + one extract)
MAX_YEARS_BACK = 5
MAX_RESULTS_PER_QUERY = 10
QUERY_SUFFIX = "exhibitor directory floor plan"

# Generic marketing-homepage boilerplate that appears when a guessed/stale URL silently falls back
# to a show's main site instead of erroring (seen live on nab24 / nab25 .mapyourshow.com, both 200).
HOMEPAGE_TELLS = ("get notified when registration opens", "register today", "book housing")
# Signal that a page really is (or embeds) an exhibitor listing / floor plan. "{boothid}" is
# EXPOCAD's unrendered tooltip template, present verbatim in SEMA's white-labelled floor plan.
DIRECTORY_TELLS = ("exhibitor", "booth", "sq. ft", "sq ft", "floor plan", "{boothid}")
# Content signature for a platform white-labelled onto a show's own domain with no URL hint
# (semashow.com/floorplan is EXPOCAD under the hood). Host/path signatures live in platforms.py.
CONTENT_SIGNATURES = {"expocad": (r"\{BOOTHID\}", r"\{AVAILBOOTHSQFEET\}")}


class ShowFinderError(Exception):
    """Plain-English reason the finder could not run (no key, no SDK, API/network failure)."""


# =============================================================================
# Pure logic (fully unit-tested offline)
# =============================================================================

def build_query(show_name: str, year: int | None = None) -> str:
    show = " ".join(str(show_name or "").split())
    return f"{show} {year} {QUERY_SUFFIX}" if year else f"{show} {QUERY_SUFFIX}"


def looks_like_directory(content: str) -> bool:
    """
    Heuristic, not a guarantee. At least two distinct directory tells and NO homepage tells.
    Validated against four real pages: two true positives (NAB 2026/2027 gallery, SEMA floor
    plan) and two true negatives (nab24 / nab25 stale-redirect homepages).
    """
    low = (content or "").lower()
    homepage_hits = sum(1 for t in HOMEPAGE_TELLS if t in low)
    directory_hits = sum(1 for t in DIRECTORY_TELLS if t in low)
    return directory_hits >= 2 and homepage_hits == 0


def classify_platform(url: str, content: str = "") -> str | None:
    """platforms.platform_for() (host + white-label path signatures), then a content fallback."""
    p = platforms.platform_for(url)
    if p:
        return p
    for platform, patterns in CONTENT_SIGNATURES.items():
        if any(re.search(pat, content or "") for pat in patterns):
            return platform
    return None


def years_to_probe(anchor_year: int | None, back: int = DEFAULT_YEARS_BACK) -> list[int]:
    """Prior editions to search, newest first, anchored on the show year the user set (or today)."""
    base = int(anchor_year) if anchor_year else date.today().year
    back = max(0, min(int(back), MAX_YEARS_BACK))
    return [base - i for i in range(1, back + 1)]


def _url_year(url: str, title: str = "") -> int | None:
    """A 4-digit (20xx) or 2-digit year embedded in the URL/title, e.g. nab26 -> 2026. Label only."""
    text = f"{url} {title}"
    m = re.search(r"\b(20[2-3]\d)\b", text)
    if m:
        return int(m.group(1))
    m = re.search(r"[a-z](2[0-9])(?=\.|/|\b)", url.lower())
    if m:
        return 2000 + int(m.group(1))
    return None


def _search(client, query: str) -> list[dict]:
    resp = client.search(query=query, search_depth="advanced", max_results=MAX_RESULTS_PER_QUERY)
    return [r for r in (resp or {}).get("results", []) if isinstance(r, dict) and r.get("url")]


def _verify(client, urls: list[str]) -> dict[str, dict]:
    """Extract every URL and judge its content: {url: {"ok", "platform", "preview"}}."""
    out: dict[str, dict] = {}
    if not urls:
        return out
    resp = client.extract(urls=urls, extract_depth="basic") or {}
    for r in resp.get("results", []):
        if not isinstance(r, dict) or not r.get("url"):
            continue
        content = r.get("raw_content") or ""
        out[r["url"]] = {"ok": looks_like_directory(content),
                         "platform": classify_platform(r["url"], content),
                         "preview": re.sub(r"\s+", " ", content)[:200]}
    for r in resp.get("failed_results", []):
        if isinstance(r, dict) and r.get("url"):
            out[r["url"]] = {"ok": False, "platform": None, "preview": "(fetch failed)"}
    return out


def _dedupe(urls: list[str]) -> list[str]:
    seen, out = set(), []
    for u in urls:
        key = u.rstrip("/").lower()
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def _lookup(client, show_name: str, year: int | None, log) -> list[dict]:
    """One search + one extract; only content-verified hits come back."""
    query = build_query(show_name, year)
    candidates = _search(client, query)
    log(f"Search `{query}`: {len(candidates)} result(s)")
    urls = _dedupe([c["url"] for c in candidates])
    verified = _verify(client, urls)
    good = []
    for c in candidates:
        v = verified.get(c["url"])
        if not v or not v["ok"]:
            continue
        if any(g["url"].rstrip("/").lower() == c["url"].rstrip("/").lower() for g in good):
            continue
        good.append({"url": c["url"], "title": (c.get("title") or "").strip(), "platform": v["platform"],
                     "year": year, "url_year": _url_year(c["url"], c.get("title") or ""),
                     "preview": v["preview"], "supported": v["platform"] is not None})
    log(f"  verified as a real exhibitor listing: {len(good)}")
    return good


def find_show_urls(show_name: str, years: list[int] | None = None, client=None, api_key: str = "",
                   log=None) -> dict:
    """
    {
      "show": ..., "current": [hit, ...], "by_year": {2025: [hit, ...], 2024: []},
      "years_with_maps": [2025], "years_probed": [2025, 2024], "log": [...]
    }
    hit = {"url", "title", "platform", "year", "url_year", "preview", "supported"}.
    Every hit has been extracted and passed looks_like_directory; `supported` says whether the
    engine can extract it (platform recognised) or it is a verified listing on some other host
    (e.g. an archived flipbook PDF) that is real but must be read by hand.
    Raises ShowFinderError for anything the caller should show inline; never a raw SDK exception.
    """
    lines: list[str] = []

    def _log(msg: str) -> None:
        lines.append(msg)
        if log:
            log(msg)

    show = " ".join(str(show_name or "").split())
    if not show:
        raise ShowFinderError("Enter a show name first (e.g. NAB Show, SEMA Show, CES).")
    client = client or make_client(api_key)
    out: dict = {"show": show, "current": [], "by_year": {}, "years_with_maps": [],
                 "years_probed": list(years or []), "log": lines}
    try:
        out["current"] = _lookup(client, show, None, _log)
        for y in years or []:
            hits = _lookup(client, show, y, _log)
            out["by_year"][y] = hits
            if hits:
                out["years_with_maps"].append(y)
    except ShowFinderError:
        raise
    except Exception as exc:   # network, quota, auth, malformed response -- all inline, never a crash
        raise ShowFinderError(f"Tavily lookup failed: {exc.__class__.__name__}: {exc}") from exc
    return out


def summarize_years(result: dict) -> str:
    """One line answering 'how many years of maps exist' from what was actually verified."""
    probed = result.get("years_probed") or []
    if not probed:
        return ""
    found = result.get("years_with_maps") or []
    parts = [f"{y}: {len(result['by_year'].get(y, []))} verified" if y in found else f"{y}: none found"
             for y in probed]
    return f"Prior editions with a verifiable directory: {len(found)} of {len(probed)} probed ({', '.join(parts)})."


# =============================================================================
# SDK plumbing (the only part that cannot be exercised offline)
# =============================================================================

def make_client(api_key: str):
    key = (api_key or "").strip()
    if not key:
        raise ShowFinderError("No Tavily API key. Add TAVILY_API_KEY to secrets or paste one in the sidebar "
                              "(free tier at tavily.com).")
    try:
        from tavily import TavilyClient
    except ImportError as exc:
        raise ShowFinderError("tavily-python is not installed (pip install tavily-python).") from exc
    return TavilyClient(api_key=key)


# =============================================================================
# CLI -- run from a machine with open internet (not the build container):
#     TAVILY_API_KEY=... python show_finder.py "NAB Show" --back 2
# =============================================================================

def _cli() -> None:
    import argparse
    import json
    import os

    ap = argparse.ArgumentParser(description="Find + content-verify a trade show's exhibitor directory URL(s).")
    ap.add_argument("show_name", help='e.g. "NAB Show", "SEMA Show", "CES"')
    ap.add_argument("--year", type=int, default=None, help="Show year to count back from (default: this year).")
    ap.add_argument("--back", type=int, default=DEFAULT_YEARS_BACK, help="Prior editions to probe.")
    args = ap.parse_args()
    try:
        res = find_show_urls(args.show_name, years=years_to_probe(args.year, args.back),
                             api_key=os.environ.get("TAVILY_API_KEY", ""), log=print)
    except ShowFinderError as exc:
        raise SystemExit(f"error: {exc}")
    print(json.dumps({k: v for k, v in res.items() if k != "log"}, indent=2, default=str))
    print(summarize_years(res))


if __name__ == "__main__":
    _cli()
