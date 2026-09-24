"""
Phase G-02 V3: Create Elasticsearch index clinical_trials_semantic_v3.

V3 additions over V2:
  Clinical filter keyword arrays (from organized_trials clinical concept columns):
    lot, biomarkers, cns_status, performance_status, treatment_setting,
    disease_state, prior_therapies, genomic_alteration, metastatic_site,
    combination_therapy, age_range, gene, organ_function,
    biomarker_measurement, biomarker_value, biomarker_test_method,
    comparator, regimen

  Phase 3 metadata fields (new):
    biomarker_status, treatment_status, endpoints_normalized,
    interventions_list, conditions_list, sponsors_list

  These enable server-side pre-filtering in ES kNN queries, eliminating
  false positives from low-relevance semantic matches.

Run:
    python _local_scripts/__phase_g02_es_index_v3.py
    python _local_scripts/__phase_g02_es_index_v3.py --recreate
"""
import argparse
import os
import sys
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / ".env")

ES_URL      = os.getenv("ELASTICSEARCH_URL",    "http://localhost:9200")
ES_API_KEY  = os.getenv("ELASTICSEARCH_API_KEY", "")
ES_USERNAME = os.getenv("ELASTICSEARCH_USERNAME", "elastic")
ES_PASSWORD = os.getenv("ELASTICSEARCH_PASSWORD", "")
ES_INDEX    = "clinical_trials_semantic_v3"

SEP = "\n" + "-" * 60 + "\n"

try:
    from elasticsearch import Elasticsearch
except ImportError:
    sys.exit("elasticsearch not installed. Run: pip install 'elasticsearch>=8.14'")


def _connect() -> Elasticsearch:
    if ES_API_KEY:
        client = Elasticsearch(ES_URL, api_key=ES_API_KEY)
    elif ES_PASSWORD:
        client = Elasticsearch(ES_URL, basic_auth=(ES_USERNAME, ES_PASSWORD),
                               verify_certs=False, ssl_show_warn=False)
    else:
        client = Elasticsearch(ES_URL)
    info    = client.info()
    version = info.get("version", {}).get("number", "serverless")
    print(f"  Elasticsearch {version} at {ES_URL}")
    return client


