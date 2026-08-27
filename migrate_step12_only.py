"""
migrate_step12_only.py
-----------------------
Migrate only nct_version_pairs from old Supabase to new PostgreSQL.
Disables RLS (or adds a permissive policy) before inserting since the table
has RLS enabled from the Supabase schema.

Run after migrate_supabase_to_postgres.py fails at step 12.
"""

import sys
from pathlib import Path
from supabase import create_client
import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
import db

_SUPA_URL = "https://kgbpqpdflqiawidpkfeh.supabase.co"
_SUPA_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtnYnBxcGRmbHFpYXdpZHBrZmVoIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MzY2MDA3NywiZXhwIjoyMDk5MjM2MDc3fQ.9qRdp_s79YSbNc6KYA2U2FL5Lr6b06HzKaJHZpZmtGI"
supa = create_client(_SUPA_URL, _SUPA_KEY)

_READ_BATCH = 500
SMALL_BATCH = 5   # curr_full_data rows are large JSON; keep batches tiny to avoid HTTP/2 disconnect


def _fresh_cur():
    """Always get a fresh connection+cursor to avoid leftover error state."""
    # Force reconnect so any prior error state is gone
    db._conn = None
    conn = db._db()
    return conn.cursor()


def fix_rls():
    """
    Try to disable RLS on nct_version_pairs so the pranay user can insert.
    Returns True if we succeeded (via disable or policy), False otherwise.
    """
    # Check first — if RLS is already off, nothing to do
    try:
        cur = _fresh_cur()
        cur.execute("""
            SELECT relrowsecurity FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'CT' AND c.relname = 'nct_version_pairs'
        """)
        row = cur.fetchone()
        cur.close()
        if row and not row["relrowsecurity"]:
            print("  OK: RLS is already disabled on nct_version_pairs")
            return True
    except Exception as e:
        print(f"  Could not check RLS status: {e}")

    # Attempt 1: disable RLS entirely (requires table ownership)
    try:
        cur = _fresh_cur()
        cur.execute('ALTER TABLE "nct_version_pairs" DISABLE ROW LEVEL SECURITY')
        cur.close()
        print("  OK: RLS disabled on nct_version_pairs")
        return True
    except Exception as e:
        print(f"  Could not disable RLS: {e}")

    # Attempt 2: add a permissive policy for pranay (requires table ownership)
    try:
        cur = _fresh_cur()
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE schemaname = 'CT'
                      AND tablename  = 'nct_version_pairs'
                      AND policyname = 'migration_pranay_all'
                ) THEN
                    EXECUTE $p$
                        CREATE POLICY migration_pranay_all
                        ON "nct_version_pairs"
                        FOR ALL
                        TO pranay
                        USING (true)
                        WITH CHECK (true)
                    $p$;
                END IF;
            END$$
        """)
        cur.close()
        print("  OK: Permissive RLS policy created/already exists for pranay")
        return True
    except Exception as e:
        print(f"  Could not create RLS policy: {e}")

    return False


def fetch_all(table, select="*"):
    rows = []
    offset = 0
    while True:
        resp = (
            supa.table(table)
            .select(select)
            .range(offset, offset + _READ_BATCH - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        print(f"  [Supabase] {table}: fetched {len(rows)} rows...", end="\r", flush=True)
        if len(batch) < _READ_BATCH:
            break
        offset += _READ_BATCH
    print(f"  [Supabase] {table}: {len(rows)} rows total          ")
    return rows


print("\n[12/12] nct_version_pairs — fixing RLS first...")
ok = fix_rls()
if not ok:
    print()
    print("ERROR: Cannot bypass RLS. Ask the dricenta.com admin to run:")
    print()
    print('  GRANT BYPASS RLS TO pranay;')
    print()
    print("Or alternatively:")
    print('  ALTER TABLE "CT".nct_version_pairs DISABLE ROW LEVEL SECURITY;')
    sys.exit(1)

print("  Fetching metadata (no JSON)...")
meta_rows = fetch_all(
    "nct_version_pairs",
    "id,nct_id,dept,note,total_versions,prev_version,curr_version,"
    "prev_date,curr_date,curr_status,modules_changed,field_change_count,"
    "field_changes,fetched_at",
)
meta_rows = [{k: v for k, v in r.items() if k not in ("id", "fetched_at")} for r in meta_rows]
print(f"  {len(meta_rows)} version pair rows found")

print(f"  Fetching curr_full_data in batches of {SMALL_BATCH} (with retry)...")
pair_data: dict[tuple, dict] = {}
offset = 0
total = 0
MAX_RETRIES = 5
while True:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = (
                supa.table("nct_version_pairs")
                .select("nct_id,dept,prev_version,curr_version,curr_full_data")
                .range(offset, offset + SMALL_BATCH - 1)
                .execute()
            )
            batch = resp.data or []
            break
        except Exception as e:
            if attempt == MAX_RETRIES:
                print(f"\n  FAILED after {MAX_RETRIES} attempts at offset {offset}: {e}")
                raise
            wait = 2 ** attempt
            print(f"\n  Retry {attempt}/{MAX_RETRIES} after error (waiting {wait}s): {e}")
            import time; time.sleep(wait)
    for r in batch:
        key = (r["nct_id"], r["dept"], r["prev_version"], r["curr_version"])
        pair_data[key] = r.get("curr_full_data")
    total += len(batch)
    print(f"  curr_full_data: fetched {total} / ~{len(meta_rows)} rows...", end="\r", flush=True)
    if len(batch) < SMALL_BATCH:
        break
    offset += SMALL_BATCH
print(f"  curr_full_data: {total} rows fetched          ")

# Merge metadata + curr_full_data
final_rows = []
for r in meta_rows:
    key = (r["nct_id"], r["dept"], r["prev_version"], r["curr_version"])
    r["curr_full_data"] = pair_data.get(key)
    final_rows.append(r)

WRITE_CHUNK = 50   # rows per reconnect — keeps each connection short-lived
conflict_cols = ["nct_id", "dept", "prev_version", "curr_version"]
total_written = 0
print(f"  Writing {len(final_rows)} rows to PostgreSQL (chunks of {WRITE_CHUNK}, 1 row/query)...")
for chunk_start in range(0, len(final_rows), WRITE_CHUNK):
    chunk = final_rows[chunk_start : chunk_start + WRITE_CHUNK]
    # Force a fresh connection for every chunk — avoids long-lived connection drops
    db._conn = None
    for attempt in range(1, 6):
        try:
            db._bulk_upsert("nct_version_pairs", chunk, conflict_cols, batch_size=1)
            total_written += len(chunk)
            print(f"  wrote {total_written}/{len(final_rows)}", end="\r", flush=True)
            break
        except Exception as e:
            db._conn = None
            if attempt == 5:
                print(f"\n  FAILED chunk at offset {chunk_start} after 5 attempts: {e}")
                raise
            import time; time.sleep(2 ** attempt)
print(f"\n  [PG] nct_version_pairs: {total_written} rows upserted")

# Final count
with db._cur() as cur:
    cur.execute("SELECT COUNT(*) FROM nct_version_pairs")
    count = cur.fetchone()["count"]
print(f"\n  nct_version_pairs in PostgreSQL: {count} rows")
print("\nDone!")
