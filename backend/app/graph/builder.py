"""Knowledge-graph construction from CPM-computed schedule data.

Builds a typed ``networkx.MultiDiGraph`` where every node has ``node_type``
and ``label`` attributes and all values are JSON-safe (dates as ISO strings)
so the graph round-trips through ``node_link_data`` → Supabase JSONB.

Node types
----------
- ``Activity``   — one per schedule activity; carries stored P6 values,
                   CPM-computed values (``computed`` dict), and mismatch flags.
                   Milestones are activities with a milestone ``task_type``.
- ``WBS``        — one per WBS path prefix; ``PART_OF`` chains give hierarchy.
- ``Calendar``   — one per P6 calendar.
- ``Constraint`` — one per applied activity constraint.
- (Phase 3 adds ``NarrativeSection`` and ``NarrativeEntity``.)

Edge types
----------
- ``PRECEDES``       Activity→Activity, attrs: rel_type FS/SS/FF/SF, lag_days,
                     driving (bool — consecutive on a computed critical chain)
- ``PART_OF``        Activity→WBS and WBS→parent WBS
- ``USES_CALENDAR``  Activity→Calendar
- ``CONSTRAINED_BY`` Activity→Constraint
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import networkx as nx

logger = logging.getLogger(__name__)


def _wbs_node_id(path_parts: List[str]) -> str:
    return "wbs:" + " > ".join(path_parts)


def build_graph(
    activities: List[Dict[str, Any]],
    calendars: Optional[List[Dict[str, Any]]] = None,
    cpm: Any = None,                      # scheduling.cpm.CpmResult (duck-typed)
    crosscheck: Optional[Dict[str, Any]] = None,
    project: Optional[Dict[str, Any]] = None,
) -> nx.MultiDiGraph:
    """Assemble the schedule knowledge graph.

    ``cpm``/``crosscheck``/``project`` are optional so a graph can still be
    built when the CPM pass failed; activity nodes then simply lack the
    ``computed`` block.
    """
    g = nx.MultiDiGraph()

    # Graph-level metadata
    g.graph["project"] = project or {}
    if crosscheck:
        g.graph["crosscheck_summary"] = crosscheck.get("summary", {})
        g.graph["crosscheck_assessment"] = crosscheck.get("assessment", [])
    if cpm is not None:
        g.graph["cpm"] = cpm.summary_dict()

    mismatched_ids = set()
    for m in (crosscheck or {}).get("mismatches", []):
        mismatched_ids.add(m.get("activity_id"))

    # ── Calendar nodes ────────────────────────────────────────────────────────
    for cal in calendars or []:
        cal_id = str(cal.get("id", ""))
        if not cal_id:
            continue
        g.add_node(
            f"cal:{cal_id}",
            node_type="Calendar",
            label=cal.get("name", "Calendar"),
            work_days=list(cal.get("work_days") or []),
            hours_per_day=cal.get("day_hr_cnt", 8.0),
            exception_count=len(cal.get("exceptions") or []),
            work_exception_count=len(cal.get("work_exceptions") or []),
        )

    # ── Activity + WBS + Constraint nodes ─────────────────────────────────────
    wbs_seen: set = set()
    for act in activities:
        aid = act.get("activity_id")
        if not aid:
            continue

        computed = None
        if cpm is not None:
            r = cpm.activities.get(aid)
            if r is not None:
                computed = r.to_dict()

        g.add_node(
            aid,
            node_type="Activity",
            label=act.get("activity_name", ""),
            task_type=act.get("task_type", "Task Dependent"),
            status=act.get("status", "Not Started"),
            wbs_path=list(act.get("wbs_path") or []),
            start_date=act.get("start_date"),
            finish_date=act.get("finish_date"),
            duration_days=act.get("duration_days", 0),
            remaining_duration_days=act.get("remaining_duration_days", 0),
            actual_start=act.get("actual_start"),
            actual_finish=act.get("actual_finish"),
            calendar_id=str(act.get("calendar_id", "")),
            stored={
                "total_float_days": act.get("stored_total_float_days"),
                "free_float_days": act.get("stored_free_float_days"),
                "is_longest_path": act.get("is_longest_path", False),
                "early_start": act.get("early_start"),
                "early_finish": act.get("early_finish"),
                "late_start": act.get("late_start"),
                "late_finish": act.get("late_finish"),
            },
            computed=computed,
            has_mismatch=aid in mismatched_ids,
        )

        # WBS chain
        path = list(act.get("wbs_path") or [])
        if path:
            leaf_id = _wbs_node_id(path)
            for depth in range(1, len(path) + 1):
                node_id = _wbs_node_id(path[:depth])
                if node_id not in wbs_seen:
                    wbs_seen.add(node_id)
                    g.add_node(node_id, node_type="WBS",
                               label=path[depth - 1], level=depth)
                if depth > 1:
                    parent_id = _wbs_node_id(path[:depth - 1])
                    if not g.has_edge(node_id, parent_id):
                        g.add_edge(node_id, parent_id, edge_type="PART_OF")
            g.add_edge(aid, leaf_id, edge_type="PART_OF")

        # Calendar edge
        cal_node = f"cal:{act.get('calendar_id', '')}"
        if g.has_node(cal_node):
            g.add_edge(aid, cal_node, edge_type="USES_CALENDAR")

        # Constraint nodes
        cstr = act.get("constraints", {}) or {}
        for slot, (ctype, cdate) in enumerate(
            ((cstr.get("type"), cstr.get("date")),
             (cstr.get("type2"), cstr.get("date2"))), start=1):
            if ctype and ctype != "None":
                cid = f"cstr:{aid}:{slot}"
                g.add_node(cid, node_type="Constraint",
                           label=ctype, cstr_type=ctype, date=cdate)
                g.add_edge(aid, cid, edge_type="CONSTRAINED_BY")

    # ── PRECEDES edges (with driving flags from critical chains) ─────────────
    driving_pairs = set()
    if cpm is not None:
        for chain in cpm.critical_paths:
            driving_pairs.update(zip(chain, chain[1:]))

    for act in activities:
        aid = act.get("activity_id")
        for link in act.get("pred_links", []):
            pid = link.get("activity_id")
            if not pid or not g.has_node(pid) or not g.has_node(aid):
                continue
            g.add_edge(
                pid, aid,
                edge_type="PRECEDES",
                rel_type=link.get("type", "FS"),
                lag_days=link.get("lag_days", 0),
                driving=(pid, aid) in driving_pairs,
            )

    logger.info(
        "build_graph: %d nodes (%d activities), %d edges",
        g.number_of_nodes(),
        sum(1 for _, d in g.nodes(data=True) if d.get("node_type") == "Activity"),
        g.number_of_edges(),
    )
    return g
