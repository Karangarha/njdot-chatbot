"""Composition of the three retrieval layers for a single compliance check.

    pin_by_anchors     -- exact metadata lookup for the section/table the
                           instruction names. Authoritative when it hits, so
                           these rows bypass ranking entirely.
    retrieve_sp_chunks -- dense (vector) search over the project's Special
                           Provision chunks (app.retrieval_langchain.sp_retriever).
    keyword_search_session_chunks -- BM25-style search, queried with
                           anchors.as_query() only (never the instruction --
                           see app.compliance.anchors for why).

fuse() (app.retrieval.hybrid_ranker) merges the dense and keyword legs with
weighted Reciprocal Rank Fusion. Pinned rows do NOT go through fuse: that
ranker deduplicates continuation chunks sharing a section_id, which is right
for chat (one hit per topic) and wrong here (a table spanning three chunks
needs all three).

    python -m pytest backend/tests/test_check_retrieval.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from app.compliance.anchors import Anchors, extract_anchors
from app.retrieval.hybrid_ranker import classify_query, fuse
from app.retrieval_langchain.sp_retriever import retrieve_sp_chunks

# Pinned rows are exact-match evidence, not ranked guesses -- but a section
# with many chunks must not crowd out the ranked half of the budget the check
# also needs (dense/keyword recall for anything the anchor regex missed).
PIN_BUDGET_FRACTION = 0.5

# Fetch more than `limit` pinned candidates before capping in Python, so a
# section that genuinely spans a handful of chunks isn't truncated by an
# unordered DB-side LIMIT before we sort them into reading order.
_PIN_OVERFETCH = 5


def project_has_section_metadata(db: Any, project_id: str, doc_type: str = "special_provision") -> bool:
    """Whether ANY of this project's chunks carry section_id metadata --
    the same question the in-process closure asks with
    ``any(... .get("section_id") ...)`` over its in-memory chunk list.

    Ingestion is normally uniform per project (a project either went through
    the section-aware chunker (Task 6) and every chunk of this doc_type has
    a section_id, or it was ingested before that and none do), but an
    unordered `.limit(1)` with no filter can still land on the one chunk
    that happens to lack section_id even when a sibling chunk has it,
    misreporting a real gap as "no metadata anywhere". Filtering server-side
    for a non-null section_id before capping to one row keeps this a cheap
    existence check rather than a scan of every chunk's content.
    """
    rows = (
        db.table("session_chunks").select("metadata")
        .eq("session_id", project_id).eq("doc_type", doc_type)
        .not_.is_("metadata->>section_id", "null")
        .limit(1).execute().data
    ) or []
    return bool(rows)


def pin_by_anchors(
    db: Any, project_id: str, anchors: Anchors, doc_type: str = "special_provision", limit: int = 0,
) -> List[Dict[str, Any]]:
    """Exact metadata lookup for the sections/tables the instruction names.

    Ordered by chunk_index so a multi-chunk table arrives in reading order --
    RRF rank order would scatter its pieces.
    """
    if anchors.is_empty or limit <= 0:
        return []

    conditions: List[str] = []
    if anchors.sections:
        quoted = ",".join(f'"{s}"' for s in anchors.sections)
        conditions.append(f"metadata->>section_id.in.({quoted})")
        # A check naming the parent section (e.g. "105.07") must also pin
        # chunks headed by its children ("105.07.01", "105.07.02") -- exact
        # match alone misses every document whose headings only go one level
        # deeper than the anchor. The boundary is a literal dot, so "105.0"
        # does NOT match "105.07" (that's a different section, not a parent):
        # PostgREST's "*" is its documented stand-in for the SQL LIKE "%"
        # wildcard. Section anchors only -- table anchors are excluded below,
        # a table caption is either the whole identifier or nothing. Same
        # rule, Python side, for callers with in-memory rows rather than a
        # filter to send: Anchors.matches_section.
        for section in anchors.sections:
            conditions.append(f"metadata->>section_id.like.{section}.*")
    for table in anchors.pinnable_tables:
        # jsonb array containment: does metadata.tables include this caption?
        # Bare single-letter captions ("TABLE A") are deliberately excluded
        # by pinnable_tables -- see Anchors.pinnable_tables. They're still
        # in anchors.as_query() for the keyword leg, where ranking (not an
        # unconditional position-0 pin) moderates an ambiguous match.
        conditions.append(f"metadata->tables.cs.{json.dumps([table])}")

    if not conditions:
        return []

    rows = (
        db.table("session_chunks").select("*")
        .eq("session_id", project_id).eq("doc_type", doc_type)
        .or_(",".join(conditions))
        .limit(limit * _PIN_OVERFETCH)
        .execute().data
    ) or []
    rows.sort(key=lambda r: (r.get("metadata") or {}).get("chunk_index") or 0)
    return rows[:limit]


@dataclass
class RetrievalResult:
    rows: List[Dict[str, Any]]
    pinned: int
    anchors: Anchors
    anchor_missing: bool


def retrieve_for_check(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
    instruction: str,
    top_k: int = 8,
    doc_type: str = "special_provision",
) -> RetrievalResult:
    """Pin anchored chunks, fuse dense+keyword search for the rest, dedupe."""
    anchors = extract_anchors(instruction)

    # int(top_k * PIN_BUDGET_FRACTION) truncates to 0 for top_k in {0, 1},
    # and pin_by_anchors's own limit<=0 guard then rejects the query -- a
    # genuine anchor match would be reported as anchor_missing, which is
    # budget starvation masquerading as the module's most important signal.
    # Whenever there's budget for any row at all, there's budget for one
    # pin; the cap still applies at normal budgets (top_k=8 -> 4, unchanged).
    pin_limit = max(1, int(top_k * PIN_BUDGET_FRACTION)) if top_k > 0 else 0
    pinned_rows = pin_by_anchors(db, project_id, anchors, doc_type, pin_limit)
    pinned_ids = {r.get("id") for r in pinned_rows}

    remaining = max(top_k - len(pinned_rows), 0)
    vector_rows: List[Dict[str, Any]] = []
    keyword_rows: List[Dict[str, Any]] = []
    # Weights come from the anchor query, not the instruction -- classify_query
    # looks for section numbers/codes, exactly what anchors.as_query() carries.
    v_weight, k_weight, _label = classify_query(anchors.as_query())

    if remaining > 0:
        vector_rows = retrieve_sp_chunks(
            db, embed_fn, project_id, instruction, match_count=remaining, doc_type=doc_type,
        )
        # The keyword leg is skipped entirely when there's no anchor: a
        # websearch_to_tsquery over an empty string matches nothing anyway,
        # and skipping avoids a pointless RPC round trip.
        if not anchors.is_empty:
            keyword_rows = (
                db.rpc(
                    "keyword_search_session_chunks",
                    {
                        "search_query": anchors.as_query(),
                        "p_session_id": project_id,
                        "p_doc_type": doc_type,
                        "match_count": remaining,
                    },
                ).execute().data
            ) or []

    # Fuse with an unbounded match_count -- dedupe against the pinned ids
    # below may drop the top-ranked row (it's already pinned), so we need
    # more than `remaining` candidates on hand to still fill the budget.
    fused = fuse(
        vector_rows, keyword_rows, v_weight, k_weight,
        match_count=len(vector_rows) + len(keyword_rows),
    )
    deduped_fused = [r for r in fused if r.get("id") not in pinned_ids][:remaining]

    rows = pinned_rows + deduped_fused

    # A missing anchor is only worth surfacing when the project *could* have
    # matched: it has section metadata at all and still came up empty. A
    # project with no metadata (pre-Task-6 ingestion) degrades silently --
    # pinning could never have worked there, so its absence is not evidence.
    # Also requires pin_limit > 0: at pin_limit == 0 pin_by_anchors never
    # even ran (its own limit<=0 guard rejects the query before touching the
    # database), so an empty pinned_rows there is budget starvation, not a
    # genuine gap -- the same two-case collapse PIN_BUDGET_FRACTION's own
    # top_k<=1 fix exists to prevent, one boundary further out.
    # Uses pinnable_is_empty, not is_empty: a check whose only anchor is a
    # bare single-letter table ("TABLE A") has nothing pin_by_anchors ever
    # attempts to pin (Anchors.pinnable_tables excludes it), so reporting a
    # gap there would flag a fill that was never attempted. That anchor
    # still reaches the keyword leg via anchors.as_query() -- only the
    # missing-anchor *signal* ignores it.
    anchor_missing = (
        pin_limit > 0
        and not anchors.pinnable_is_empty
        and not pinned_rows
        and project_has_section_metadata(db, project_id, doc_type)
    )

    return RetrievalResult(rows=rows, pinned=len(pinned_rows), anchors=anchors, anchor_missing=anchor_missing)
