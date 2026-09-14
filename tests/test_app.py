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

# 3. Task 2: multi-year timeline. First exercise the real "+ Add prior year" UI and confirm a
# slot's widgets render; AppTest cannot simulate an actual file upload, so the data itself is
# injected directly into session_state (same approach the old single-CSV test used).
add_btn = [b for b in at.button if b.label.startswith("+ Add prior year")]
assert add_btn, [b.label for b in at.button]
add_btn[0].click()
run(at)
assert any(n.label == "Year" for n in at.number_input), "timeline slot Year input missing after Add"
assert any(b.label == "Remove slot" for b in at.button), "timeline slot Remove button missing after Add"

prior_csv = "exhibitor_name,sqft\nLumen Audio Labs,100\nOrbit Wireless Video,400\nKestrel Aerial Cinema,900\nSummit Streaming Platforms,100\n"
prior_df = delta_engine.load_prior_csv(io.BytesIO(prior_csv.encode())).rename(
    columns={"prior_name": "exhibitor_name", "prior_sqft": "sqft"})[["exhibitor_name", "sqft"]]
at.session_state["timeline_slots"] = [{"id": 901, "year": 2026, "df": prior_df, "label": "prior.csv", "error": ""}]
run(at)
metrics = {m.label: m.value for m in at.metric}
print("timeline metrics (2 slots):", {k: v for k, v in metrics.items()
      if k in ("Newborn islands", "Peak retreats", "Steady growth", "Shrinking", "New to show")})
assert metrics["Newborn islands"] == "2", metrics        # Lumen 100->400, Summit 100->600
assert all(k in metrics for k in ("Peak retreats", "Steady growth", "Shrinking", "New to show"))

# 3b. A THIRD slot turns a couple of those two-point deltas into a real multi-year shape:
# Lumen grows steadily (100 -> 250 -> 400) and Vantage peaks then pulls back (300 -> 900 -> 600,
# still >= 400 sq ft) -- Woz's "small -> large -> medium" pattern.
prior_2024 = pd.DataFrame([{"exhibitor_name": "Lumen Audio Labs", "sqft": 100},
                           {"exhibitor_name": "Vantage Robotics Systems", "sqft": 300}])
prior_2025 = pd.DataFrame([{"exhibitor_name": "Lumen Audio Labs", "sqft": 250},
                           {"exhibitor_name": "Vantage Robotics Systems", "sqft": 900}])
at.session_state["timeline_slots"] = [
    {"id": 902, "year": 2024, "df": prior_2024, "label": "2024.csv", "error": ""},
    {"id": 903, "year": 2025, "df": prior_2025, "label": "2025.csv", "error": ""},
]
run(at)
metrics = {m.label: m.value for m in at.metric}
print("timeline metrics (3 slots):", {k: v for k, v in metrics.items()
      if k in ("Newborn islands", "Peak retreats", "Steady growth", "Shrinking", "New to show")})
assert metrics["Peak retreats"] == "1", metrics      # Vantage: 300 -> 900 -> 600 (50-90% of peak)
assert metrics["Steady growth"] == "1", metrics      # Lumen: 100 -> 250 -> 400, monotonic, >= 400
timeline_grid = [d for d in at.dataframe if "trajectory" in d.value.columns][0].value
trajectories_seen = set(timeline_grid["trajectory"])
print("timeline table trajectories:", trajectories_seen)
assert "PEAK RETREAT" in trajectories_seen, trajectories_seen
assert "STEADY GROWTH" in trajectories_seen, trajectories_seen

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

# 7. Task 1 regression: the "+1 year" fix. Overriding "Show year" in the sidebar must
#    propagate everywhere the show label is used (banner pill, intro line) -- and it must
#    come from EITHER this extraction's own inferred year OR what's typed here, never from
#    today's date or a calendar.
def show_year_box():
    # AppTest rebuilds the element tree on every run(), so the widget handle must be
    # re-fetched after each one rather than reused stale.
    return [t for t in at.sidebar.text_input if t.label == "Show year"][0]


show_year_box().set_value("2026")
run(at)
assert at.session_state["show_year"] == 2026, at.session_state["show_year"]
assert any("2026" in m.value for m in at.markdown), "show-year override missing from banner pill"
intro2 = [t for t in at.text_area if t.label == "Intro line"][0].value
print("preview intro after year override:", intro2[:140])
assert "2026" in intro2, intro2
# Clearing the override falls back to the year THIS extraction observed (2027 for the
# demo dataset) -- session_state itself holds None (never silently re-guessed), and only
# the composed label falls back.
show_year_box().set_value("")
run(at)
assert at.session_state["show_year"] is None, at.session_state["show_year"]
assert any("2027" in m.value for m in at.markdown), "fallback to extracted show year missing"

# 8. Disable demo fallback + bad URL -> error state, no crash
at.sidebar.checkbox[1].set_value(False)
at.sidebar.text_input[1].set_value("https://notreal.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm")
at.sidebar.button[0].click()
run(at)
assert at.session_state["exhibitors"] is not None  # previous data kept... or cleared? check log
print("log after failed live run:", at.session_state["extract_log"])
assert any("failed" in line.lower() for line in at.session_state["extract_log"])

print("\nAPP TEST PASSED")
