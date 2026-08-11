"""Neo4j-backed knowledge graph for schedule + narrative data.

Replaces the earlier ``app.graph`` (networkx + Supabase-JSONB) implementation.
See ``schema.py`` for constraints and ``seed.py`` for the write path.
"""

from app.graph_neo4j.digest import build_digest
from app.graph_neo4j.schema import ensure_constraints
from app.graph_neo4j.seed import (
    clear_project,
    seed_estimate,
    seed_key_map,
    seed_narrative,
    seed_schedule,
)
from app.graph_neo4j.tools import build_cypher_chain, build_tools, get_critical_path, search_narrative

__all__ = [
    "ensure_constraints",
    "clear_project",
    "seed_estimate",
    "seed_key_map",
    "seed_narrative",
    "seed_schedule",
    "build_cypher_chain",
    "build_tools",
    "get_critical_path",
    "search_narrative",
    "build_digest",
]
