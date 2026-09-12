"""Offline checks: scraper against the mock MapYourShow server, delta engine, freight, pitch, Apollo mock."""
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
import scraper  # noqa: E402

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


def test_fallback():
    rows, meta = scraper.load_fallback_dataset()
    assert len(rows) == 20 and meta["source"] == "demo"
    assert all(set(scraper.EXHIBITOR_COLUMNS) <= set(r) for r in rows)
    print("fallback OK")


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


def test_apollo_mock_and_pitch(delta_df: pd.DataFrame):
    org = apollo.get_organization("acmebroadcast.com", "", mock=True)
    assert org["found"] and org["source"] == "MOCK" and org["employees"]
    assert apollo.get_organization("acmebroadcast.com", "", mock=True) == org  # deterministic
    assert apollo.get_organization("", "", mock=True)["error"] == "no domain"
    assert apollo.get_organization("x.com", "", mock=False)["error"] == "no API key"
    ppl = apollo.search_people("acmebroadcast.com", "", mock=True)
    assert all(p["source"] == "MOCK" for p in ppl)
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
    print("apollo mock + pitch OK")
    print(out[["exhibitor_name", "sqft", "yoy_status", "trigger_badge"]].to_string())


if __name__ == "__main__":
    proc = start_server()
    try:
        current = test_scraper()
        test_fallback()
        delta_df = test_delta(current)
        test_freight()
        test_apollo_mock_and_pitch(delta_df)
        print("\nALL MODULE TESTS PASSED")
    finally:
        proc.terminate()
