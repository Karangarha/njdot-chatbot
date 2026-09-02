"""Compact NL digest of the knowledge graph for Q&A context.

The digest is the "global summary" half of GraphRAG: a compact block injected
into every session query so the LLM always sees the project's shape — data
date, computed finish, critical path, milestones, phases, cross-check
findings — and can decide what to drill into with the graph tools. Ported
from the retired ``app.graph.digest`` (same content, Neo4j queries instead of
networkx node/edge iteration).
"""

from __future__ import annotations

from typing import Any, Dict, List

from langchain_neo4j import Neo4jGraph

_MAX_CHAIN_ROWS = 12
_MAX_MILESTONES = 20
_MAX_PHASES = 15
_MAX_NARRATIVE_LINKS = 12
_MAX_FINDINGS = 6


def build_digest(graph: Neo4jGraph, project_id: str = "default") -> str:
    """Render the always-injected schedule/narrative digest for a project."""
    lines: List[str] = []

    project_rows = graph.query(
        "MATCH (p:Project {projectId: $pid}) RETURN p LIMIT 1",
        params={"pid": project_id},
    )
    project: Dict[str, Any] = project_rows[0]["p"] if project_rows else {}

    # ── Header ────────────────────────────────────────────────────────────────
    name = project.get("projectName") or "Project"
    bits = [f"Schedule digest for {name}."]
    if project.get("dataDate"):
        bits.append(f"Data date: {project['dataDate']}.")
    if project.get("projectFinish"):
        bits.append(f"Computed project finish: {project['projectFinish']}.")
    if project.get("mustFinishDate"):
        bits.append(f"Must-finish date: {project['mustFinishDate']}.")
    if project.get("criticalCount") is not None:
        bits.append(f"{project['criticalCount']} of "
                    f"{project.get('computedCount', '?')} activities are "
                    f"critical (computed total float <= 0).")
    bits.append("All float/critical values were computed deterministically by "
                "a CPM engine, not by an LLM.")
    lines += [" ".join(bits), ""]

    import json
    chains: List[List[str]] = json.loads(project.get("criticalPathsJson") or "[]")

    # ── Critical path ─────────────────────────────────────────────────────────
    if chains:
        chain_ids = [aid for chain in chains for aid in chain]
        act_rows = graph.query(
            "UNWIND $ids AS tid MATCH (a:Activity {taskId: tid}) "
            "RETURN a.taskId AS taskId, a.name AS name, a.computedEarlyFinish AS ef",
            params={"ids": chain_ids},
        )
        by_id = {r["taskId"]: r for r in act_rows}
        lines.append("Critical path (computed):")
        for i, chain in enumerate(chains, 1):
            shown = chain[:_MAX_CHAIN_ROWS]
            parts = []
            for aid in shown:
                r = by_id.get(aid, {})
                parts.append(f"{aid} {r.get('name', '')} (finish {r.get('ef', '?')})")
            suffix = f" ... +{len(chain) - len(shown)} more" if len(chain) > len(shown) else ""
            lines.append(f"  Chain {i}: " + " -> ".join(parts) + suffix)
        lines.append("")

    # ── Milestones ────────────────────────────────────────────────────────────
    milestones = graph.query(
        "MATCH (a:Activity {projectId: $pid}) WHERE a.taskType CONTAINS 'Milestone' "
        "RETURN a.taskId AS taskId, a.name AS name, a.computedEarlyStart AS es, "
        "       a.startDate AS startDate, a.computedTotalFloat AS totalFloat, "
        "       a.isCritical AS isCritical "
        "ORDER BY coalesce(a.computedEarlyStart, a.startDate) LIMIT $limit",
        params={"pid": project_id, "limit": _MAX_MILESTONES},
    )
    if milestones:
        lines.append("Milestones (computed dates; Critical Yes/No):")
        for m in milestones:
            crit = "Yes" if m.get("isCritical") else "No"
            date = m.get("es") or m.get("startDate") or "?"
            lines.append(f"  {m['taskId']} {m.get('name', '')}: {date}"
                         f" (float {m.get('totalFloat', '?')}, critical {crit})")
        lines.append("")

    # ── Phases ────────────────────────────────────────────────────────────────
    phases = graph.query(
        "MATCH (a:Activity {projectId: $pid})-[:PART_OF]->(w:WBS) "
        "WHERE NOT (w)-[:PART_OF]->(:WBS) OR true "
        "WITH w.path AS phase, count(a) AS activityCount, "
        "     min(coalesce(a.computedEarlyStart, a.startDate)) AS startRange, "
        "     max(coalesce(a.computedEarlyFinish, a.finishDate)) AS endRange, "
        "     sum(CASE WHEN a.isCritical THEN 1 ELSE 0 END) AS criticalCount "
        "RETURN phase, activityCount, startRange, endRange, criticalCount "
        "ORDER BY phase LIMIT $limit",
        params={"pid": project_id, "limit": _MAX_PHASES},
    )
    if phases:
        lines.append("Phases (activity count, computed date range, critical count):")
        for p in phases:
            rng = (f"{p['startRange']} -> {p['endRange']}"
                   if p.get("startRange") and p.get("endRange") else "dates n/a")
            lines.append(f"  {p['phase']}: {p['activityCount']} activities, {rng}, "
                        f"{p['criticalCount']} critical")
        lines.append("")

    # ── Cross-check / logic findings ──────────────────────────────────────────
    warnings = project.get("warnings") or []
    mismatch_count_rows = graph.query(
        "MATCH (a:Activity {projectId: $pid}) WHERE a.hasMismatch = true RETURN count(a) AS n",
        params={"pid": project_id},
    )
    mismatch_count = mismatch_count_rows[0]["n"] if mismatch_count_rows else 0
    findings = list(warnings[:3])
    if mismatch_count:
        findings.append(f"{mismatch_count} activities have P6/CPM cross-check mismatches.")
    if findings:
        lines.append("Schedule findings (computed):")
        for f in findings[:_MAX_FINDINGS]:
            lines.append(f"  - {f}")
        lines.append("")

    # ── Narrative links ───────────────────────────────────────────────────────
    mentions = graph.query(
        "MATCH (e:NarrativeEntity {projectId: $pid})-[m:MENTIONS]->(a:Activity) "
        "WHERE m.confidence >= 0.7 "
        "RETURN e.entityType AS entityType, e.text AS text, "
        "       a.taskId AS taskId, a.name AS name, m.confidence AS confidence "
        "ORDER BY m.confidence DESC LIMIT $limit",
        params={"pid": project_id, "limit": _MAX_NARRATIVE_LINKS},
    )
    if mentions:
        lines.append("Narrative commitments/entities linked to schedule activities:")
        for m in mentions:
            lines.append(f"  - {m.get('entityType', 'Entity')} \"{m.get('text', '')}\""
                         f" -> {m['taskId']} {m.get('name', '')}")
        lines.append("")

    nar_count_rows = graph.query(
        "MATCH (c:NarrativeChunk {projectId: $pid}) RETURN count(c) AS n",
        params={"pid": project_id},
    )
    nar_count = nar_count_rows[0]["n"] if nar_count_rows else 0
    if nar_count:
        lines.append(f"The designer narrative ({nar_count} sections) is in the graph — "
                     f"use search_narrative for full text.")

    return "\n".join(lines).strip()
