"""Key map retrieval — Supabase pgvector, session-scoped.

Mirror of ``sp_retriever`` for the key map (key sheet) document: wraps the
``match_session_chunks`` RPC as a LangChain ``Tool`` filtered to
``doc_type='key_map'`` rows (seeded by ``app.api.session``'s reuse path from
the review's Neo4j ``KeyMapChunk`` nodes).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List

from langchain_core.tools import Tool

logger = logging.getLogger(__name__)

_MATCH_THRESHOLD = 0.2


def retrieve_keymap_chunks(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
    query: str,
    match_count: int = 6,
) -> List[Dict[str, Any]]:
    """Vector-search this project's key map chunks. Returns raw RPC rows
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
    return [r for r in rows if r.get("doc_type") == "key_map"]


def build_keymap_tool(
    db: Any,
    embed_fn: Callable[[str], List[float]],
    project_id: str,
) -> Tool:
    """LangChain Tool wrapping ``retrieve_keymap_chunks`` for a bound project."""

    def _tool(query: str) -> str:
        rows = retrieve_keymap_chunks(db, embed_fn, project_id, query)
        if not rows:
            return json.dumps({"chunks": [], "note": "No matching key map text found."})
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
        name="search_key_map",
        description=(
            "Search the project's key map (key sheet): utility "
            "companies/owners, project location (latitude/longitude, "
            "north/south of I-195), route and municipality, contract and "
            "federal project numbers, structure numbers, the index of "
            "sheets, and general plan notes. Input: a natural-language "
            "question or keywords."
        ),
    )
