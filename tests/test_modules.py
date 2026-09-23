"""Offline logic checks: MapYourShow HTTP client against a local test server, delta engine, freight, pitch, Apollo (no key).

The company names below are unit-test inputs for the classification logic only; they never reach the app.
Extractor correctness is tested against REAL captured payloads in tests/test_v3_extractors.py."""
import io
import os
import subprocess
import sys
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import apollo  # noqa: E402
import delta_engine  # noqa: E402
import freight  # noqa: E402
import pitch_generator as pg  # noqa: E402
import platforms  # noqa: E402
import scraper  # noqa: E402
import show_finder  # noqa: E402

PORT = 8765


def start_server():
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "mock_mys_server.py"), str(PORT)])
    time.sleep(0.8)
    return proc


def test_scraper():
    scraper.is_mapyourshow = lambda url: True  # the mock server is on localhost
    url = f"http://127.0.0.1:{PORT}/8_0/explore/exhibitor-gallery.cfm"
    log = []
    rows, meta = scraper.scrape_mapyourshow(url, log=log.append)
    names = {r["exhibitor_name"] for r in rows}
    # name filter
    assert "Nevada Pavilion" not in names and "State of Ohio" not in names and "Gamma Lighting Association" not in names
    assert {"Acme Broadcast, Inc.", "Smallco Widgets", "Beta Media Systems", "Delta Cameras", "No Booth Corp",
            "Ghost Exhibits"} == names, names
    by = {r["exhibitor_name"]: r for r in rows}
    acme = by["Acme Broadcast, Inc."]
    assert acme["sqft"] == 600 and acme["width"] == 20 and acme["length"] == 30 and acme["hall"] == "Central Hall"
    assert acme["is_sponsor"] is True and acme["has_video_listing"] is True and acme["booth_number"] == "C1200"
    delta = by["Delta Cameras"]
    assert delta["sqft"] == 600 and delta["booth_number"] == "N800, N802", delta  # two booths summed
    assert by["No Booth Corp"]["sqft"] == 0 and by["No Booth Corp"]["size_source"] == "unknown"
    assert by["Ghost Exhibits"]["sqft"] == 900  # floor plan only, not in gallery
    assert by["Smallco Widgets"]["is_sponsor"] is False
    assert meta["showid"] == "TEST26" and meta["sized"] == 5 and meta["hall_errors"] == 0
    # websites for filtered rows only
    targets = [r for r in rows if 400 <= r["sqft"] <= 3000]
    scraper.enrich_websites(targets, url, log=log.append)
    assert by["Acme Broadcast, Inc."]["website"] == "https://www.acmebroadcast.com"
    assert by["Beta Media Systems"]["website"] == "https://betamedia.io"   # scheme added
    assert by["Ghost Exhibits"]["website"] == ""                            # no detail
    assert by["Smallco Widgets"]["website"] == ""                           # out of range, never fetched
    assert scraper.infer_show_name("https://ces2026.mapyourshow.com/8_0/x") == "CES 2026"
    assert scraper.infer_show_name("https://nab27.mapyourshow.com/") == "NAB Show 2027"
    print("scraper OK:", log)
    return pd.DataFrame(rows, columns=scraper.EXHIBITOR_COLUMNS)


def test_show_year():
    """infer_show_base / infer_show_year: the split that fixes the '+1 year' bug (Task 1)."""
    cases = [
        ("https://nab27.mapyourshow.com/8_0/x", "NAB Show", 2027),
        ("https://ces2026.mapyourshow.com/8_0/x", "CES", 2026),
        ("https://woc26.mapyourshow.com/x", "World of Concrete", 2026),
        ("https://packexpo.mapyourshow.com/x", "PACK EXPO", None),
        ("https://imts-2026.mapyourshow.com/x", "IMTS", 2026),
        ("https://nab.mapyourshow.com/x", "NAB Show", None),  # no digits at all -> no year, never guessed
    ]
    for url, exp_base, exp_year in cases:
        got_base, got_year = scraper.infer_show_base(url), scraper.infer_show_year(url)
        assert got_base == exp_base, (url, got_base, exp_base)
        assert got_year == exp_year, (url, got_year, exp_year)
    # infer_show_name stays a thin wrapper: base+year when a year is present, base alone otherwise.
    assert scraper.infer_show_name("https://nab27.mapyourshow.com/") == "NAB Show 2027"
    assert scraper.infer_show_name("https://ces2026.mapyourshow.com/8_0/x") == "CES 2026"
    assert scraper.infer_show_name("https://nab.mapyourshow.com/x") == "NAB Show"
    print("show year/base OK")


