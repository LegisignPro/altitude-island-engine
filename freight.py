"""
freight.py -- Home-field / freight arbitrage detector.

Altitude Exhibits builds, stores and installs in Las Vegas. An exhibitor whose
HQ (and therefore its current exhibit house and warehouse) sits on the East
Coast, in the Midwest, the South, or overseas pays cross-country freight both
ways, drayage on every crate, and an out-of-town I&D crew for every Vegas
show. That is the arbitrage: a locally built and locally stored asset removes
most of that line. HQ state comes from Apollo (observed), never guessed.

    NV                         -> "Vegas Local"            (storage / asset-takeover angle)
    CA, OR, WA                 -> "West Coast"             (short haul, low savings)
    AZ, UT, ID, CO, NM, MT, WY -> "Mountain West"          (moderate savings)
    every other US state       -> "High Freight Savings Potential"
    non-US                     -> "High Freight Savings Potential (International)"
    unknown                    -> "HQ unknown"
"""

from __future__ import annotations

WEST_COAST = {"CA", "OR", "WA"}
MOUNTAIN_WEST = {"AZ", "UT", "ID", "CO", "NM", "MT", "WY"}
EAST_COAST = {"ME", "NH", "VT", "MA", "RI", "CT", "NY", "NJ", "PA", "DE", "MD", "DC", "VA", "NC", "SC", "GA", "FL"}
MIDWEST = {"OH", "MI", "IN", "IL", "WI", "MN", "IA", "MO", "ND", "SD", "NE", "KS"}

TAG_LOCAL = "Vegas Local"
TAG_WEST = "West Coast"
TAG_MOUNTAIN = "Mountain West"
TAG_HIGH = "High Freight Savings Potential"
TAG_HIGH_INTL = "High Freight Savings Potential (International)"
TAG_UNKNOWN = "HQ unknown"


def region_of(state: str, country: str) -> str:
    s = (state or "").upper()
    c = (country or "").strip().lower()
    if s == "NV":
        return "Nevada"
    if s in WEST_COAST:
        return "West Coast"
    if s in MOUNTAIN_WEST:
        return "Mountain West"
    if s in EAST_COAST:
        return "East Coast"
    if s in MIDWEST:
        return "Midwest"
    if s:
        return "South / Central"
    if c and c not in {"united states", "usa", "us", "united states of america"}:
        return "International"
    return "Unknown"


def freight_tag(state: str, country: str) -> str:
    region = region_of(state, country)
    return {
        "Nevada": TAG_LOCAL,
        "West Coast": TAG_WEST,
        "Mountain West": TAG_MOUNTAIN,
        "East Coast": TAG_HIGH,
        "Midwest": TAG_HIGH,
        "South / Central": TAG_HIGH,
        "International": TAG_HIGH_INTL,
    }.get(region, TAG_UNKNOWN)


def is_high_freight(tag: str) -> bool:
    return tag in (TAG_HIGH, TAG_HIGH_INTL)


def is_vegas_local(tag: str) -> bool:
    return tag == TAG_LOCAL
