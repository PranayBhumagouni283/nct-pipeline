"""
es_retriever.py -- FastAPI semantic retrieval service for clinical trials (V2).

Requirements:
    fastapi uvicorn[standard] FlagEmbedding elasticsearch>=8.14 python-dotenv

Architecture:
  POST /retrieve
    Query
      --> BGE-M3 dense encode (1024-dim)
      --> ES hybrid search: native RRF (kNN + BM25), fallback to manual RRF
      --> BGE-reranker-v2-m3 reranking
      --> Trial-level aggregation with diversity bonus
      --> Top-K trials returned

  GET /health
    {"status": "ok", "timestamp": "..."}

Usage:
    python es_retriever.py [--host 0.0.0.0] [--port 8000]
    uvicorn es_retriever:app --host 0.0.0.0 --port 8000
"""

import argparse
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, Field

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────

ES_INDEX   = os.getenv("ELASTICSEARCH_INDEX", "clinical_trials_semantic_v2")
MODEL_NAME = "BAAI/bge-m3"
RERANKER_NAME = "BAAI/bge-reranker-v2-m3"
DENSE_DIM  = 1024

# ── Module-level model singletons (loaded once at startup) ─────────────────────

_embed_model  = None
_reranker     = None
_es_client    = None
_no_reranker  = False   # set via --no-reranker CLI flag


def _load_models() -> None:
    global _embed_model, _reranker
    try:
        from FlagEmbedding import BGEM3FlagModel, FlagReranker
    except ImportError:
        raise RuntimeError(
            "FlagEmbedding not installed. Run: pip install FlagEmbedding"
        )
    print(f"[es_retriever] Loading embedding model: {MODEL_NAME} ...")
    _embed_model = BGEM3FlagModel(MODEL_NAME, use_fp16=True, local_files_only=True)
    if _no_reranker:
        print("[es_retriever] --no-reranker: skipping reranker load.")
    else:
        print(f"[es_retriever] Loading reranker: {RERANKER_NAME} ...")
        _reranker = FlagReranker(RERANKER_NAME, use_fp16=True, local_files_only=True)
    print("[es_retriever] Models ready.")


def _get_es():
    global _es_client
    if _es_client is None:
        from elasticsearch import Elasticsearch
        url      = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
        api_key  = os.getenv("ELASTICSEARCH_API_KEY", "")
        username = os.getenv("ELASTICSEARCH_USERNAME", "elastic")
        password = os.getenv("ELASTICSEARCH_PASSWORD", "")
        if api_key:
            _es_client = Elasticsearch(url, api_key=api_key)
        elif password:
            _es_client = Elasticsearch(url, basic_auth=(username, password), verify_certs=False)
        else:
            _es_client = Elasticsearch(url)
    return _es_client


# ── FastAPI lifespan — load models at startup ──────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_models()
    yield
    # No cleanup needed for in-process models


app = FastAPI(title="Clinical Trials Semantic Retrieval", version="2.0.0", lifespan=lifespan)


# ── Request / Response schemas ─────────────────────────────────────────────────

class RetrieveFilters(BaseModel):
    dept:         str | None        = None
    indication:   str | None        = None
    phase:        str | None        = None
    study_status: str | None        = None
    nct_ids:      list[str]         = Field(default_factory=list)


class RetrieveRequest(BaseModel):
    query:       str
    filters:     RetrieveFilters    = Field(default_factory=RetrieveFilters)
    chunk_types: list[str]          = Field(default_factory=list)
    top_k:       int                = 10
    candidate_k: int                = 50


class TrialResult(BaseModel):
    nct_id:       str
    trial_score:  float
    best_chunk:   str
    chunk_texts:  dict[str, str]    # {chunk_type: text[:600]} top 3 distinct chunk types
    study_status: str
    phase:        str
    department:   str
    rerank_score: float


class RetrieveResponse(BaseModel):
    query:                  str
    trials:                 list[TrialResult]
    total_chunks_retrieved: int
    latency_ms:             dict[str, float]
    retrieval_method:       str   # "native_rrf" or "manual_rrf_fallback"


# ── Encode query ───────────────────────────────────────────────────────────────

def _encode_query(query: str) -> list[float]:
    """Encode a single query string to BGE-M3 dense vector."""
    output = _embed_model.encode(
        [query],
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False,
        batch_size=1,
    )
    return output["dense_vecs"][0].tolist()


# ── Build ES filter clauses ────────────────────────────────────────────────────

