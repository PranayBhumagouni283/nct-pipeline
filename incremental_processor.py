"""
incremental_processor.py — Phase F-08

Reads PENDING jobs from trial_processing_queue, parses field_changes TEXT
into structured events, and populates the history tables.

Usage:
    python incremental_processor.py              # process all PENDING (default 2000)
    python incremental_processor.py --max 500    # limit batch size
    python incremental_processor.py --dept ADC   # only process one dept

Tables written:
    trial_change_events     — every parsed field-change event
    trial_status_history    — status transitions
    trial_phase_history     — phase transitions
    trial_date_history      — date field changes
    trial_processing_queue  — status updated (DONE / FAILED)
"""

from __future__ import annotations
import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.extensions

import db
import change_parser

# ── CHANGE_MATRIX — authoritative field → processing stage mapping ─────────────
# Keys: module name (lowercase) → field keyword (lowercase) or '*' → list of stages
# Stages: STATUS_HISTORY | PHASE_HISTORY | DATE_HISTORY | EMBED_INVALIDATE
CHANGE_MATRIX: dict[str, dict[str, list[str]]] = {
    "study status": {
        "overall status": ["STATUS_HISTORY", "EMBED_INVALIDATE"],
        "*":              ["EMBED_INVALIDATE"],
    },
    "status": {
        "overall status": ["STATUS_HISTORY", "EMBED_INVALIDATE"],
        "*":              ["EMBED_INVALIDATE"],
    },
    "study design": {
        "phase":          ["PHASE_HISTORY", "EMBED_INVALIDATE"],
        "*":              ["EMBED_INVALIDATE"],
    },
    "design": {
        "phase":          ["PHASE_HISTORY", "EMBED_INVALIDATE"],
        "*":              ["EMBED_INVALIDATE"],
    },
    "description": {
        "*":              ["EMBED_INVALIDATE"],
    },
    "eligibility": {
        "*":              ["EMBED_INVALIDATE"],
    },
    "outcome measures": {
        "*":              ["EMBED_INVALIDATE"],
    },
    "sponsor/collaborators": {
        "*":              ["EMBED_INVALIDATE"],
    },
    "contacts/locations": {
        "*":              [],   # site changes don't invalidate embeddings
    },
    "status dates": {
        "completion date":         ["DATE_HISTORY", "EMBED_INVALIDATE"],
        "primary completion date": ["DATE_HISTORY", "EMBED_INVALIDATE"],
        "start date":              ["DATE_HISTORY"],
        "*":                       [],
    },
    # default for any other module
    "__default__": {
        "*":              ["EMBED_INVALIDATE"],
    },
}


def _stages_for_event(module: str, field_name: str) -> list[str]:
    """Look up which processing stages apply to a given module+field pair."""
    mod_key   = module.lower().strip()
    field_key = field_name.lower().split(".")[0].strip()  # strip outcome sub-path

    mod_map = CHANGE_MATRIX.get(mod_key) or CHANGE_MATRIX["__default__"]
    stages  = mod_map.get(field_key) or mod_map.get("*") or []
    return stages


# ── DB helpers (supplement db.py) ─────────────────────────────────────────────

def _fetch_pending_jobs(max_jobs: int, dept_filter: str | None) -> list[dict]:
    rows = []
    with db._cur() as cur:
        if dept_filter:
            cur.execute(
                """
                SELECT id, nct_id, dept, job_type, source_run_date, payload
                FROM "CT".trial_processing_queue
                WHERE status = 'PENDING' AND dept = %s
                ORDER BY
                    CASE priority WHEN 'IMMEDIATE' THEN 1
                                  WHEN 'NORMAL'    THEN 2
                                  ELSE 3 END,
                    created_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (dept_filter, max_jobs),
            )
        else:
            cur.execute(
                """
                SELECT id, nct_id, dept, job_type, source_run_date, payload
                FROM "CT".trial_processing_queue
                WHERE status = 'PENDING'
                ORDER BY
                    CASE priority WHEN 'IMMEDIATE' THEN 1
                                  WHEN 'NORMAL'    THEN 2
                                  ELSE 3 END,
                    created_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (max_jobs,),
            )
        rows = [dict(r) for r in cur.fetchall()]
    return rows


