"""
Migrate keywords and general terms from local files into PostgreSQL (dricenta.com, schema "CT").

Tables:
  dept_keywords      (dept, drug_name, alias_names)
  dept_general_terms (dept, term)

Sources:
  ADC  -> ADC/keywords/ADC_Assets_With_Alias_Names__July2026.xlsx
  ADC  -> ADC/keywords/general_terms.txt
  ASMB -> ASMB/Keywords/ASMB_Keywords.xlsx
"""

from pathlib import Path
import db

HERE = Path(__file__).parent

print("Connecting to PostgreSQL...")


# ---Helpers ---──────────────────────────────────────────────────────────────────

def load_xlsx_keywords(path: Path) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    headers = [str(h).strip().lower() if h else "" for h in rows[0]]
    drug_col  = next((i for i, h in enumerate(headers) if "drug" in h or "asset" in h or "name" in h), 0)
    alias_col = next((i for i, h in enumerate(headers) if "alias" in h), 1)

    records = []
    for row in rows[1:]:
        drug  = str(row[drug_col]).strip() if row[drug_col] else ""
        alias = str(row[alias_col]).strip() if len(row) > alias_col and row[alias_col] else ""
        if not drug or drug.lower() in ("none", "drug name", "asset name"):
            continue
        if alias.lower() in ("none", "-"):
            alias = ""
        records.append({"drug_name": drug, "alias_names": alias})
    return records


def load_txt_terms(path: Path) -> list[str]:
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def insert_keywords(dept: str, records: list[dict], batch: int = 500):
    with db._cur() as cur:
        cur.execute("DELETE FROM dept_keywords WHERE dept = %s", (dept,))
    print(f"  Cleared existing {dept} keywords")

    rows = [{"dept": dept, "drug_name": r["drug_name"], "alias_names": r["alias_names"]} for r in records]
    db._bulk_insert("dept_keywords", rows, batch_size=batch)
    # Print progress
    for i in range(0, len(rows), batch):
        print(f"  Inserted {min(i+batch, len(rows))}/{len(rows)} {dept} keywords")


def insert_general_terms(dept: str, terms: list[str]):
    with db._cur() as cur:
        cur.execute("DELETE FROM dept_general_terms WHERE dept = %s", (dept,))
    print(f"  Cleared existing {dept} general terms")

    if not terms:
        print(f"  No general terms for {dept} — skipping")
        return

    rows = [{"dept": dept, "term": t} for t in terms]
    db._bulk_insert("dept_general_terms", rows)
    print(f"  Inserted {len(rows)} {dept} general terms")


# ---ADC ---──────────────────────────────────────────────────────────────────────
print("\n--- ADC Keywords ---")
adc_kw_path = HERE / "ADC" / "keywords" / "ADC_Assets_With_Alias_Names__July2026.xlsx"
adc_keywords = load_xlsx_keywords(adc_kw_path)
print(f"  Loaded {len(adc_keywords)} ADC drugs from Excel")
insert_keywords("ADC", adc_keywords)

print("\n---ADC General Terms ---")
adc_gt_path = HERE / "ADC" / "keywords" / "general_terms.txt"
adc_terms = load_txt_terms(adc_gt_path)
print(f"  Loaded {len(adc_terms)} ADC general terms")
insert_general_terms("ADC", adc_terms)

# ---ASMB ---─────────────────────────────────────────────────────────────────────
print("\n---ASMB Keywords ---")
asmb_kw_path = HERE / "ASMB" / "Keywords" / "ASMB_Keywords.xlsx"
asmb_keywords = load_xlsx_keywords(asmb_kw_path)
print(f"  Loaded {len(asmb_keywords)} ASMB drugs from Excel")
insert_keywords("ASMB", asmb_keywords)

print("\n---ASMB General Terms ---")
insert_general_terms("ASMB", [])

# ---Verification ---─────────────────────────────────────────────────────────────
print("\n---Verification ---")
adc_kw  = db.load_dept_keywords("ADC")
adc_gt  = db.load_dept_general_terms("ADC")
asmb_kw = db.load_dept_keywords("ASMB")
print(f"  ADC  keywords     : {len(adc_kw)}")
print(f"  ADC  general terms: {len(adc_gt)}")
print(f"  ASMB keywords     : {len(asmb_kw)}")
print(f"  ASMB general terms: 0")
print("\nDone!")
