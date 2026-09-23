"""
v3 tests: extractor mechanics, quality layer, show-file loading, DROPPED OUT, and -- most
important -- parity against REAL captures.

tests/fixtures/real/<show>_<platform>_raw.json is the raw payload the Map Grabber saved while
running on the live show page (debug mode), and <show>_<year>_<platform>.csv is the CSV it
downloaded in the same run. test_real_* re-parses the raw payload with the Python normaliser and
checks it reproduces the browser's CSV row for row: the same companies with the same sq ft.
The small hand-built records in the mechanics tests exist only to pin edge cases (JSONP, square
metres, open booths) that the real captures don't happen to contain; they never reach the app.

Run:  python -m pytest -q tests/test_v3.py
"""

import glob
import json
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import delta_engine  # noqa: E402
import platforms  # noqa: E402
import quality  # noqa: E402
import showfile  # noqa: E402
from extractors import a2z, common, expocad, expofp, mys  # noqa: E402

REAL = os.path.join(ROOT, "tests", "fixtures", "real")
NORMALISE = {"a2z": a2z.normalise, "expocad": expocad.normalise, "expofp": expofp.normalise,
             "mapyourshow": mys.normalise}


# ------------------------------------------------------------------ mechanics

def test_jsonp_stripping():
    assert a2z.strip_jsonp('aig([{"name":"X"}]);') == [{"name": "X"}]
    assert a2z.strip_jsonp('[{"name":"X"}]') == [{"name": "X"}]
    assert a2z.strip_jsonp('  window.cb_1 ( [] )  ') == []


def test_a2z_status0_sqm_multibooth_shared():
    recs = [
        {"name": "", "status": 0, "size": 100, "unit": "sq ft", "label": {"text": "A1"}, "id": 1},
        {"name": "Open", "status": 0, "size": 100, "unit": "sq ft", "label": {"text": "A2"}, "id": 2},
        {"name": "Twin Booth Co", "status": 2, "size": 200, "dimension": "10 x 20", "unit": "sq ft",
         "label": {"text": "B1"}, "id": 3, "hyperLinkFieldValue": "3"},
        {"name": "Twin Booth Co", "status": 2, "size": 300, "dimension": "10 x 30", "unit": "sq ft",
         "label": {"text": "B2"}, "id": 4},
        {"name": "Metric GmbH", "status": 2, "size": 36, "dimension": "6 x 6", "unit": "sq m",
         "label": {"text": "C1"}, "id": 5},
        {"name": "Host Co", "status": 2, "size": 400, "dimension": "20 x 20", "unit": "sq ft",
         "label": {"text": "D1"}, "id": 6, "coExhs": [{"name": "Guest Co"}]},
    ]
    rows, meta = a2z.normalise({"records": recs, "label_count": 3, "title": "2026 TEST - Event Map"})
    by = {r["exhibitor_name"]: r for r in rows}
    assert "Open" not in by and "" not in by                              # status 0 dropped
    assert by["Twin Booth Co"]["sqft"] == 500 and by["Twin Booth Co"]["booth_count"] == 2
    assert by["Twin Booth Co"]["booth_number"] == "B1, B2"
    assert by["Metric GmbH"]["sqft"] == round(36 * common.SQM_TO_SQFT)   # m2 -> ft2
    assert by["Metric GmbH"]["size_source"] == "a2z-map-sqm"
    assert by["Host Co"]["shared_booth"] and by["Host Co"]["sqft"] == 200 and by["Host Co"]["booth_sqft"] == 400
    assert meta["show_year"] == 2026 and meta["platform_count"] == 3


def test_shoelace_and_expofp_layer_parsing():
    assert common.shoelace([[0, 0], [20, 0], [20, 20], [0, 20]]) == 400
    # L-shape: 30x20 minus a 10x10 notch = 500
    assert common.shoelace([[0, 0], [30, 0], [30, 20], [10, 20], [10, 10], [0, 10]]) == 500
    js = ("// V2\nwindow['__fp'] = window['__fp1'] = `<svg xmlns=\"http://www.w3.org/2000/svg\" units=\"ft\">"
          "<g data-layer=\"1\"><rect id=\"bA100\" x=\"0\" y=\"0\" width=\"20\" height=\"30\" transform=\"rotate(90)\"/>"
          "<g id=\"bA200\"><g><g><path data-index=\"0\"/></g></g><rect id=\"\" width=\"5\" height=\"5\"/></g>"
          "</g></svg>`;\r\nwindow['__fpPaths'] = window['__fpPaths1'] = "
          "[{\"positions\":[[0,0],[30,0],[30,20],[10,20],[10,10],[0,10],[0,0]],\"cells\":[]}];\r\n"
          "var __fpLayers = [{\"name\":\"1\"}];\r\n")
    layer = expofp.parse_layer("1", js)
    assert layer["units"] == "ft"
    shapes = {s["id"]: s for s in layer["shapes"]}
    assert shapes["bA100"]["width"] * shapes["bA100"]["height"] == 600      # rotation doesn't change area
    assert common.shoelace(shapes["bA200"]["positions"]) == 500
    assert expofp.layer_names(js) == ["1"]


