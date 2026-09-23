"""
apollo.py -- Apollo.io firmographics and contact discovery.

Two live endpoints are used, both keyed on the company's domain (which comes
from the MapYourShow exhibitor detail page, never from a web-search guess):

  Organisation enrichment  GET  /api/v1/organizations/enrich?domain=acme.com
      -> estimated_num_employees, city / state / country (HQ), industry,
         annual_revenue, linkedin_url. Costs 1 credit per organisation on
         every plan (Free plan: 75 credits / month), so the app caps the
         number of enrichments per run (sidebar budget) and caches results.

  People search            POST /api/v1/mixed_people/search
      -> people at the domain whose title matches the buyer titles, with
         name, title, linkedin_url and employment_history (start dates), from
         which time-in-current-role is computed. 0 credits. Email addresses
         are NOT revealed by search (Apollo returns a placeholder); a verified
         email is a separate 1-credit people/match call that the Free plan
         blocks, so the export leaves `email` blank unless Apollo returned a
         real one. Instantly / Smartlead can fill it with their own finders.

Header on both calls:  x-api-key: <master API key>
Key creation:          https://app.apollo.io/#/settings/integrations/api
                       (tick "Set as master API key" -- search + enrichment
                       endpoints require it).

No key, no data: there is no mock mode (removed in v3). Without a key the
Apollo tab simply stays empty.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

import requests

APOLLO_BASE = "https://api.apollo.io/api/v1"
TIMEOUT = 12

# Titles that own the trade-show budget, in priority order.
TARGET_TITLES = [
    "Event Manager",
    "Trade Show Director",
    "CMO",
    "Marketing Director",
    "VP Marketing",
]
MAX_EMPLOYEES = 1000          # larger companies almost always have an agency of record
NEW_HIRE_MONTHS = 6           # time-in-role below this = NEW HIRE TRIGGER

US_STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}


def domain_from_website(website: str) -> str:
    """'https://www.acme.com/products' -> 'acme.com'. Blank in, blank out."""
    if not website:
        return ""
    host = urlparse(website if "://" in website else f"https://{website}").netloc.lower()
    return re.sub(r"^www\.", "", host)


def normalise_state(state: str | None, country: str | None) -> str:
    """Return a 2-letter US state code, or '' for non-US / unknown."""
    if not state:
        return ""
    s = state.strip()
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return US_STATE_CODES.get(s.lower(), "")


def _headers(api_key: str) -> dict:
    return {"x-api-key": api_key, "accept": "application/json", "Cache-Control": "no-cache",
            "Content-Type": "application/json"}


def _months_since(iso_date: str | None) -> int | None:
    if not iso_date:
        return None
    m = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?", str(iso_date))
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    today = date.today()
    return max(0, (today.year - y) * 12 + (today.month - mo))


# ---------------------------------------------------------------------------
# Organisation enrichment (1 credit)
# ---------------------------------------------------------------------------

def get_organization(domain: str, api_key: str) -> dict:
    """
    Firmographics for one domain. Returns a dict with keys:
        found, source, employees, hq_city, hq_state, hq_country, industry,
        revenue_usd, linkedin, error
    Never raises. `source` is "Apollo".
    """
    empty = {"found": False, "source": "Apollo", "employees": None, "hq_city": "", "hq_state": "",
             "hq_country": "", "industry": "", "revenue_usd": None, "linkedin": "", "error": ""}
    if not domain:
        return {**empty, "error": "no domain"}
    if not api_key:
        return {**empty, "error": "no API key"}
    try:
        resp = requests.get(f"{APOLLO_BASE}/organizations/enrich", params={"domain": domain},
                            headers=_headers(api_key), timeout=TIMEOUT)
        if resp.status_code in (401, 403):
            return {**empty, "error": f"Apollo rejected the key (HTTP {resp.status_code})"}
        if resp.status_code == 422:
            return {**empty, "error": "endpoint not available on this Apollo plan (HTTP 422)"}
        if resp.status_code == 429:
            return {**empty, "error": "Apollo rate limit (HTTP 429)"}
        resp.raise_for_status()
        org = resp.json().get("organization") or {}
    except requests.exceptions.RequestException as exc:
        return {**empty, "error": f"request failed: {exc.__class__.__name__}"}
    except ValueError:
        return {**empty, "error": "non-JSON response"}
    if not org:
        return {**empty, "error": "no match in Apollo"}
    return _org_fields(org)


def _org_fields(org: dict) -> dict:
    country = str(org.get("country") or "")
    state = normalise_state(org.get("state"), country)
    try:
        employees = int(org.get("estimated_num_employees")) if org.get("estimated_num_employees") else None
    except (TypeError, ValueError):
        employees = None
    try:
        revenue = float(org.get("annual_revenue")) if org.get("annual_revenue") else None
    except (TypeError, ValueError):
        revenue = None
    return {
        "found": True, "source": "Apollo",
        "employees": employees,
        "hq_city": str(org.get("city") or ""),
        "hq_state": state,
        "hq_country": country,
        "industry": str(org.get("industry") or "").title(),
        "revenue_usd": revenue,
        "linkedin": str(org.get("linkedin_url") or ""),
        "error": "",
    }


# ---------------------------------------------------------------------------
# People search (0 credits)
# ---------------------------------------------------------------------------

def search_people(domain: str, api_key: str, titles: list[str] | None = None,
                  per_page: int = 5) -> list[dict]:
    """
    People at `domain` with a matching title. Each record:
        first_name, last_name, name, title, email, linkedin, months_in_role,
        new_hire, source
    Never raises; returns [] on any failure.
    """
    titles = titles or TARGET_TITLES
    if not domain:
        return []
    if not api_key:
        return []
    try:
        resp = requests.post(
            f"{APOLLO_BASE}/mixed_people/search",
            params={"q_organization_domains_list[]": [domain], "person_titles[]": titles,
                    "page": 1, "per_page": per_page},
            headers=_headers(api_key), timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        people = resp.json().get("people") or []
    except (requests.exceptions.RequestException, ValueError):
        return []
    out = []
    for p in people:
        email = str(p.get("email") or "")
        if "email_not_unlocked" in email or "@" not in email:
            email = ""  # Apollo placeholder; a real address is a paid people/match call
        months = _months_in_role(p)
        out.append({
            "first_name": str(p.get("first_name") or ""),
            "last_name": str(p.get("last_name") or ""),
            "name": str(p.get("name") or f"{p.get('first_name', '')} {p.get('last_name', '')}").strip(),
            "title": str(p.get("title") or ""),
            "email": email,
            "linkedin": str(p.get("linkedin_url") or ""),
            "months_in_role": months,
            "new_hire": months is not None and months < NEW_HIRE_MONTHS,
            "source": "Apollo",
        })
    return out


def _months_in_role(person: dict) -> int | None:
    """Apollo exposes the current job's start date in employment_history[]."""
    if person.get("time_in_current_role") is not None:
        try:
            return int(person["time_in_current_role"])
        except (TypeError, ValueError):
            pass
    history = person.get("employment_history") or []
    current = [h for h in history if h.get("current")] or history[:1]
    if not current:
        return None
    return _months_since(current[0].get("start_date"))


def reveal_email(person_id: str, api_key: str) -> str:
    """
    PAID PLANS ONLY -- POST /api/v1/people/match {"id": person_id} returns the
    verified work email for 1 credit. Not called by the app; provided so a paid
    key can be wired in with one line in app.py.
    """
    if not person_id or not api_key:
        return ""
    try:
        resp = requests.post(f"{APOLLO_BASE}/people/match",
                             json={"id": person_id, "reveal_personal_emails": False},
                             headers=_headers(api_key), timeout=TIMEOUT)
        if resp.status_code != 200:
            return ""
        email = str((resp.json().get("person") or {}).get("email") or "")
        return email if "@" in email and "email_not_unlocked" not in email else ""
    except (requests.exceptions.RequestException, ValueError):
        return ""

