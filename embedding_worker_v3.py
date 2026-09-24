"""
embedding_worker_v3.py -- V3 BGE-M3 embedding worker targeting Elasticsearch v3 index.

Source DB  : dricenta.com (production) via DATABASE_URL in .env
ES Index   : clinical_trials_semantic_v3
Model      : BAAI/bge-m3 (1024-dim dense, cosine)

V3 additions over V2:
  Clinical concept filter arrays (from organized_trials):
    lot, biomarkers, cns_status, performance_status, treatment_setting,
    disease_state, prior_therapies, genomic_alteration, metastatic_site,
    combination_therapy, age_range
  Phase 14 additional columns:
    gene, organ_function (from organ_function_requirement),
    biomarker_measurement, biomarker_value (from biomarker_value_or_cutoff),
    biomarker_test_method, comparator, regimen

  These are stored as keyword arrays in ES, enabling server-side pre-filtering
  in kNN queries so clinical concept constraints are enforced at ES level
  (not only at PostgreSQL level), improving retrieval precision.

  Staging table : trial_semantic_documents_v3 (created automatically)
  Chunk text    : identical to V2 (clinical metadata is ES metadata, not embedded)

Two-pass operation:
  Pass 1 (populate): Generate V3 chunk text from organized_trials + tracking_list.
                     Upsert into trial_semantic_documents_v3 as PENDING or METADATA_PENDING.
  Pass 2a (embed):   Fetch PENDING docs, encode with BGE-M3, index into ES v3,
                     mark docs ACTIVE.
  Pass 2b (meta):    Fetch METADATA_PENDING docs, re-index with fresh metadata but
                     reuse existing ES embedding (no re-encode), mark docs ACTIVE.

Usage:
    python embedding_worker_v3.py                     # full run (all depts)
    python embedding_worker_v3.py --populate-only     # Pass 1 only
    python embedding_worker_v3.py --embed-only        # Pass 2 only
    python embedding_worker_v3.py --dept ADC          # single dept
    python embedding_worker_v3.py --dept ADC --max 100
    python embedding_worker_v3.py --chunk-type TRIAL_SUMMARY
    python embedding_worker_v3.py --status            # show status and exit
    python embedding_worker_v3.py --retry-failed      # reset FAILED → PENDING and re-embed
    python embedding_worker_v3.py --retry-failed --max-retries 5
    python embedding_worker_v3.py --reindex-all       # reset ACTIVE → PENDING and full rebuild
    python embedding_worker_v3.py --reconcile         # check ES vs staging, fix gaps
    python embedding_worker_v3.py --lease-minutes 30  # override stuck-PROCESSING timeout

Requirements:
    pip install FlagEmbedding elasticsearch>=8.14 psycopg2-binary python-dotenv
"""
import argparse
import hashlib
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from chunk_generator_v3 import CHUNK_TYPES, content_hash, generate_chunks

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("embedding_worker_v3")

ES_INDEX      = "clinical_trials_semantic_v3"
MODEL_NAME    = "BAAI/bge-m3"
DENSE_DIM     = 1024
EMBEDDING_VER = 3

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
CREATE TABLE IF NOT EXISTS "CT".trial_semantic_documents_v3 (
    id               SERIAL PRIMARY KEY,
    nct_id           TEXT NOT NULL,
    dept             TEXT NOT NULL,
    chunk_type       TEXT NOT NULL,
    content_text     TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    document_status  TEXT NOT NULL DEFAULT 'PENDING',
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT trial_semantic_documents_v3_uq UNIQUE (nct_id, dept, chunk_type)
)
"""

# Steps 1: add all required columns that may not exist yet
_ADD_COLUMNS = [
    """ALTER TABLE "CT".trial_semantic_documents_v3
       ADD COLUMN IF NOT EXISTS metadata_hash TEXT""",
    """ALTER TABLE "CT".trial_semantic_documents_v3
       ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0""",
    """ALTER TABLE "CT".trial_semantic_documents_v3
       ADD COLUMN IF NOT EXISTS last_error TEXT""",
    """ALTER TABLE "CT".trial_semantic_documents_v3
       ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ""",
]


def ensure_staging_table() -> None:
    with _cur() as cur:
        cur.execute(_CREATE_TABLE)
        for stmt in _ADD_COLUMNS:
            cur.execute(stmt)
    log.info("Staging table: trial_semantic_documents_v3 ready.")


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
        log.info(f"Elasticsearch {version} at {url} → {ES_INDEX}")
    return _es_client


def _reconnect_es():
    """Reconnect ES client only when needed (on transport errors)."""
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
        log.info(f"Loading {MODEL_NAME} (first run downloads ~2.3 GB) ...")
        _model = BGEM3FlagModel(MODEL_NAME, use_fp16=True)
        log.info("Model ready.")
    return _model


# ── Pass 1: Populate trial_semantic_documents_v3 ───────────────────────────────

# V3: adds all clinical concept columns to the SELECT
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
        -- V3: clinical concept filter columns
        ot.line_of_therapy         AS lot,
        ot.biomarkers,
        ot.cns_status,
        ot.performance_status,
        ot.treatment_setting,
        ot.disease_state,
        ot.prior_therapies,
        ot.genomic_alteration,
        ot.metastatic_site,
        ot.combination_therapy,
        ot.age_range_normalized    AS age_range,
        ot.gene,
        ot.organ_function_requirement  AS organ_function,
        ot.biomarker_measurement,
        ot.biomarker_value_or_cutoff   AS biomarker_value,
        ot.biomarker_test_method,
        ot.comparator,
        ot.regimen,
        COALESCE(tl_agg.indications, ARRAY[]::text[]) AS indications
    FROM "CT".organized_trials ot
    LEFT JOIN LATERAL (
        SELECT ARRAY_AGG(DISTINCT tl.indication ORDER BY tl.indication)
               FILTER (WHERE tl.indication IS NOT NULL AND tl.indication != '') AS indications
        FROM "CT".tracking_list tl
        WHERE tl.nct_id = ot.nct_id AND tl.dept = ot.dept
    ) tl_agg ON true
"""