def test_delta(current: pd.DataFrame):
    csv = io.BytesIO(b"Exhibitor,Booth Size,Sq Ft\nAcme Broadcast Inc,10x10,100\nDelta Cameras,20x30,600\n"
                     b"Beta Media Systems LLC,30x40,1200\nGhost Exhibits,10x20,200\n")
    prior = delta_engine.load_prior_csv(csv)
    assert set(prior["name_key"]) == {"acmebroadcast", "deltacameras", "betamediasystems", "ghostexhibits"}
    out = delta_engine.compute_delta(current, prior)
    st = dict(zip(out["exhibitor_name"], out["yoy_status"]))
    assert st["Acme Broadcast, Inc."] == delta_engine.STATUS_NEWBORN, st
    assert st["Delta Cameras"] == delta_engine.STATUS_STAGNANT
    assert st["Beta Media Systems"] == delta_engine.STATUS_UPGRADE
    assert st["Ghost Exhibits"] == delta_engine.STATUS_UPGRADE  # 200 -> 900: not "under 200", so upgrade
    assert st["Smallco Widgets"] == delta_engine.STATUS_NEW
    # width/length fallback
    prior2 = delta_engine.load_prior_csv(io.BytesIO(b"company,width,length\nSmallco Widgets,10,10\n"))
    assert prior2["prior_sqft"].iloc[0] == 100
    none = delta_engine.compute_delta(current, None)
    assert (none["yoy_status"] == delta_engine.STATUS_NONE).all()
    print("delta OK")
    return out


def test_classify_trajectory():
    """classify_trajectory on hand-built (year, sqft) series (Task 2)."""
    ct = delta_engine.classify_trajectory
    # Woz's two worked examples.
    assert ct([(2024, 100), (2025, 900), (2026, 600)], 3) == delta_engine.TRAJ_PEAK_RETREAT
    assert ct([(2024, 100), (2025, 400), (2026, 600)], 3) == delta_engine.TRAJ_STEADY_GROWTH
    # Only one slot loaded at all -> nothing to compare against, for anyone.
    assert ct([(2026, 600)], 1) == delta_engine.TRAJ_NONE
    # 3 slots loaded, but this company only appears in the current one.
    assert ct([(2026, 600)], 3) == delta_engine.TRAJ_NEW
    # Exactly 2 points reduce to the original single-step semantics.
    assert ct([(2025, 100), (2026, 600)], 3) == delta_engine.TRAJ_NEWBORN
    assert ct([(2025, 600), (2026, 900)], 3) == delta_engine.TRAJ_UPGRADE
    assert ct([(2025, 900), (2026, 600)], 3) == delta_engine.TRAJ_DOWNSIZE
    assert ct([(2025, 600), (2026, 600)], 3) == delta_engine.TRAJ_STAGNANT
    # 3+ points, monotonic increase -> steady growth (even with an early inline year).
    assert ct([(2024, 600), (2025, 900), (2026, 1200)], 3) == delta_engine.TRAJ_STEADY_GROWTH
    # 3+ points, monotonic DECREASE from the very first year on file: that's a plain decline,
    # not a "retreat" -- there was no growth into the peak (peak IS the first point), even
    # though the final value happens to land in the 50-90% band.
    assert ct([(2024, 1200), (2025, 900), (2026, 600)], 3) == delta_engine.TRAJ_SHRINKING
    # 3+ points, up-down-up -> more than one direction change -> volatile.
    assert ct([(2023, 400), (2024, 900), (2025, 400), (2026, 900)], 4) == delta_engine.TRAJ_VOLATILE
    # A late-arriving newborn still wins outright even with 3+ points of history.
    assert ct([(2024, 100), (2025, 150), (2026, 400)], 3) == delta_engine.TRAJ_NEWBORN
    # Peak retreat that would ALSO look like a straight decline by percentage alone is still
    # correctly told apart from SHRINKING purely by "did it grow first" (peak not the 1st pt).
    assert ct([(2023, 300), (2024, 1000), (2025, 700), (2026, 550)], 4) == delta_engine.TRAJ_PEAK_RETREAT
    print("classify_trajectory OK")


