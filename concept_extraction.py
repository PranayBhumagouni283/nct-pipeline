"""
concept_extraction.py — Post-pipeline concept extraction for organized_trials.

Called from combined_pipeline.py after new/modified trials are upserted.
Runs regex + LLM (GPT-4o-mini) extraction on a list of NCT IDs and writes
all 21 concept columns to both PROD and AWS RDS.

Extraction policy:
  - Regex extracts REGEX_PRIMARY (12 cols) + age_range_normalized + endpoints_normalized
  - LLM extracts all 19 cols (excludes age_range_normalized, endpoints_normalized)
  - LLM is primary; regex fills NULL gaps for the 12 shared cols
  - endpoints_normalized: regex only (LLM reads wrong source text)
  - age_range_normalized: regex only (structured parsing)
  - Outputs are normalized: variant forms mapped to canonical names via dictionary
"""
import os, sys
from pathlib import Path
import importlib.util
import psycopg2, psycopg2.extras
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Load extraction helpers from _local_scripts ────────────────────────────────
_EXT_PATH = Path(__file__).parent / "_local_scripts" / "__extract_clinical_concepts.py"
_spec = importlib.util.spec_from_file_location("_ext_concepts", _EXT_PATH)
_ext  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ext)

regex_pass           = _ext.regex_pass
llm_pass             = _ext.llm_pass
load_dict_rows       = _ext.load_dict_rows
connect              = _ext.connect
REGEX_PRIMARY        = _ext.REGEX_PRIMARY
CONCEPT_TO_DICT_KEYS = _ext.CONCEPT_TO_DICT_KEYS

# ── Column lists ───────────────────────────────────────────────────────────────
ALL_CONCEPT_COLS = [
    "line_of_therapy", "biomarkers", "biomarker_status", "prior_therapies",
    "treatment_status", "treatment_setting", "disease_state",
    "organ_function_requirement", "performance_status", "cns_status",
    "genomic_alteration", "endpoints_normalized", "gene", "metastatic_site",
    "combination_therapy", "comparator", "regimen",
    "biomarker_measurement", "biomarker_value_or_cutoff", "biomarker_test_method",
    "age_range_normalized",
]

# LLM handles everything except the two regex-only cols
LLM_COLS = [c for c in ALL_CONCEPT_COLS
            if c not in ("age_range_normalized", "endpoints_normalized")]

# Source columns needed to perform extraction
_FETCH_SQL_COLS = ", ".join([
    "nct_id",
    '"eligibilityCriteria"', '"briefSummary"', '"conditions"',
    '"Primary Outcomes"', '"Secondary Outcomes"',
    '"detailedDescription"', '"Interventions"',
    '"MeSH Conditions"', '"MeSH Interventions"',
    '"Minimum Age"', '"Standard Ages"',
    "min_age_value", "min_age_unit",
])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalise(val: str | None) -> str | None:
    if not val:
        return None
    parts = {v.strip().upper() for v in str(val).split("|") if v.strip()}
    return "|".join(sorted(parts)) if parts else None


def _build_variant_lookup(dict_rows: list) -> dict:
    """Build {concept: {VARIANT_UPPER: canonical}} from dict_rows."""
    lookup: dict = {}
    for dr in dict_rows:
        concept, canonical, variants_str = dr["concept"], dr["canonical_value"], dr.get("variants") or ""
        if concept not in lookup:
            lookup[concept] = {}
        lookup[concept][canonical.upper()] = canonical
        for v in variants_str.split(";"):
            v = v.strip()
            if v:
                lookup[concept][v.upper()] = canonical
    return lookup


def _normalize_value(val: str | None, col: str, variant_lookup: dict) -> str | None:
    """Map pipe-separated values through variant lookup to canonical forms."""
    if not val:
        return None
    concepts = CONCEPT_TO_DICT_KEYS.get(col, [])
    lookups  = [variant_lookup.get(c, {}) for c in concepts]
    parts, seen = [], set()
    for token in val.split("|"):
        token = token.strip()
        if not token:
            continue
        mapped = token
        for lk in lookups:
            if token.upper() in lk:
                mapped = lk[token.upper()]
                break
        if mapped.upper() not in seen:
            seen.add(mapped.upper())
            parts.append(mapped)
    return "|".join(sorted(parts)) if parts else None


