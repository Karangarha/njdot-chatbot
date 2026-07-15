"""Special Provisions retrieval — Supabase pgvector, session-scoped.

Wraps the existing ``match_session_chunks`` RPC (same call
``app.api.session.session_query`` already makes) as a LangChain ``Tool``,
rather than forcing a ``SupabaseVectorStore`` fit against a custom RPC
signature. See ``backend/scripts/migrate_session_chunks.sql`` for the table
this reads (``session_chunks``, ``doc_type='special_provision'``, 24h TTL).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List

from langchain_core.tools import Tool

logger = logging.getLogger(__name__)

_MATCH_THRESHOLD = 0.2


def retrieve_sp_chunks(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
    query: str,
    match_count: int = 8,
) -> List[Dict[str, Any]]:
    """Vector-search this project's Special Provision chunks.

    Returns raw RPC rows (``content``, ``doc_type``, ``metadata``, ``similarity``) —
    same shape ``session_query`` already consumes, so downstream formatting
    (source lists, citations) doesn't need to change in Phase 6.
    """
    embedding = embed_fn(query)
    rows = (
        db.rpc(
            "match_session_chunks",
            {
                "query_embedding": embedding,
                "p_session_id": project_id,
                "match_count": match_count,
                "match_threshold": _MATCH_THRESHOLD,
            },
        )
        .execute()
        .data
    ) or []
    return [r for r in rows if r.get("doc_type") == "special_provision"]


def build_sp_tool(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
) -> Tool:
    """LangChain Tool wrapping ``retrieve_sp_chunks`` for a bound project."""

    def _tool(query: str) -> str:
        rows = retrieve_sp_chunks(db, embed_fn, project_id, query)
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