def test_build_trajectories():
    """build_trajectories() over several synthetic year slots (Task 2)."""
    slots = [
        {"year": 2024, "label": "2024", "source": "csv",
         "df": pd.DataFrame([{"exhibitor_name": "Acme Broadcast Inc", "sqft": 100},
                             {"exhibitor_name": "Vantage Robotics Systems Inc", "sqft": 500},
                             {"exhibitor_name": "Stagnant Co", "sqft": 600}])},
        {"year": 2025, "label": "2025", "source": "csv",
         "df": pd.DataFrame([{"exhibitor_name": "Acme Broadcast, Inc.", "sqft": 400},
                             {"exhibitor_name": "Vantage Robotics Systems", "sqft": 900},
                             {"exhibitor_name": "Stagnant Co", "sqft": 600},
                             {"exhibitor_name": "Left After 2025", "sqft": 500}])},
        {"year": 2026, "label": "2026 (live)", "source": "live",
         "df": pd.DataFrame([{"exhibitor_name": "Acme Broadcast, Inc.", "sqft": 600},
                             {"exhibitor_name": "Vantage Robotics Systems", "sqft": 600},
                             {"exhibitor_name": "Stagnant Co", "sqft": 600},
                             {"exhibitor_name": "Brand New Exhibitor", "sqft": 450}])},
    ]
    traj = delta_engine.build_trajectories(slots)
    assert {"sqft_2024", "sqft_2025", "sqft_2026", "trajectory", "yoy_status", "is_current_year"} <= set(traj.columns)
    by_key = {r["name_key"]: r for _, r in traj.iterrows()}

    acme = by_key[delta_engine.name_key("Acme Broadcast Inc")]
    assert acme["exhibitor_name"] == "Acme Broadcast, Inc."   # latest spelling wins
    assert acme["sqft_2024"] == 100 and acme["sqft_2025"] == 400 and acme["sqft_2026"] == 600
    assert acme["trajectory"] == delta_engine.TRAJ_STEADY_GROWTH
    assert acme["is_current_year"] is True

    vantage = by_key[delta_engine.name_key("Vantage Robotics Systems")]
    assert vantage["sqft_2024"] == 500 and vantage["sqft_2025"] == 900 and vantage["sqft_2026"] == 600
    assert vantage["trajectory"] == delta_engine.TRAJ_PEAK_RETREAT
    assert vantage["peak_sqft"] == 900 and vantage["peak_year"] == 2025

    stagnant = by_key[delta_engine.name_key("Stagnant Co")]
    assert stagnant["trajectory"] == delta_engine.STATUS_STAGNANT
    assert stagnant["yoy_status"] == delta_engine.STATUS_STAGNANT

    left = by_key[delta_engine.name_key("Left After 2025")]
    assert left["is_current_year"] is False and pd.isna(left["sqft_2026"])
    assert left["yoy_status"] == delta_engine.STATUS_NONE   # not exhibiting this year -- no signal to trigger on
    assert left["newborn_island"] is False

    brand_new = by_key[delta_engine.name_key("Brand New Exhibitor")]
    assert brand_new["trajectory"] == delta_engine.TRAJ_NEW and brand_new["yoy_status"] == delta_engine.STATUS_NEW

    summary = delta_engine.trajectory_summary(traj)
    assert summary["steady_growth"] == 1 and summary["peak_retreat"] == 1 and summary["stagnant"] == 1
    assert summary["new"] == 1
    print("build_trajectories OK")
    return traj