def test_expofp_shared_booth_poi_and_venue_page():
    raw = {"data": {"title": "Test Expo 2026", "startDate": "2026-10-13",
                    "exhibitors": [{"id": 1, "name": "Solo Co"}, {"id": 2, "name": "Pav A"}, {"id": 3, "name": "Pav B"}],
                    "booths": [{"name": "S1", "exhibitors": [1]}, {"name": "P1", "exhibitors": [2, 3]},
                               {"name": "Food Court", "special": True, "exhibitors": [1]}]},
           "layers": [{"name": "(default)", "units": "ft", "shapes": [
               {"id": "bS1", "tag": "rect", "width": 20, "height": 20},
               {"id": "bP1", "tag": "rect", "width": 40, "height": 50}]}]}
    rows, meta = expofp.normalise(raw)
    by = {r["exhibitor_name"]: r for r in rows}
    assert by["Solo Co"]["sqft"] == 400 and not by["Solo Co"]["shared_booth"]
    assert by["Solo Co"]["booth_number"] == "S1"                          # POI booth skipped
    assert by["Pav A"]["sqft"] == 1000 and by["Pav A"]["booth_sqft"] == 2000 and by["Pav A"]["shared_with"] == 2
    assert meta["show_year"] == 2026 and meta["show_base"] == "Test Expo"
    with pytest.raises(common.ExtractionError, match="calendar/marketing page"):
        expofp.fetch_raw("https://expofp.com/venetian-convention-and-expo-center/g2e-2026")


def test_expocad_join_and_status():
    raw = {"booths": [{"number": "100", "status": "2", "exhibitorIndex": "0", "areaF": "400 SqFt", "dimF": "20' x 20'"},
                      {"number": "101", "status": "0", "exhibitorIndex": "1", "areaF": "100 SqFt", "dimF": "10' x 10'"},
                      {"number": "102", "status": "2", "exhibitorIndex": "0", "areaF": "100 SqFt", "dimF": "10' x 10'"}],
           "exhibitors": [{"name": "Two Booths Inc", "exhId": "E1", "state": "OH"}, {"name": "Open", "exhId": "E2"}],
           "config_title": "Test World 2026"}
    rows, meta = expocad.normalise(raw)
    assert len(rows) == 1 and rows[0]["sqft"] == 500 and rows[0]["width"] == 20 and rows[0]["state"] == "OH"
    assert meta["show_year"] == 2026 and meta["platform_count"] == 2


def test_mys_showid_year_and_custom_domain_detection():
    assert mys.year_from_showid("IMTS26") == 2026 and mys.year_from_showid("NAB2027") == 2027
    assert mys.year_from_showid("DIRECTORY") is None                        # never guessed
    assert platforms.platform_for("https://directory.imts.com/8_0/explore/exhibitor-gallery.cfm") == "mapyourshow"
    assert platforms.platform_for("https://example.com/about") is None


# ------------------------------------------------------------------ quality

def _rows(n, sqft=400, **kw):
    return [{**common.blank_row(), "exhibitor_name": f"Co {i}", "sqft": sqft, "booth_count": 1,
             "booth_sqft": sqft, "width": 20, "length": sqft / 20, **kw} for i in range(n)]


def test_quality_grades():
    assert quality.check(_rows(60))["grade"] == "PASS"
    assert quality.check(_rows(5))["grade"] == "FAIL"                                     # row count
    assert quality.check(_rows(20))["grade"] == "WARN"
    assert quality.check(_rows(60, sqft=0))["grade"] == "FAIL"                             # sized share
    assert quality.check(_rows(60), {"platform_count": 100})["grade"] == "FAIL"            # coverage 60%
    assert quality.check(_rows(60), {"platform_count": 70})["grade"] == "WARN"             # coverage 86%
    assert quality.check(_rows(60), {"show_year": 2025}, chosen_year=2026)["grade"] == "WARN"
    assert quality.check(_rows(60, sqft=60000))["grade"] == "FAIL"                         # implausible
    dup = _rows(60)
    dup[1]["exhibitor_name"] = dup[0]["exhibitor_name"]
    assert quality.check(dup)["grade"] == "WARN"
    odd = _rows(60, sqft=37.16, width=None, length=None)                                    # m2 read as ft2
    assert any(f["check"] == "Standard sizes" and f["level"] == "WARN" for f in quality.check(odd)["findings"])
    assert quality.check(_rows(250, sqft=100))["grade"] == "WARN"                           # island sanity


