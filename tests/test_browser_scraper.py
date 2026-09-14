"""
Task 3 tests: the A2Z/EXPOCAD/ExpoFP normalisers against hand-written fixtures (no browser) -- the
original happy-path shapes AND the "weird" shapes that made the first live EXPOCAD run return zero
rows (abbreviated camelCase keys, combined booth+exhibitor records, nested exhibitor objects,
mismatched join keys, keyed dicts, columns+rows tables, names-only, nothing usable) -- plus
one Playwright test that serves a tiny local page and confirms capture_json_responses() actually
picks up a fetch()ed JSON response. Chromium is pre-installed in this container/session at
/opt/pw-browsers -- do NOT run `playwright install`.
"""
import http.server
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

import browser_scraper as bs  # noqa: E402
import scraper  # noqa: E402


def load_fixture(name: str) -> list[dict]:
    with open(os.path.join(FIXTURES, f"{name}_sample.json")) as f:
        return json.load(f)


def test_pick_num_polygon():
    assert bs.pick({"BoothNumber": "100"}, "boothnumber") == "100"
    assert bs.pick({"boothNumber": "100"}, "BoothNumber") == "100"   # case-insensitive both ways
    assert bs.pick({"Other": 1}, "boothnumber") is None
    assert bs.pick("not a dict", "x") is None

    assert bs.num(400) == 400.0
    assert bs.num("400") == 400.0
    assert bs.num("400 sq ft") == 400.0
    assert bs.num(None) is None
    assert bs.num("", default=0) == 0
    assert bs.num("nothing numeric here") is None

    square = [(0, 0), (30, 0), (30, 30), (0, 30)]
    assert bs.polygon_area(square) == 900.0
    dict_square = [{"x": 0, "y": 0}, {"x": 20, "y": 0}, {"x": 20, "y": 20}, {"x": 0, "y": 20}]
    assert bs.polygon_area(dict_square) == 400.0
    assert bs.polygon_area([(0, 0), (1, 1)]) == 0.0   # fewer than 3 points -> no area
    print("pick/num/polygon_area OK")


def test_normalize_a2z():
    rows = bs.normalize_a2z(load_fixture("a2z"))
    by = {r["exhibitor_name"]: r for r in rows}
    assert "Nevada Pavilion" not in by, "pavilion should be name-filtered out"
    assert set(by) == {"Acme Broadcast, Inc.", "Beta Media Systems", "Gamma Robotics"}
    assert by["Acme Broadcast, Inc."]["sqft"] == 600   # two booths summed: 400 + 200
    assert by["Acme Broadcast, Inc."]["booth_number"] == "1200, 1400"
    assert by["Acme Broadcast, Inc."]["website"] == "https://acmebroadcast.com"
    assert by["Beta Media Systems"]["sqft"] == 400     # from Width x Length, no AreaSqFt given
    assert by["Beta Media Systems"]["website"] == "betamedia.io"
    assert by["Gamma Robotics"]["sqft"] == 900
    assert all(r["size_source"] == "a2z-json" for r in rows)
    assert set(scraper.EXHIBITOR_COLUMNS) <= set(rows[0].keys())
    print("normalize_a2z OK")


def test_normalize_expocad():
    rows = bs.normalize_expocad(load_fixture("expocad"))
    by = {r["exhibitor_name"]: r for r in rows}
    assert "State of Ohio" not in by, "government listing should be name-filtered out"
    assert set(by) == {"Delta Cameras", "Epsilon Displays", "Zeta Robotics"}
    assert by["Delta Cameras"]["sqft"] == 400          # parsed from the string "400 sq ft"
    assert by["Epsilon Displays"]["sqft"] == 400        # from Width x Length (20 x 20)
    assert by["Zeta Robotics"]["sqft"] == 900           # polygon-only booth, shoelace formula
    assert by["Zeta Robotics"]["size_source"] == "expocad-json"
    assert set(scraper.EXHIBITOR_COLUMNS) <= set(rows[0].keys())
    print("normalize_expocad OK")


