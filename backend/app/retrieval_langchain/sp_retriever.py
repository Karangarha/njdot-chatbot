"""Special Provisions retrieval — Supabase pgvector, session-scoped.

Wraps the existing ``match_session_chunks`` RPC (same call
``app.api.session.session_query`` already makes) as a LangChain ``Tool``,
rather than forcing a ``SupabaseVectorStore`` fit against a custom RPC
signature. The table this reads is ``session_chunks`` (``doc_type='special_provision'``).
Despite the name, rows written by review ingestion and the backfill script
default ``expires_at`` to 10 years out, not a short session TTL — confirmed
against the live column default (``now() + '10 years'::interval``); no
tracked migration file defines this table's DDL.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List

from langchain_core.tools import Tool

logger = logging.getLogger(__name__)

# Retrieval used to drop every chunk below 0.2 cosine. For chat that is a
# reasonable floor; for a compliance check it is not, because the query is a
# 150-word rule that embeds far from a 600-token clause even when that clause
# is the right one. Below the floor the check received no SP text at all and
# answered from its other sources, silently. Callers now choose.
_DEFAULT_MATCH_THRESHOLD = 0.0

# match_session_chunks ranks across every doc_type in the session, so a
# post-filter to one type throws rows away. Ask for enough that the survivors
# still fill the caller's budget.
_DOC_TYPE_OVERFETCH = 4


def retrieve_sp_chunks(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
    query: str,
    match_count: int = 8,
    match_threshold: float = _DEFAULT_MATCH_THRESHOLD,
    doc_type: str = "special_provision",
) -> List[Dict[str, Any]]:
    """Vector-search this project's Special Provision chunks.

    Returns raw RPC rows (``content``, ``doc_type``, ``metadata``,
    ``similarity``) -- the shape ``session_query`` already consumes.
    """
    embedding = embed_fn(query)
    rows = (
        db.rpc(
            "match_session_chunks",
            {
                "query_embedding": embedding,
                "p_session_id": project_id,
                "match_count": match_count * _DOC_TYPE_OVERFETCH,
                "match_threshold": match_threshold,
            },
        )
        .execute()
        .data
    ) or []
    return [r for r in rows if r.get("doc_type") == doc_type][:match_count]


def build_sp_tool(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
) -> Tool:
    """LangChain Tool wrapping ``retrieve_sp_chunks`` for a bound project."""

    def _tool(query: str) -> str:
        # Chat tool call: a weak match here is noise for the user, so keep
        # the historical 0.2 floor rather than the compliance-path default.
        rows = retrieve_sp_chunks(db, embed_fn, project_id, query, match_threshold=0.2)
        if not rows:
            return json.dumps({"chunks": [], "note": "No matching Special Provision text found."})
        chunks = [
            {
                "page_pdf": r.get("metadata", {}).get("page_pdf"),
                "similarity": round(r.get("similarity", 0.0), 3),
                "content": r.get("content", ""),
            }
            for r in rows
        ]
        return json.dumps({"chunks": chunks}, default=str)

    return Tool.from_function(
        func=_tool,
        name="search_special_provisions",
        description=(
            "Search the project's Special Provisions document for structural, "
            "legal, regulatory, and contact-related text (e.g. multi-year "
            "funding clauses, conflicts with nearby projects, permit "
            "conditions). Input: a natural-language question or keywords."
        ),
    )