def _build_filters(req: RetrieveRequest) -> list[dict]:
    """Construct the ES bool filter list from request params."""
    filters: list[dict] = [
        {"term": {"searchable": True}},
        {"term": {"is_latest": True}},
        {"term": {"embedding_status": "ACTIVE"}},
    ]
    f = req.filters
    if f.dept:
        filters.append({"term": {"department": f.dept}})
    if f.indication:
        filters.append({"term": {"indications": f.indication}})
    if f.phase:
        filters.append({"term": {"phase": f.phase}})
    if f.study_status:
        filters.append({"term": {"study_status": f.study_status}})
    if f.nct_ids:
        filters.append({"terms": {"nct_id": f.nct_ids}})
    if req.chunk_types:
        filters.append({"terms": {"chunk_type": req.chunk_types}})
    return filters


# ── Native RRF retrieval ───────────────────────────────────────────────────────

def _native_rrf_search(
    query:       str,
    dense_vec:   list[float],
    filters:     list[dict],
    candidate_k: int,
) -> tuple[list[dict], str]:
    """
    Attempt native ES RRF retriever. Returns (hits, method_label).
    Raises on failure so caller can fall back to manual RRF.
    """
    es = _get_es()
    body: dict[str, Any] = {
        "retriever": {
            "rrf": {
                "retrievers": [
                    {
                        "knn": {
                            "field": "dense_embedding",
                            "query_vector": dense_vec,
                            "num_candidates": candidate_k * 2,
                            "k": candidate_k,
                            "filter": {"bool": {"filter": filters}},
                        }
                    },
                    {
                        "standard": {
                            "query": {
                                "bool": {
                                    "must": {"match": {"text": query}},
                                    "filter": filters,
                                }
                            }
                        }
                    },
                ],
                "rank_window_size": candidate_k,
                "rank_constant": 60,
            }
        },
        "size": candidate_k,
        "_source": ["nct_id", "chunk_type", "text", "department", "phase", "study_status"],
    }
    resp = es.search(index=ES_INDEX, body=body)
    return resp["hits"]["hits"], "native_rrf"


# ── Manual RRF fallback ────────────────────────────────────────────────────────

def _manual_rrf_search(
    query:       str,
    dense_vec:   list[float],
    filters:     list[dict],
    candidate_k: int,
    rank_constant: int = 60,
) -> tuple[list[dict], str]:
    """Run kNN and BM25 separately, merge with manual RRF scoring."""
    es = _get_es()
    source_fields = ["nct_id", "chunk_type", "text", "department", "phase", "study_status"]

    dense_resp = es.search(index=ES_INDEX, body={
        "knn": {
            "field": "dense_embedding",
            "query_vector": dense_vec,
            "k": candidate_k,
            "num_candidates": candidate_k * 2,
            "filter": {"bool": {"filter": filters}},
        },
        "_source": source_fields,
        "size": candidate_k,
    })

    bm25_resp = es.search(index=ES_INDEX, body={
        "query": {
            "bool": {
                "must": {"match": {"text": query}},
                "filter": filters,
            }
        },
        "_source": source_fields,
        "size": candidate_k,
    })

    dense_hits = dense_resp["hits"]["hits"]
    bm25_hits  = bm25_resp["hits"]["hits"]

    scores: dict[str, float] = {}
    docs:   dict[str, dict]  = {}

    for rank, hit in enumerate(dense_hits, 1):
        doc_id = hit["_id"]
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (rank + rank_constant)
        docs[doc_id] = hit

    for rank, hit in enumerate(bm25_hits, 1):
        doc_id = hit["_id"]
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (rank + rank_constant)
        if doc_id not in docs:
            docs[doc_id] = hit

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    merged = []
    for doc_id, rrf_score in ranked:
        hit = dict(docs[doc_id])
        hit["_score"] = rrf_score
        merged.append(hit)

    return merged, "manual_rrf_fallback"


# ── Rerank ─────────────────────────────────────────────────────────────────────

def _rerank(query: str, hits: list[dict]) -> list[dict]:
    """
    Score all (query, chunk_text) pairs with BGE-reranker-v2-m3.
    When --no-reranker is active, assigns rerank_score from RRF position order.
    """
    if not hits:
        return hits

    if _no_reranker:
        # Use RRF rank position as a proxy score (higher = better)
        for i, hit in enumerate(hits):
            hit["rerank_score"] = 1.0 - i / max(len(hits), 1)
        return hits

    pairs = [(query, hit["_source"].get("text", "")) for hit in hits]
    scores = _reranker.compute_score(pairs, normalize=True)

    if isinstance(scores, float):
        scores = [scores]

    for hit, score in zip(hits, scores):
        hit["rerank_score"] = float(score)

    return sorted(hits, key=lambda h: h.get("rerank_score", 0.0), reverse=True)