# Step 2: METADATA_PENDING logic in UPSERT.
# When content_hash is unchanged but metadata_hash changed → METADATA_PENDING.
# When content_hash changed → PENDING (re-embed required).
# When neither changed → skip (WHERE clause prevents update).
_UPSERT_DOC = """
    INSERT INTO "CT".trial_semantic_documents_v3
        (nct_id, dept, chunk_type, content_text, content_hash, metadata_hash, document_status)
    VALUES %s
    ON CONFLICT (nct_id, dept, chunk_type) DO UPDATE
    SET
        content_text    = EXCLUDED.content_text,
        content_hash    = EXCLUDED.content_hash,
        metadata_hash   = EXCLUDED.metadata_hash,
        document_status = CASE
            WHEN "CT".trial_semantic_documents_v3.content_hash != EXCLUDED.content_hash
            THEN 'PENDING'
            WHEN "CT".trial_semantic_documents_v3.metadata_hash IS DISTINCT FROM EXCLUDED.metadata_hash
                 AND "CT".trial_semantic_documents_v3.document_status = 'ACTIVE'
            THEN 'METADATA_PENDING'
            ELSE "CT".trial_semantic_documents_v3.document_status
        END,
        updated_at = NOW()
    WHERE "CT".trial_semantic_documents_v3.content_hash    != EXCLUDED.content_hash
       OR "CT".trial_semantic_documents_v3.metadata_hash  IS DISTINCT FROM EXCLUDED.metadata_hash
       OR "CT".trial_semantic_documents_v3.document_status = 'PENDING'
"""


# ── Metadata hash ──────────────────────────────────────────────────────────────

_METADATA_FIELDS = [
    "Overall Status", "phases", "studyType", "Primary Purpose", "Primary Drug",
    "Sponsors", "Collaborators", "responsiblePartyleadSponsor", "Funder Type",
    "Enrollment", "Allocation", "Intervention Model", "Masking", "FDA Regulated Drug",
    "Sex", "Minimum Age", "Standard Ages", "healthyVolunteers",
    "Start Date", "Primary Completion Date", "Study First Post Date", "Last Update Post Date",
    "countries",
    "line_of_therapy", "biomarkers", "cns_status", "performance_status",
    "treatment_setting", "disease_state", "prior_therapies", "genomic_alteration",
    "metastatic_site", "combination_therapy", "age_range_normalized",
    "gene", "organ_function_requirement", "biomarker_measurement",
    "biomarker_value_or_cutoff", "biomarker_test_method", "comparator", "regimen",
]


def metadata_hash(row: dict, indications: list | None = None) -> str:
    """
    SHA-256 of structured metadata + indications for a trial row.
    indications are sorted before hashing so their order is non-semantic.
    Works with both organize_trials column names and aliased fetch_sql names.
    """
    # Handle both raw column names (from Pass 1 SELECT) and aliased names (from fetch_sql)
    # For Pass 1 row dicts the column is "lot" (aliased); for _METADATA_FIELDS it's "line_of_therapy"
    # We build the canonical dict covering both naming conventions.

    # Resolve indications: prefer explicit arg, fall back to row key
    if indications is None:
        raw_ind = row.get("indications") or []
        if isinstance(raw_ind, str):
            raw_ind = [i.strip() for i in raw_ind.split(",") if i.strip()]
        indications = list(raw_ind)

    fields = {
        "indications": sorted(indications),
    }
    for k in _METADATA_FIELDS:
        v = row.get(k)
        fields[k] = str(v) if v is not None else ""

    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Pass 1: populate ──────────────────────────────────────────────────────────

def populate_chunks(dept_filter: str | None, chunk_type_filter: str | None) -> tuple[int, int]:
    """Pass 1: generate V3 chunk text and upsert into trial_semantic_documents_v3."""
    sql    = _SELECT_TRIALS
    params: list = []
    if dept_filter:
        sql += " WHERE ot.dept = %s"
        params.append(dept_filter)

    with _cur() as cur:
        cur.execute(sql, params or None)
        rows = cur.fetchall()

    total = len(rows)
    log.info(f"Pass 1: {total} trials to process")

    trials_done  = 0
    docs_written = 0
    _COMMIT_EVERY = 500  # larger batches = fewer round-trips

    for batch_start in range(0, total, _COMMIT_EVERY):
        batch = rows[batch_start : batch_start + _COMMIT_EVERY]
        batch_trials = 0
        batch_docs   = 0

        for attempt in range(1, 4):
            try:
                with _cur() as cur:
                    batch_rows = []
                    for row in batch:
                        row_dict = dict(row)
                        chunks   = generate_chunks(row_dict)
                        nct_id   = row["nct_id"]
                        dept     = row["dept"]
                        raw_ind  = row_dict.get("indications") or []
                        if isinstance(raw_ind, str):
                            raw_ind = [i.strip() for i in raw_ind.split(",") if i.strip()]
                        mhash = metadata_hash(row_dict, list(raw_ind))
                        batch_trials += 1
                        for chunk in chunks:
                            ct = chunk["chunk_type"]
                            if chunk_type_filter and ct != chunk_type_filter:
                                continue
                            text  = chunk["content_text"]
                            chash = content_hash(text)
                            # 7th element = document_status default 'PENDING'
                            batch_rows.append((nct_id, dept, ct, text, chash, mhash, "PENDING"))
                    if batch_rows:
                        psycopg2.extras.execute_values(
                            cur, _UPSERT_DOC, batch_rows, page_size=1000
                        )
                        batch_docs = cur.rowcount
                break
            except psycopg2.OperationalError as e:
                batch_trials = 0
                batch_docs   = 0
                log.warning(f"Connection error attempt {attempt}/3: {e}")
                if attempt == 3:
                    log.error("Batch failed after 3 attempts, skipping.")
                else:
                    time.sleep(5 * attempt)

        trials_done  += batch_trials
        docs_written += batch_docs
        log.info(f"[{trials_done}/{total}] Docs written: {docs_written}")

    return trials_done, docs_written


