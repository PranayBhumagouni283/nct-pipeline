"""
embedding_worker.py — Populate trial chunks + generate pgvector embeddings.

Two-pass operation (both run by default):
  Pass 1 (populate): Reads organized_trials, generates chunk text via chunk_generator,
                     upserts into trial_semantic_documents. Content-hash gate: only
                     marks document_status='PENDING' when content actually changed.
  Pass 2 (embed):    Fetches PENDING rows from trial_semantic_documents, encodes with
                     all-MiniLM-L6-v2 (384-dim), stores vectors in trial_embeddings,
                     marks documents ACTIVE.

Usage:
    python embedding_worker.py                          # full run (populate + embed)
    python embedding_worker.py --populate-only          # only populate chunks
    python embedding_worker.py --embed-only             # only embed PENDING docs
    python embedding_worker.py --dept ADC               # restrict to one dept
    python embedding_worker.py --max 1000               # cap docs per run
    python embedding_worker.py --chunk-type ELIGIBILITY # one chunk type only

Requirements:
    pip install sentence-transformers
"""
import argparse
import os
import sys
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from chunk_generator import CHUNK_TYPES, content_hash, generate_chunks

load_dotenv()

_MODEL_NAME = "all-MiniLM-L6-v2"
_EMBED_DIM  = 384

# ── DB connection ──────────────────────────────────────────────────────────────

_conn = None


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        dsn = os.getenv("DATABASE_URL") or os.getenv("DB_URL")
        if not dsn:
            sys.exit("DATABASE_URL not set in .env")
        _conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    return _conn


@contextmanager
def _cur():
    conn = _get_conn()
    with conn.cursor() as cur:
        yield cur
    conn.commit()


# ── Pass 1: Populate trial_semantic_documents ──────────────────────────────────

_SELECT_TRIALS = """
    SELECT
        nct_id, dept,
        "Brief Title", "Official Title", "Overall Status",
        "briefSummary", "detailedDescription",
        "conditions", "phases",
        "Interventions", "Primary Outcomes", "Secondary Outcomes",
        "eligibilityCriteria",
        "MeSH Conditions", "MeSH Interventions",
        "Sponsors", "Collaborators",
        "Enrollment", "studyType",
        "Primary Drug", "Allocation", "Intervention Model", "Primary Purpose"
    FROM "CT".organized_trials
"""

# Upsert: insert new chunk row; if same (nct_id, dept, chunk_type) already exists,
# update content + hash only when content changed, and reset status to PENDING.
_UPSERT_DOC = """
    INSERT INTO "CT".trial_semantic_documents
        (nct_id, dept, chunk_type, content_text, content_hash, document_status)
    VALUES (%s, %s, %s, %s, %s, 'PENDING')
    ON CONFLICT (nct_id, dept, chunk_type) DO UPDATE
    SET
        content_text    = EXCLUDED.content_text,
        content_hash    = EXCLUDED.content_hash,
        document_status = CASE
            WHEN "CT".trial_semantic_documents.content_hash = EXCLUDED.content_hash
            THEN "CT".trial_semantic_documents.document_status
            ELSE 'PENDING'
        END,
        updated_at = NOW()
    WHERE "CT".trial_semantic_documents.content_hash != EXCLUDED.content_hash
       OR "CT".trial_semantic_documents.document_status = 'PENDING'
"""


def populate_chunks(dept_filter: str | None, chunk_type_filter: str | None) -> tuple[int, int]:
    """
    Upsert chunk text for all trials into trial_semantic_documents.
    Returns (trials_processed, new_or_updated_docs).
    """
    sql = _SELECT_TRIALS
    params: list = []
    if dept_filter:
        sql += " WHERE dept = %s"
        params.append(dept_filter)

    with _cur() as cur:
        cur.execute(sql, params or None)
        rows = cur.fetchall()

    trials_processed = 0
    docs_written = 0

    with _cur() as cur:
        for row in rows:
            chunks = generate_chunks(dict(row))
            nct_id = row["nct_id"]
            dept   = row["dept"]
            trials_processed += 1
            for chunk in chunks:
                ct = chunk["chunk_type"]
                if chunk_type_filter and ct != chunk_type_filter:
                    continue
                text  = chunk["content_text"]
                chash = content_hash(text)
                cur.execute(_UPSERT_DOC, (nct_id, dept, ct, text, chash))
                docs_written += cur.rowcount

    return trials_processed, docs_written


# ── Pass 2: Embed PENDING documents ───────────────────────────────────────────

def _load_model():
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        sys.exit(
            "sentence-transformers not installed.\n"
            "Install with:  pip install sentence-transformers\n"
            "Then re-run this script."
        )
    print(f"  Loading model {_MODEL_NAME!r} ...")
    model = SentenceTransformer(_MODEL_NAME)
    print("  Model ready.")
    return model