def test_normalize_expofp():
    rows = bs.normalize_expofp(load_fixture("expofp"))
    by = {r["exhibitor_name"]: r for r in rows}
    assert "Association of Widget Makers" not in by, "association should be name-filtered out"
    assert set(by) == {"Ironclad Fasteners", "Nimbus Robotics"}
    assert by["Ironclad Fasteners"]["sqft"] == 600      # two booths, pre-computed area summed: 400 + 200
    assert by["Ironclad Fasteners"]["booth_number"] == "101, 102"
    assert by["Nimbus Robotics"]["sqft"] == 900          # single booth via bare "exhibitorId"
    assert all(r["size_source"] == "expofp-json" for r in rows)
    print("normalize_expofp OK")


# ---------------------------------------------------------------------------
# Tolerance tests: the shapes that made the first live EXPOCAD run (swe.expocad.com/Events/we26)
# return zero rows. None of these payloads matches the original alias hypotheses exactly; each
# must still yield an honest, partial-or-flagged result -- never zero rows without a reason, and
# never an invented number.
# ---------------------------------------------------------------------------

def test_expocad_combined_records_abbreviated_keys():
    """One record type carrying booth + exhibitor fields, camelCase abbreviations (bthId, bthNo,
    exhName, sqFt), a paginated wrapper, and unknown extra fields mixed in."""
    log = []
    rows = bs.normalize_expocad(load_fixture("expocad_combined"), log=log.append)
    by = {r["exhibitor_name"]: r for r in rows}
    assert "Pavilion of Nations" not in by and "" not in by
    assert set(by) == {"Nova Widgets", "Quantum Rigging", "Helix Lighting"}, set(by)
    assert by["Nova Widgets"]["sqft"] == 400 and by["Nova Widgets"]["booth_number"] == "1207"
    assert by["Nova Widgets"]["size_source"] == "expocad-json"   # sqFt is an exact alias -> clean
    assert by["Nova Widgets"]["website"] == "novawidgets.com" and by["Nova Widgets"]["hall"] == "A"
    assert by["Quantum Rigging"]["sqft"] == 900
    # sqFt: null -> a partial row, never a guessed number
    assert by["Helix Lighting"]["sqft"] == 0 and by["Helix Lighting"]["size_source"] == "unknown"
    assert by["Helix Lighting"]["booth_number"] == "1601"
    joined = "\n".join(log)
    assert "bthNo (fuzzy)" in joined and "strategy = self-contained" in joined, joined
    print("expocad combined/abbreviated OK")


def test_expocad_nested_exhibitor_object():
    """Exhibitor and dimensions nested one level deep inside each booth; an 'Area' whose value is
    text must be ignored, and a 'Size' of '10x20' is a footprint statement (200 sq ft)."""
    log = []
    rows = bs.normalize_expocad(load_fixture("expocad_nested"), log=log.append)
    by = {r["exhibitor_name"]: r for r in rows}
    assert "Department of Commerce" not in by
    assert set(by) == {"Orbit Optics", "Cobalt Audio", "Mesa Robotics"}, set(by)
    assert by["Orbit Optics"]["sqft"] == 600 and by["Orbit Optics"]["width"] == 20 and by["Orbit Optics"]["length"] == 30
    assert by["Orbit Optics"]["website"] == "https://orbitoptics.com"
    assert by["Orbit Optics"]["size_source"] == "expocad-json"      # nested-leaf matches are clean
    assert by["Cobalt Audio"]["sqft"] == 200 and by["Cobalt Audio"]["width"] == 10 and by["Cobalt Audio"]["length"] == 20
    assert by["Mesa Robotics"]["sqft"] == 0 and by["Mesa Robotics"]["size_source"] == "unknown"   # "North Hall" is not an area
    # The one-record hall WRAPPER must not be mistaken for an exhibitor array and joined against.
    assert "strategy = self-contained" in "\n".join(log), "\n".join(log)
    assert len(by["Orbit Optics"]["booth_number"].split(",")) == 1
    print("expocad nested exhibitor object OK")


