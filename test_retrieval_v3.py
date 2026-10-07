"""
test_retrieval_v3.py — Complex query smoke-test for V3 semantic retrieval.

Tests 10 queries across different V3 clinical metadata dimensions:
  - Pure semantic (no filters)
  - LOT-scoped
  - Biomarker + gene
  - CNS / metastatic site
  - Treatment setting + disease state
  - Combination therapy
  - KOL / PI queries
  - Genomic alteration
  - Multi-filter compound
  - Cross-indication comparison

Usage:
    python test_retrieval_v3.py                           # uses RETRIEVAL_SERVICE_URL env var
    python test_retrieval_v3.py --url http://localhost:8000
    python test_retrieval_v3.py --url http://localhost:8000 --dept ADC
    python test_retrieval_v3.py --es-only                 # BM25 direct ES (no model needed)
"""

import argparse
import json
import os
import sys
import time
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

# ── ANSI colours ───────────────────────────────────────────────────────────────
G  = "\033[92m"  # green
Y  = "\033[93m"  # yellow
R  = "\033[91m"  # red
B  = "\033[94m"  # blue
W  = "\033[97m"  # white
DIM= "\033[2m"
RST= "\033[0m"

# ── Test cases ─────────────────────────────────────────────────────────────────
# Each entry: (label, query_text, filters_dict)

TEST_CASES: list[tuple[str, str, dict]] = [

    # 1. Pure semantic — no filters
    (
        "SEMANTIC: ADC mechanism + payload toxicity",
        "antibody drug conjugate with auristatin payload and peripheral neuropathy management",
        {},
    ),

    # 2. LOT-scoped
    (
        "LOT: 2L+ HER2+ breast cancer after trastuzumab",
        "HER2 positive breast cancer second line or later after trastuzumab failure",
        {"lot": ["2L", "2L+", "3L+"]},
    ),

    # 3. Biomarker + gene
    (
        "BIOMARKER+GENE: BRCA1/2 mutation PARP inhibitor",
        "BRCA mutated ovarian cancer PARP inhibitor maintenance therapy",
        {"biomarkers": ["BRCA1", "BRCA2"], "gene": ["BRCA1", "BRCA2"]},
    ),

    # 4. CNS / brain metastases
    (
        "CNS: brain mets NSCLC targeted therapy",
        "non-small cell lung cancer with active brain metastases EGFR mutation targeted therapy",
        {"cns_status": ["active brain metastases", "brain metastases allowed"],
         "metastatic_site": ["brain", "CNS"]},
    ),

    # 5. Treatment setting + disease state
    (
        "SETTING: neoadjuvant triple negative breast cancer",
        "neoadjuvant chemotherapy immunotherapy combination triple negative breast cancer pathologic complete response",
        {"treatment_setting": ["neoadjuvant"], "disease_state": ["early stage", "locally advanced"]},
    ),

    # 6. Combination therapy
    (
        "COMBO: checkpoint inhibitor + VEGF/VEGFR",
        "PD-1 PD-L1 checkpoint inhibitor combined with bevacizumab or axitinib renal cell carcinoma",
        {"combination_therapy": ["checkpoint inhibitor + VEGF", "IO + TKI"]},
    ),

    # 7. Performance status + organ function
    (
        "ELIGIBILITY: PS2 patients hepatic impairment",
        "clinical trial enrolling patients with ECOG performance status 2 or hepatic impairment reduced dose",
        {"performance_status": ["ECOG 0-2", "ECOG 2"],
         "organ_function": ["hepatic impairment allowed", "Child-Pugh B"]},
    ),

    # 8. Genomic alteration — specific mutations
    (
        "GENOMIC: KRAS G12C inhibitor",
        "KRAS G12C mutation specific inhibitor sotorasib adagrasib lung or colorectal cancer",
        {"genomic_alteration": ["KRAS G12C", "KRAS mutation"]},
    ),

    # 9. Prior therapy constraint
    (
        "PRIOR THERAPY: CDK4/6 inhibitor refractory HR+ BC",
        "hormone receptor positive breast cancer progression after CDK4/6 inhibitor endocrine therapy",
        {"prior_therapies": ["CDK4/6 inhibitor", "prior CDK4/6i"],
         "lot": ["2L", "2L+"]},
    ),

    # 10. KOL / PI-focused query
    (
        "KOL: principal investigators ADC trials oncology",
        "principal investigator lead oncologist antibody drug conjugate phase 2 phase 3",
        {"chunk_types": ["PI_INFO"]},
    ),
]