def test_demo_leakage_is_fail_and_unoverridable():
    rows = _rows(60)
    rows[3]["size_source"] = "demo"
    q = quality.check(rows)
    assert q["grade"] == "FAIL" and q["demo"]
    # the exact shape fallback_harvester.py wrote
    fake = pd.DataFrame([{"exhibitor_name": "Clearwave Networking (SEMA Division)", "booth_number": "E6155",
                          "sqft": 2500, "exhid": "demo_sema_2026_015", "size_source": "demo"}] * 30)
    path = os.path.join(ROOT, "tests", "_tmp_fake.csv")
    fake.to_csv(path, index=False)
    try:
        with pytest.raises(showfile.ShowFileError, match="generated/demo"):
            showfile.load(path)
        with open(path, "rb") as fh, pytest.raises(ValueError, match="generated/demo"):
            delta_engine.load_prior_csv(fh)
    finally:
        os.remove(path)


def test_dropped_out():
    y24 = pd.DataFrame({"exhibitor_name": ["Stays", "Leaves", "Left Long Ago"], "sqft": [400, 900, 100]})
    y25 = pd.DataFrame({"exhibitor_name": ["Stays", "Leaves"], "sqft": [400, 900]})
    y26 = pd.DataFrame({"exhibitor_name": ["Stays"], "sqft": [400]})
    t = delta_engine.build_trajectories([{"year": y, "label": str(y), "source": "prior", "df": d}
                                         for y, d in ((2024, y24), (2025, y25), (2026, y26))])
    traj = dict(zip(t["exhibitor_name"], t["trajectory"]))
    assert traj["Leaves"] == delta_engine.TRAJ_DROPPED
    assert traj["Left Long Ago"] != delta_engine.TRAJ_DROPPED      # absent two editions: not "just dropped"
    assert delta_engine.trajectory_summary(t)["dropped_out"] == 1


# ------------------------------------------------------------------ real captures

def _real_pairs():
    out = []
    for raw_path in sorted(glob.glob(os.path.join(REAL, "*_raw.json"))):
        stem = os.path.basename(raw_path)[:-len("_raw.json")]          # e.g. imex-america_2026_expofp
        csv_path = os.path.join(REAL, stem + ".csv")
        out.append(pytest.param(raw_path, csv_path if os.path.exists(csv_path) else None, id=stem))
    return out


@pytest.mark.parametrize("raw_path,csv_path", _real_pairs())
def test_real_capture_parity(raw_path, csv_path):
    with open(raw_path) as fh:
        cap = json.load(fh)
    raw = dict(cap["raw"])
    if cap["platform"] in ("a2z", "mapyourshow"):
        raw.setdefault("title", f"{cap['meta'].get('show_name', '')} {cap['meta'].get('show_year', '')}".strip())
    rows, meta = NORMALISE[cap["platform"]](raw)
    sidecar = os.path.join(REAL, "expected_counts.json")
    expected = json.load(open(sidecar)).get(os.path.basename(raw_path), {}) if os.path.exists(sidecar) else {}
    if expected:
        assert len(rows) == expected["companies"], (len(rows), expected)
        assert sum(r["sqft"] >= 400 for r in rows) == expected["islands_400"]
    q = quality.check(rows, meta)
    assert q["grade"] != "FAIL", q["notes"]
    assert not q["demo"]
    if csv_path:
        browser, _ = showfile.load(csv_path)
        py = pd.DataFrame(rows)
        # a trimmed fixture keeps every booth of the selected companies (so their rows must match the
        # browser exactly); co-exhibitors pulled in only to keep shared-booth splits right are not compared
        # join on the normalised name: when two listings spell a company differently (case, spacing)
        # the displayed spelling is whichever booth came first, which is not meaningful
        browser["k"] = browser["exhibitor_name"].map(common.name_key)
        py["k"] = py["exhibitor_name"].map(common.name_key)
        merged = browser[["k", "sqft"]].merge(py[["k", "sqft"]], on="k", how="left", suffixes=("_js", "_py"),
                                              indicator=True)
        assert len(browser) >= 40, len(browser)
        assert (merged["_merge"] == "both").all(), merged[merged["_merge"] != "both"].head()
        diff = merged[(merged["sqft_js"] - merged["sqft_py"]).abs() > 1]
        assert diff.empty, diff.head()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
