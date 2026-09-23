"""
Build tests/fixtures/real/ from Map Grabber debug captures (<show>_<year>_<platform>_raw.json).

For each capture:
  1. Parse the FULL raw payload with the Python normaliser and compare it with the CSV the browser
     produced in the same run (every company, every sq ft). Prints the parity result; a mismatch
     is an error.
  2. Trim the payload to ~N companies (keeping every booth, shape and co-exhibitor those companies
     touch, so shared-booth splits stay exact) and write it with the browser's CSV rows for the same
     companies, so the repo test re-checks parity without megabytes of fixtures.
  3. Record the full-show counts in expected_counts.json.

    python tests/make_fixtures.py <capture.json> [...]
"""

import io
import json
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import showfile  # noqa: E402
from extractors import a2z, common, expocad, expofp, mys  # noqa: E402

OUT = os.path.join(ROOT, "tests", "fixtures", "real")
NORM = {"a2z": a2z.normalise, "expocad": expocad.normalise, "expofp": expofp.normalise, "mapyourshow": mys.normalise}
N_COMPANIES = 60


def prep(cap):
    raw = dict(cap["raw"])
    if cap["platform"] in ("a2z", "mapyourshow"):
        raw.setdefault("title", f"{cap['meta'].get('show_name', '')} {cap['meta'].get('show_year', '')}".strip())
    return raw


def parity(rows, csv_text):
    browser, _ = showfile.load(io.StringIO(csv_text), filename="browser.csv")
    py = pd.DataFrame(rows)[["exhibitor_name", "sqft"]]
    browser["k"], py["k"] = browser["exhibitor_name"].map(common.name_key), py["exhibitor_name"].map(common.name_key)
    m = browser[["k", "sqft"]].merge(py[["k", "sqft"]], on="k", how="outer", suffixes=("_js", "_py"), indicator=True)
    only = m[m["_merge"] != "both"]
    diff = m[(m["_merge"] == "both") & ((m["sqft_js"] - m["sqft_py"]).abs() > 1)]
    return len(browser), len(py), only, diff


