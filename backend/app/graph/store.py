"""Per-session knowledge-graph persistence (Supabase JSONB blob).

The graph is serialized with ``networkx.node_link_data`` into a single JSONB
column — traversal always happens in-process with networkx, so the database
never needs to walk edges.  One upsert at ingestion, one select per query
session, plus a small in-process cache so a multi-question chat doesn't
re-fetch and re-deserialize on every request (same single-process assumption
as ``session.py``'s ``_progress`` dict).

Run ``scripts/migrate_session_graphs.sql`` once before using.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import networkx as nx

logger = logging.getLogger(__name__)

_TABLE = "session_graphs"

# session_id → (graph, cpm_summary); insertion-ordered, capped
_CACHE: Dict[str, Tuple[nx.MultiDiGraph, Dict[str, Any]]] = {}
_CACHE_MAX = 8


def _cache_put(session_id: str, graph: nx.MultiDiGraph, summary: Dict[str, Any]) -> None:
    _CACHE.pop(session_id, None)
    _CACHE[session_id] = (graph, summary)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))


def save_graph(
    db: Any,
    session_id: str,
    graph: nx.MultiDiGraph,
    cpm_summary: Optional[Dict[str, Any]] = None,
) -> None:
    """Upsert the session graph; raises on failure (caller decides fatality)."""
    payload = nx.node_link_data(graph, edges="links")
    summary = cpm_summary or graph.graph.get("cpm") or {}
    db.table(_TABLE).upsert(
        {
            "session_id": session_id,
            "graph": payload,
            "cpm_summary": summary,
        },
        on_conflict="session_id",
    ).execute()
    _cache_put(session_id, graph, summary)
    logger.info(
        "save_graph: session %s — %d nodes / %d edges persisted",
        session_id, graph.number_of_nodes(), graph.number_of_edges(),
    )


def load_graph(
    db: Any,
    session_id: str,
) -> Optional[Tuple[nx.MultiDiGraph, Dict[str, Any]]]:
    """Load a session graph, or None if absent/expired/unreadable."""
    cached = _CACHE.get(session_id)
    if cached is not None:
        return cached
    try:
        rows = (
            db.table(_TABLE)
            .select("graph, cpm_summary")
            .eq("session_id", session_id)
            .gt("expires_at", "now()")
            .limit(1)
            .execute()
            .data
        ) or []
        if not rows:
            return None
        graph = nx.node_link_graph(
            rows[0]["graph"], directed=True, multigraph=True, edges="links")
        summary = rows[0].get("cpm_summary") or {}
        _cache_put(session_id, graph, summary)
        return graph, summary
    except Exception as exc:
        logger.warning("load_graph: session %s failed — %s", session_id, exc)
        return None
