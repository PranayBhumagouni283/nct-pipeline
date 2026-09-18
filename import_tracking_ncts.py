import openpyxl
import re
import psycopg2

NCT_PATTERN = re.compile(r'NCT\d{8}', re.IGNORECASE)

wb = openpyxl.load_workbook(
    r'C:\Users\LAPTOP\Downloads\Trial Database_ASMB.xlsx',
    read_only=True, data_only=True
)

SHEET_DEPT_MAP = [
    ('Liver_Diseases',      'Liver Diseases'),
    ('Infectious_Diseases', 'Infectious Diseases'),
]

DBS = [
    {
        'label': 'dricenta.com (original)',
        'host': 'dricenta.com', 'port': 5432, 'dbname': 'postgres',
        'user': 'pranay', 'password': '5TOiJ8no0hhlSZXMryQF_kxK',
        'options': '-csearch_path="CT"', 'sslmode': 'disable',
    },
    {
        'label': 'AWS RDS (duplicate)',
        'host': 'nct-tracking-db.cvisguiwivvz.eu-north-1.rds.amazonaws.com',
        'port': 5432, 'dbname': 'nctdb',
        'user': 'postgres', 'password': 'Pranay280320',
        'options': '-csearch_path="CT"', 'sslmode': 'require',
    },
]

# Extract unique (dept, indication, nct_id) from both sheets
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

# Insert into both DBs
for db in DBS:
    print(f"\n=== {db['label']} ===")
    conn = psycopg2.connect(
        host=db['host'], port=db['port'], dbname=db['dbname'],
        user=db['user'], password=db['password'],
        options=db['options'], sslmode=db['sslmode']
    )
    cur = conn.cursor()
    inserted = 0
    skipped = 0
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