INDEX_MAPPING = {
    "settings": {
        "analysis": {
            "analyzer": {
                "clinical_english": {
                    "type": "english",
                    "stopwords": "_english_",
                }
            }
        },
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            # ── Identity ──────────────────────────────────────────────────────
            "chunk_id":      {"type": "keyword"},
            "nct_id":        {"type": "keyword"},
            "document_id":   {"type": "long"},
            "chunk_type":    {"type": "keyword"},
            "chunk_version": {"type": "integer"},

            # ── Semantic content ──────────────────────────────────────────────
            "text": {
                "type":     "text",
                "analyzer": "clinical_english",
            },

            # ── Dense vector (BGE-M3 1024-dim, cosine) ────────────────────────
            "dense_embedding": {
                "type":       "dense_vector",
                "dims":       1024,
                "index":      True,
                "similarity": "cosine",
            },

            # ── Scope ─────────────────────────────────────────────────────────
            "department":  {"type": "keyword"},
            "indications": {"type": "keyword"},   # array

            # ── Core clinical metadata ─────────────────────────────────────────
            "phase":              {"type": "keyword"},
            "study_status":       {"type": "keyword"},
            "study_type":         {"type": "keyword"},
            "primary_purpose":    {"type": "keyword"},
            "primary_drug":       {"type": "keyword"},
            "sponsors":           {"type": "keyword"},   # array (pipe-split)
            "collaborators":      {"type": "keyword"},   # array (pipe-split)
            "lead_sponsor":       {"type": "keyword"},   # from responsiblePartyleadSponsor
            "funder_type":        {"type": "keyword"},
            "conditions":         {"type": "text", "fields": {"raw": {"type": "keyword"}}},
            "countries":          {"type": "keyword"},   # array
            "enrollment":         {"type": "long"},      # numeric; NULL when unknown
            "intervention_model": {"type": "keyword"},
            "allocation":         {"type": "keyword"},
            "masking":            {"type": "keyword"},
            "fda_regulated_drug": {"type": "keyword"},
            "sex":                {"type": "keyword"},
            "minimum_age":        {"type": "keyword"},
            "standard_ages":      {"type": "keyword"},
            "healthy_volunteers": {"type": "keyword"},

            # ── V3 NEW: Clinical concept filter arrays ─────────────────────────
            # Sourced from organized_trials clinical concept columns.
            # All stored as keyword arrays split on pipe separator.
            "lot":                  {"type": "keyword"},  # line_of_therapy
            "biomarkers":           {"type": "keyword"},
            "cns_status":           {"type": "keyword"},
            "performance_status":   {"type": "keyword"},
            "treatment_setting":    {"type": "keyword"},
            "disease_state":        {"type": "keyword"},
            "prior_therapies":      {"type": "keyword"},
            "genomic_alteration":   {"type": "keyword"},
            "metastatic_site":      {"type": "keyword"},
            "combination_therapy":  {"type": "keyword"},
            "age_range":            {"type": "keyword"},
            # Phase 14 columns
            "gene":                    {"type": "keyword"},
            "organ_function":          {"type": "keyword"},  # organ_function_requirement
            "biomarker_measurement":   {"type": "keyword"},
            "biomarker_value":         {"type": "keyword"},  # biomarker_value_or_cutoff
            "biomarker_test_method":   {"type": "keyword"},
            "comparator":              {"type": "keyword"},
            "regimen":                 {"type": "keyword"},
            # Phase 3: new metadata fields
            "biomarker_status":        {"type": "keyword"},   # array — HER2_POSITIVE, EGFR_WILD_TYPE, etc.
            "treatment_status":        {"type": "keyword"},   # array — TREATMENT_NAIVE, PRETREATED, etc.
            "endpoints_normalized":    {"type": "keyword"},   # array — PFS, OS, ORR, etc.
            "interventions_list":      {"type": "keyword"},   # array — individual intervention names (pipe-split)
            "conditions_list":         {"type": "keyword"},   # array — individual conditions (comma-split from conditions text)
            "sponsors_list":           {"type": "keyword"},   # array — all sponsors including collaborators (pipe-split Sponsors column)

            # ── Dates ─────────────────────────────────────────────────────────
            "study_start_date":        {"type": "date", "format": "yyyy-MM-dd||strict_date_optional_time||epoch_millis", "ignore_malformed": True},
            "primary_completion_date": {"type": "date", "format": "yyyy-MM-dd||strict_date_optional_time||epoch_millis", "ignore_malformed": True},
            "first_post_date":         {"type": "date", "format": "yyyy-MM-dd||strict_date_optional_time||epoch_millis", "ignore_malformed": True},
            "last_update_post_date":   {"type": "date", "format": "yyyy-MM-dd||strict_date_optional_time||epoch_millis", "ignore_malformed": True},

            # ── Version / lifecycle ───────────────────────────────────────────
            "content_hash":  {"type": "keyword"},
            "is_latest":     {"type": "boolean"},
            "is_superseded": {"type": "boolean"},
            "searchable":    {"type": "boolean"},

            # ── Embedding metadata ────────────────────────────────────────────
            "embedding_model":       {"type": "keyword"},
            "embedding_version":     {"type": "integer"},
            "embedding_dimension":   {"type": "integer"},
            "embedding_status":      {"type": "keyword"},
            "embedding_retry_count": {"type": "integer"},

            # ── Quality ───────────────────────────────────────────────────────
            "text_length": {"type": "integer"},

            # ── Timestamps ───────────────────────────────────────────────────
            "embedding_created_at": {"type": "date", "format": "strict_date_optional_time"},
            "indexed_at":           {"type": "date", "format": "strict_date_optional_time"},
        }
    }
}

V3_CLINICAL_FIELDS = [
    "lot", "biomarkers", "cns_status", "performance_status",
    "treatment_setting", "disease_state", "prior_therapies",
    "genomic_alteration", "metastatic_site", "combination_therapy",
    "age_range", "gene", "organ_function", "biomarker_measurement",
    "biomarker_value", "biomarker_test_method", "comparator", "regimen",
]