# ── Pass 2: Encode + index into Elasticsearch v3 ──────────────────────────────

def _claim_docs(
    status: str,
    dept_filter: str | None,
    chunk_type_filter: str | None,
    limit: int,
) -> list[dict]:
    """
    Atomically claim docs with given status as PROCESSING using FOR UPDATE SKIP LOCKED.
    Sets processing_started_at = NOW() for lease timeout tracking.
    Returns full doc rows with organized_trials metadata (including V3 clinical fields).
    """
    where_parts = [f"document_status = '{status}'"]
    claim_params: list = []
    if dept_filter:
        where_parts.append("dept = %s")
        claim_params.append(dept_filter)
    if chunk_type_filter:
        where_parts.append("chunk_type = %s")
        claim_params.append(chunk_type_filter)
    claim_params.append(limit)

    claim_sql = f"""
        UPDATE "CT".trial_semantic_documents_v3
        SET document_status        = 'PROCESSING',
            processing_started_at  = NOW(),
            updated_at             = NOW()
        WHERE id IN (
            SELECT id FROM "CT".trial_semantic_documents_v3
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

    fetch_sql = """
        SELECT
            tsd.id, tsd.nct_id, tsd.dept, tsd.chunk_type,
            tsd.content_text, tsd.content_hash, tsd.metadata_hash,
            tsd.retry_count,
            ot."Overall Status"      AS study_status,
            ot.phases                AS phase,
            ot."studyType"           AS study_type,
            ot."Primary Purpose"     AS primary_purpose,
            ot."Primary Drug"        AS primary_drug,
            ot."Sponsors"            AS sponsors,
            ot."Collaborators"       AS collaborators,
            ot."responsiblePartyleadSponsor" AS lead_sponsor,
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
            -- V3: clinical concept filter columns
            ot.line_of_therapy         AS lot,
            ot.biomarkers,
            ot.cns_status,
            ot.performance_status,
            ot.treatment_setting,
            ot.disease_state,
            ot.prior_therapies,
            ot.genomic_alteration,
            ot.metastatic_site,
            ot.combination_therapy,
            ot.age_range_normalized    AS age_range,
            ot.gene,
            ot.organ_function_requirement  AS organ_function,
            ot.biomarker_measurement,
            ot.biomarker_value_or_cutoff   AS biomarker_value,
            ot.biomarker_test_method,
            ot.comparator,
            ot.regimen,
            -- New metadata fields (Phase 3)
            ot.biomarker_status,
            ot.treatment_status,
            ot.endpoints_normalized,
            ot."Interventions"             AS interventions_raw,
            COALESCE(tl_agg.indications, ARRAY[]::text[]) AS indications
        FROM "CT".trial_semantic_documents_v3 tsd
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


# Backward-compat alias used by embed_pending
def _claim_pending(dept_filter, chunk_type_filter, limit):
    return _claim_docs("PENDING", dept_filter, chunk_type_filter, limit)


def _split_pipe(value: str | None) -> list[str]:
    """Split a pipe-separated string into a cleaned list."""
    if not value:
        return []
    return [v.strip() for v in str(value).split("|") if v.strip()]


def _split_clinical(value: str | None) -> list[str]:
    """
    Split a clinical concept pipe-separated field into a keyword list.
    DB stores values as 'A | B' (spaces) or 'A|B' (no spaces) — normalize both.
    """
    if not value:
        return []
    normalized = str(value).replace("|", " | ")
    return [v.strip() for v in normalized.split(" | ") if v.strip()]


