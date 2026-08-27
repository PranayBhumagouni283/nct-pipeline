"""
tag_excel_primary_drug.py
--------------------------
Reads the "New Trials (Not Tracked)" sheet from the CT Search Excel,
applies Primary Drug tagging using ADC drug keywords from the DB,
and writes results to a new CSV + Excel file.

Usage:
    python tag_excel_primary_drug.py
    python tag_excel_primary_drug.py --dept ASMB      # different dept
    python tag_excel_primary_drug.py --no-excel        # CSV only (faster)
"""

import re
import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db

# ── Config ────────────────────────────────────────────────────────────────────

INPUT_FILE  = Path(r"C:\Users\LAPTOP\Downloads\NCT 5 Lakh+\data\output\CT_Search_Intervention_20260824_1816.xlsx")
SHEET_NAME  = "New Trials (Not Tracked)"
INTERV_COL  = "Interventions"
OUTPUT_DIR  = INPUT_FILE.parent
OUTPUT_STEM = INPUT_FILE.stem + "_tagged"


# ── Drug keyword loading (same logic as combined_pipeline.py) ─────────────────

def load_drug_keywords(dept: str) -> list[tuple[str, list[str]]]:
    drugs: list[tuple[str, list[str]]] = []
    for row in db.load_dept_keywords(dept):
        name = str(row.get("drug_name", "") or "").strip()
        if not name or name in ("nan", "-"):
            continue
        alias_raw = str(row.get("alias_names", "") or "").strip()
        aliases: list[str] = []
        if alias_raw and alias_raw not in ("-", "nan", "—", ""):
            for a in re.split(r"[|,]", alias_raw):
                a = a.strip()
                if a and a not in ("-", "—", ""):
                    aliases.append(a)
        drugs.append((name, aliases))
    return drugs


def compile_patterns(drugs: list[tuple[str, list[str]]]) -> list[tuple[str, re.Pattern]]:
    """One compiled regex per drug — all terms (name + aliases) OR-ed with word boundaries."""
    compiled = []
    for drug_name, aliases in drugs:
        terms = [t for t in ([drug_name] + aliases) if t and t not in ("-", "—")]
        if not terms:
            continue
        pattern = "|".join(
            r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])" for t in terms
        )
        compiled.append((drug_name, re.compile(pattern, re.IGNORECASE)))
    return compiled


def tag_row(interventions: str, compiled: list[tuple[str, re.Pattern]]) -> str:
    if not interventions or not isinstance(interventions, str):
        return ""
    matched = [name for name, pat in compiled if pat.search(interventions)]
    return " | ".join(matched)


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dept: str, write_excel: bool):
    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas not installed. Run: pip install pandas openpyxl")
        sys.exit(1)

    # 1. Load keywords
    print(f"\nLoading {dept} drug keywords from DB...")
    drugs = load_drug_keywords(dept)
    print(f"  {len(drugs)} keywords loaded")
    compiled = compile_patterns(drugs)
    print(f"  {len(compiled)} patterns compiled\n")

    # 2. Read Excel
    print(f"Reading sheet '{SHEET_NAME}' from {INPUT_FILE.name}...")
    t0 = time.time()
    df = pd.read_excel(INPUT_FILE, sheet_name=SHEET_NAME, dtype=str, engine="openpyxl")
    print(f"  {len(df):,} rows loaded in {time.time()-t0:.1f}s")

    if INTERV_COL not in df.columns:
        print(f"ERROR: Column '{INTERV_COL}' not found. Available: {list(df.columns)}")
        sys.exit(1)

    # 3. Tag Primary Drug
    print(f"\nTagging Primary Drug (this may take a few minutes for {len(df):,} rows)...")
    t0 = time.time()
    CHUNK = 50_000

    results = []
    for start in range(0, len(df), CHUNK):
        chunk = df[INTERV_COL].iloc[start:start + CHUNK]
        results.extend(tag_row(v, compiled) for v in chunk)
        done = min(start + CHUNK, len(df))
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed > 0 else 0
        eta = (len(df) - done) / rate if rate > 0 else 0
        print(f"  {done:>8,} / {len(df):,}  ({rate:,.0f} rows/s  ETA {eta:.0f}s)")

    df["Primary Drug"] = results
    tagged = sum(1 for v in results if v)
    print(f"\n  Tagged: {tagged:,} / {len(df):,} rows ({tagged/len(df)*100:.1f}%)")
    print(f"  Total time: {time.time()-t0:.1f}s")

    # 4. Save CSV (always)
    csv_path = OUTPUT_DIR / (OUTPUT_STEM + ".csv")
    print(f"\nSaving CSV → {csv_path.name}...")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  Done — {csv_path.stat().st_size / 1e6:.1f} MB")

    # 5. Save Excel (optional — slow for 1M rows)
    if write_excel:
        xlsx_path = OUTPUT_DIR / (OUTPUT_STEM + ".xlsx")
        print(f"\nSaving Excel → {xlsx_path.name}  (this can take several minutes)...")
        t0 = time.time()
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name=SHEET_NAME, index=False)
        print(f"  Done in {time.time()-t0:.1f}s — {xlsx_path.stat().st_size / 1e6:.1f} MB")

    print("\nAll done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dept",       default="ADC", help="Department to load keywords from (default: ADC)")
    parser.add_argument("--no-excel",   action="store_true", help="Skip Excel output, CSV only (much faster)")
    args = parser.parse_args()
    main(args.dept, not args.no_excel)
