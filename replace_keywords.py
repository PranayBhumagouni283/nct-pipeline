"""
replace_keywords.py
-------------------
Step 1: Replace all ADC dept_keywords with drugs from the new Excel.

Excel : C:\\Users\\LAPTOP\\Downloads\\ADC_DB_Asset List-21Aug2026.xlsx
Sheet : Matched DB & Excel (2066)

Action:
  - DELETE all existing ADC keywords from dept_keywords
  - INSERT fresh rows from Excel (drug_name, alias_names)
"""

import openpyxl
import db

DEPT       = "ADC"
EXCEL_PATH = r"C:\Users\LAPTOP\Downloads\ADC_DB_Asset List-21Aug2026.xlsx"
SHEET_NAME = "Matched DB & Excel (2066)"

# ── Read Excel ────────────────────────────────────────────────────────────────
print(f"Reading Excel: {EXCEL_PATH}")
wb = openpyxl.load_workbook(EXCEL_PATH, read_only=True)
ws = wb[SHEET_NAME]

drugs = []
for row in ws.iter_rows(min_row=2, values_only=True):
    drug  = str(row[0]).strip() if row[0] else ""
    alias = str(row[1]).strip() if row[1] else ""
    if not drug or drug.lower() in ("nan", "-", "—"):
        continue
    # Treat em-dash or plain dash as no aliases
    clean_alias = alias if alias and alias not in ("-", "—", "nan") else None
    drugs.append((drug, clean_alias))

print(f"Drugs loaded from Excel: {len(drugs)}")

# ── Replace in DB ─────────────────────────────────────────────────────────────
with db._cur() as cur:
    cur.execute("SELECT COUNT(*) AS cnt FROM dept_keywords WHERE dept = %s", (DEPT,))
    before = cur.fetchone()["cnt"]
    print(f"DB keywords before   : {before}")

    cur.execute("DELETE FROM dept_keywords WHERE dept = %s", (DEPT,))
    print(f"Deleted {before} existing ADC keywords")

    inserted = 0
    for drug, alias in drugs:
        cur.execute(
            "INSERT INTO dept_keywords (dept, drug_name, alias_names) VALUES (%s, %s, %s)",
            (DEPT, drug, alias),
        )
        inserted += 1

    cur.execute("SELECT COUNT(*) AS cnt FROM dept_keywords WHERE dept = %s", (DEPT,))
    after = cur.fetchone()["cnt"]
    print(f"Inserted             : {inserted}")
    print(f"DB keywords after    : {after}")

print("\nStep 1 complete.")
