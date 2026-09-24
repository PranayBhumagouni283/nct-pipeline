"""
Phase 3: Backfill new ES metadata fields into clinical_trials_semantic_v3.

New fields being added:
  biomarker_status     — e.g. HER2_POSITIVE, EGFR_WILD_TYPE (pipe-separated in PG)
  treatment_status     — e.g. TREATMENT_NAIVE, PRETREATED (pipe-separated in PG)
  endpoints_normalized — e.g. PFS, OS, ORR (pipe-separated in PG)
  interventions_list   — individual intervention names (pipe-separated Interventions column)
  conditions_list      — individual conditions (comma-separated conditions column)
  sponsors_list        — all sponsors including collaborating (pipe-separated Sponsors column)

This script does NOT re-embed. It reads PG values, fetches existing ES doc IDs by nct_id+dept,
and issues partial _update operations to add just the new fields.

Run:
    python _local_scripts/__phase3_es_metadata_backfill.py
    python _local_scripts/__phase3_es_metadata_backfill.py --dept ADC
    python _local_scripts/__phase3_es_metadata_backfill.py --dry-run
"""
import argparse
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

import psycopg2
import psycopg2.extras
from elasticsearch import Elasticsearch, helpers

PG_DSN     = os.environ["DATABASE_URL"]
ES_URL     = os.environ["ELASTICSEARCH_URL"]
ES_API_KEY = os.environ["ELASTICSEARCH_API_KEY"]
ES_INDEX   = os.environ.get("ELASTICSEARCH_INDEX", "clinical_trials_semantic_v3")

BATCH_SIZE = 500


def _split_pipe(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in str(value).split("|") if v.strip()]


def _split_clinical(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = str(value).replace("|", " | ")
    return [v.strip() for v in normalized.split(" | ") if v.strip()]


def _split_conditions(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in str(value).split(",") if v.strip()]


def main(dept_filter: str | None, dry_run: bool) -> None:
    print(f"[phase3-backfill] Connecting to PG...")
    conn = psycopg2.connect(PG_DSN)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print(f"[phase3-backfill] Connecting to ES index={ES_INDEX!r}...")
    es = Elasticsearch(ES_URL, api_key=ES_API_KEY, request_timeout=60)
    if not es.indices.exists(index=ES_INDEX):
        print(f"[phase3-backfill] ERROR: ES index {ES_INDEX!r} does not exist.")
        sys.exit(1)

    where_clause = ""
    params: list = []
    if dept_filter:
        where_clause = "WHERE ot.dept = %s"
        params.append(dept_filter)

    print(f"[phase3-backfill] Fetching new metadata from PG (dept={dept_filter or 'ALL'})...")
    cur.execute(f"""
        SELECT
            ot.nct_id,
            ot.dept,
            ot.biomarker_status,
            ot.treatment_status,
            ot.endpoints_normalized,
            ot."Interventions"   AS interventions_raw,
            ot.conditions,
            ot."Sponsors"        AS sponsors_raw
        FROM "CT".organized_trials ot
        {where_clause}
        ORDER BY ot.nct_id
    """, params or None)

    rows = cur.fetchall()
    print(f"[phase3-backfill] Got {len(rows)} trial rows from PG.")

    # Build a dict: (nct_id, dept) → new field values
    meta_map: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["nct_id"], row["dept"])
        meta_map[key] = {
            "biomarker_status":     _split_clinical(row.get("biomarker_status")),
            "treatment_status":     _split_clinical(row.get("treatment_status")),
            "endpoints_normalized": _split_clinical(row.get("endpoints_normalized")),
            "interventions_list":   _split_pipe(row.get("interventions_raw")),
            "conditions_list":      _split_conditions(row.get("conditions")),
            "sponsors_list":        _split_pipe(row.get("sponsors_raw")),
        }

    cur.close()
    conn.close()

    print(f"[phase3-backfill] Scrolling ES to find all chunk IDs...")
    # Scroll through all docs in the index (or filtered by dept)
    query: dict = {"query": {"match_all": {}}}
    if dept_filter:
        query = {"query": {"term": {"department": dept_filter}}}

    scroll_resp = es.search(
        index=ES_INDEX,
        body=query,
        scroll="5m",
        size=1000,
        _source=["nct_id", "department"],
    )

    scroll_id   = scroll_resp["_scroll_id"]
    hits        = scroll_resp["hits"]["hits"]
    total_docs  = scroll_resp["hits"]["total"]["value"]
    print(f"[phase3-backfill] Total ES docs to update: {total_docs}")

    updated   = 0
    skipped   = 0
    not_found = 0
    processed = 0
    bulk_ops  : list = []

    def flush_bulk():
        nonlocal updated
        if not bulk_ops:
            return
        if dry_run:
            print(f"  [dry-run] Would send {len(bulk_ops)} update ops")
            bulk_ops.clear()
            return
        success, errors = helpers.bulk(es, bulk_ops, stats_only=False, raise_on_error=False)
        updated += success
        if errors:
            print(f"  [warn] {len(errors)} bulk errors")
        bulk_ops.clear()

    while True:
        for hit in hits:
            doc_id   = hit["_id"]
            nct_id   = hit["_source"].get("nct_id", "")
            dept     = hit["_source"].get("department", "")
            key      = (nct_id, dept)
            new_meta = meta_map.get(key)
            processed += 1

            if new_meta is None:
                not_found += 1
                continue

            bulk_ops.append({
                "_op_type": "update",
                "_index":   ES_INDEX,
                "_id":      doc_id,
                "doc":      new_meta,
            })

            if len(bulk_ops) >= BATCH_SIZE:
                flush_bulk()
                print(f"  processed {processed}/{total_docs} ({100*processed//total_docs}%) — updated {updated}")

        # Next scroll page
        scroll_resp = es.scroll(scroll_id=scroll_id, scroll="5m")
        scroll_id   = scroll_resp["_scroll_id"]
        hits        = scroll_resp["hits"]["hits"]
        if not hits:
            break

    flush_bulk()
    es.clear_scroll(scroll_id=scroll_id)

    print(f"[phase3-backfill] Done.")
    print(f"  Updated:   {updated}")
    print(f"  Skipped:   {skipped}")
    print(f"  Not found: {not_found} (ES docs with no matching PG row — expected for deleted trials)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dept",    default=None, help="Filter by department (ADC, Liver Diseases, Infectious Diseases)")
    p.add_argument("--dry-run", action="store_true", help="Show what would be done without writing to ES")
    args = p.parse_args()
    main(args.dept, args.dry_run)
