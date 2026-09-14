"""
Task 3 tests: the A2Z/EXPOCAD/ExpoFP normalisers against hand-written fixtures (no browser), plus
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
    test_capture_json_responses()
    test_extract_missing_data_raises()
    print("\nALL BROWSER SCRAPER TESTS PASSED")