def _fetch_rows(db_url: str, nct_ids: list[str]) -> list:
    conn = psycopg2.connect(db_url)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SET search_path TO "CT"')
    placeholders = ",".join(["%s"] * len(nct_ids))
    cur.execute(
        f"SELECT {_FETCH_SQL_COLS} FROM organized_trials WHERE nct_id IN ({placeholders})",
        nct_ids,
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def _push(db_url: str, label: str, results: dict[str, dict], verbose: bool) -> None:
    """Write extracted concept values to organized_trials in batches of 100."""
    items = [(nct_id, vals) for nct_id, vals in results.items() if vals]
    if not items:
        return
    BATCH = 100
    written = 0
    for i in range(0, len(items), BATCH):
        chunk = items[i: i + BATCH]
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        cur  = conn.cursor()
        cur.execute('SET search_path TO "CT"')
        conn.autocommit = False
        for nct_id, vals in chunk:
            set_parts = [f'"{col}" = %s' for col in vals]
            cur.execute(
                f"UPDATE organized_trials SET {', '.join(set_parts)} WHERE nct_id = %s",
                list(vals.values()) + [nct_id],
            )
            written += 1
        conn.commit()
        cur.close()
        conn.close()
    if verbose:
        print(f"  [{label}] Concept extraction written: {written} trials")


# ── Public API ─────────────────────────────────────────────────────────────────

def run_for_nct_ids(nct_ids: list[str], verbose: bool = True) -> None:
    """
    Run concept extraction for the given NCT IDs and push to PROD + AWS.
    Reads DATABASE_URL and DATABASE_URL_AWS from environment (.env).
    Silently skips LLM if OPENAI_API_KEY is not set.
    Wrapped in try/except so pipeline continues on any failure.
    """
    if not nct_ids:
        return

    prod_url = os.environ.get("DATABASE_URL")
    aws_url  = os.environ.get("DATABASE_URL_AWS")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if not prod_url:
        if verbose:
            print("  [Concept Extraction] DATABASE_URL not set — skipping")
        return

    try:
        _run(nct_ids, prod_url, aws_url, openai_key, verbose)
    except Exception as e:
        print(f"  [Concept Extraction] ERROR (non-fatal): {e}")


def _run(nct_ids: list[str], prod_url: str, aws_url: str | None, openai_key: str | None, verbose: bool) -> None:
    if verbose:
        print(f"\n  [Concept Extraction] {len(nct_ids)} trial(s) to extract...")

    # Load regex dictionary from PROD
    # load_dict_rows returns a grouped dict {concept: [rows]}; flatten for _build_variant_lookup
    dict_conn    = connect(prod_url)
    dict_grouped = load_dict_rows(dict_conn)
    dict_conn.close()
    dict_rows_flat = [row for rows in dict_grouped.values() for row in rows]
    var_lookup     = _build_variant_lookup(dict_rows_flat)

    # Setup OpenAI client (optional — regex-only mode if key not set)
    client = None
    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
        except ImportError:
            if verbose:
                print("  [Concept Extraction] openai not installed — LLM skipped, regex only")

    # Fetch trial source text from PROD
    rows = _fetch_rows(prod_url, nct_ids)
    if not rows:
        if verbose:
            print("  [Concept Extraction] No rows found")
        return

    # Extract for each trial
    results: dict[str, dict] = {}
    for i, row in enumerate(rows, 1):
        nct_id = row["nct_id"]
        row_d  = dict(row)

        # Pass 1: Regex (REGEX_PRIMARY + age_range_normalized + endpoints_normalized)
        regex_res = regex_pass(row_d, dict_grouped)

        # Pass 2: LLM for all 19 non-regex-only cols (regardless of what regex found)
        llm_res: dict = {}
        if client:
            llm_res = llm_pass(row_d, LLM_COLS, client)

        # Merge: LLM primary; regex fills gaps; endpoints + age are regex-only
        merged: dict = {}

        for col in ("age_range_normalized", "endpoints_normalized"):
            val = _normalise(regex_res.get(col))
            if val:
                merged[col] = val

        for col in LLM_COLS:
            raw = llm_res.get(col) or regex_res.get(col)
            if raw:
                normalized = _normalize_value(_normalise(raw), col, var_lookup)
                if normalized:
                    merged[col] = normalized

        if merged:
            results[nct_id] = merged

        if verbose and i % 20 == 0:
            print(f"  [Concept Extraction] {i}/{len(rows)} processed...")

    if not results:
        if verbose:
            print("  [Concept Extraction] No results extracted")
        return

    if verbose:
        print(f"  [Concept Extraction] Extracted for {len(results)}/{len(rows)} trials")

    # Push to PROD
    _push(prod_url, "PROD", results, verbose)

    # Push to AWS
    if aws_url:
        _push(aws_url, "AWS", results, verbose)