def _mark_processing(job_id: int) -> None:
    with db._cur() as cur:
        cur.execute(
            'UPDATE "CT".trial_processing_queue SET status = %s WHERE id = %s',
            ("PROCESSING", job_id),
        )


def _mark_done(job_id: int) -> None:
    with db._cur() as cur:
        cur.execute(
            'UPDATE "CT".trial_processing_queue SET status = %s, processed_at = %s WHERE id = %s',
            ("DONE", datetime.now(timezone.utc), job_id),
        )


def _mark_failed(job_id: int, error: str) -> None:
    with db._cur() as cur:
        cur.execute(
            'UPDATE "CT".trial_processing_queue SET status = %s, processed_at = %s, error_message = %s WHERE id = %s',
            ("FAILED", datetime.now(timezone.utc), error[:1000], job_id),
        )


def _fetch_field_changes_row(nct_id: str, dept: str, run_date) -> dict | None:
    with db._cur() as cur:
        cur.execute(
            """
            SELECT field_changes, curr_status, modules_changed
            FROM "CT".field_changes_log
            WHERE nct_id = %s AND dept = %s AND run_date = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (nct_id, dept, run_date),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _insert_change_events(nct_id: str, dept: str, run_date, events: list[dict]) -> None:
    if not events:
        return
    records = [
        (
            nct_id,
            dept,
            run_date,
            ev["module"],
            ev["field_name"],
            ev["change_type"],
            ev.get("old_value"),
            ev.get("new_value"),
        )
        for ev in events
    ]
    with db._cur() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO "CT".trial_change_events
                (nct_id, dept, run_date, module, field_name, change_type, old_value, new_value)
            VALUES %s
            ON CONFLICT DO NOTHING
            """,
            records,
        )


def _upsert_status_history(nct_id: str, dept: str, run_date, old_status: str | None, new_status: str) -> None:
    with db._cur() as cur:
        cur.execute(
            """
            INSERT INTO "CT".trial_status_history (nct_id, dept, run_date, old_status, new_status)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (nct_id, dept, run_date) DO NOTHING
            """,
            (nct_id, dept, run_date, old_status, new_status),
        )


def _upsert_phase_history(nct_id: str, dept: str, run_date, old_phase: str | None, new_phase: str) -> None:
    with db._cur() as cur:
        cur.execute(
            """
            INSERT INTO "CT".trial_phase_history (nct_id, dept, run_date, old_phase, new_phase)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (nct_id, dept, run_date) DO NOTHING
            """,
            (nct_id, dept, run_date, old_phase, new_phase),
        )


def _upsert_date_history(nct_id: str, dept: str, run_date, date_field: str, old_date: str | None, new_date: str) -> None:
    with db._cur() as cur:
        cur.execute(
            """
            INSERT INTO "CT".trial_date_history (nct_id, dept, run_date, date_field, old_date, new_date)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (nct_id, dept, run_date, date_field) DO NOTHING
            """,
            (nct_id, dept, run_date, date_field, old_date, new_date),
        )


def _invalidate_embeddings(nct_id: str, dept: str) -> None:
    """Mark all ACTIVE embeddings for this trial as PENDING re-embed (Phase G hook)."""
    with db._cur() as cur:
        cur.execute(
            """
            UPDATE "CT".trial_semantic_documents
            SET document_status = 'PENDING'
            WHERE nct_id = %s
              AND document_status = 'ACTIVE'
            """,
            (nct_id,),
        )


# ── Job processors ─────────────────────────────────────────────────────────────

