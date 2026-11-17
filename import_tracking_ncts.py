"""
import_tracking_ncts.py — One-time utility: import NCT IDs from Excel into tracking_list.
Usage: python import_tracking_ncts.py
Reads DATABASE_URL from .env.
"""
import os, re
import openpyxl
import psycopg2
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

NCT_PATTERN = re.compile(r'NCT\d{8}', re.IGNORECASE)

EXCEL_PATH = r'C:\Users\LAPTOP\Downloads\Trial Database_ASMB.xlsx'

SHEET_DEPT_MAP = [
    ('Liver_Diseases',      'Liver Diseases'),
    ('Infectious_Diseases', 'Infectious Diseases'),
]

wb = openpyxl.load_workbook(EXCEL_PATH, read_only=True, data_only=True)

records = []
for sheet_name, dept in SHEET_DEPT_MAP:
    ws = wb[sheet_name]
    seen = set()
    non_nct = 0
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue
        if not any(row):
            continue
        indication   = str(row[0]).strip() if row[0] else ''
        trial_id_raw = str(row[3]).strip() if row[3] else ''
        for token in trial_id_raw.replace('\r', '').split('\n'):
            token = token.strip()
            m = NCT_PATTERN.match(token)
            if m:
                nct_id = m.group(0).upper()
                key = (dept, indication, nct_id)
                if key not in seen:
                    seen.add(key)
                    records.append({'dept': dept, 'indication': indication, 'nct_id': nct_id})
            elif token:
                non_nct += 1
    total = len([r for r in records if r['dept'] == dept])
    print(f"{dept}: {total} unique NCTs, {non_nct} non-NCT identifiers skipped")

print(f"\nTotal records to insert: {len(records)}")

prod_url = os.environ.get("DATABASE_URL")
if not prod_url:
    raise SystemExit("DATABASE_URL not set — check your .env file")

conn = psycopg2.connect(prod_url)
cur = conn.cursor()
inserted = skipped = 0
for r in records:
    cur.execute(
        '''INSERT INTO "CT".tracking_list (nct_id, dept, indication, added_by, added_date)
           VALUES (%s, %s, %s, %s, CURRENT_DATE)
           ON CONFLICT DO NOTHING''',
        (r['nct_id'], r['dept'], r['indication'], 'import')
    )
    if cur.rowcount > 0:
        inserted += 1
    else:
        skipped += 1
conn.commit()
cur.close()
conn.close()
print(f"  Inserted: {inserted}  Duplicates skipped: {skipped}")
print('\nDone.')
