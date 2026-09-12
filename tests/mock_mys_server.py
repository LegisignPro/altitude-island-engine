"""Local stand-in for a MapYourShow host, mirroring the three JSON endpoints + detail page."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

EXHIBITORS = [  # exhid, name, halls, booth, sponsor, video
    ("1001", "Acme Broadcast, Inc.", ["C"], "C1200", True, True),
    ("1002", "Nevada Pavilion", ["C"], "C1300", False, False),
    ("1003", "Smallco Widgets", ["N"], "N400", False, False),
    ("1004", "State of Ohio", ["W"], "W100", False, False),
    ("1005", "Beta Media Systems", ["W"], "W2000", False, True),
    ("1006", "Gamma Lighting Association", ["N"], "N700", False, False),
    ("1007", "Delta Cameras", ["N"], "N800", False, False),
    ("1008", "No Booth Corp", [], "", False, False),
]
BOOTHS = {  # hall -> list of (exhid, name, booth, width_in, height_in, area)
    "C": [("1001", "Acme Broadcast, Inc.", "C1200", 240, 360, 600), ("1002", "Nevada Pavilion", "C1300", 600, 600, 2500)],
    "N": [("1003", "Smallco Widgets", "N400", 120, 120, 100), ("1006", "Gamma Lighting Association", "N700", 240, 240, 400),
          ("1007", "Delta Cameras", "N800", 240, 240, 400), ("1007", "Delta Cameras", "N802", 120, 240, 200),
          ("9999", "Ghost Exhibits", "N900", 360, 360, 900)],
    "W": [("1004", "State of Ohio", "W100", 240, 240, 400), ("1005", "Beta Media Systems", "W2000", 480, 600, 2000)],
}
WEBSITES = {"1001": "https://www.acmebroadcast.com", "1005": "betamedia.io", "1007": "https://deltacams.com"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if self.headers.get("X-Requested-With") != "XMLHttpRequest" and "remote-proxy" in u.path:
            self.send_response(403); self.end_headers(); return
        if u.path.endswith("/ajax/remote-proxy.cfm"):
            if q.get("action") == "getsearchoptions":
                return self._send({"DATA": [{"fieldvalue": "C", "fielddisplay": "Central Hall"},
                                            {"fieldvalue": "N", "fielddisplay": "North Hall"},
                                            {"fieldvalue": "W", "fielddisplay": "West Hall"}]})
            if q.get("action") == "search":
                hits = [{"id": e[0], "fields": {"exhid_l": e[0], "exhname_t": e[1], "hallid_la": e[2],
                                                "boothsdisplay_la": [e[3] + "randomstring"] if e[3] else [],
                                                "featured_b": e[4], "video_b": e[5]}} for e in EXHIBITORS]
                return self._send({"DATA": {"results": {"exhibitor": {"hit": hits}}}})
        if "/floorplan/02/_remote-proxy.cfm" in u.path and q.get("action") == "GetBoothByHall":
            cols = ["OBJECTTYPE", "EXHID", "EXHNAME", "BOOTHDISPLAY", "FEATUREPROPERTIES"]
            data = [["booth", b[0], b[1], b[2], json.dumps({"properties": {"boothWidth": b[3], "boothHeight": b[4], "area": b[5]}})]
                    for b in BOOTHS.get(q.get("hallid"), [])]
            data.append(["label", "", "", "", "{}"])
            return self._send({"COLUMNS": cols, "DATA": data})
        if u.path.endswith("/floorplan/"):
            return self._send('<html><script>var ShowID = "TEST26"; app="floorplan/02/x"</script></html>', "text/html")
        if "exhibitor-details.cfm" in u.path:
            site = WEBSITES.get(q.get("exhid"), "")
            return self._send(f'<html><script>var x = {{ websiteValue: "{site}" }}</script></html>', "text/html")
        self.send_response(404); self.end_headers()


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
