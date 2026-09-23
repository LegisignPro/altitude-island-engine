"""
Drive the Streamlit app headlessly with AppTest on REAL data only.

Uses the Map Grabber CSVs captured from live shows in tests/fixtures/real/ (two editions of the
same show when available, so the timeline has a genuine year-over-year comparison). There is no
demo dataset and no Apollo mock in v3, so this test never touches invented companies.

Run:  python tests/test_app.py
"""
import glob
import os
import sys

from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import quality  # noqa: E402
import showfile  # noqa: E402

REAL = os.path.join(ROOT, "tests", "fixtures", "real")


def run(at: AppTest) -> AppTest:
    at.run(timeout=90)
    assert not at.exception, at.exception
    return at


def by_label(widgets, start):
    return [w for w in widgets if w.label.startswith(start)]


# 0. Empty state: instructions + Map Grabber bookmark + Platform Check, no data, no demo
at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=90)
run(at)
text = " ".join(m.value for m in at.markdown)
assert "How this works" in text and "Map Grabber bookmark" in text and "Platform Check" in text
assert not by_label(at.sidebar.checkbox, "Load demo"), "demo option must be gone"
assert not by_label(at.sidebar.checkbox, "Use mock"), "mock option must be gone"
assert at.session_state["exhibitors"] is None
assert any(c.value.startswith("javascript:") for c in at.code), "bookmarklet code missing"

# 1. Run Extraction with no URL -> nothing loaded, no fallback
by_label(at.sidebar.button, "Run Extraction")[0].click()
run(at)
assert at.session_state["exhibitors"] is None, "an empty run must not load anything"
assert any("No URL" in line for line in at.session_state["extract_log"])

# 2. Seed with real grabber CSVs (what 'Load CSVs' does), newest year = current show
files = sorted(glob.glob(os.path.join(REAL, "*.csv")))
loaded = [showfile.load(f) for f in files]
groups = {}
for df, meta in loaded:
    groups.setdefault((meta["platform"], meta["show_base"]), []).append((df, meta))
multi = [g for g in groups.values() if len({m["show_year"] for _, m in g}) >= 2]
series = sorted((multi[0] if multi else max(groups.values(), key=len)), key=lambda t: t[1]["show_year"])
cur_df, cur_meta = series[-1]
print("current show:", cur_meta["filename"], len(cur_df), "companies; prior:", [m["filename"] for _, m in series[:-1]])

at.session_state["exhibitors"] = cur_df
at.session_state["meta"] = {**cur_meta, "extracted_at": "test"}
at.session_state["quality"] = quality.check(cur_df.to_dict("records"), cur_meta)
at.session_state["show_base"] = cur_meta["show_base"] or ""
at.session_state["show_year"] = cur_meta["show_year"]
slots = []
for i, (df, meta) in enumerate(series[:-1], start=1):
    slots.append({"id": i, "year": meta["show_year"], "df": df[["exhibitor_name", "sqft"]], "label": meta["filename"],
                  "error": "", "quality": "PASS"})
    at.session_state[f"slot_year_{i}"] = meta["show_year"]
at.session_state["timeline_slots"] = slots
at.session_state["_slot_seq"] = len(slots)
run(at)

metrics = {m.label: m.value for m in at.metric}
print("metrics:", metrics)
assert metrics["Total Exhibitors"] == f"{len(cur_df):,}"
expected = int(((cur_df["sqft"] >= 400) & (cur_df["sqft"] <= 3000)).sum())
assert metrics["Target Islands"] == f"{expected:,}", (metrics, expected)
assert any("Quality PASS" in m.value or "Quality WARN" in m.value for m in at.markdown), "quality pill missing"
assert not any("DEMO" in w.value.upper() for w in at.warning)

# 3. Timeline: real YoY comparison when two editions are on file
if slots:
    assert "Newborn islands" in metrics and "Dropped out" in metrics
    print("timeline:", {k: metrics[k] for k in ("Newborn islands", "Peak retreats", "New to show", "Dropped out")})

# 4. Apollo without a key: button disabled, no fake enrichment
run_apollo = by_label(at.button, "Run Apollo enrichment")
assert run_apollo and run_apollo[0].disabled, "Apollo must be disabled without a key"
assert not at.session_state["orgs"]

# 5. Exports are allowed on PASS/WARN and stamped with the grade
dl = [b for b in at.get("download_button") if "campaign" in str(b.proto.label).lower()]
assert dl and not dl[0].proto.disabled

# 6. A FAIL grade blocks exports (demo leakage can't be overridden)
q = quality.check(cur_df.to_dict("records"), cur_meta)
at.session_state["quality"] = {**q, "grade": "FAIL", "demo": True, "notes": "test"}
run(at)
dl = [b for b in at.get("download_button") if "campaign" in str(b.proto.label).lower()]
assert dl and dl[0].proto.disabled, "FAIL must block the campaign export"
assert not by_label(at.checkbox, "Use anyway"), "demo FAIL must not offer an override"

# 7. Bad live URL -> clear error, previous data kept, no fallback
by_label(at.sidebar.text_input, "Directory / floor-plan URL")[0].set_value("https://example.com/not-a-floor-plan")
by_label(at.sidebar.button, "Run Extraction")[0].click()
run(at)
assert any("failed" in line.lower() for line in at.session_state["extract_log"]), at.session_state["extract_log"]

print("\nAPP TEST PASSED")
