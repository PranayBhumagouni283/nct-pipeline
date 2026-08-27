"""
Replace ADC general terms (Asset Pipeline) with the approved set.
Run once from NCT_Combined_Pipeline/:  python update_general_terms.py
"""

import db

DEPT = "ADC"

KEEP = [
    "ADC",
    "anti-body drug conjugate",
    "antibody drug conjugate",
    "antibody drug-conjugate",
    "antibody-drug conjugate",
]

with db._cur() as cur:
    # Remove all existing general terms for ADC
    cur.execute('DELETE FROM dept_general_terms WHERE dept = %s', (DEPT,))
    deleted = cur.rowcount

    # Insert only the approved set
    for term in KEEP:
        cur.execute(
            'INSERT INTO dept_general_terms (dept, term) VALUES (%s, %s) ON CONFLICT DO NOTHING',
            (DEPT, term),
        )

    print(f"Deleted {deleted} old terms.")
    print(f"Inserted {len(KEEP)} terms:")
    for t in KEEP:
        print(f"  - {t}")