def pick_keys(rows, n):
    """A spread: the biggest islands, some mid, some small, some shared."""
    rows = sorted(rows, key=lambda r: -r["sqft"])
    shared = [r for r in rows if r.get("shared_booth")][:8]
    picks = rows[:20] + rows[len(rows) // 3: len(rows) // 3 + 15] + rows[-10:] + shared
    seen, out = set(), []
    for r in picks:
        k = common.name_key(r["exhibitor_name"])
        if k not in seen:
            seen.add(k)
            out.append(r["exhibitor_name"])
    return out[:n]


def trim(platform, raw, names):
    keys = {common.name_key(n) for n in names}
    if platform == "expofp":
        d = raw["data"]
        ex_ids = {e["id"] for e in d["exhibitors"] if common.name_key(e["name"]) in keys}
        booths = [b for b in d["booths"] if not b.get("special") and set(b.get("exhibitors") or []) & ex_ids]
        all_ids = {i for b in booths for i in b["exhibitors"]}
        # keep the other companies on those booths so the shared split is unchanged
        exhibitors = [e for e in d["exhibitors"] if e["id"] in all_ids]
        bnames = {"b" + str(b["name"]) for b in booths}
        layers = [{**layer, "shapes": [s for s in layer["shapes"] if s["id"] in bnames]} for layer in raw["layers"]]
        return {"data": {**d, "booths": booths, "exhibitors": exhibitors}, "layers": layers}
    if platform == "expocad":
        ex = raw["exhibitors"]
        keep_idx = {i for i, e in enumerate(ex) if e and common.name_key(e.get("name")) in keys}
        booths = [b for b in raw["booths"] if str(b.get("exhibitorIndex")).isdigit() and int(b["exhibitorIndex"]) in keep_idx]
        # re-index exhibitors compactly
        order = sorted(keep_idx)
        remap = {old: new for new, old in enumerate(order)}
        booths = [{**b, "exhibitorIndex": str(remap[int(b["exhibitorIndex"])])} for b in booths]
        return {**raw, "booths": booths, "exhibitors": [ex[i] for i in order]}
    if platform == "a2z":
        recs = [r for r in raw["records"] if common.name_key(r.get("name")) in keys]
        ids = {str(r.get("hyperLinkFieldValue") or r.get("id")) for r in recs}
        return {**raw, "records": recs, "details": {k: v for k, v in (raw.get("details") or {}).items() if k in ids}}
    if platform == "mapyourshow":
        hits = [h for h in raw["gallery"] if common.name_key((h.get("fields") or {}).get("exhname_t")) in keys]
        ids = {str((h.get("fields") or {}).get("exhid_l") or h.get("id")) for h in hits}
        hb = {}
        for hall, p in (raw.get("hall_booths") or {}).items():
            cols = p.get("COLUMNS") or []
            ix = cols.index("EXHID") if "EXHID" in cols else None
            data = [row for row in p.get("DATA") or [] if ix is not None and str(row[ix]) in ids]
            if data:
                hb[hall] = {"COLUMNS": cols, "DATA": data}
        return {**raw, "gallery": hits, "hall_booths": hb,
                "details": {k: v for k, v in (raw.get("details") or {}).items() if k in ids}}
    raise ValueError(platform)


def main(paths):
    os.makedirs(OUT, exist_ok=True)
    counts_path = os.path.join(OUT, "expected_counts.json")
    counts = json.load(open(counts_path)) if os.path.exists(counts_path) else {}
    for path in paths:
        cap = json.load(open(path))
        platform, meta = cap["platform"], cap["meta"]
        raw = prep(cap)
        rows, pmeta = NORM[platform](raw)
        n_js, n_py, only, diff = parity(rows, cap["csv"])
        stem = f"{common.name_key(meta.get('show_name') or 'show').replace(' ', '-')}_{meta.get('show_year') or 'unknown'}_{platform}"
        print(f"{stem}: browser {n_js} companies, python {n_py}; only-one-side {len(only)}, sq-ft mismatches {len(diff)}")
        if len(only) or len(diff):
            print(only.head(10).to_string(), "\n", diff.head(10).to_string())
            raise SystemExit(f"PARITY FAILED for {stem}")
        names = pick_keys(rows, N_COMPANIES)
        small = trim(platform, raw, names)
        srows, _ = NORM[platform](small)
        keys = {common.name_key(n) for n in names}     # only the selected companies are compared
        with open(os.path.join(OUT, f"{stem}_raw.json"), "w") as fh:
            json.dump({"platform": platform, "url": cap["url"], "captured_at": cap["captured_at"], "meta": meta,
                       "raw": small, "trimmed_from": {"companies": len(rows)}}, fh, separators=(",", ":"))
        # write the browser's own rows (original CSV lines) for the kept companies
        full = pd.read_csv(io.StringIO(cap["csv"].lstrip("﻿")), dtype=str, keep_default_na=False)
        full[full["exhibitor_name"].map(common.name_key).isin(keys)].to_csv(
            os.path.join(OUT, f"{stem}.csv"), index=False)
        counts[f"{stem}_raw.json"] = {
            "full_show": {"companies": len(rows), "islands_400": sum(r["sqft"] >= 400 for r in rows),
                          "shared": sum(bool(r["shared_booth"]) for r in rows),
                          "platform_count": pmeta.get("platform_count"), "show_year": pmeta.get("show_year")},
            "companies": len(srows), "islands_400": sum(r["sqft"] >= 400 for r in srows)}
        print(f"  trimmed fixture: {len(srows)} companies, {os.path.getsize(os.path.join(OUT, stem + '_raw.json')) // 1024} KB")
    with open(counts_path, "w") as fh:
        json.dump(counts, fh, indent=1, sort_keys=True)


if __name__ == "__main__":
    main(sys.argv[1:])