def test_freight():
    assert freight.freight_tag("NV", "United States") == freight.TAG_LOCAL
    assert freight.freight_tag("CA", "United States") == freight.TAG_WEST
    assert freight.freight_tag("NY", "United States") == freight.TAG_HIGH
    assert freight.freight_tag("OH", "United States") == freight.TAG_HIGH
    assert freight.freight_tag("TX", "United States") == freight.TAG_HIGH
    assert freight.freight_tag("", "Germany") == freight.TAG_HIGH_INTL
    assert freight.freight_tag("", "") == freight.TAG_UNKNOWN
    assert apollo.normalise_state("New York", "United States") == "NY"
    assert apollo.normalise_state("ny", "") == "NY"
    print("freight OK")


def test_apollo_and_pitch(delta_df: pd.DataFrame):
    # v3: no mock mode. No key -> an explicit error, never placeholder data.
    assert apollo.get_organization("x.com", "")["error"] == "no API key"
    assert not apollo.get_organization("x.com", "").get("found")
    assert apollo.search_people("x.com", "") == []
    assert not hasattr(apollo, "_mock_organization") and not hasattr(apollo, "_mock_people")
    assert apollo._months_in_role({"employment_history": [{"current": True, "start_date": "2026-07-01"}]}) == 2
    assert apollo.domain_from_website("https://www.acme.com/x") == "acme.com"
    assert apollo.domain_from_website("betamedia.io") == "betamedia.io"

    df = delta_df.copy()
    df["freight_tag"] = [freight.TAG_HIGH, freight.TAG_WEST, freight.TAG_LOCAL, freight.TAG_UNKNOWN,
                         freight.TAG_HIGH, freight.TAG_WEST][: len(df)]
    df["new_hire"] = [False, True, False, False, False, False][: len(df)]
    df["first_name"] = ["Dana", "Marcus", "", "", "", ""][: len(df)]
    df["contact_title"] = ["Event Manager", "CMO", "", "", "", ""][: len(df)]
    df["hq_state"] = ["NY", "CA", "NV", "", "OH", "WA"][: len(df)]
    df["hq_country"] = "United States"
    out = pg.assign_pitches(df, "TEST 2026")
    by = dict(zip(out["exhibitor_name"], out["trigger"]))
    assert by["Acme Broadcast, Inc."] == "A"      # newborn beats everything
    row_b = out[out["new_hire"]].iloc[0]
    assert row_b["trigger"] == "B" and "congratulations" in row_b["custom_intro_line"]
    assert list(out["priority"]) == sorted(out["priority"])  # sorted by priority
    assert (out["est_value"] == out["sqft"] * 150).all()
    assert "D" in set(out["trigger"]) and "C" in set(out["trigger"])
    exp = pg.build_export(out, "TEST 2026")
    assert list(exp.columns) == pg.EXPORT_COLUMNS and len(exp) == len(out)
    assert set(["email", "first_name", "company_name", "booth_sqft", "trigger_badge", "pitch_angle",
                "custom_intro_line"]) <= set(exp.columns)
    print("apollo (no mock) + pitch OK")
    print(out[["exhibitor_name", "sqft", "yoy_status", "trigger_badge"]].to_string())


def test_platform_detection():
    # Host-based matches, including one platform on each vendor domain.
    assert platforms.platform_for("https://ole.a2zinc.net/OLEWest2025/Public/eventmap.aspx?ID=54823") == "a2z"
    assert platforms.platform_for(
        "https://s36.a2zinc.net/clients/FSPA/opss2026/Public/EventMap.aspx?shmode=E&ID=3007") == "a2z"
    assert platforms.platform_for("https://swe.expocad.com/Events/we26/index.html") == "expocad"
    assert platforms.platform_for("https://show.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm") == "mapyourshow"
    assert platforms.platform_for("https://demo.expofp.com/somemap") == "expofp"
    # White-labelled A2Z on a show's own domain (real example from Woz: a2z.aafp.org) -- caught by
    # the /Public/EventMap.aspx path signature since the host alone gives no hint.
    assert platforms.platform_for("https://a2z.aafp.org/future2026/Public/eventmap.aspx?shMode=E") == "a2z"
    # White-label MapYourShow (IMTS) by its /8_0/ path; the live probe confirms it at extraction time.
    assert platforms.platform_for("https://directory.imts.com/8_0/explore/exhibitor-gallery.cfm?featured=false") == "mapyourshow"
    assert platforms.platform_for("https://www.expocad.com/host/fx/informa/26dcw/exfx.html") == "expocad"
    assert platforms.roadmap_name("https://moneyus2026visitorview.coconnex.com/") is not None
    # A URL with neither a known host nor a known path stays unrecognised, not a guess.
    assert platforms.platform_for("https://example.com/some/random/page") is None
    print("platform detection OK")


