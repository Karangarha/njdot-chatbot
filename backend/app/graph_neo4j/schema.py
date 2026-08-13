"""Neo4j constraints for the schedule/narrative knowledge graph.

Idempotent — safe to call on every startup/seed.

Constraints are composite on ``(projectId, <id>)``, NOT the id alone. P6/XER
schedules conventionally reuse the same activity IDs across projects (M100 =
Advertisement, M950 = Contract Completion, cal1, etc. are near-universal
conventions) — a single-property uniqueness constraint on ``taskId`` means
``MERGE (a:Activity {taskId: 'M100'})`` for one project silently matches and
overwrites a DIFFERENT project's node with the same ID, leaking stale
properties across projects. Composite keys prevent that collision.
"""

from __future__ import annotations

import logging

from langchain_neo4j import Neo4jGraph

logger = logging.getLogger(__name__)

# Old single-property constraints (pre-composite-key fix) — dropped before
# recreating, since Neo4j won't let a constraint name be redefined in place.
_OLD_CONSTRAINT_NAMES = [
    "activity_id", "narrative_chunk_id", "wbs_path", "calendar_id",
]

_CONSTRAINTS = [
    "CREATE CONSTRAINT activity_id_per_project IF NOT EXISTS "
    "FOR (a:Activity) REQUIRE (a.projectId, a.taskId) IS UNIQUE",
    "CREATE CONSTRAINT narrative_chunk_id_per_project IF NOT EXISTS "
    "FOR (c:NarrativeChunk) REQUIRE (c.projectId, c.id) IS UNIQUE",
    "CREATE CONSTRAINT wbs_path_per_project IF NOT EXISTS "
    "FOR (w:WBS) REQUIRE (w.projectId, w.path) IS UNIQUE",
    "CREATE CONSTRAINT calendar_id_per_project IF NOT EXISTS "
    "FOR (c:Calendar) REQUIRE (c.projectId, c.calendarId) IS UNIQUE",
    "CREATE CONSTRAINT constraint_id_per_project IF NOT EXISTS "
    "FOR (c:Constraint) REQUIRE (c.projectId, c.constraintId) IS UNIQUE",
    "CREATE CONSTRAINT narrative_entity_id_per_project IF NOT EXISTS "
    "FOR (e:NarrativeEntity) REQUIRE (e.projectId, e.id) IS UNIQUE",
    "CREATE CONSTRAINT project_id IF NOT EXISTS "
    "FOR (p:Project) REQUIRE p.projectId IS UNIQUE",
]


def ensure_constraints(graph: Neo4jGraph) -> None:
    """Create the graph's uniqueness constraints if they don't already exist.

    Drops the old (buggy) single-property constraints first, if present.
    """
    for name in _OLD_CONSTRAINT_NAMES:
        graph.query(f"DROP CONSTRAINT {name} IF EXISTS")
    for stmt in _CONSTRAINTS:
        graph.query(stmt)
    logger.info("ensure_constraints: %d constraints verified", len(_CONSTRAINTS))