def _fmt_score(s: float) -> str:
    if s >= 0.7:
        return f"{G}{s:.4f}{RST}"
    if s >= 0.4:
        return f"{Y}{s:.4f}{RST}"
    return f"{R}{s:.4f}{RST}"


def run_service_tests(base_url: str, dept: str | None, top_k: int = 5) -> None:
    """Fire queries at the /retrieve endpoint."""
    url = base_url.rstrip("/") + "/retrieve"
    passed = failed = 0

    print(f"\n{W}{'═'*72}{RST}")
    print(f"{W}  V3 Semantic Retrieval Test  │  {base_url}  │  dept={dept or 'ALL'}{RST}")
    print(f"{W}{'═'*72}{RST}\n")

    for i, (label, query, extra_filters) in enumerate(TEST_CASES, 1):
        chunk_types = extra_filters.pop("chunk_types", [])
        filters = {"dept": dept, **extra_filters}

        payload = {
            "query":       query,
            "filters":     filters,
            "chunk_types": chunk_types,
            "top_k":       top_k,
            "candidate_k": 50,
        }

        t0 = time.perf_counter()
        try:
            resp = requests.post(url, json=payload, timeout=60)
            elapsed = (time.perf_counter() - t0) * 1000
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000
            print(f"{R}[FAIL]{RST} Test {i:02d}: {label}")
            print(f"       {R}Error: {exc}{RST}\n")
            failed += 1
            continue

        trials     = data.get("trials", [])
        latency    = data.get("latency_ms", {})
        method     = data.get("retrieval_method", "?")
        total_chks = data.get("total_chunks_retrieved", 0)

        status = f"{G}[PASS]{RST}" if trials else f"{Y}[EMPTY]{RST}"
        if trials:
            passed += 1
        else:
            failed += 1

        print(f"{status} Test {i:02d}: {W}{label}{RST}")
        print(f"  {DIM}Query   :{RST} {query[:90]}{'...' if len(query)>90 else ''}")
        print(f"  {DIM}Method  :{RST} {method}  |  chunks retrieved: {total_chks}  |  "
              f"encode {latency.get('encode_ms','?')}ms  rerank {latency.get('rerank_ms','?')}ms  "
              f"total {latency.get('total_ms','?')}ms")

        if trials:
            print(f"  {DIM}Top trials:{RST}")
            for rank, t in enumerate(trials, 1):
                score_str = _fmt_score(t.get("trial_score", 0))
                rerank    = t.get("rerank_score", 0)
                nct       = t.get("nct_id", "?")
                phase     = t.get("phase", "?")
                status_   = t.get("study_status", "?")
                dept_     = t.get("department", "?")
                chunk_txt = t.get("best_chunk", "")[:120].replace("\n", " ")
                print(f"    {rank}. {B}{nct}{RST}  score={score_str}  rerank={rerank:.4f}  "
                      f"phase={phase}  status={status_}  dept={dept_}")
                print(f"       {DIM}{chunk_txt}...{RST}")
        else:
            print(f"  {Y}No trials returned — check filters or index content.{RST}")

        print()

    print(f"{W}{'─'*72}{RST}")
    print(f"  Results: {G}{passed} passed{RST}  {R}{failed} failed{RST}  of {len(TEST_CASES)} tests")
    print(f"{W}{'─'*72}{RST}\n")


