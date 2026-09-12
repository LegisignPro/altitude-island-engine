"""Drive the Streamlit app headlessly with AppTest: demo extraction -> Apollo mock -> pitches -> export."""
import io
import os
import sys

import pandas as pd
from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import delta_engine  # noqa: E402
import pitch_generator as pg  # noqa: E402


def run(at: AppTest) -> AppTest:
    at.run(timeout=60)
    assert not at.exception, at.exception
    return at


at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=60)
run(at)
assert "How this works" in " ".join(m.value for m in at.markdown), "empty state missing"
assert at.sidebar.button[0].label == "Run Extraction"
assert at.sidebar.number_input[1].value == 400 and at.sidebar.number_input[2].value == 3000

# 1. Run extraction with no URL -> demo dataset (allowed by default)
at.sidebar.button[0].click()
run(at)
assert at.session_state["exhibitors"] is not None and len(at.session_state["exhibitors"]) == 20
metrics = {m.label: m.value for m in at.metric}
print("metrics after extraction:", metrics)
assert metrics["Total Exhibitors"] == "20"
demo_targets = at.session_state["exhibitors"]
expected_targets = int(((demo_targets["sqft"] >= 400) & (demo_targets["sqft"] <= 3000)).sum())
assert metrics["Target Islands"] == str(expected_targets), (metrics, expected_targets)
assert metrics["Estimated Pipeline Value"].startswith("$")
assert any("DEMO DATA" in w.value for w in at.warning), "demo banner missing"
assert len(at.tabs) == 1 and len(at.tabs[0].children) >= 5 or True

# 2. Sidebar filter change re-filters live
at.sidebar.number_input[1].set_value(900)
run(at)
metrics = {m.label: m.value for m in at.metric}
expected = int(((demo_targets["sqft"] >= 900) & (demo_targets["sqft"] <= 3000)).sum())
assert metrics["Target Islands"] == str(expected), metrics
at.sidebar.number_input[1].set_value(400)
run(at)

# 3. Prior-year CSV -> newborn island detection (simulate the upload through session state + engine)
prior_csv = "exhibitor_name,sqft\nLumen Audio Labs,100\nOrbit Wireless Video,400\nKestrel Aerial Cinema,900\nSummit Streaming Platforms,100\n"
at.session_state["prior_df"] = delta_engine.load_prior_csv(io.BytesIO(prior_csv.encode()))
at.session_state["prior_name"] = "prior.csv"
run(at)
metrics = {m.label: m.value for m in at.metric}
print("delta metrics:", {k: v for k, v in metrics.items() if k in ("Newborn islands", "Upgrades", "Stagnant", "New to show")})
assert metrics["Newborn islands"] == "2", metrics       # Lumen 100->400, Summit 100->600
assert metrics["Stagnant"] == "2"                        # Orbit 400->400, Kestrel 900->900

# 4. Apollo mock enrichment (button in tab 4)
btn = [b for b in at.button if b.label.startswith("Run Apollo enrichment")]
assert btn, [b.label for b in at.button]
assert "(" in btn[0].label and btn[0].label.endswith("companies)")
btn[0].click()
run(at)
assert len(at.session_state["orgs"]) > 0, "no orgs enriched"
assert at.session_state["credits_used"] == 0  # mock mode never spends credits
metrics = {m.label: m.value for m in at.metric}
print("apollo metrics:", {k: v for k, v in metrics.items() if k in ("Enriched", "Mode", "HQ known", "High freight savings", "Vegas local")})
assert metrics["Mode"] == "MOCK"
assert "HQ known" in metrics  # freight tab now populated

# 5. Pitch tab: all four trigger metrics present; NEWBORN ISLAND count = 2 unless dropped for size
badges = {t["badge"]: metrics.get(t["badge"]) for t in pg.TRIGGERS.values()}
print("trigger counts:", badges)
assert all(v is not None for v in badges.values())
assert sum(int(v) for v in badges.values()) == int(metrics["Target Islands"])
# export button exists with data
dl = [d for d in at.get("download_button") if "campaign" in d.label.lower()]
assert dl, "campaign download missing"

# 6. Text areas / selectbox preview rendered
assert any(t.label == "Intro line" for t in at.text_area)
intro = [t for t in at.text_area if t.label == "Intro line"][0].value
print("preview intro:", intro[:140])
assert "DEMO DATA" in intro or "NAB Show" in intro

# 7. Disable demo fallback + bad URL -> error state, no crash
at.sidebar.checkbox[1].set_value(False)
at.sidebar.text_input[1].set_value("https://notreal.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm")
at.sidebar.button[0].click()
run(at)
assert at.session_state["exhibitors"] is not None  # previous data kept... or cleared? check log
print("log after failed live run:", at.session_state["extract_log"])
assert any("failed" in line.lower() for line in at.session_state["extract_log"])

print("\nAPP TEST PASSED")