def test_a2z_join_key_differs_and_fuzzy_area():
    """Booths carry a bare 'id' + 'label'; exhibitors point at booths via a 'boothIds' list; the
    area key ('AreaSquareFt') is a spelling no alias list enumerates -> fuzzy, flagged."""
    log = []
    rows = bs.normalize_a2z(load_fixture("a2z_oddjoin"), log=log.append)
    by = {r["exhibitor_name"]: r for r in rows}
    assert set(by) == {"Acme Broadcast, Inc.", "Gamma Robotics", "Zephyr Drones"}, set(by)
    assert by["Acme Broadcast, Inc."]["sqft"] == 600 and by["Acme Broadcast, Inc."]["booth_number"] == "1200, 1400"
    assert by["Acme Broadcast, Inc."]["hall"] == "Hall A"
    assert by["Gamma Robotics"]["sqft"] == 900
    # fuzzy-matched area -> visibly lower-confidence source tag, distinct from a clean parse
    assert by["Acme Broadcast, Inc."]["size_source"] == "a2z-json-fuzzy"
    # exhibitor with no booth at all -> partial row, 0 sq ft, never dropped silently
    assert by["Zephyr Drones"]["sqft"] == 0 and by["Zephyr Drones"]["size_source"] == "unknown"
    assert by["Zephyr Drones"]["website"] == "zephyr.io"
    joined = "\n".join(log)
    assert "value overlap 100%" in joined and "AreaSquareFt (fuzzy)" in joined, joined
    print("a2z odd join key + fuzzy area OK")


def test_expofp_keyed_dict_and_split_tables():
    """Exhibitors as a dict keyed by id, booths across a list AND a columns+rows table; ExpoFP's
    pre-computed area is used and its geometry never recomputed."""
    rows = bs.normalize_expofp(load_fixture("expofp_weird"))
    by = {r["exhibitor_name"]: r for r in rows}
    assert set(by) == {"Ironclad Fasteners", "Nimbus Robotics"}, set(by)
    assert by["Ironclad Fasteners"]["sqft"] == 600           # 400 (list) + 200 (columns+rows table)
    assert by["Ironclad Fasteners"]["booth_number"] == "101, 301"
    assert by["Nimbus Robotics"]["sqft"] == 900               # NOT the 1-unit polygon
    assert all(r["size_source"] == "expofp-json" for r in rows)
    print("expofp keyed dict + split tables OK")


def test_names_only_is_best_effort_not_zero_rows():
    log = []
    rows = bs.normalize_expocad(load_fixture("names_only"), log=log.append)
    assert {r["exhibitor_name"] for r in rows} == {"Solaris Media", "Tundra Networks"}
    assert all(r["sqft"] == 0 and r["size_source"] == "unknown" for r in rows)
    assert any("BEST-EFFORT" in line for line in log), log
    print("names-only best-effort OK")


def test_no_names_returns_empty_with_diagnostics():
    log = []
    rows = bs.normalize_expocad(load_fixture("no_names"), log=log.append)
    assert rows == []
    joined = "\n".join(log)
    assert "boothId" in joined and "never found" in joined and "name" in joined, joined
    print("no-names diagnostics OK")


