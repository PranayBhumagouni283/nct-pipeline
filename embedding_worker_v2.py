"""
embedding_worker_v2.py -- V2 BGE-M3 embedding worker targeting Elasticsearch v2 index.

Source DB  : dricenta.com (production) via DATABASE_URL in .env
ES Index   : clinical_trials_semantic_v2
Model      : BAAI/bge-m3 (1024-dim dense, cosine)

V2 changes vs V1:
  - indications[]   : array from tracking_list JOIN (core scope fix)
  - primary_drug    : keyword metadata field
  - collaborators   : text+keyword metadata field
  - funder_type     : keyword metadata field
  - countries[]     : array split from pipe-separated string
  - masking         : keyword metadata field
  - fda_regulated_drug : keyword metadata field
  - Staging table   : trial_semantic_documents_v2 (created automatically)
  - chunk_generator_v2.py : updated TRIAL_SUMMARY, PROTOCOL, PI_INFO

Two-pass operation:
  Pass 1 (populate): Generate V2 chunk text from organized_trials + tracking_list.
                     Upsert into trial_semantic_documents_v2 as PENDING.
  Pass 2 (embed):    Fetch PENDING docs, encode with BGE-M3, index into ES v2,
                     mark docs ACTIVE.

Usage:
    python embedding_worker_v2.py                     # full run (all depts)
    python embedding_worker_v2.py --populate-only     # Pass 1 only
    python embedding_worker_v2.py --embed-only        # Pass 2 only
    python embedding_worker_v2.py --dept ADC          # single dept
    python embedding_worker_v2.py --dept ADC --max 100
    python embedding_worker_v2.py --chunk-type TRIAL_SUMMARY

Requirements:
    pip install FlagEmbedding elasticsearch>=8.14 psycopg2-binary python-dotenv
"""
import argparse
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from chunk_generator_v2 import CHUNK_TYPES, content_hash, generate_chunks

load_dotenv(Path(__file__).parent / ".env")

ES_INDEX      = "clinical_trials_semantic_v2"
MODEL_NAME    = "BAAI/bge-m3"
DENSE_DIM     = 1024
EMBEDDING_VER = 2

# ── DB connection ──────────────────────────────────────────────────────────────

_db_conn = None