def run_es_direct_tests(dept: str | None, top_k: int = 5) -> None:
    """BM25-only test directly against ES (no model needed)."""
    try:
        from elasticsearch import Elasticsearch
    except ImportError:
        print(f"{R}elasticsearch package not installed. Run: pip install elasticsearch{RST}")
        sys.exit(1)

    url      = os.getenv("ELASTICSEARCH_URL", "")
    api_key  = os.getenv("ELASTICSEARCH_API_KEY", "")
    username = os.getenv("ELASTICSEARCH_USERNAME", "elastic")
    password = os.getenv("ELASTICSEARCH_PASSWORD", "")
    es_index = os.getenv("ELASTICSEARCH_INDEX", "clinical_trials_semantic_v3")

    if not url:
        print(f"{R}ELASTICSEARCH_URL not set in environment.{RST}")
        sys.exit(1)

    if api_key:
        es = Elasticsearch(url, api_key=api_key)
    elif password:
        es = Elasticsearch(url, basic_auth=(username, password), verify_certs=False)
    else:
        es = Elasticsearch(url)

    print(f"\n{W}{'═'*72}{RST}")
    print(f"{W}  V3 BM25 Direct ES Test  │  {es_index}  │  dept={dept or 'ALL'}{RST}")
    print(f"{W}{'═'*72}{RST}\n")

    # Check index health first
    count = es.count(index=es_index)["count"]
    print(f"  {G}Index docs: {count:,}{RST}\n")

    passed = failed = 0

    for i, (label, query, extra_filters) in enumerate(TEST_CASES, 1):
        extra_filters.pop("chunk_types", None)

        filters: list[dict] = [
            {"term": {"searchable": True}},
            {"term": {"is_latest": True}},
            {"term": {"embedding_status": "ACTIVE"}},
        ]
        if dept:
            filters.append({"term": {"department": dept}})

        _CLINICAL_ARRAY_FIELDS = [
            "lot", "biomarkers", "cns_status", "performance_status",
            "treatment_setting", "disease_state", "prior_therapies",
            "genomic_alteration", "metastatic_site", "combination_therapy",
            "age_range", "gene", "organ_function",
        ]
        for field in _CLINICAL_ARRAY_FIELDS:
            vals = extra_filters.get(field, [])
            if vals:
                filters.append({"terms": {field: vals}})

        body = {
            "query": {
                "bool": {
                    "must":   {"match": {"text": query}},
                    "filter": filters,
                }
            },
            "_source": ["nct_id", "chunk_type", "text", "department", "phase", "study_status"],
            "size": top_k,
        }

        t0 = time.perf_counter()
        try:
            resp = es.search(index=es_index, body=body)
            elapsed = (time.perf_counter() - t0) * 1000
        except Exception as exc:
            print(f"{R}[FAIL]{RST} Test {i:02d}: {label} — {exc}\n")
            failed += 1
            continue

        hits = resp["hits"]["hits"]
        total = resp["hits"]["total"]["value"]
        status = f"{G}[PASS]{RST}" if hits else f"{Y}[EMPTY]{RST}"
        if hits:
            passed += 1
        else:
            failed += 1

        print(f"{status} Test {i:02d}: {W}{label}{RST}")
        print(f"  {DIM}Query    :{RST} {query[:90]}{'...' if len(query)>90 else ''}")
        print(f"  {DIM}Total hit:{RST} {total}  |  {elapsed:.0f}ms")

        for rank, h in enumerate(hits, 1):
            s   = h.get("_source", {})
            sc  = _fmt_score(h.get("_score", 0))
            nct = s.get("nct_id", "?")
            txt = s.get("text", "")[:120].replace("\n", " ")
            ctype = s.get("chunk_type", "?")
            print(f"    {rank}. {B}{nct}{RST}  score={sc}  type={ctype}  "
                  f"phase={s.get('phase','?')}  status={s.get('study_status','?')}")
            print(f"       {DIM}{txt}...{RST}")

        if not hits:
            print(f"  {Y}No hits — filter may be too restrictive or terms not in index.{RST}")
        print()

    print(f"{W}{'─'*72}{RST}")
    print(f"  Results: {G}{passed} passed{RST}  {R}{failed} failed{RST}  of {len(TEST_CASES)} tests")
    print(f"{W}{'─'*72}{RST}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="V3 semantic retrieval smoke-test")
    parser.add_argument("--url",     default=os.getenv("RETRIEVAL_SERVICE_URL", ""),
                        help="Retrieval service base URL (default: $RETRIEVAL_SERVICE_URL)")
    parser.add_argument("--dept",    default=None,
                        help="Optional dept filter, e.g. ADC or 'Infectious Diseases'")
    parser.add_argument("--top-k",   type=int, default=5, help="Top K trials per query (default: 5)")
    parser.add_argument("--es-only", action="store_true",
                        help="Skip retrieval service; run BM25 directly against ES")
    args = parser.parse_args()

    if args.es_only:
        run_es_direct_tests(dept=args.dept, top_k=args.top_k)
    elif args.url:
        run_service_tests(base_url=args.url, dept=args.dept, top_k=args.top_k)
    else:
        print(f"{Y}No --url provided and RETRIEVAL_SERVICE_URL not set.{RST}")
        print(f"Options:")
        print(f"  1. Run against retrieval service:  python test_retrieval_v3.py --url http://localhost:8000")
        print(f"  2. Run BM25 direct against ES:      python test_retrieval_v3.py --es-only")
        sys.exit(1)


if __name__ == "__main__":
    main()