def test_extract_failure_message_is_diagnostic(monkeypatch=None):
    """_extract() must put the field report INTO the ScrapeError, not just 'no rows matched'."""
    orig_capture, orig_ensure = bs.capture_json_responses, bs.ensure_browser
    bs.ensure_browser = lambda: True
    bs.capture_json_responses = lambda url, timeout_s=0, log=None: load_fixture("no_names")
    try:
        try:
            bs.extract_expocad("https://swe.expocad.com/Events/we26/index.html")
            raise AssertionError("expected ScrapeError")
        except scraper.ScrapeError as exc:
            msg = str(exc)
            assert "boothId" in msg and "never found" in msg, msg
            assert "--normalise expocad" in msg
        # a names-only payload EXTRACTS (partial rows) and the log says every row is unsized
        log = []
        bs.capture_json_responses = lambda url, timeout_s=0, log=None: load_fixture("names_only")
        rows, meta = bs.extract_expocad("https://swe.expocad.com/Events/we26/index.html", log=log.append)
        assert len(rows) == 2 and meta["sized"] == 0 and meta["total"] == 2
        assert any("NO footprints" in line for line in log), log
        assert meta["parse_log"], "parse diagnostics should be carried in meta"
    finally:
        bs.capture_json_responses, bs.ensure_browser = orig_capture, orig_ensure
    print("_extract diagnostic failure path OK")


def test_strict_two_array_expocad_and_coexhibitors():
    """The ORIGINAL two-array EXPOCAD hypothesis (booths keyed by BoothID, exhibitors keyed by the
    same BoothID) still joins; a booth with a nested exhibitor LIST yields one row per name."""
    strict = [{"url": "b", "status": 200, "json": [
                  {"BoothID": "B1", "Area": "400 sq ft", "Hall": "North"},
                  {"BoothID": "B2", "Width": 30, "Length": 30, "Hall": "North"},
                  {"BoothID": "B3", "Area": 100, "Hall": "South"}]},
              {"url": "e", "status": 200, "json": [
                  {"BoothID": "B1", "CompanyName": "Alpha Co", "Website": "alpha.io"},
                  {"BoothID": "B2", "CompanyName": "Alpha Co", "Website": "alpha.io"},
                  {"BoothID": "B3", "CompanyName": "Beta Co", "Website": ""}]}]
    rows = bs.normalize_expocad(strict)
    by = {r["exhibitor_name"]: r for r in rows}
    assert by["Alpha Co"]["sqft"] == 1300 and by["Alpha Co"]["booth_number"] == "B1, B2"
    assert by["Alpha Co"]["hall"] == "North" and by["Alpha Co"]["website"] == "alpha.io"
    assert by["Beta Co"]["sqft"] == 100 and by["Beta Co"]["size_source"] == "expocad-json"

    coex = [{"url": "c", "status": 200, "json": {"booths": [
        {"boothNumber": "500", "areaSqFt": 800, "exhibitors": [{"name": "Co-A"}, {"name": "Co-B"}]},
        {"boothNumber": "600", "areaSqFt": 300, "exhibitors": [{"name": "Solo Inc"}]}]}}]
    rows = bs.normalize_expocad(coex)
    by = {r["exhibitor_name"]: r for r in rows}
    assert set(by) == {"Co-A", "Co-B", "Solo Inc"}, set(by)
    assert by["Co-A"]["sqft"] == 800 and by["Solo Inc"]["sqft"] == 300
    print("strict two-array + co-exhibitor list OK")


def test_generic_id_never_hijacks_a_join():
    """A booth's own numeric 'id' overlapping exhibitor ids by coincidence must not win over the
    specific (even fuzzily named) exhibitor reference."""
    caps = [{"url": "x/booths", "status": 200, "json": [
                {"id": 1, "exhibitorRef": 101, "boothNo": "A1", "sqft": 400},
                {"id": 2, "exhibitorRef": 102, "boothNo": "A2", "sqft": 200},
                {"id": 3, "exhibitorRef": 103, "boothNo": "A3", "sqft": 900}]},
            {"url": "x/exh", "status": 200, "json": [
                {"id": 101, "name": "Right One"}, {"id": 1, "name": "Coincidence Co"},
                {"id": 2, "name": "Other Co"}, {"id": 102, "name": "Second Right"}, {"id": 103, "name": "Third Right"}]}]
    rows = bs.normalize_a2z(caps)
    by = {r["exhibitor_name"]: r["sqft"] for r in rows}
    assert by == {"Right One": 400, "Second Right": 200, "Third Right": 900,
                  "Coincidence Co": 0, "Other Co": 0}, by
    print("generic-id join guard OK")