def _get_db(force_reconnect: bool = False):
    global _db_conn
    if _db_conn is None or _db_conn.closed or force_reconnect:
        if _db_conn and not _db_conn.closed:
            try:
                _db_conn.close()
            except Exception:
                pass
        dsn = os.getenv("DATABASE_URL") or os.getenv("DB_URL")
        if not dsn:
            sys.exit("DATABASE_URL not set in .env")
        _db_conn = psycopg2.connect(
            dsn,
            cursor_factory=psycopg2.extras.RealDictCursor,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
    return _db_conn


@contextmanager
def _cur():
    conn = _get_db()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except psycopg2.OperationalError:
        try:
            conn.rollback()
        except Exception:
            pass
        _get_db(force_reconnect=True)
        raise


# ── Ensure staging table exists ────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS "CT".trial_semantic_documents_v2 (
    id               SERIAL PRIMARY KEY,
    nct_id           TEXT NOT NULL,
    dept             TEXT NOT NULL,
    chunk_type       TEXT NOT NULL,
    content_text     TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    document_status  TEXT NOT NULL DEFAULT 'PENDING',
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT trial_semantic_documents_v2_uq UNIQUE (nct_id, dept, chunk_type)
)
"""

def ensure_staging_table() -> None:
    with _cur() as cur:
        cur.execute(_CREATE_TABLE)
    print("  Staging table: trial_semantic_documents_v2 ready.")


# ── Elasticsearch client ───────────────────────────────────────────────────────

_es_client = None


def _build_es_client():
    try:
        from elasticsearch import Elasticsearch
    except ImportError:
        sys.exit("elasticsearch not installed. Run: pip install 'elasticsearch>=8.14'")
    url      = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
    api_key  = os.getenv("ELASTICSEARCH_API_KEY", "")
    username = os.getenv("ELASTICSEARCH_USERNAME", "elastic")
    password = os.getenv("ELASTICSEARCH_PASSWORD", "")
    kwargs   = {"request_timeout": 60}
    if api_key:
        return Elasticsearch(url, api_key=api_key, **kwargs)
    elif password:
        return Elasticsearch(url, basic_auth=(username, password), verify_certs=False, **kwargs)
    return Elasticsearch(url, **kwargs)


def _get_es():
    global _es_client
    if _es_client is None:
        _es_client = _build_es_client()
        info    = _es_client.info()
        version = info.get("version", {}).get("number", "serverless")
        url     = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
        print(f"  Elasticsearch {version} at {url} → {ES_INDEX}")
    return _es_client


def _fresh_es():
    global _es_client
    try:
        if _es_client is not None:
            _es_client.close()
    except Exception:
        pass
    _es_client = _build_es_client()
    return _es_client


# ── BGE-M3 model ──────────────────────────────────────────────────────────────

_model = None


def _get_model():
    global _model
    if _model is None:
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError:
            sys.exit("FlagEmbedding not installed. Run: pip install FlagEmbedding")
        print(f"  Loading {MODEL_NAME} (first run downloads ~2.3 GB) ...")
        _model = BGEM3FlagModel(MODEL_NAME, use_fp16=True)
        print("  Model ready.")
    return _model


# ── Pass 1: Populate trial_semantic_documents_v2 ───────────────────────────────

# Fetches organized_trials + indications from tracking_list via LATERAL JOIN
_SELECT_TRIALS = """
    SELECT
        ot.nct_id,
        ot.dept,
        ot."Brief Title",
        ot."Official Title",
        ot."Overall Status",
        ot."briefSummary",
        ot."detailedDescription",
        ot.conditions,
        ot.phases,
        ot."Interventions",
        ot."Primary Outcomes",
        ot."Secondary Outcomes",
        ot."eligibilityCriteria",
        ot."MeSH Conditions",
        ot."MeSH Interventions",
        ot."Sponsors",
        ot."Collaborators",
        ot."Enrollment",
        ot."studyType",
        ot."Primary Drug",
        ot."Allocation",
        ot."Intervention Model",
        ot."Primary Purpose",
        ot."Masking",
        ot."Funder Type",
        ot."FDA Regulated Drug",
        ot.countries,
        ot."Start Date",
        ot."Primary Completion Date",
        ot."Study First Post Date",
        ot."Last Update Post Date",
        ot."Sex",
        ot."Minimum Age",
        ot."Standard Ages",
        ot."healthyVolunteers",
        ot."Overall Officials",
        ot."Central Contacts",
        COALESCE(tl_agg.indications, ARRAY[]::text[]) AS indications
    FROM "CT".organized_trials ot
    LEFT JOIN LATERAL (
        SELECT ARRAY_AGG(DISTINCT tl.indication ORDER BY tl.indication)
               FILTER (WHERE tl.indication IS NOT NULL AND tl.indication != '') AS indications
        FROM "CT".tracking_list tl
        WHERE tl.nct_id = ot.nct_id AND tl.dept = ot.dept
    ) tl_agg ON true
"""

_UPSERT_DOC = """
    INSERT INTO "CT".trial_semantic_documents_v2
        (nct_id, dept, chunk_type, content_text, content_hash, document_status)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (nct_id, dept, chunk_type) DO UPDATE
    SET
        content_text    = EXCLUDED.content_text,
        content_hash    = EXCLUDED.content_hash,
        document_status = CASE
            WHEN "CT".trial_semantic_documents_v2.content_hash = EXCLUDED.content_hash
            THEN "CT".trial_semantic_documents_v2.document_status
            ELSE 'PENDING'
        END,
        updated_at = NOW()
    WHERE "CT".trial_semantic_documents_v2.content_hash != EXCLUDED.content_hash
       OR "CT".trial_semantic_documents_v2.document_status = 'PENDING'
