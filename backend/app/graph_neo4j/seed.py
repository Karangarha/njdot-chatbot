"""Neo4j write path for the schedule/narrative knowledge graph.

Replaces ``app.graph.builder.build_graph`` + ``narrative_graph.add_narrative_to_graph``
+ ``store.save_graph`` — same source data and field mapping, Cypher ``UNWIND``/
``MERGE`` batch writes instead of networkx node/edge mutation + JSONB persistence.

Every node is tagged with ``projectId`` and every write ``MERGE``s on a
composite ``(projectId, <id>)`` key, so the graph holds many projects at
once, isolated from each other (multi-project design — see
``graph_neo4j.tools``'s Cypher fencing). Nothing here wipes existing data;
``clear_project`` still exists as a utility for an explicit "delete this
project's graph data" action, but ingestion no longer calls it automatically.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from langchain_neo4j import Neo4jGraph

from app.graph_neo4j.entities import extract_entities, link_entities, narrative_to_sections
from app.graph_neo4j.schema import ensure_constraints

logger = logging.getLogger(__name__)


def clear_project(graph: Neo4jGraph, project_id: str) -> None:
    """Delete all nodes tagged with this projectId (and their relationships)."""
    graph.query(
        "MATCH (n {projectId: $projectId}) DETACH DELETE n",
        params={"projectId": project_id},
    )
    logger.info("clear_project: cleared projectId=%s", project_id)


def _wbs_path_str(path_parts: List[str]) -> str:
    return " > ".join(path_parts)


def seed_schedule(
    graph: Neo4jGraph,
    activities: List[Dict[str, Any]],
    calendars: Optional[List[Dict[str, Any]]] = None,
    cpm: Any = None,                      # scheduling.cpm.CpmResult (duck-typed)
    crosscheck: Optional[Dict[str, Any]] = None,
    project: Optional[Dict[str, Any]] = None,
    project_id: str = "default",
) -> Dict[str, int]:
    """Seed the schedule side of the graph: Activity/WBS/Calendar/Constraint
    nodes and PART_OF/USES_CALENDAR/CONSTRAINED_BY/PRECEDES edges.

    ``cpm``/``crosscheck``/``project`` are optional so the graph can still be
    seeded when the CPM pass failed; activity nodes then simply lack the
    ``computed*`` properties.
    """
    ensure_constraints(graph)

    mismatched_ids = {m.get("activity_id") for m in (crosscheck or {}).get("mismatches", [])}
    open_starts = set(getattr(cpm, "open_starts", []) or [])
    open_ends = set(getattr(cpm, "open_ends", []) or [])

    # ── Calendars ────────────────────────────────────────────────────────────
    cal_rows = []
    for cal in calendars or []:
        cal_id = str(cal.get("id", ""))
        if not cal_id:
            continue
        cal_rows.append({
            "calendarId": cal_id,
            "name": cal.get("name", "Calendar"),
            "workDays": list(cal.get("work_days") or []),
            "hoursPerDay": cal.get("day_hr_cnt", 8.0),
            "exceptionCount": len(cal.get("exceptions") or []),
            "workExceptionCount": len(cal.get("work_exceptions") or []),
        })
    if cal_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MERGE (c:Calendar {calendarId: row.calendarId, projectId: $projectId}) "
            "SET c += row",
            params={"rows": cal_rows, "projectId": project_id},
        )

    # ── Activities ───────────────────────────────────────────────────────────
    act_rows = []
    wbs_paths_seen: Dict[str, int] = {}                    # path -> level
    wbs_parent: Dict[str, Optional[str]] = {}
    act_wbs_leaf: Dict[str, str] = {}

    for act in activities:
        aid = act.get("activity_id")
        if not aid:
            continue

        computed: Dict[str, Any] = {}
        if cpm is not None:
            r = cpm.activities.get(aid)
            if r is not None:
                cd = r.to_dict()
                computed = {
                    "computedEarlyStart": cd.get("es"),
                    "computedEarlyFinish": cd.get("ef"),
                    "computedLateStart": cd.get("ls"),
                    "computedLateFinish": cd.get("lf"),
                    "computedTotalFloat": cd.get("total_float"),
                    "computedFreeFloat": cd.get("free_float"),
                    "isCritical": cd.get("is_critical", False),
                    "onDrivingPath": cd.get("on_driving_path", False),
                    "excluded": cd.get("excluded", False),
                }

        props = {
            "taskId": aid,
            "name": act.get("activity_name", ""),
            "taskType": act.get("task_type", "Task Dependent"),
            "status": act.get("status", "Not Started"),
            "wbsPath": list(act.get("wbs_path") or []),
            "startDate": act.get("start_date"),
            "finishDate": act.get("finish_date"),
            "durationDays": act.get("duration_days", 0),
            "remainingDurationDays": act.get("remaining_duration_days", 0),
            "actualStart": act.get("actual_start"),
            "actualFinish": act.get("actual_finish"),
            "calendarId": str(act.get("calendar_id", "")),
            "storedTotalFloat": act.get("stored_total_float_days"),
            "storedFreeFloat": act.get("stored_free_float_days"),
            "isLongestPath": act.get("is_longest_path", False),
            "storedEarlyStart": act.get("early_start"),
            "storedEarlyFinish": act.get("early_finish"),
            "storedLateStart": act.get("late_start"),
            "storedLateFinish": act.get("late_finish"),
            "hasMismatch": aid in mismatched_ids,
            "isOpenStart": aid in open_starts,
            "isOpenEnd": aid in open_ends,
            **computed,
        }
        act_rows.append(props)

        path = list(act.get("wbs_path") or [])
        if path:
            for depth in range(1, len(path) + 1):
                node_path = _wbs_path_str(path[:depth])
                if node_path not in wbs_paths_seen:
                    wbs_paths_seen[node_path] = depth
                    wbs_parent[node_path] = (
                        _wbs_path_str(path[:depth - 1]) if depth > 1 else None
                    )
            act_wbs_leaf[aid] = _wbs_path_str(path)

    if act_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MERGE (a:Activity {taskId: row.taskId, projectId: $projectId}) "
            "SET a += row",
            params={"rows": act_rows, "projectId": project_id},
        )

    # ── WBS nodes + PART_OF chain ────────────────────────────────────────────
    wbs_rows = [
        {"path": path, "label": path.rsplit(" > ", 1)[-1], "level": level}
        for path, level in wbs_paths_seen.items()
    ]
    if wbs_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MERGE (w:WBS {path: row.path, projectId: $projectId}) "
            "SET w.label = row.label, w.level = row.level",
            params={"rows": wbs_rows, "projectId": project_id},
        )
    wbs_parent_rows = [
        {"path": path, "parent": parent}
        for path, parent in wbs_parent.items() if parent is not None
    ]
    if wbs_parent_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MATCH (w:WBS {path: row.path, projectId: $projectId}), "
            "      (p:WBS {path: row.parent, projectId: $projectId}) "
            "MERGE (w)-[:PART_OF]->(p)",
            params={"rows": wbs_parent_rows, "projectId": project_id},
        )
    act_wbs_rows = [{"taskId": aid, "path": path} for aid, path in act_wbs_leaf.items()]
    if act_wbs_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MATCH (a:Activity {taskId: row.taskId, projectId: $projectId}), "
            "      (w:WBS {path: row.path, projectId: $projectId}) "
            "MERGE (a)-[:PART_OF]->(w)",
            params={"rows": act_wbs_rows, "projectId": project_id},
        )

    # ── USES_CALENDAR edges ──────────────────────────────────────────────────
    cal_edge_rows = [
        {"taskId": act.get("activity_id"), "calendarId": str(act.get("calendar_id", ""))}
        for act in activities
        if act.get("activity_id") and str(act.get("calendar_id", ""))
    ]
    if cal_edge_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MATCH (a:Activity {taskId: row.taskId, projectId: $projectId}), "
            "      (c:Calendar {calendarId: row.calendarId, projectId: $projectId}) "
            "MERGE (a)-[:USES_CALENDAR]->(c)",
            params={"rows": cal_edge_rows, "projectId": project_id},
        )

    # ── Constraint nodes + CONSTRAINED_BY edges ─────────────────────────────
    cstr_rows = []
    cstr_edge_rows = []
    for act in activities:
        aid = act.get("activity_id")
        if not aid:
            continue
        cstr = act.get("constraints", {}) or {}
        for slot, (ctype, cdate) in enumerate(
            ((cstr.get("type"), cstr.get("date")),
             (cstr.get("type2"), cstr.get("date2"))), start=1):
            if ctype and ctype != "None":
                cid = f"cstr:{aid}:{slot}"
                cstr_rows.append({"constraintId": cid, "type": ctype, "date": cdate})
                cstr_edge_rows.append({"taskId": aid, "constraintId": cid})
    if cstr_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MERGE (c:Constraint {constraintId: row.constraintId, projectId: $projectId}) "
            "SET c.type = row.type, c.date = row.date",
            params={"rows": cstr_rows, "projectId": project_id},
        )
        graph.query(
            "UNWIND $rows AS row "
            "MATCH (a:Activity {taskId: row.taskId, projectId: $projectId}), "
            "      (c:Constraint {constraintId: row.constraintId, projectId: $projectId}) "
            "MERGE (a)-[:CONSTRAINED_BY]->(c)",
            params={"rows": cstr_edge_rows, "projectId": project_id},
        )

    # ── PRECEDES edges (with driving flags from critical chains) ────────────
    driving_pairs = set()
    if cpm is not None:
        for chain in cpm.critical_paths:
            driving_pairs.update(zip(chain, chain[1:]))

    act_ids = {act.get("activity_id") for act in activities if act.get("activity_id")}
    prec_rows = []
    for act in activities:
        aid = act.get("activity_id")
        for link in act.get("pred_links", []):
            pid = link.get("activity_id")
            if not pid or pid not in act_ids or aid not in act_ids:
                continue
            prec_rows.append({
                "predId": pid, "succId": aid,
                "relType": link.get("type", "FS"),
                "lagDays": link.get("lag_days", 0),
                "driving": (pid, aid) in driving_pairs,
            })
    if prec_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MATCH (p:Activity {taskId: row.predId, projectId: $projectId}), "
            "      (s:Activity {taskId: row.succId, projectId: $projectId}) "
            "MERGE (p)-[r:PRECEDES]->(s) "
            "SET r.relType = row.relType, r.lagDays = row.lagDays, r.driving = row.driving",
            params={"rows": prec_rows, "projectId": project_id},
        )

    # ── Project summary node (precomputed CPM values — not re-derived via Cypher) ──
    # ``criticalPaths`` is a list of lists (chains of activity IDs), which Neo4j
    # can't store as a native property (no nested lists) — kept as a JSON string
    # and parsed back in graph_neo4j/tools.py's get_critical_path.
    summary = cpm.summary_dict() if cpm is not None else {}
    duration_days = 0
    if cpm is not None and cpm.data_date and cpm.project_finish:
        duration_days = (cpm.project_finish - cpm.data_date).days
    project_props = {
        "projectId": project_id,
        "projectName": (project or {}).get("project_name") or (project or {}).get("project_code"),
        "mustFinishDate": (project or {}).get("must_finish_date"),
        "criticalPathsJson": json.dumps(summary.get("critical_paths", [])),
        "projectFinish": summary.get("project_finish"),
        "dataDate": summary.get("data_date"),
        "criticalCount": summary.get("critical_count"),
        "computedCount": summary.get("computed_count"),
        "warnings": summary.get("warnings", []),
        # Precomputed so a re-run can read the duration straight back without
        # re-parsing the XER / re-running CPM — see review.py's reseed=False path.
        "durationDays": duration_days,
    }
    graph.query(
        "MERGE (p:Project {projectId: $projectId}) SET p += $props",
        params={"projectId": project_id, "props": project_props},
    )

    stats = {
        "activities": len(act_rows),
        "calendars": len(cal_rows),
        "wbs_nodes": len(wbs_rows),
        "constraints": len(cstr_rows),
        "precedes_edges": len(prec_rows),
    }
    logger.info("seed_schedule: %s", stats)
    return stats


def seed_narrative(
    graph: Neo4jGraph,
    nar_chunks: List[Dict[str, Any]],
    embeddings: List[List[float]],
    activities: List[Dict[str, Any]],
    project_id: str = "default",
    llm: Any = None,
) -> Dict[str, int]:
    """Seed the narrative side of the graph: NarrativeChunk/NarrativeEntity
    nodes and FROM_CHUNK/RELATES_TO/MENTIONS edges.

    ``llm`` may be None (or extraction may fail) — chunks are still added so
    the narrative text always lives in the graph. ``embeddings`` must align
    1:1 with ``nar_chunks`` order (used for in-process cosine search, not a
    native Neo4j vector index — see Phase 0 decision).
    """
    sections = narrative_to_sections(nar_chunks)

    chunk_rows = []
    for i, s in enumerate(sections):
        chunk_rows.append({
            "id": s["id"],
            "text": s["text"],
            "heading": s["heading"] or "Narrative section",
            "pagePdf": s["page_pdf"],
            "embedding": embeddings[i] if i < len(embeddings) else None,
        })
    if chunk_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MERGE (c:NarrativeChunk {id: row.id, projectId: $projectId}) "
            "SET c += row",
            params={"rows": chunk_rows, "projectId": project_id},
        )

    entities: List[Dict[str, Any]] = []
    relations: List[Dict[str, Any]] = []
    if llm is not None and sections:
        entities, relations = extract_entities(sections, llm)

    ent_rows = [{
        "id": e["id"], "entityType": e["entity_type"], "text": e["text"],
        "quote": e["quote"], "dates": e["dates"],
    } for e in entities]
    if ent_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MERGE (e:NarrativeEntity {id: row.id, projectId: $projectId}) "
            "SET e += row",
            params={"rows": ent_rows, "projectId": project_id},
        )

    from_chunk_rows = [
        {"entityId": e["id"], "sectionId": e["section_id"]}
        for e in entities if e.get("section_id")
    ]
    if from_chunk_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MATCH (e:NarrativeEntity {id: row.entityId, projectId: $projectId}), "
            "      (c:NarrativeChunk {id: row.sectionId, projectId: $projectId}) "
            "MERGE (e)-[:FROM_CHUNK]->(c)",
            params={"rows": from_chunk_rows, "projectId": project_id},
        )

    rel_rows = [{"from": r["from"], "to": r["to"], "label": r["label"]} for r in relations]
    if rel_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MATCH (a:NarrativeEntity {id: row.from, projectId: $projectId}), "
            "      (b:NarrativeEntity {id: row.to, projectId: $projectId}) "
            "MERGE (a)-[rel:RELATES_TO]->(b) SET rel.label = row.label",
            params={"rows": rel_rows, "projectId": project_id},
        )

    mention_links = link_entities(entities, activities) if activities else []
    mention_rows = [
        {"entityId": eid, "taskId": aid, **attrs}
        for eid, aid, attrs in mention_links
    ]
    if mention_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MATCH (e:NarrativeEntity {id: row.entityId, projectId: $projectId}), "
            "      (a:Activity {taskId: row.taskId, projectId: $projectId}) "
            "MERGE (e)-[m:MENTIONS]->(a) "
            "SET m.matchMethod = row.match_method, m.confidence = row.confidence, "
            "    m.evidence = row.evidence",
            params={"rows": mention_rows, "projectId": project_id},
        )

    stats = {
        "chunks": len(chunk_rows),
        "entities": len(ent_rows),
        "relations": len(rel_rows),
        "mentions": len(mention_rows),
    }
    logger.info("seed_narrative: %s", stats)
    return stats


def seed_special_provision(
    graph: Neo4jGraph,
    chunks: List[Dict[str, Any]],
    vectors: List[List[float]],
    project_id: str = "default",
) -> Dict[str, int]:
    """Seed Special Provision chunks as SPChunk nodes, mirroring the
    NarrativeChunk half of ``seed_narrative``.

    Persisted once per project (instead of the old ephemeral in-process
    search) so a re-run can reuse these chunks/embeddings via
    ``graph_neo4j.tools.search_special_provision`` instead of re-parsing,
    re-chunking, and re-embedding the SP PDF on every call. ``chunks``/
    ``vectors`` are exactly what ``ingestion.session_chunker
    .chunk_special_provision()`` + ``embeddings.embed_documents()`` produce
    — must align 1:1 by index.
    """
    chunk_rows = [
        {
            "id": f"sp-{i}",
            "content": c.get("content", ""),
            "pagePdf": (c.get("metadata") or {}).get("page_pdf"),
            "embedding": vectors[i] if i < len(vectors) else None,
        }
        for i, c in enumerate(chunks)
    ]
    if chunk_rows:
        graph.query(
            "UNWIND $rows AS row "
            "MERGE (s:SPChunk {id: row.id, projectId: $projectId}) "
            "SET s += row",
            params={"rows": chunk_rows, "projectId": project_id},
        )

    stats = {"chunks": len(chunk_rows)}
    logger.info("seed_special_provision: %s", stats)
    return stats
