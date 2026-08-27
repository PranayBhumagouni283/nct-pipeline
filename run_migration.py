"""One-shot DB migration — adds indication column + new scan columns."""
import psycopg2
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")
DB_URL = os.environ["DATABASE_URL"]

conn = psycopg2.connect(DB_URL)
conn.autocommit = True

S = [
    # 1. New table
    (
        "1-create dept_indications",
        """CREATE TABLE IF NOT EXISTS "CT".dept_indications (
            id SERIAL PRIMARY KEY,
            dept TEXT NOT NULL,
            indication TEXT NOT NULL,
            keywords TEXT NOT NULL,
            UNIQUE(dept, indication)
        )""",
    ),
    # 2. tracking_list
    ("2a-tracking_list add col", 'ALTER TABLE "CT".tracking_list ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT \'\''),
    ("2b-tracking_list drop pk", 'ALTER TABLE "CT".tracking_list DROP CONSTRAINT IF EXISTS tracking_list_pkey'),
    ("2c-tracking_list add pk",  'ALTER TABLE "CT".tracking_list ADD PRIMARY KEY (nct_id, dept, indication)'),
    # 3. modified_log
    ("3a-modified_log add col",  'ALTER TABLE "CT".modified_log ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT \'\''),
    ("3b-modified_log drop pk",  'ALTER TABLE "CT".modified_log DROP CONSTRAINT IF EXISTS modified_log_pkey'),
    ("3c-modified_log add pk",   'ALTER TABLE "CT".modified_log ADD PRIMARY KEY (nct_id, dept, indication, run_date)'),
    # 4. field_changes_log
    ("4-field_changes add col",  'ALTER TABLE "CT".field_changes_log ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT \'\''),
    # 5. pipeline_state
    ("5a-pipeline_state add col",'ALTER TABLE "CT".pipeline_state ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT \'\''),
    ("5b-pipeline_state drop pk",'ALTER TABLE "CT".pipeline_state DROP CONSTRAINT IF EXISTS pipeline_state_pkey'),
    ("5c-pipeline_state add pk", 'ALTER TABLE "CT".pipeline_state ADD PRIMARY KEY (dept, indication)'),
    # 6. run_history
    ("6-run_history add col",    'ALTER TABLE "CT".run_history ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT \'\''),
    # 7. rejected_trials
    ("7a-rejected add col",      'ALTER TABLE "CT".rejected_trials ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT \'\''),
    ("7b-rejected drop pk",      'ALTER TABLE "CT".rejected_trials DROP CONSTRAINT IF EXISTS rejected_trials_pkey'),
    ("7c-rejected add pk",       'ALTER TABLE "CT".rejected_trials ADD PRIMARY KEY (nct_id, dept, indication)'),
    # 8. new_candidates_log
    ("8a-candidates add indication",   'ALTER TABLE "CT".new_candidates_log ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT \'\''),
    ("8b-candidates add mesh",         'ALTER TABLE "CT".new_candidates_log ADD COLUMN IF NOT EXISTS mesh_conditions TEXT DEFAULT \'\''),
    ("8c-candidates add collaborators",'ALTER TABLE "CT".new_candidates_log ADD COLUMN IF NOT EXISTS collaborators TEXT DEFAULT \'\''),
    ("8d-candidates add phase",        'ALTER TABLE "CT".new_candidates_log ADD COLUMN IF NOT EXISTS phase TEXT DEFAULT \'\''),
    ("8e-candidates add study_type",   'ALTER TABLE "CT".new_candidates_log ADD COLUMN IF NOT EXISTS study_type TEXT DEFAULT \'\''),
    ("8f-candidates add enrollment",   'ALTER TABLE "CT".new_candidates_log ADD COLUMN IF NOT EXISTS enrollment TEXT DEFAULT \'\''),
    ("8g-candidates drop pk",          'ALTER TABLE "CT".new_candidates_log DROP CONSTRAINT IF EXISTS new_candidates_log_pkey'),
    ("8h-candidates add pk",           'ALTER TABLE "CT".new_candidates_log ADD PRIMARY KEY (nct_id, dept, indication, run_date)'),
    # 9. unmatched_log
    ("9a-unmatched add indication",   'ALTER TABLE "CT".unmatched_log ADD COLUMN IF NOT EXISTS indication TEXT NOT NULL DEFAULT \'\''),
    ("9b-unmatched add mesh",         'ALTER TABLE "CT".unmatched_log ADD COLUMN IF NOT EXISTS mesh_conditions TEXT DEFAULT \'\''),
    ("9c-unmatched add collaborators",'ALTER TABLE "CT".unmatched_log ADD COLUMN IF NOT EXISTS collaborators TEXT DEFAULT \'\''),
    ("9d-unmatched add phase",        'ALTER TABLE "CT".unmatched_log ADD COLUMN IF NOT EXISTS phase TEXT DEFAULT \'\''),
    ("9e-unmatched add study_type",   'ALTER TABLE "CT".unmatched_log ADD COLUMN IF NOT EXISTS study_type TEXT DEFAULT \'\''),
    ("9f-unmatched add enrollment",   'ALTER TABLE "CT".unmatched_log ADD COLUMN IF NOT EXISTS enrollment TEXT DEFAULT \'\''),
    ("9g-unmatched drop unique",      'ALTER TABLE "CT".unmatched_log DROP CONSTRAINT IF EXISTS unmatched_log_nct_id_dept_key'),
    ("9h-unmatched add unique",       'ALTER TABLE "CT".unmatched_log ADD UNIQUE (nct_id, dept, indication)'),
]

errors = []
with conn.cursor() as cur:
    for name, stmt in S:
        try:
            cur.execute(stmt)
            print(f"OK  [{name}]")
        except Exception as e:
            msg = f"ERR [{name}]: {e}"
            print(msg)
            errors.append(msg)

conn.close()
print()
if errors:
    print(f"{len(errors)} ERROR(S) — review above.")
else:
    print("All statements succeeded. Migration complete.")