def create_index(client: Elasticsearch, recreate: bool) -> None:
    exists = client.indices.exists(index=ES_INDEX).body
    if exists:
        print(SEP + f"Index {ES_INDEX!r} already exists.")
        info      = client.cat.count(index=ES_INDEX, params={"format": "json"})
        doc_count = info[0].get("count", "?") if info else "?"
        print(f"  Document count: {doc_count}")
        if not recreate:
            print("  Use --recreate to drop and recreate. Skipping.")
            return
        confirm = input(f"  Drop and recreate {ES_INDEX!r}? Deletes all indexed data. [y/N] ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            return
        client.indices.delete(index=ES_INDEX)
        print(f"  Deleted: {ES_INDEX}")

    print(SEP + f"Creating index: {ES_INDEX!r}")
    client.indices.create(index=ES_INDEX, body=INDEX_MAPPING)
    print(f"  Created: {ES_INDEX}")
    print(f"    dense_embedding : 1024-dim cosine (BGE-M3)")
    print(f"    text            : English analyzer (BM25)")
    print(f"    V3 new fields   : {', '.join(V3_CLINICAL_FIELDS)}")


def verify_index(client: Elasticsearch) -> None:
    print(SEP + "Verification")
    mapping   = client.indices.get_mapping(index=ES_INDEX)
    idx_map   = mapping[ES_INDEX]["mappings"]
    props     = idx_map.get("properties", {})
    dynamic   = idx_map.get("dynamic", "")
    vec       = props.get("dense_embedding", {})
    txt       = props.get("text", {})

    results: list[tuple[str, bool, str]] = []

    # ── structural checks ──────────────────────────────────────────────────────
    results.append(("dynamic == strict",
                    str(dynamic) == "strict",
                    f"got {dynamic!r}"))
    results.append(("dense_embedding dims == 1024",
                    vec.get("dims") == 1024,
                    f"got {vec.get('dims')}"))
    results.append(("dense_embedding similarity == cosine",
                    vec.get("similarity") == "cosine",
                    f"got {vec.get('similarity')}"))
    results.append(("text analyzer == clinical_english",
                    txt.get("analyzer") == "clinical_english",
                    f"got {txt.get('analyzer')}"))

    # ── field-type checks ─────────────────────────────────────────────────────
    def _type(field: str) -> str:
        return props.get(field, {}).get("type", "<missing>")

    results.append(("sponsors is keyword",
                    _type("sponsors") == "keyword",
                    f"got {_type('sponsors')}"))
    results.append(("collaborators is keyword",
                    _type("collaborators") == "keyword",
                    f"got {_type('collaborators')}"))
    results.append(("lead_sponsor exists",
                    "lead_sponsor" in props,
                    "field missing" if "lead_sponsor" not in props else "ok"))
    results.append(("enrollment is long",
                    _type("enrollment") == "long",
                    f"got {_type('enrollment')}"))

    for f in ("department", "indications", "primary_drug", "countries"):
        results.append((f"{f} is keyword",
                        _type(f) == "keyword",
                        f"got {_type(f)}"))

    # ── V3 clinical fields ────────────────────────────────────────────────────
    for f in V3_CLINICAL_FIELDS:
        results.append((f"V3 field: {f}",
                        f in props,
                        "ok" if f in props else "MISSING"))

    # ── print ─────────────────────────────────────────────────────────────────
    print(f"  Index        : {ES_INDEX}")
    print(f"  Total fields : {len(props)}")
    passes = sum(1 for _, ok, _ in results if ok)
    fails  = sum(1 for _, ok, _ in results if not ok)
    for label, ok, detail in results:
        marker = "PASS" if ok else "FAIL"
        note   = f"  ({detail})" if not ok else ""
        print(f"  [{marker}] {label}{note}")
    print(f"\n  Summary: {passes} PASS  /  {fails} FAIL")


def main():
    p = argparse.ArgumentParser(description="Create clinical_trials_semantic_v3 ES index")
    p.add_argument("--recreate", action="store_true")
    args = p.parse_args()

    print(SEP + f"Connecting to Elasticsearch at {ES_URL}")
    client = _connect()
    create_index(client, recreate=args.recreate)
    verify_index(client)
    print(SEP + "Phase G-02 V3 complete.")
    print(f"  Index: {ES_INDEX}")
    print(SEP)


if __name__ == "__main__":
    main()