# ---------------------------------------------------------------------------
# show_finder: query construction, verification heuristic, platform labelling and result
# assembly against a MOCKED Tavily client. The live api.tavily.com path cannot be reached from
# the build container, so the decision logic is what gets covered here.
# ---------------------------------------------------------------------------

REAL_DIRECTORY = ("NAB Show 2026 Exhibitor Directory. Browse exhibitors by hall. Booth SL1523 ... "
                  "Floor plan. 400 sq ft island booths ...")
STALE_HOMEPAGE = ("NAB Show - Register today! Get notified when registration opens. Exhibitor info, "
                  "booth sales open now. Book housing.")
TEASER_PAGE = "Explore the 3D floor plan preview. Coming soon."
FLIPBOOK_2025 = "NAB 2025 Show Directory. Adobe SL1523, SL1723 ... Exhibitor listings by booth number."
SEMA_WHITELABEL = "Floor plan {BOOTHID} {AVAILBOOTHSQFEET} exhibitor booth tooltip template"


class MockTavily:
    """Canned responses keyed by query / url, plus call counting so tests can prove nothing unverified
    leaks through and nothing is fetched twice."""

    def __init__(self, searches: dict, pages: dict, fail_extract: tuple = ()):
        self.searches, self.pages, self.fail_extract = searches, pages, fail_extract
        self.search_calls, self.extract_calls = [], []

    def search(self, query, **kw):
        self.search_calls.append(query)
        return {"results": [{"url": u, "title": t, "content": ""} for u, t in self.searches.get(query, [])]}

    def extract(self, urls, **kw):
        self.extract_calls.append(list(urls))
        return {"results": [{"url": u, "raw_content": self.pages[u]} for u in urls if u in self.pages],
                "failed_results": [{"url": u, "error": "403"} for u in urls if u in self.fail_extract]}


class ExplodingTavily:
    def search(self, *a, **kw):
        raise ConnectionError("proxy 403")

    def extract(self, *a, **kw):
        raise ConnectionError("proxy 403")