def _fetch_pending(dept_filter: str | None, chunk_type_filter: str | None, limit: int) -> list[dict]:
    sql = """
        SELECT id, nct_id, dept, chunk_type, content_text
        FROM "CT".trial_semantic_documents
        WHERE document_status = 'PENDING'
    """
    params: list = []
    if dept_filter:
        sql += " AND dept = %s"
        params.append(dept_filter)
    if chunk_type_filter:
        sql += " AND chunk_type = %s"
        params.append(chunk_type_filter)
    sql += " ORDER BY id LIMIT %s"
    params.append(limit)

    with _cur() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


_UPSERT_EMBEDDING = """
    INSERT INTO "CT".trial_embeddings
        (document_id, nct_id, dept, chunk_type, embedding_model, embedding, is_active)
    VALUES (%s, %s, %s, %s, %s, %s::vector, TRUE)
    ON CONFLICT (document_id, embedding_model) DO UPDATE
    SET embedding  = EXCLUDED.embedding,
        is_active  = TRUE,
        created_at = NOW()
"""

_DEACTIVATE_OLD = """
    UPDATE "CT".trial_embeddings
    SET is_active = FALSE
    WHERE nct_id      = %s
      AND dept        = %s
      AND chunk_type  = %s
      AND document_id != %s
      AND is_active   = TRUE
"""

_MARK_ACTIVE = """
    UPDATE "CT".trial_semantic_documents
    SET document_status = 'ACTIVE', updated_at = NOW()
    WHERE id = %s
"""


def embed_pending(model, docs: list[dict]) -> int:
    """
    Generate embeddings for docs, store in trial_embeddings, mark ACTIVE.
    Returns count stored.
    """
    if not docs:
        return 0

    from sentence_transformers import SentenceTransformer  # already imported via model

    texts      = [d["content_text"] for d in docs]
    embeddings = model.encode(texts, show_progress_bar=True, normalize_embeddings=True)

    stored = 0
    with _cur() as cur:
        for doc, vec in zip(docs, embeddings):
            # Vector string format pgvector expects: '[f1,f2,...,f384]'
            vec_str = "[" + ",".join(f"{v:.8f}" for v in vec.tolist()) + "]"

            # Deactivate prior active embeddings for this trial+chunk_type
            cur.execute(_DEACTIVATE_OLD, (doc["nct_id"], doc["dept"], doc["chunk_type"], doc["id"]))

            # Upsert new embedding
            cur.execute(_UPSERT_EMBEDDING, (
                doc["id"], doc["nct_id"], doc["dept"],
                doc["chunk_type"], _MODEL_NAME, vec_str,
            ))

            # Mark document ACTIVE
            cur.execute(_MARK_ACTIVE, (doc["id"],))
            stored += 1

    return stored


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Populate trial semantic chunks and generate pgvector embeddings"
    )
    p.add_argument("--dept",          help="Restrict to one department (e.g. ADC)")
    p.add_argument("--max",           type=int, default=500,
                   help="Max docs to embed per run (default: 500)")
    p.add_argument("--chunk-type",    choices=CHUNK_TYPES,
                   help="Process only this chunk type")
    p.add_argument("--populate-only", action="store_true",
                   help="Only run Pass 1 (populate chunks), skip embedding")
    p.add_argument("--embed-only",    action="store_true",
                   help="Only run Pass 2 (embed PENDING), skip populate")
    args = p.parse_args()

    # ── Pass 1 ────────────────────────────────────────────────────────────────
    if not args.embed_only:
        print("\n── Pass 1: Populate trial_semantic_documents ─────────────────")
        dept_label = args.dept or "all depts"
        ct_label   = args.chunk_type or "all chunk types"
        print(f"  Scope: {dept_label} / {ct_label}")
        trials, docs = populate_chunks(args.dept, args.chunk_type)
        print(f"  Trials processed : {trials}")
        print(f"  Docs new/updated : {docs}")

    if args.populate_only:
        print("\nDone (populate-only).")
        return

    # ── Pass 2 ────────────────────────────────────────────────────────────────
    print("\n── Pass 2: Embed PENDING documents ───────────────────────────")
    model   = _load_model()
    pending = _fetch_pending(args.dept, args.chunk_type, args.max)
    print(f"  PENDING docs found : {len(pending)}")
    if not pending:
        print("  Nothing to embed.")
    else:
        stored = embed_pending(model, pending)
        print(f"  Embeddings stored  : {stored}")

    print("\nDone.")


if __name__ == "__main__":
    main()
