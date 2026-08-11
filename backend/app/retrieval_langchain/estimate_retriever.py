"""Cost estimate retrieval — Supabase pgvector, session-scoped.

Mirror of ``sp_retriever``/``keymap_retriever`` for the project's DBE Goal
Memo. The indexed text is the vision transcription of page 1 (DBE Goal Memos
are scans with no text layer — see ``app.ingestion.estimate_extractor``),
copied into ``session_chunks`` (``doc_type='estimate'``) from the review's
Neo4j ``EstimateChunk`` nodes by ``app.api.session``'s reuse path.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List

from langchain_core.tools import Tool

logger = logging.getLogger(__name__)

_MATCH_THRESHOLD = 0.2


def retrieve_estimate_chunks(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
    query: str,
    match_count: int = 6,
) -> List[Dict[str, Any]]:
    """Vector-search this project's estimate chunks. Returns raw RPC rows
    (``content``, ``doc_type``, ``metadata``, ``similarity``) — same shape as
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
    return [r for r in rows if r.get("doc_type") == "estimate"]


def build_estimate_tool(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
) -> Tool:
    """LangChain Tool wrapping ``retrieve_estimate_chunks`` for a bound project."""

    def _tool(query: str) -> str:
        rows = retrieve_estimate_chunks(db, embed_fn, project_id, query)
        if not rows:
            return json.dumps({"chunks": [], "note": "No matching cost estimate text found."})
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
        name="search_estimate",
        description=(
            "Search the project's DBE Goal Memo / Engineer's Estimate: the "
            "total estimated construction cost, the DBE/ESBE goal percentage, "
            "and project identifiers (project name, municipality/county, "
            "Federal Project Number, NJDOT Job Number). Input: a "
            "natural-language question or keywords."
        ),
    )