# ── Trial-level aggregation ────────────────────────────────────────────────────

def _aggregate_trials(hits: list[dict], top_k: int) -> list[TrialResult]:
    """
    Group chunk-level hits by nct_id.
    TrialScore = best_rerank_score + min(distinct_chunk_types - 1, 2) * 0.05
    Keep chunk_texts for top 3 distinct chunk types (truncated to 600 chars).
    """
    trials: dict[str, dict] = {}

    for hit in hits:
        src  = hit["_source"]
        nct  = src.get("nct_id", "")
        text = src.get("text", "")
        ctype = src.get("chunk_type", "unknown")
        rerank_score = hit.get("rerank_score", 0.0)

        if nct not in trials:
            trials[nct] = {
                "nct_id":       nct,
                "best_chunk":   text,
                "rerank_score": rerank_score,
                "study_status": src.get("study_status", ""),
                "phase":        src.get("phase", ""),
                "department":   src.get("department", ""),
                # ordered dict keyed by chunk_type, value = first seen text
                "chunk_type_texts": {},
            }
        else:
            if rerank_score > trials[nct]["rerank_score"]:
                trials[nct]["rerank_score"] = rerank_score
                trials[nct]["best_chunk"]   = text

        ct_map = trials[nct]["chunk_type_texts"]
        if ctype not in ct_map and len(ct_map) < 3:
            ct_map[ctype] = text[:600]

    results: list[TrialResult] = []
    for nct, t in trials.items():
        distinct_types = len(t["chunk_type_texts"])
        trial_score    = t["rerank_score"] + min(distinct_types - 1, 2) * 0.05
        results.append(TrialResult(
            nct_id       = nct,
            trial_score  = trial_score,
            best_chunk   = t["best_chunk"],
            chunk_texts  = t["chunk_type_texts"],
            study_status = t["study_status"],
            phase        = t["phase"],
            department   = t["department"],
            rerank_score = t["rerank_score"],
        ))

    results.sort(key=lambda r: r.trial_score, reverse=True)
    return results[:top_k]


# ── Route handlers ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/retrieve", response_model=RetrieveResponse)
def retrieve(req: RetrieveRequest):
    query = req.query.strip()
    if not query:
        return RetrieveResponse(
            query=req.query,
            trials=[],
            total_chunks_retrieved=0,
            latency_ms={"encode_ms": 0, "retrieval_ms": 0, "rerank_ms": 0, "aggregation_ms": 0, "total_ms": 0},
            retrieval_method="none",
        )

    t_total_start = time.perf_counter()

    # 1. Encode query
    t0 = time.perf_counter()
    dense_vec = _encode_query(query)
    encode_ms = (time.perf_counter() - t0) * 1000

    # 2. Build filters
    filters = _build_filters(req)

    # 3. Hybrid retrieval (native RRF → manual RRF fallback)
    t0 = time.perf_counter()
    try:
        hits, method = _native_rrf_search(query, dense_vec, filters, req.candidate_k)
    except Exception as exc:
        print(f"[es_retriever] Native RRF failed ({exc}), falling back to manual RRF")
        hits, method = _manual_rrf_search(query, dense_vec, filters, req.candidate_k)
    retrieval_ms = (time.perf_counter() - t0) * 1000
    total_chunks = len(hits)

    # 4. Rerank
    t0 = time.perf_counter()
    hits = _rerank(query, hits)
    rerank_ms = (time.perf_counter() - t0) * 1000

    # 5. Aggregate to trial level
    t0 = time.perf_counter()
    trials = _aggregate_trials(hits, req.top_k)
    aggregation_ms = (time.perf_counter() - t0) * 1000

    total_ms = (time.perf_counter() - t_total_start) * 1000

    return RetrieveResponse(
        query=req.query,
        trials=trials,
        total_chunks_retrieved=total_chunks,
        latency_ms={
            "encode_ms":      round(encode_ms, 1),
            "retrieval_ms":   round(retrieval_ms, 1),
            "rerank_ms":      round(rerank_ms, 1),
            "aggregation_ms": round(aggregation_ms, 1),
            "total_ms":       round(total_ms, 1),
        },
        retrieval_method=method,
    )


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clinical Trials Semantic Retrieval Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--no-reranker", action="store_true",
                        help="Skip loading BGE-reranker (saves ~500MB RAM; uses RRF order instead)")
    args = parser.parse_args()
    if args.no_reranker:
        _no_reranker = True
    uvicorn.run(app, host=args.host, port=args.port)
