"""Utility Agreement Plan retrieval -- Supabase pgvector, session-scoped.

Mirror of ``estimate_retriever``/``keymap_retriever``. The indexed text is
``ingestion.utility_plan_extractor.render_utility_plan_facts()``'s output,
written into ``session_chunks`` (``doc_type='utility_plan'``) at upload time
-- see ``app.api.session``'s ``_process_session``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List

from langchain_core.tools import Tool

logger = logging.getLogger(__name__)

_MATCH_THRESHOLD = 0.2


def retrieve_utility_plan_chunks(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
    query: str,
    match_count: int = 6,
) -> List[Dict[str, Any]]:
    """Vector-search this session's utility plan chunks. Returns raw RPC rows
    (``content``, ``doc_type``, ``metadata``, ``similarity``) -- same shape as
    ``retrieve_sp_chunks``."""
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
    return [r for r in rows if r.get("doc_type") == "utility_plan"]


def build_utility_plan_tool(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
) -> Tool:
    """LangChain Tool wrapping ``retrieve_utility_plan_chunks`` for a bound project."""

    def _tool(query: str) -> str:
        rows = retrieve_utility_plan_chunks(db, embed_fn, project_id, query)
        if not rows:
            return json.dumps({"chunks": [], "note": "No matching utility agreement plan text found."})
        chunks = [
            {
                "utility_owner": r.get("metadata", {}).get("utility_owner"),
                "similarity": round(r.get("similarity", 0.0), 3),
                "content": r.get("content", ""),
            }
            for r in rows
        ]
        return json.dumps({"chunks": chunks}, default=str)

    return Tool.from_function(
        func=_tool,
        name="search_utility_plans",
        description=(
            "Search the project's Utility Agreement Plan sheets: utility "
            "owner/company, utility type (gas, water, sewer, electric, "
            "telecom), route/milepost/contract identifiers, and the "
            "allowable lane-closure hour schedule. Input: a natural-language "
            "question or keywords."
        ),
    )
