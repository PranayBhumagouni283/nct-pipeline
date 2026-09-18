"""
create_processing_queue.py — One-time setup: create trial_processing_queue in PROD.
"""
import os
import psycopg2
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

DDL = """
CREATE TABLE IF NOT EXISTS "CT".trial_processing_queue (
    id               BIGSERIAL PRIMARY KEY,
    nct_id           TEXT        NOT NULL,
    dept             TEXT        NOT NULL,
    job_type         TEXT        NOT NULL DEFAULT 'TRIAL_MODIFIED',
    priority         TEXT        NOT NULL DEFAULT 'NORMAL',
    source_run_date  DATE,
    payload          JSONB       NOT NULL DEFAULT '{}',
    status           TEXT        NOT NULL DEFAULT 'PENDING',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (nct_id, dept, job_type, source_run_date)
);
"""

prod_url = os.environ.get("DATABASE_URL")

if not prod_url:
    print("DATABASE_URL not set — check your .env file")
else:
    conn = psycopg2.connect(prod_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(DDL)
    cur.close()
    conn.close()
    print("  trial_processing_queue — OK")

print("\nDone.")
