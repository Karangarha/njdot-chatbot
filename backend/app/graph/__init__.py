"""Knowledge-graph layer: schedule + narrative as a typed graph (GraphRAG).

- ``builder``   — construct a networkx MultiDiGraph from CPM-computed schedule
                  data (and, later, narrative sections/entities)
- ``store``     — per-session persistence to Supabase (JSONB blob)
- ``narrative_graph`` — narrative sections + LLM-extracted entities (Phase 3)
- ``digest``    — compact NL digest for Q&A context (Phase 4)
- ``tools``     — graph query functions exposed to the LLM (Phase 4)
"""

from app.graph.builder import build_graph
from app.graph.narrative_graph import add_narrative_to_graph, narrative_links_chunk
from app.graph.store import load_graph, save_graph

__all__ = [
    "add_narrative_to_graph",
    "build_graph",
    "load_graph",
    "narrative_links_chunk",
    "save_graph",
]
