"""Pydantic request/response schemas for the NJDOT Chatbot API.

All models use Pydantic v2.  ``CitationItem`` deliberately accepts extra
fields (``extra="ignore"``) so that the ``verified`` flag emitted by
``CitationSerializer`` is silently dropped at the API boundary — it is an
internal quality signal, not part of the public contract.
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict


# ── Request ───────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    """Body expected on ``POST /api/query``.

    Blank-query validation is intentionally left to the endpoint handler so
    that the API returns HTTP 400 (not the Pydantic-generated 422).
    """

    query:      str
    collection: Optional[str] = None   # None → search all collections


# ── Citation item ─────────────────────────────────────────────────────────────

class CitationItem(BaseModel):
    """One verified source reference returned with an answer."""

    # Drop internal keys emitted by CitationSerializer (e.g. ``verified``)
    model_config = ConfigDict(extra="ignore")

    document:     Optional[str] = None
    section:      Optional[str] = None
    page_printed: Optional[int] = None
    page_pdf:     Optional[int] = None
    chunk_id:     Optional[str] = None


# ── BDC alert item ────────────────────────────────────────────────────────────

class BDCAlertItem(BaseModel):
    """One Baseline Document Change amendment that affects a retrieved section."""

    bdc_id:              str
    section_id:          str
    effective_date:      Optional[str] = None
    subject:             Optional[str] = None
    implementation_code: Optional[str] = None
    change_type:         Optional[str] = None


# ── Response ──────────────────────────────────────────────────────────────────

class QueryResponse(BaseModel):
    """Body returned by ``POST /api/query``."""

    answer:           str
    citations:        List[CitationItem]
    query_type:       str                  # "semantic" | "keyword-heavy"
    response_time_ms: int
    bdc_alerts:       List[BDCAlertItem] = []  # non-empty when retrieved sections have BDC amendments


# ── Debug models ──────────────────────────────────────────────────────────────

class DebugChunkItem(BaseModel):
    """Metadata for a single retrieved chunk returned by ``POST /api/debug``."""

    chunk_id:      str
    collection:    str
    section_id:    str
    section_title: str
    doc:           str
    page_printed:  Optional[int]
    rrf_score:     float
    vector_rank:   Optional[int]   # 1-based rank in vector results; None if absent
    keyword_rank:  Optional[int]   # 1-based rank in keyword results; None if absent
    content_preview: str           # first 300 chars of chunk content (+ "..." if truncated)


class DebugResponse(BaseModel):
    """Body returned by ``POST /api/debug``."""

    query:              str
    query_type:         str    # "keyword-heavy" or "semantic"
    vector_weight:      float
    keyword_weight:     float
    bm25_cleaned_query: str    # the cleaned query passed to keyword_search_chunks
    retrieve_k:         int    # how many chunks were returned to the LLM
    chunks:             List[DebugChunkItem]
    answer:             Optional[str]   # LLM answer for quality comparison
    response_time_ms:   int


# ── Compliance checklist (app.compliance.eval_engine) ──────────────────────────

class EvaluationSchema(BaseModel):
    """Structured output shape enforced via ``.with_structured_output()`` for
    each individual compliance-check LLM call."""

    status:   Literal["Pass", "Fail", "Missing"]
    evidence: str   # verbatim extraction or exact metric found
    source:   str   # page number, document name, or Task ID


class ReviewCheckResult(BaseModel):
    """One evaluated check, with catalog identity attached to its EvaluationSchema result."""

    id:       str
    category: str
    name:     str
    status:   Literal["Pass", "Fail", "Missing"]
    evidence: str
    source:   str


class ReviewResponse(BaseModel):
    """Internal LangChain-native shape returned by ``app.compliance.eval_engine``.

    ``app.api.review`` maps this to the frontend's existing contract (lowercase
    ``pass``/``warning``/``fail`` status, ``reasoning``/``finding``/``evidence``
    fields) via ``_to_frontend_shape()`` so ``DocumentReview.tsx`` needs no changes.
    """

    project_name:           str
    project_duration_days:  int
    summary:                Dict[str, int]
    checks:                 List[ReviewCheckResult]
    manual_review_items:    List[str] = []
    model_used:             str
    # Neo4j projectId == review_projects.id (once saved) — see graph_neo4j's
    # multi-project isolation design. File paths are set only when the
    # caller was authenticated (backend-led Storage upload); None for an
    # anonymous review, which is never persisted.
    project_id:                    str
    schedule_file_path:            Optional[str] = None
    narrative_pdf_path:            Optional[str] = None
    special_provision_pdf_path:    Optional[str] = None