def test_show_finder():
    assert show_finder.build_query("  NAB   Show ") == "NAB Show exhibitor directory floor plan"
    assert show_finder.build_query("SEMA Show", 2025) == "SEMA Show 2025 exhibitor directory floor plan"

    assert show_finder.looks_like_directory(REAL_DIRECTORY)
    assert show_finder.looks_like_directory(FLIPBOOK_2025)
    assert show_finder.looks_like_directory(SEMA_WHITELABEL)
    assert not show_finder.looks_like_directory(STALE_HOMEPAGE), "homepage tells must veto a 200"
    assert not show_finder.looks_like_directory(TEASER_PAGE), "one tell is not enough"
    assert not show_finder.looks_like_directory("")

    # platform labelling reuses platforms.platform_for (host + white-label path), then content
    assert show_finder.classify_platform("https://nab26.mapyourshow.com/8_0/explore/exhview.cfm") == "mapyourshow"
    assert show_finder.classify_platform("https://exhibitors.ces.tech/8_0/explore/exhibitor-gallery.cfm") == "mapyourshow"
    assert show_finder.classify_platform("https://a2z.aafp.org/Public/EventMap.aspx?ID=1") == "a2z"
    assert show_finder.classify_platform("https://www.semashow.com/floorplan", SEMA_WHITELABEL) == "expocad"
    assert show_finder.classify_platform("https://user-123.cld.bz/NAB-2025-Show-Directory", FLIPBOOK_2025) is None

    assert show_finder.years_to_probe(2026, 2) == [2025, 2024]
    assert show_finder.years_to_probe(2026, 0) == []
    assert show_finder.years_to_probe(2026, 99) == [2025, 2024, 2023, 2022, 2021]   # capped
    assert len(show_finder.years_to_probe(None, 1)) == 1

    q0, q25, q24 = (show_finder.build_query("NAB Show", y) for y in (None, 2025, 2024))
    mock = MockTavily(
        searches={
            q0: [("https://nab26.mapyourshow.com/8_0/explore/exhview.cfm", "NAB Show 2026 Exhibitors"),
                 ("https://nab24.mapyourshow.com/8_0/explore/exhview.cfm", "NAB Show"),        # 200 but stale homepage
                 ("https://nabshow.com/blocked", "blocked"),                                   # extract fails
                 ("https://nab26.mapyourshow.com/8_0/explore/exhview.cfm/", "dup")],          # duplicate
            q25: [("https://user-35215390377.cld.bz/NAB-2025-Show-Directory", "NAB 2025 Show Directory")],
            q24: [("https://nab24.expofp.com/", "3D floor plan")],
        },
        pages={
            "https://nab26.mapyourshow.com/8_0/explore/exhview.cfm": REAL_DIRECTORY,
            "https://nab24.mapyourshow.com/8_0/explore/exhview.cfm": STALE_HOMEPAGE,
            "https://user-35215390377.cld.bz/NAB-2025-Show-Directory": FLIPBOOK_2025,
            "https://nab24.expofp.com/": TEASER_PAGE,
        },
        fail_extract=("https://nabshow.com/blocked",),
    )
    log = []
    res = show_finder.find_show_urls("NAB Show", years=[2025, 2024], client=mock, log=log.append)
    assert mock.search_calls == [q0, q25, q24]
    assert len(mock.extract_calls) == 3 and len(mock.extract_calls[0]) == 3, mock.extract_calls   # deduped before extract
    cur = res["current"]
    assert [h["url"] for h in cur] == ["https://nab26.mapyourshow.com/8_0/explore/exhview.cfm"], cur
    assert cur[0]["platform"] == "mapyourshow" and cur[0]["supported"] and cur[0]["url_year"] == 2026
    # the stale-homepage 200 and the failed fetch are NOT offered -- unverified never leaks out
    assert all("nab24" not in h["url"] and "blocked" not in h["url"] for h in cur)
    assert [h["url"] for h in res["by_year"][2025]] == ["https://user-35215390377.cld.bz/NAB-2025-Show-Directory"]
    assert res["by_year"][2025][0]["supported"] is False and res["by_year"][2025][0]["platform"] is None
    assert res["by_year"][2024] == []
    assert res["years_with_maps"] == [2025] and res["years_probed"] == [2025, 2024]
    summary = show_finder.summarize_years(res)
    assert "1 of 2" in summary and "2025: 1 verified" in summary and "2024: none found" in summary, summary
    assert any("verified as a real exhibitor listing: 1" in line for line in log), log

    # failure modes surface as ShowFinderError, never a raw exception
    for bad in (lambda: show_finder.find_show_urls("", client=mock),
                lambda: show_finder.find_show_urls("NAB Show", api_key=""),
                lambda: show_finder.find_show_urls("NAB Show", client=ExplodingTavily())):
        try:
            bad()
            raise AssertionError("expected ShowFinderError")
        except show_finder.ShowFinderError as exc:
            assert str(exc)
    try:
        show_finder.find_show_urls("NAB Show", client=ExplodingTavily())
    except show_finder.ShowFinderError as exc:
        assert "ConnectionError" in str(exc)
    assert show_finder.summarize_years({"years_probed": []}) == ""
    print("show_finder OK")


if __name__ == "__main__":
    proc = start_server()
    try:
        current = test_scraper()
        test_show_year()
        delta_df = test_delta(current)
        test_classify_trajectory()
        test_build_trajectories()
        test_freight()
        test_apollo_and_pitch(delta_df)
        test_platform_detection()
        test_show_finder()
        print("\nALL MODULE TESTS PASSED")
    finally:
        proc.terminate()