"""


def populate_chunks(dept_filter: str | None, chunk_type_filter: str | None) -> tuple[int, int]:
    """Pass 1: generate V2 chunk text and upsert into trial_semantic_documents_v2."""
    sql    = _SELECT_TRIALS
    params: list = []
    if dept_filter:
        sql += " WHERE ot.dept = %s"
        params.append(dept_filter)

    with _cur() as cur:
        cur.execute(sql, params or None)
        rows = cur.fetchall()

    total = len(rows)
    print(f"  Trials to process: {total}")

    trials_done  = 0
    docs_written = 0
    _COMMIT_EVERY = 100

    for batch_start in range(0, total, _COMMIT_EVERY):
        batch = rows[batch_start : batch_start + _COMMIT_EVERY]
        batch_trials = 0
        batch_docs   = 0

        for attempt in range(1, 4):
            try:
                with _cur() as cur:
                    batch_rows = []
                    for row in batch:
                        chunks = generate_chunks(dict(row))
                        nct_id = row["nct_id"]
                        dept   = row["dept"]
                        batch_trials += 1
                        for chunk in chunks:
                            ct = chunk["chunk_type"]
                            if chunk_type_filter and ct != chunk_type_filter:
                                continue
                            text  = chunk["content_text"]
                            chash = content_hash(text)
                            batch_rows.append((nct_id, dept, ct, text, chash, "PENDING"))
                    for row_args in batch_rows:
                        cur.execute(_UPSERT_DOC, row_args)
                        batch_docs += cur.rowcount
                break
            except psycopg2.OperationalError as e:
                batch_trials = 0
                batch_docs   = 0
                print(f"\n  [WARN] Connection error attempt {attempt}/3: {e}")
                if attempt == 3:
                    print("  [ERROR] Batch failed after 3 attempts, skipping.")
                else:
                    time.sleep(5 * attempt)

        trials_done  += batch_trials
        docs_written += batch_docs
        print(f"  [{trials_done}/{total}] Docs written: {docs_written}", flush=True)

    return trials_done, docs_written


# ── Pass 2: Encode + index into Elasticsearch v2 ──────────────────────────────

def _claim_pending(dept_filter: str | None, chunk_type_filter: str | None, limit: int) -> list[dict]:
    """
    Atomically claim PENDING docs as PROCESSING using FOR UPDATE SKIP LOCKED.
    Safe for multiple parallel workers — each worker claims a non-overlapping batch.
    Returns full doc rows with organized_trials metadata joined in.
    """
    # Step 1: atomic claim via UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)
    where_parts = ["document_status = 'PENDING'"]
    claim_params: list = []
    if dept_filter:
        where_parts.append("dept = %s")
        claim_params.append(dept_filter)
    if chunk_type_filter:
        where_parts.append("chunk_type = %s")
        claim_params.append(chunk_type_filter)
    claim_params.append(limit)

    claim_sql = f"""
        UPDATE "CT".trial_semantic_documents_v2
        SET document_status = 'PROCESSING', updated_at = NOW()
        WHERE id IN (
            SELECT id FROM "CT".trial_semantic_documents_v2
            WHERE {" AND ".join(where_parts)}
            ORDER BY id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id
    """
    with _cur() as cur:
        cur.execute(claim_sql, claim_params)
        claimed_ids = [r["id"] for r in cur.fetchall()]

    if not claimed_ids:
        return []

    # Step 2: fetch full data (metadata join) for the claimed ids
    fetch_sql = """
        SELECT
            tsd.id, tsd.nct_id, tsd.dept, tsd.chunk_type,
            tsd.content_text, tsd.content_hash,
            ot."Overall Status"      AS study_status,
            ot.phases                AS phase,
            ot."studyType"           AS study_type,
            ot."Primary Purpose"     AS primary_purpose,
            ot."Primary Drug"        AS primary_drug,
            ot."Sponsors"            AS sponsors,
            ot."Collaborators"       AS collaborators,
            ot."Funder Type"         AS funder_type,
            ot.conditions            AS conditions,
            ot.countries             AS countries,
            ot."Enrollment"          AS enrollment,
            ot."Intervention Model"  AS intervention_model,
            ot."Allocation"          AS allocation,
            ot."Masking"             AS masking,
            ot."FDA Regulated Drug"  AS fda_regulated_drug,
            ot."Sex"                 AS sex,
            ot."Minimum Age"         AS minimum_age,
            ot."Standard Ages"       AS standard_ages,
            ot."healthyVolunteers"   AS healthy_volunteers,
            ot."Start Date"                  AS study_start_date,
            ot."Primary Completion Date"     AS primary_completion_date,
            ot."Study First Post Date"       AS first_post_date,
            ot."Last Update Post Date"       AS last_update_post_date,
            COALESCE(tl_agg.indications, ARRAY[]::text[]) AS indications
        FROM "CT".trial_semantic_documents_v2 tsd
        LEFT JOIN "CT".organized_trials ot
               ON ot.nct_id = tsd.nct_id AND ot.dept = tsd.dept
        LEFT JOIN LATERAL (
            SELECT ARRAY_AGG(DISTINCT tl.indication ORDER BY tl.indication)
                   FILTER (WHERE tl.indication IS NOT NULL AND tl.indication != '') AS indications
            FROM "CT".tracking_list tl
            WHERE tl.nct_id = tsd.nct_id AND tl.dept = tsd.dept
        ) tl_agg ON true
        WHERE tsd.id = ANY(%s)
        ORDER BY tsd.id
    """
    with _cur() as cur:
        cur.execute(fetch_sql, (claimed_ids,))
        return [dict(r) for r in cur.fetchall()]


def _split_pipe(value: str | None) -> list[str]:
    """Split a pipe-separated string into a cleaned list."""
    if not value:
        return []
    return [v.strip() for v in str(value).split("|") if v.strip()]


def _build_es_doc(doc: dict, dense_vec: list[float]) -> dict:
    now      = datetime.now(timezone.utc).isoformat()
    chunk_id = f"{doc['nct_id']}_{doc['chunk_type']}_v2"

    # countries stored as pipe-separated string → array
    countries = _split_pipe(doc.get("countries"))

    # indications comes as a PostgreSQL array (already a list from psycopg2)
    indications = doc.get("indications") or []
    if isinstance(indications, str):
        indications = [i.strip() for i in indications.split(",") if i.strip()]

    return {
        # Identity
        "chunk_id":      chunk_id,
        "nct_id":        doc["nct_id"],
        "document_id":   doc["id"],
        "chunk_type":    doc["chunk_type"],
        "chunk_version": EMBEDDING_VER,
        # Semantic content
        "text":           doc["content_text"],
        # Dense vector
        "dense_embedding": dense_vec,
        # Scope (V2)
        "department":  doc.get("dept", ""),
        "indications": indications,
        # Clinical metadata
        "phase":              doc.get("phase")            or "",
        "study_status":       doc.get("study_status")     or "",
        "study_type":         doc.get("study_type")       or "",
        "primary_purpose":    doc.get("primary_purpose")  or "",
        "primary_drug":       doc.get("primary_drug")     or "",
        "sponsors":           doc.get("sponsors")         or "",
        "collaborators":      doc.get("collaborators")    or "",
        "funder_type":        doc.get("funder_type")      or "",
        "conditions":         doc.get("conditions")       or "",
        "countries":          countries,
        "enrollment":         doc.get("enrollment")       or "",
        "intervention_model": doc.get("intervention_model") or "",
        "allocation":         doc.get("allocation")       or "",
        "masking":            doc.get("masking")          or "",
        "fda_regulated_drug": doc.get("fda_regulated_drug") or "",
        "sex":                doc.get("sex")              or "",
        "minimum_age":        doc.get("minimum_age")      or "",
        "standard_ages":      doc.get("standard_ages")    or "",
        "healthy_volunteers": doc.get("healthy_volunteers") or "",
        # Dates
        "study_start_date":        doc.get("study_start_date")        or None,
        "primary_completion_date": doc.get("primary_completion_date") or None,
        "first_post_date":         doc.get("first_post_date")         or None,
        "last_update_post_date":   doc.get("last_update_post_date")   or None,
        # Version / lifecycle
        "content_hash":  doc.get("content_hash", ""),
        "is_latest":     True,
        "is_superseded": False,
        "searchable":    True,
        # Embedding metadata
        "embedding_model":       MODEL_NAME,
        "embedding_version":     EMBEDDING_VER,
        "embedding_dimension":   DENSE_DIM,
        "embedding_status":      "ACTIVE",
        "embedding_retry_count": 0,
        # Quality
        "text_length": len(doc.get("content_text", "")),
        # Timestamps
        "embedding_created_at": now,
        "indexed_at":           now,
    }


def _mark_active(doc_id: int) -> None:
    for attempt in range(3):
        try:
            with _cur() as cur:
                cur.execute(
                    """UPDATE "CT".trial_semantic_documents_v2
                       SET document_status = 'ACTIVE', updated_at = NOW()
                       WHERE id = %s""",
                    (doc_id,),
                )
            return
        except psycopg2.OperationalError:
            if attempt == 2:
                raise
            time.sleep(5)


def _mark_failed(doc_id: int) -> None:
    for attempt in range(3):
        try:
            with _cur() as cur:
                cur.execute(
                    """UPDATE "CT".trial_semantic_documents_v2
                       SET document_status = 'FAILED', updated_at = NOW()
                       WHERE id = %s""",
                    (doc_id,),
                )
            return
        except psycopg2.OperationalError:
            if attempt == 2:
                raise
            time.sleep(5)


def _reset_stuck_processing() -> int:
    """Reset PROCESSING docs that have been stuck >5 min (interrupted run, not active workers)."""
    with _cur() as cur:
        cur.execute(
            """UPDATE "CT".trial_semantic_documents_v2
               SET document_status = 'PENDING', updated_at = NOW()
               WHERE document_status = 'PROCESSING'
                 AND updated_at < NOW() - INTERVAL '5 minutes'"""
        )
        count = cur.rowcount
    if count:
        print(f"  Reset {count} stuck PROCESSING doc(s) → PENDING.")
    return count


def _reset_failed() -> int:
    """Reset FAILED docs back to PENDING so they can be retried (--retry-failed)."""
    with _cur() as cur:
        cur.execute(
            """UPDATE "CT".trial_semantic_documents_v2
               SET document_status = 'PENDING', updated_at = NOW()
               WHERE document_status = 'FAILED'"""
        )
        count = cur.rowcount
    print(f"  Reset {count} FAILED doc(s) → PENDING for retry.")
    return count


def embed_pending(docs: list[dict], batch_size: int = 16) -> int:
    """Encode a batch of PENDING docs with BGE-M3 and bulk-index into ES v2."""
    if not docs:
        return 0

    model  = _get_model()
    stored = 0
    total  = len(docs)

    for batch_start in range(0, total, batch_size):
        batch = docs[batch_start : batch_start + batch_size]
        texts = [d["content_text"] for d in batch]

        # Encode with BGE-M3
        t0 = time.perf_counter()
        try:
            output = model.encode(
                texts,
                return_dense=True,
                return_sparse=False,
                return_colbert_vecs=False,
                batch_size=batch_size,
                max_length=8192,
            )
            dense_vecs = output["dense_vecs"]
        except Exception as exc:
            print(f"\n  [ENCODE ERROR] batch {batch_start}: {exc}")
            for doc in batch:
                _mark_failed(doc["id"])
            continue
        encode_ms = (time.perf_counter() - t0) * 1000

        # Build ES bulk operations
        bulk_ops = []
        for doc, dense in zip(batch, dense_vecs):
            vec_list = dense.tolist() if hasattr(dense, "tolist") else list(dense)
            es_doc   = _build_es_doc(doc, vec_list)
            bulk_ops.append({"index": {"_index": ES_INDEX, "_id": es_doc["chunk_id"]}})
            bulk_ops.append(es_doc)

        # Bulk index with one retry on connection drop
        es = _fresh_es() if batch_start % (batch_size * 10) == 0 else _get_es()
        bulk_ok = False
        for attempt in range(2):
            try:
                resp = es.bulk(operations=bulk_ops)
                if resp.get("errors"):
                    for item in resp.get("items", []):
                        err = item.get("index", {}).get("error")
                        if err:
                            print(f"\n  [ES ERROR] {err}")
                bulk_ok = True
                break
            except Exception as exc:
                print(f"\n  [ES BULK ERROR attempt {attempt+1}] {exc}")
                if attempt == 0:
                    time.sleep(3)
                    es = _fresh_es()

        if not bulk_ok:
            for doc in batch:
                _mark_failed(doc["id"])
            continue

        for doc in batch:
            _mark_active(doc["id"])
            stored += 1

        pct = (batch_start + len(batch)) / total * 100
        print(
            f"  [{batch_start + len(batch):>5}/{total}] {pct:5.1f}%  "
            f"encode {encode_ms:5.0f}ms  stored {stored}",
            flush=True,
        )

    return stored


# ── Status reporting ───────────────────────────────────────────────────────────

def print_status() -> None:
    with _cur() as cur:
        cur.execute("""
            SELECT document_status, COUNT(*) AS n
            FROM "CT".trial_semantic_documents_v2
            GROUP BY document_status
            ORDER BY document_status
        """)
        rows = cur.fetchall()
    print("\n  trial_semantic_documents_v2 status:")
    for r in rows:
        print(f"    {r['document_status']:<15} {r['n']:>7}")

    try:
        es    = _get_es()
        count = es.count(index=ES_INDEX)["count"]
        print(f"\n  ES index {ES_INDEX!r}: {count} documents")
    except Exception as e:
        print(f"\n  ES count failed: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="V2 BGE-M3 embedding worker → clinical_trials_semantic_v2")
    p.add_argument("--populate-only",  action="store_true", help="Pass 1 only (chunk text → DB)")
    p.add_argument("--embed-only",     action="store_true", help="Pass 2 only (PENDING → ES)")
    p.add_argument("--retry-failed",   action="store_true", help="Reset FAILED docs to PENDING and re-embed")
    p.add_argument("--dept",           type=str, default=None, help="Filter to one department")
    p.add_argument("--chunk-type",     type=str, default=None, choices=CHUNK_TYPES)
    p.add_argument("--fetch-size",     type=int, default=200,  help="Docs fetched per loop iteration (default: 200)")
    p.add_argument("--batch-size",     type=int, default=16,   help="BGE-M3 encoding batch size (default: 16)")
    p.add_argument("--status",         action="store_true", help="Print status and exit")
    args = p.parse_args()

    SEP = "\n" + "=" * 60 + "\n"

    print(SEP + "embedding_worker_v2.py")
    print(f"  Source DB  : {os.getenv('DATABASE_URL', '(not set)')[:50]}...")
    print(f"  ES Index   : {ES_INDEX}")
    print(f"  Dept filter: {args.dept or 'ALL'}")
    print(f"  Chunk type : {args.chunk_type or 'ALL'}")

    ensure_staging_table()

    if args.status:
        print_status()
        return

    run_populate = not args.embed_only
    run_embed    = not args.populate_only

    # ── Pass 1 ────────────────────────────────────────────────────────────────
    if run_populate:
        print(SEP + "Pass 1 — Generating V2 chunk text → trial_semantic_documents_v2")
        t0 = time.perf_counter()
        trials, docs = populate_chunks(args.dept, args.chunk_type)
        elapsed = time.perf_counter() - t0
        print(f"\n  Pass 1 complete: {trials} trials, {docs} docs written ({elapsed:.0f}s)")

    # ── Pass 2 ────────────────────────────────────────────────────────────────
    if run_embed:
        print(SEP + "Pass 2 — Encoding with BGE-M3 → Elasticsearch v2")

        # Reset any docs stuck as PROCESSING from a prior interrupted run
        _reset_stuck_processing()

        # Optionally reset FAILED docs for retry
        if args.retry_failed:
            _reset_failed()

        t0           = time.perf_counter()
        total_stored = 0
        iteration    = 0

        # Fetch-encode loop: atomically claims fetch_size docs at a time.
        # Safe to interrupt and resume, and safe to run multiple workers in parallel.
        while True:
            pending = _claim_pending(args.dept, args.chunk_type, args.fetch_size)
            if not pending:
                break
            iteration += 1
            print(f"\n  [Iteration {iteration}] {len(pending)} PENDING docs "
                  f"(total stored so far: {total_stored})")
            stored       = embed_pending(pending, batch_size=args.batch_size)
            total_stored += stored
            print(f"  [Iteration {iteration}] done — {stored} stored, running total: {total_stored}")

        elapsed = time.perf_counter() - t0
        print(f"\n  Pass 2 complete: {total_stored} embeddings stored ({elapsed:.0f}s)")

    print_status()
    print(SEP + "Done.")


if __name__ == "__main__":
    main()