def _process_trial_modified(job: dict) -> dict:
    """Parse field_changes text and populate history tables. Returns summary dict."""
    nct_id   = job["nct_id"]
    dept     = job["dept"]
    run_date = job["source_run_date"]

    fcl_row = _fetch_field_changes_row(nct_id, dept, run_date)
    if not fcl_row:
        return {"events": 0, "status": False, "phase": False, "dates": 0, "embeds": 0}

    field_changes_text = fcl_row.get("field_changes") or ""
    events = change_parser.parse(field_changes_text)

    # Insert all structured events
    _insert_change_events(nct_id, dept, run_date, events)

    # Determine which stages to run
    all_stages: set[str] = set()
    for ev in events:
        all_stages.update(_stages_for_event(ev["module"], ev["field_name"]))

    status_written = phase_written = False
    dates_written  = 0
    embeds_done    = 0

    if "STATUS_HISTORY" in all_stages:
        result = change_parser.extract_status_change(events)
        if result:
            old_s, new_s = result
            if new_s:
                _upsert_status_history(nct_id, dept, run_date, old_s, new_s)
                status_written = True

    if "PHASE_HISTORY" in all_stages:
        result = change_parser.extract_phase_change(events)
        if result:
            old_p, new_p = result
            if new_p:
                _upsert_phase_history(nct_id, dept, run_date, old_p, new_p)
                phase_written = True

    if "DATE_HISTORY" in all_stages:
        for date_field, old_d, new_d in change_parser.extract_date_changes(events):
            if new_d:
                _upsert_date_history(nct_id, dept, run_date, date_field, old_d, new_d)
                dates_written += 1

    if "EMBED_INVALIDATE" in all_stages:
        _invalidate_embeddings(nct_id, dept)
        embeds_done = 1

    return {
        "events":  len(events),
        "status":  status_written,
        "phase":   phase_written,
        "dates":   dates_written,
        "embeds":  embeds_done,
    }


def _process_new_trial(job: dict) -> dict:
    """A newly tracked trial — just invalidate embeddings to trigger initial embed in Phase G."""
    _invalidate_embeddings(job["nct_id"], job["dept"])
    return {"events": 0, "status": False, "phase": False, "dates": 0, "embeds": 1}


def _process_tracking_list_added(job: dict) -> dict:
    """Same as new trial — mark for embedding."""
    return _process_new_trial(job)


# ── Main loop ──────────────────────────────────────────────────────────────────

_JOB_DISPATCH = {
    "TRIAL_MODIFIED":       _process_trial_modified,
    "NEW_TRIAL":            _process_new_trial,
    "TRACKING_LIST_ADDED":  _process_tracking_list_added,
}


def run(max_jobs: int = 2000, dept_filter: str | None = None) -> None:
    print(f"\n[incremental_processor] Starting — max_jobs={max_jobs}, dept={dept_filter or 'ALL'}")

    jobs = _fetch_pending_jobs(max_jobs, dept_filter)
    if not jobs:
        print("  No PENDING jobs found.")
        return

    print(f"  Found {len(jobs)} PENDING job(s).")
    done = failed = 0
    total_events = total_status = total_phase = total_dates = total_embeds = 0

    for job in jobs:
        job_id   = job["id"]
        nct_id   = job["nct_id"]
        job_type = job["job_type"]

        try:
            _mark_processing(job_id)
            handler = _JOB_DISPATCH.get(job_type)
            if handler is None:
                _mark_failed(job_id, f"Unknown job_type: {job_type}")
                failed += 1
                continue

            result = handler(job)
            _mark_done(job_id)
            done += 1

            total_events += result.get("events", 0)
            total_status += int(result.get("status", False))
            total_phase  += int(result.get("phase",  False))
            total_dates  += result.get("dates", 0)
            total_embeds += result.get("embeds", 0)

            print(
                f"  [DONE] {nct_id} ({job_type}) — "
                f"{result.get('events',0)} events, "
                f"status={result.get('status',False)}, "
                f"phase={result.get('phase',False)}, "
                f"dates={result.get('dates',0)}"
            )

        except Exception as exc:
            _mark_failed(job_id, str(exc))
            failed += 1
            print(f"  [FAIL] {nct_id} ({job_type}): {exc}", file=sys.stderr)

    print(f"""
[incremental_processor] Complete
  Jobs processed  : {done} done, {failed} failed
  Change events   : {total_events}
  Status changes  : {total_status}
  Phase changes   : {total_phase}
  Date changes    : {total_dates}
  Embed invalidate: {total_embeds}
""")


def main():
    parser = argparse.ArgumentParser(description="Process pending trial change jobs")
    parser.add_argument("--max",  type=int, default=2000, help="Max jobs to process (default 2000)")
    parser.add_argument("--dept", type=str, default=None,  help="Filter to one department")
    args = parser.parse_args()
    run(max_jobs=args.max, dept_filter=args.dept)


if __name__ == "__main__":
    main()