def _safe_int(value) -> int | None:
    """Return value as int, or None if absent / non-numeric."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _build_es_doc(doc: dict, dense_vec: list[float]) -> dict:
    now = datetime.now(timezone.utc).isoformat()

    # chunk_id includes dept to prevent cross-dept overwrite
    chunk_id = f"{doc['nct_id']}_{doc['dept']}_{doc['chunk_type']}_v3"

    # countries stored as pipe-separated string → array
    countries = _split_pipe(doc.get("countries"))

    # indications comes as a PostgreSQL array (already a list from psycopg2)
    indications = doc.get("indications") or []
    if isinstance(indications, str):
        indications = [i.strip() for i in indications.split(",") if i.strip()]

    # sponsors: pipe-delimited full Sponsors list → keyword array (for ANY-mode filtering)
    # collaborators: pipe-delimited → keyword array
    sponsors_raw  = doc.get("sponsors") or ""
    sponsors_list = _split_pipe(sponsors_raw) if sponsors_raw.strip() else []
    # Keep single-element lead_sponsor list for backward compat ES field
    sponsors      = [doc.get("lead_sponsor", "").strip()] if doc.get("lead_sponsor", "").strip() else []
    collaborators = _split_pipe(doc.get("collaborators"))

    # conditions_list: comma-separated raw conditions → keyword array
    conditions_raw = doc.get("conditions") or ""
    conditions_list = [c.strip() for c in conditions_raw.split(",") if c.strip()] if conditions_raw else []

    # interventions_list: pipe-separated interventions → keyword array
    interventions_raw = doc.get("interventions_raw") or ""
    interventions_list = _split_pipe(interventions_raw) if interventions_raw.strip() else []

    # phases DB column stores a single keyword like 'PHASE1_PHASE2'.
    # Wrap in list so ES keyword array type is consistent.
    phase_raw = doc.get("phase") or ""
    phase     = [phase_raw.strip()] if phase_raw.strip() else []

    retry_count = doc.get("retry_count", 0) or 0

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
        # Scope
        "department":  doc.get("dept", ""),
        "indications": indications,
        # Core clinical metadata
        "phase":              phase,
        "study_status":       doc.get("study_status")     or "",
        "study_type":         doc.get("study_type")       or "",
        "primary_purpose":    doc.get("primary_purpose")  or "",
        "primary_drug":       doc.get("primary_drug")     or "",
        "sponsors":           sponsors,
        "sponsors_list":      sponsors_list,
        "collaborators":      collaborators,
        "lead_sponsor":       doc.get("lead_sponsor")     or "",
        "funder_type":        doc.get("funder_type")      or "",
        "conditions":         doc.get("conditions")       or "",
        "conditions_list":    conditions_list,
        "countries":          countries,
        "enrollment":         _safe_int(doc.get("enrollment")),
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
        # V3 NEW: clinical concept filter keyword arrays
        "lot":                _split_clinical(doc.get("lot")),
        "biomarkers":         _split_clinical(doc.get("biomarkers")),
        "cns_status":         _split_clinical(doc.get("cns_status")),
        "performance_status": _split_clinical(doc.get("performance_status")),
        "treatment_setting":  _split_clinical(doc.get("treatment_setting")),
        "disease_state":      _split_clinical(doc.get("disease_state")),
        "prior_therapies":    _split_clinical(doc.get("prior_therapies")),
        "genomic_alteration": _split_clinical(doc.get("genomic_alteration")),
        "metastatic_site":    _split_clinical(doc.get("metastatic_site")),
        "combination_therapy":_split_clinical(doc.get("combination_therapy")),
        "age_range":          _split_clinical(doc.get("age_range")),
        # Phase 14 clinical columns
        "gene":                  _split_clinical(doc.get("gene")),
        "organ_function":        _split_clinical(doc.get("organ_function")),
        "biomarker_measurement": _split_clinical(doc.get("biomarker_measurement")),
        "biomarker_value":       _split_clinical(doc.get("biomarker_value")),
        "biomarker_test_method": _split_clinical(doc.get("biomarker_test_method")),
        "comparator":            _split_clinical(doc.get("comparator")),
        "regimen":               _split_clinical(doc.get("regimen")),
        # Phase 3: new metadata fields
        "biomarker_status":      _split_clinical(doc.get("biomarker_status")),
        "treatment_status":      _split_clinical(doc.get("treatment_status")),
        "endpoints_normalized":  _split_clinical(doc.get("endpoints_normalized")),
        "interventions_list":    interventions_list,
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
        "embedding_retry_count": retry_count,
        # Quality
        "text_length": len(doc.get("content_text", "")),
        # Timestamps
        "embedding_created_at": now,
        "indexed_at":           now,
    }


# ── Status update helpers ─────────────────────────────────────────────────────

def _mark_active(doc_ids: list[int]) -> None:
    """Mark only successfully indexed docs ACTIVE."""
    if not doc_ids:
        return
    for attempt in range(3):
        try:
            with _cur() as cur:
                cur.execute(
                    """UPDATE "CT".trial_semantic_documents_v3
                       SET document_status        = 'ACTIVE',
                           processing_started_at  = NULL,
                           updated_at             = NOW()
                       WHERE id = ANY(%s)""",
                    (doc_ids,),
                )
            return
        except psycopg2.OperationalError:
            if attempt == 2:
                raise
            time.sleep(5)


def _mark_failed(doc_ids: list[int], error_msg: str = "") -> None:
    """Mark individually failed docs FAILED, increment retry_count, store last_error."""
    if not doc_ids:
        return
    # Truncate error message to avoid oversized text values
    error_text = (error_msg or "")[:2000]
    for attempt in range(3):
        try:
            with _cur() as cur:
                cur.execute(
                    """UPDATE "CT".trial_semantic_documents_v3
                       SET document_status        = 'FAILED',
                           retry_count            = retry_count + 1,
                           last_error             = %s,
                           processing_started_at  = NULL,
                           updated_at             = NOW()
                       WHERE id = ANY(%s)""",
                    (error_text, doc_ids),
                )
            return
        except psycopg2.OperationalError:
            if attempt == 2:
                raise
            time.sleep(5)


def _reset_stuck_processing(lease_minutes: int = 60) -> int:
    """Reset PROCESSING docs stuck beyond the lease timeout back to PENDING."""
    with _cur() as cur:
        cur.execute(
            """UPDATE "CT".trial_semantic_documents_v3
               SET document_status        = 'PENDING',
                   processing_started_at  = NULL,
                   updated_at             = NOW()
               WHERE document_status = 'PROCESSING'
                 AND processing_started_at < NOW() - (INTERVAL '1 minute' * %s)""",
            (lease_minutes,),
        )
        count = cur.rowcount
    if count:
        log.info(f"Reset {count} stuck PROCESSING doc(s) → PENDING (lease={lease_minutes}min).")
    return count


def _reset_failed(max_retries: int = 3) -> int:
    """Reset FAILED docs (below max_retries) to PENDING for retry."""
    with _cur() as cur:
        cur.execute(
            """UPDATE "CT".trial_semantic_documents_v3
               SET document_status = 'PENDING', updated_at = NOW()
               WHERE document_status = 'FAILED'
                 AND retry_count < %s""",
            (max_retries,),
        )
        count = cur.rowcount
    log.info(f"Reset {count} FAILED doc(s) → PENDING for retry (max_retries={max_retries}).")
    return count


# ── Pass 2a: embed PENDING docs ───────────────────────────────────────────────

def embed_pending(docs: list[dict], batch_size: int = 16) -> int:
    """Encode PENDING docs with BGE-M3 and bulk-index into ES v3."""
    if not docs:
        return 0

    model  = _get_model()
    stored = 0
    total  = len(docs)

    for batch_start in range(0, total, batch_size):
        batch = docs[batch_start : batch_start + batch_size]
        texts = [d["content_text"] for d in batch]

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
            err_msg = f"ENCODE ERROR batch {batch_start}: {exc}"
            log.error(err_msg)
            ids = [doc["id"] for doc in batch]
            for doc in batch:
                log.error(
                    f"  encode_fail nct_id={doc['nct_id']} dept={doc['dept']} "
                    f"chunk_type={doc['chunk_type']} staging_id={doc['id']} "
                    f"retry_count={doc.get('retry_count', 0)} ts={datetime.now(timezone.utc).isoformat()}"
                )
            _mark_failed(ids, error_msg=str(exc))
            continue
        encode_ms = (time.perf_counter() - t0) * 1000

        # Build bulk operations; keep parallel list of doc_ids for per-item tracking
        bulk_ops     = []
        bulk_doc_ids = []
        for doc, dense in zip(batch, dense_vecs):
            vec_list = dense.tolist() if hasattr(dense, "tolist") else list(dense)
            es_doc   = _build_es_doc(doc, vec_list)
            bulk_ops.append({"index": {"_index": ES_INDEX, "_id": es_doc["chunk_id"]}})
            bulk_ops.append(es_doc)
            bulk_doc_ids.append(doc["id"])

        es      = _get_es()
        bulk_ok = False
        for attempt in range(2):
            try:
                resp    = es.bulk(operations=bulk_ops)
                bulk_ok = True
                break
            except Exception as exc:
                log.error(f"ES BULK ERROR attempt {attempt+1}: {exc}")
                if attempt == 0:
                    time.sleep(3)
                    try:
                        es = _reconnect_es()
                    except Exception as reconnect_exc:
                        log.error(f"ES reconnect failed: {reconnect_exc}")

        if not bulk_ok:
            err_msg = "ES bulk request failed after 2 attempts"
            ids = [doc["id"] for doc in batch]
            for doc in batch:
                log.error(
                    f"  bulk_fail nct_id={doc['nct_id']} dept={doc['dept']} "
                    f"chunk_type={doc['chunk_type']} staging_id={doc['id']} "
                    f"operation=bulk_index retry_count={doc.get('retry_count', 0)} "
                    f"ts={datetime.now(timezone.utc).isoformat()}"
                )
            _mark_failed(ids, error_msg=err_msg)
            continue

        # Per-item success/failure tracking — ES bulk can partially succeed.
        # Step 3: validate item count matches batch size
        items = resp.get("items", [])
        if len(items) != len(batch):
            err_msg = (
                f"ES bulk response items count mismatch: "
                f"expected {len(batch)}, got {len(items)}"
            )
            log.error(err_msg)
            _mark_failed([doc["id"] for doc in batch], error_msg=err_msg)
            continue

        successful_ids: list[int] = []
        failed_ids:     list[int] = []
        failed_errors:  list[str] = []

        for doc, doc_id, item in zip(batch, bulk_doc_ids, items):
            err = item.get("index", {}).get("error")
            if err:
                err_str = str(err)
                log.error(
                    f"  es_item_fail nct_id={doc['nct_id']} dept={doc['dept']} "
                    f"chunk_type={doc['chunk_type']} staging_id={doc_id} "
                    f"operation=es_index error={err_str!r} "
                    f"retry_count={doc.get('retry_count', 0)} "
                    f"ts={datetime.now(timezone.utc).isoformat()}"
                )
                failed_ids.append(doc_id)
                failed_errors.append(err_str)
            else:
                successful_ids.append(doc_id)

        _mark_active(successful_ids)
        if failed_ids:
            # Use the first error message as representative for all failed docs in batch
            _mark_failed(failed_ids, error_msg=failed_errors[0] if failed_errors else "ES index error")
        stored += len(successful_ids)

        pct = (batch_start + len(batch)) / total * 100
        log.info(
            f"[{batch_start + len(batch):>5}/{total}] {pct:5.1f}%  "
            f"encode {encode_ms:5.0f}ms  stored {stored}"
            + (f"  FAILED {len(failed_ids)}" if failed_ids else "")
        )

    return stored


# ── Pass 2b: METADATA_PENDING updates (no re-encode) ─────────────────────────

def process_metadata_pending(docs: list[dict], batch_size: int = 50) -> int:
    """
    Pass 2b: Re-index METADATA_PENDING docs with fresh metadata but reuse the
    existing dense_embedding from ES (no BGE-M3 re-encode needed).
    """
    if not docs:
        return 0

    es      = _get_es()
    stored  = 0
    total   = len(docs)

    for batch_start in range(0, total, batch_size):
        batch = docs[batch_start : batch_start + batch_size]

        # Build chunk_ids to fetch existing embeddings from ES
        chunk_ids = [
            f"{doc['nct_id']}_{doc['dept']}_{doc['chunk_type']}_v3"
            for doc in batch
        ]

        # Fetch existing ES documents to extract current dense_embedding
        try:
            mget_resp = es.mget(
                index=ES_INDEX,
                body={"ids": chunk_ids},
                _source_includes=["dense_embedding"],
            )
        except Exception as exc:
            log.error(f"ES mget failed for METADATA_PENDING batch: {exc}")
            try:
                es = _reconnect_es()
            except Exception:
                pass
            err_msg = f"ES mget failed: {exc}"
            for doc in batch:
                log.error(
                    f"  meta_mget_fail nct_id={doc['nct_id']} dept={doc['dept']} "
                    f"chunk_type={doc['chunk_type']} staging_id={doc['id']} "
                    f"operation=mget error={str(exc)!r} "
                    f"ts={datetime.now(timezone.utc).isoformat()}"
                )
            _mark_failed([doc["id"] for doc in batch], error_msg=err_msg)
            continue

        # Build a map of chunk_id → existing dense_embedding
        embedding_map: dict[str, list[float]] = {}
        mget_docs = mget_resp.get("docs", [])
        for item in mget_docs:
            if item.get("found"):
                cid = item["_id"]
                vec = item.get("_source", {}).get("dense_embedding")
                if vec:
                    embedding_map[cid] = vec

        # Build bulk ops — skip docs missing from ES (reset to PENDING instead)
        bulk_ops         = []
        bulk_doc_ids     = []
        missing_ids      = []

        for doc, chunk_id in zip(batch, chunk_ids):
            existing_vec = embedding_map.get(chunk_id)
            if existing_vec is None:
                log.warning(
                    f"  meta_no_vec chunk_id={chunk_id} staging_id={doc['id']} "
                    f"→ resetting to PENDING for full re-embed"
                )
                missing_ids.append(doc["id"])
                continue

            es_doc = _build_es_doc(doc, existing_vec)
            bulk_ops.append({"index": {"_index": ES_INDEX, "_id": chunk_id}})
            bulk_ops.append(es_doc)
            bulk_doc_ids.append(doc["id"])

        # Reset docs missing from ES to PENDING so they get a full embed pass
        if missing_ids:
            with _cur() as cur:
                cur.execute(
                    """UPDATE "CT".trial_semantic_documents_v3
                       SET document_status       = 'PENDING',
                           processing_started_at = NULL,
                           updated_at            = NOW()
                       WHERE id = ANY(%s)""",
                    (missing_ids,),
                )

        if not bulk_ops:
            continue

        # Execute bulk re-index
        bulk_ok = False
        for attempt in range(2):
            try:
                resp    = es.bulk(operations=bulk_ops)
                bulk_ok = True
                break
            except Exception as exc:
                log.error(f"ES METADATA BULK ERROR attempt {attempt+1}: {exc}")
                if attempt == 0:
                    time.sleep(3)
                    try:
                        es = _reconnect_es()
                    except Exception:
                        pass

        if not bulk_ok:
            err_msg = "ES metadata bulk request failed after 2 attempts"
            for doc in batch:
                if doc["id"] in bulk_doc_ids:
                    log.error(
                        f"  meta_bulk_fail nct_id={doc['nct_id']} dept={doc['dept']} "
                        f"chunk_type={doc['chunk_type']} staging_id={doc['id']} "
                        f"operation=meta_bulk_index "
                        f"ts={datetime.now(timezone.utc).isoformat()}"
                    )
            _mark_failed(bulk_doc_ids, error_msg=err_msg)
            continue

        # Per-item tracking
        items = resp.get("items", [])
        if len(items) != len(bulk_doc_ids):
            err_msg = (
                f"ES metadata bulk response items mismatch: "
                f"expected {len(bulk_doc_ids)}, got {len(items)}"
            )
            log.error(err_msg)
            _mark_failed(bulk_doc_ids, error_msg=err_msg)
            continue

        successful_ids: list[int] = []
        failed_ids:     list[int] = []

        for doc_id, item in zip(bulk_doc_ids, items):
            err = item.get("index", {}).get("error")
            if err:
                log.error(f"  meta_item_fail staging_id={doc_id} error={err!r}")
                failed_ids.append(doc_id)
            else:
                successful_ids.append(doc_id)

        _mark_active(successful_ids)
        if failed_ids:
            _mark_failed(failed_ids, error_msg="ES metadata index item error")
        stored += len(successful_ids)

        pct = (batch_start + len(batch)) / total * 100
        log.info(
            f"  [META {batch_start + len(batch):>5}/{total}] {pct:5.1f}%  "
            f"stored {stored}"
            + (f"  FAILED {len(failed_ids)}" if failed_ids else "")
            + (f"  RESET_TO_PENDING {len(missing_ids)}" if missing_ids else "")
        )

    return stored


# ── --reindex-all ─────────────────────────────────────────────────────────────

def reindex_all(dept_filter: str | None, chunk_type_filter: str | None) -> None:
    """
    Step 5: Reset ALL ACTIVE staging docs to PENDING (preserve content_text/content_hash)
    then run populate + embed for a full rebuild without dropping the ES index.
    """
    es = _get_es()
    if not es.indices.exists(index=ES_INDEX).body:
        log.error(f"ES index {ES_INDEX!r} does not exist. Run __phase_g02_es_index_v3.py first.")
        sys.exit(1)

    log.info("--reindex-all: resetting ACTIVE docs to PENDING ...")
    where_parts = ["document_status = 'ACTIVE'"]
    params: list = []
    if dept_filter:
        where_parts.append("dept = %s")
        params.append(dept_filter)
    if chunk_type_filter:
        where_parts.append("chunk_type = %s")
        params.append(chunk_type_filter)

    with _cur() as cur:
        cur.execute(
            f"""UPDATE "CT".trial_semantic_documents_v3
               SET document_status = 'PENDING', updated_at = NOW()
               WHERE {" AND ".join(where_parts)}""",
            params or None,
        )
        count = cur.rowcount
    log.info(f"  Reset {count} ACTIVE docs → PENDING.")


# ── --reconcile ───────────────────────────────────────────────────────────────

def reconcile(dept_filter: str | None, chunk_type_filter: str | None) -> None:
    """
    Step 8: For each ACTIVE staging row, verify the ES document exists.
    Missing → reset to PENDING.
    Also mark searchable=false for trials that are no longer in organized_trials.
    """
    log.info("--reconcile: checking ACTIVE staging rows against ES ...")

    where_parts = ["document_status = 'ACTIVE'"]
    params: list = []
    if dept_filter:
        where_parts.append("dept = %s")
        params.append(dept_filter)
    if chunk_type_filter:
        where_parts.append("chunk_type = %s")
        params.append(chunk_type_filter)

    with _cur() as cur:
        cur.execute(
            f"""SELECT id, nct_id, dept, chunk_type
                FROM "CT".trial_semantic_documents_v3
                WHERE {" AND ".join(where_parts)}
                ORDER BY id""",
            params or None,
        )
        active_rows = [dict(r) for r in cur.fetchall()]

    log.info(f"  {len(active_rows)} ACTIVE staging rows to check.")
    if not active_rows:
        return

    es = _get_es()
    MGET_BATCH = 200
    missing_staging_ids: list[int] = []

    for batch_start in range(0, len(active_rows), MGET_BATCH):
        batch = active_rows[batch_start : batch_start + MGET_BATCH]
        chunk_ids = [
            f"{r['nct_id']}_{r['dept']}_{r['chunk_type']}_v3"
            for r in batch
        ]
        try:
            resp = es.mget(
                index=ES_INDEX,
                body={"ids": chunk_ids},
                _source_includes=[],
            )
        except Exception as exc:
            log.error(f"ES mget failed during reconcile batch: {exc}")
            continue

        for row, item in zip(batch, resp.get("docs", [])):
            if not item.get("found"):
                log.warning(
                    f"  reconcile_missing chunk_id={item.get('_id')} "
                    f"staging_id={row['id']} → resetting to PENDING"
                )
                missing_staging_ids.append(row["id"])

    if missing_staging_ids:
        with _cur() as cur:
            cur.execute(
                """UPDATE "CT".trial_semantic_documents_v3
                   SET document_status = 'PENDING', updated_at = NOW()
                   WHERE id = ANY(%s)""",
                (missing_staging_ids,),
            )
        log.info(f"  Reset {len(missing_staging_ids)} missing ES docs → PENDING.")
    else:
        log.info("  All ACTIVE staging rows have corresponding ES documents.")

    # Mark searchable=false for ES docs whose nct_id+dept no longer exists in organized_trials
    log.info("  Checking for orphaned ES docs (no longer in organized_trials) ...")
    try:
        # Scroll through all ES chunk_ids and check against DB
        # Use search with _source: ["nct_id", "department"] and track missing nct_ids
        search_resp = es.search(
            index=ES_INDEX,
            body={
                "_source": ["nct_id", "department"],
                "query": {"match_all": {}},
                "size": 1000,
            },
            scroll="2m",
        )
        scroll_id = search_resp.get("_scroll_id")
        hits = search_resp["hits"]["hits"]
        orphan_chunk_ids: list[str] = []

        with _cur() as cur:
            while hits:
                nct_dept_pairs = list({(h["_source"]["nct_id"], h["_source"]["department"]) for h in hits})
                if nct_dept_pairs:
                    nct_ids = [p[0] for p in nct_dept_pairs]
                    depts   = [p[1] for p in nct_dept_pairs]
                    cur.execute(
                        """SELECT nct_id, dept
                           FROM "CT".organized_trials
                           WHERE (nct_id, dept) = ANY(
                               SELECT UNNEST(%s::text[]), UNNEST(%s::text[])
                           )""",
                        (nct_ids, depts),
                    )
                    found_pairs = {(r["nct_id"], r["dept"]) for r in cur.fetchall()}
                    for h in hits:
                        pair = (h["_source"]["nct_id"], h["_source"]["department"])
                        if pair not in found_pairs:
                            orphan_chunk_ids.append(h["_id"])

                scroll_resp = es.scroll(scroll_id=scroll_id, scroll="2m")
                scroll_id   = scroll_resp.get("_scroll_id")
                hits        = scroll_resp["hits"]["hits"]

        if scroll_id:
            try:
                es.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass

        if orphan_chunk_ids:
            log.info(f"  {len(orphan_chunk_ids)} orphaned ES docs → marking searchable=false")
            bulk_ops = []
            for cid in orphan_chunk_ids:
                bulk_ops.append({"update": {"_index": ES_INDEX, "_id": cid}})
                bulk_ops.append({"doc": {"searchable": False}})
            if bulk_ops:
                es.bulk(operations=bulk_ops)
        else:
            log.info("  No orphaned ES docs found.")

    except Exception as exc:
        log.error(f"  Orphan check failed: {exc}")

    log.info(f"Reconcile complete. Missing: {len(missing_staging_ids)}, Orphaned: {len(orphan_chunk_ids if 'orphan_chunk_ids' in dir() else [])}.")


# ── Status reporting ───────────────────────────────────────────────────────────

def print_status(lease_minutes: int = 60, max_retries: int = 3) -> None:
    """Step 9: Enhanced status report."""
    counts: dict[str, int] = {}
    stuck_count   = 0
    maxretry_count = 0

    with _cur() as cur:
        cur.execute("""
            SELECT document_status, COUNT(*) AS n
            FROM "CT".trial_semantic_documents_v3
            GROUP BY document_status
            ORDER BY document_status
        """)
        for r in cur.fetchall():
            counts[r["document_status"]] = r["n"]

        cur.execute(
            """SELECT COUNT(*) AS n
               FROM "CT".trial_semantic_documents_v3
               WHERE document_status = 'PROCESSING'
                 AND processing_started_at < NOW() - (INTERVAL '1 minute' * %s)""",
            (lease_minutes,),
        )
        stuck_count = cur.fetchone()["n"]

        cur.execute(
            """SELECT COUNT(*) AS n
               FROM "CT".trial_semantic_documents_v3
               WHERE document_status = 'FAILED'
                 AND retry_count >= %s""",
            (max_retries,),
        )
        maxretry_count = cur.fetchone()["n"]

    total = sum(counts.values())

    print(f"\ntrial_semantic_documents_v3:")
    for status in ["PENDING", "PROCESSING", "METADATA_PENDING", "ACTIVE", "FAILED"]:
        n = counts.get(status, 0)
        print(f"  {status:<20} {n:>7}")
    # Print any unexpected statuses
    for status, n in sorted(counts.items()):
        if status not in {"PENDING", "PROCESSING", "METADATA_PENDING", "ACTIVE", "FAILED"}:
            print(f"  {status:<20} {n:>7}  (unexpected)")
    print(f"  {'Total staging:':<20} {total:>7}")
    print(f"\n  Stuck PROCESSING (>{lease_minutes}min): {stuck_count}")
    print(f"  FAILED with retry_count >= {max_retries}:    {maxretry_count}")

    try:
        es    = _get_es()
        count = es.count(index=ES_INDEX)["count"]
        approx_trials = count // 9 if count else 0
        print(f"\nES index: {ES_INDEX}")
        print(f"  ES documents:     {count}")
        print(f"  (approx {approx_trials} trials indexed)")
    except Exception as e:
        print(f"\n  ES count failed: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="V3 BGE-M3 embedding worker → clinical_trials_semantic_v3")
    p.add_argument("--populate-only",  action="store_true", help="Pass 1 only (chunk text → DB)")
    p.add_argument("--embed-only",     action="store_true", help="Pass 2 only (PENDING → ES)")
    p.add_argument("--retry-failed",   action="store_true", help="Reset FAILED docs to PENDING and re-embed")
    p.add_argument("--reindex-all",    action="store_true", help="Reset ACTIVE → PENDING and full rebuild (no ES index drop)")
    p.add_argument("--reconcile",      action="store_true", help="Check ACTIVE staging vs ES, fix gaps, mark orphans searchable=false")
    p.add_argument("--dept",           type=str, default=None, help="Filter to one department")
    p.add_argument("--chunk-type",     type=str, default=None, choices=CHUNK_TYPES)
    p.add_argument("--fetch-size",     type=int, default=200,  help="Docs fetched per loop iteration (default: 200)")
    p.add_argument("--batch-size",     type=int, default=16,   help="BGE-M3 encoding batch size (default: 16)")
    p.add_argument("--max-retries",    type=int, default=3,    help="Max retry_count before --retry-failed skips a doc (default: 3)")
    p.add_argument("--lease-minutes",  type=int, default=60,   help="Minutes before stuck PROCESSING docs are reset (default: 60)")
    p.add_argument("--status",         action="store_true", help="Print status and exit")
    args = p.parse_args()

    SEP = "\n" + "=" * 60 + "\n"

    print(SEP + "embedding_worker_v3.py")
    print(f"  Source DB  : {os.getenv('DATABASE_URL', '(not set)')[:50]}...")
    print(f"  ES Index   : {ES_INDEX}")
    print(f"  Dept filter: {args.dept or 'ALL'}")
    print(f"  Chunk type : {args.chunk_type or 'ALL'}")
    print(f"  Lease mins : {args.lease_minutes}")
    print(f"  Max retries: {args.max_retries}")

    ensure_staging_table()

    if args.status:
        print_status(lease_minutes=args.lease_minutes, max_retries=args.max_retries)
        return

    # ── --reconcile ───────────────────────────────────────────────────────────
    if args.reconcile:
        print(SEP + "Reconcile — checking ACTIVE staging vs ES index")
        reconcile(args.dept, args.chunk_type)
        print_status(lease_minutes=args.lease_minutes, max_retries=args.max_retries)
        print(SEP + "Done.")
        return

    # ── --reindex-all ─────────────────────────────────────────────────────────
    if args.reindex_all:
        print(SEP + "--reindex-all: full rebuild without dropping ES index")
        reindex_all(args.dept, args.chunk_type)
        # Fall through to populate + embed below (run_populate + run_embed both True)

    run_populate = not args.embed_only
    run_embed    = not args.populate_only

    # ── Pass 1 ────────────────────────────────────────────────────────────────
    if run_populate:
        print(SEP + "Pass 1 — Generating V3 chunk text → trial_semantic_documents_v3")
        t0 = time.perf_counter()
        trials, docs = populate_chunks(args.dept, args.chunk_type)
        elapsed = time.perf_counter() - t0
        log.info(f"Pass 1 complete: {trials} trials, {docs} docs written ({elapsed:.0f}s)")

    # ── Pass 2 ────────────────────────────────────────────────────────────────
    if run_embed:
        print(SEP + "Pass 2a — Encoding with BGE-M3 → Elasticsearch v3")

        _reset_stuck_processing(lease_minutes=args.lease_minutes)

        if args.retry_failed:
            _reset_failed(max_retries=args.max_retries)

        t0           = time.perf_counter()
        total_stored = 0
        iteration    = 0

        while True:
            pending = _claim_pending(args.dept, args.chunk_type, args.fetch_size)
            if not pending:
                break
            iteration += 1
            log.info(f"[Iteration {iteration}] {len(pending)} PENDING docs "
                     f"(total stored so far: {total_stored})")
            stored       = embed_pending(pending, batch_size=args.batch_size)
            total_stored += stored
            log.info(f"[Iteration {iteration}] done — {stored} stored, running total: {total_stored}")

        elapsed = time.perf_counter() - t0
        log.info(f"Pass 2a complete: {total_stored} embeddings stored ({elapsed:.0f}s)")

        # ── Pass 2b: METADATA_PENDING ─────────────────────────────────────────
        print(SEP + "Pass 2b — Metadata-only updates → Elasticsearch v3")
        t0               = time.perf_counter()
        total_meta       = 0
        meta_iteration   = 0

        while True:
            meta_docs = _claim_docs("METADATA_PENDING", args.dept, args.chunk_type, args.fetch_size)
            if not meta_docs:
                break
            meta_iteration += 1
            log.info(f"[Meta Iteration {meta_iteration}] {len(meta_docs)} METADATA_PENDING docs")
            stored       = process_metadata_pending(meta_docs, batch_size=min(args.fetch_size, 200))
            total_meta  += stored
            log.info(f"[Meta Iteration {meta_iteration}] done — {stored} updated, running total: {total_meta}")

        elapsed = time.perf_counter() - t0
        log.info(f"Pass 2b complete: {total_meta} metadata-only updates ({elapsed:.0f}s)")

    print_status(lease_minutes=args.lease_minutes, max_retries=args.max_retries)
    print(SEP + "Done.")


if __name__ == "__main__":
    main()