def test_profile_and_fuzzy_helpers():
    assert bs.norm_key("Booth_Number") == "boothnumber" and bs.norm_key(" AREA sq ft ") == "areasqft"
    prof = bs.profile_array([{"AreaSqFeet": 400, "Exhibitor": {"Name": "X Co"}, "Booth Number": "12"}], "$")
    assert prof.fields["area"].label == "AreaSqFeet" and prof.fields["area"].method == "exact"
    assert prof.fields["name"].label == "Exhibitor.Name"
    assert prof.fields["booth_number"].label == "Booth Number"
    # a value-type mismatch blocks a name-alias hit: "Exhibitor": 42 is an id, not a name
    prof2 = bs.profile_array([{"Exhibitor": 42, "CompanyName": "Y Co"}], "$")
    assert prof2.fields["name"].label == "CompanyName"
    # the same list-of-dicts is BOTH a polygon candidate and a sub-record path
    prof3 = bs.profile_array([{"Vertices": [[0, 0], [10, 0], [10, 10], [0, 10]], "Name": "Z"}], "$")
    assert prof3.fields["polygon"].label == "Vertices"
    print("profile/fuzzy helpers OK")


# ---------------------------------------------------------------------------
# Playwright interception against a tiny local page (no external network needed)
# ---------------------------------------------------------------------------

PORT = 8766
PAGE_HTML = b"""<!doctype html><html><body>
<script>
  fetch('/data.json').then(r => r.json()).then(d => { window.__loaded = true; });
</script>
</body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/data.json":
            body = json.dumps({"boothNumber": "100", "areaSqFt": 400, "companyId": "1",
                               "companyName": "Test Co"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE_HTML)))
            self.end_headers()
            self.wfile.write(PAGE_HTML)


def test_capture_json_responses():
    server = http.server.HTTPServer(("127.0.0.1", PORT), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            captures = bs.capture_json_responses(f"http://127.0.0.1:{PORT}/", timeout_s=20)
        except bs.BrowserUnavailable as exc:
            print(f"capture_json_responses SKIPPED (no browser on this host): {exc}")
            return
        assert len(captures) == 1, captures
        assert captures[0]["json"]["companyName"] == "Test Co"
        assert captures[0]["status"] == 200
        print("capture_json_responses OK:", captures[0]["url"])
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_extract_missing_data_raises():
    """A page with no matching JSON at all must raise ScrapeError, never crash silently."""
    empty_html = b"<!doctype html><html><body>no fetches here</body></html>"

    class _EmptyHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(empty_html)))
            self.end_headers()
            self.wfile.write(empty_html)

    port = PORT + 1
    server = http.server.HTTPServer(("127.0.0.1", port), _EmptyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            captures = bs.capture_json_responses(f"http://127.0.0.1:{port}/", timeout_s=20)
        except bs.BrowserUnavailable as exc:
            print(f"test_extract_missing_data_raises SKIPPED (no browser on this host): {exc}")
            return
        assert captures == []
        print("capture on an empty page correctly returns zero captures")
    finally:
        server.shutdown()
        thread.join(timeout=5)


if __name__ == "__main__":
    test_pick_num_polygon()
    test_normalize_a2z()
    test_normalize_expocad()
    test_normalize_expofp()
    test_expocad_combined_records_abbreviated_keys()
    test_expocad_nested_exhibitor_object()
    test_a2z_join_key_differs_and_fuzzy_area()
    test_expofp_keyed_dict_and_split_tables()
    test_names_only_is_best_effort_not_zero_rows()
    test_no_names_returns_empty_with_diagnostics()
    test_extract_failure_message_is_diagnostic()
    test_strict_two_array_expocad_and_coexhibitors()
    test_generic_id_never_hijacks_a_join()
    test_profile_and_fuzzy_helpers()
    test_capture_json_responses()
    test_extract_missing_data_raises()
    print("\nALL BROWSER SCRAPER TESTS PASSED")
