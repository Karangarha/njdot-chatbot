"""Graph query tools exposed to the LLM during session Q&A.

Pure functions over ``(graph, cpm_summary)`` returning JSON-safe dicts with
capped list sizes, plus provider-neutral ``ToolSpec`` definitions that
``generation.tool_loop`` renders into OpenAI function-calling or Anthropic
tool-use payloads.

These are the "local traversal" half of GraphRAG: the digest gives the LLM
the project's shape; these tools let it drill into logic chains, activity
detail, and narrative text on demand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import networkx as nx

_MAX_ROWS = 25
_MAX_PATHS = 3
_MAX_PATH_LEN = 30
_SNIPPET_CHARS = 500


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    fn: Callable[..., Dict[str, Any]] = field(repr=False, default=None)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _act_row(g: nx.MultiDiGraph, aid: str) -> Dict[str, Any]:
    d = g.nodes[aid]
    c = d.get("computed") or {}
    return {
        "activity_id": aid,
        "name": d.get("label", ""),
        "task_type": d.get("task_type"),
        "status": d.get("status"),
        "wbs_path": " > ".join(d.get("wbs_path") or []),
        "start": c.get("es") or d.get("start_date"),
        "finish": c.get("ef") or d.get("finish_date"),
        "total_float": c.get("total_float"),
        "is_critical": bool(c.get("is_critical")),
    }


def _is_activity(g: nx.MultiDiGraph, n: str) -> bool:
    return g.has_node(n) and g.nodes[n].get("node_type") == "Activity"


def _precedes_subgraph(g: nx.MultiDiGraph) -> nx.DiGraph:
    sub = nx.DiGraph()
    for u, v, d in g.edges(data=True):
        if d.get("edge_type") == "PRECEDES":
            sub.add_edge(u, v)
    return sub


# ── Tool functions ─────────────────────────────────────────────────────────────

def get_activity(g, summary, activity_id: str) -> Dict[str, Any]:
    """Full detail for one activity: dates, float, links, constraints, narrative."""
    if not _is_activity(g, activity_id):
        return {"error": f"Activity '{activity_id}' not found"}
    d = g.nodes[activity_id]
    row = _act_row(g, activity_id)
    c = d.get("computed") or {}
    row.update({
        "duration_days": d.get("duration_days"),
        "computed": {"early_start": c.get("es"), "early_finish": c.get("ef"),
                     "late_start": c.get("ls"), "late_finish": c.get("lf"),
                     "total_float": c.get("total_float"),
                     "free_float": c.get("free_float"),
                     "on_driving_path": c.get("on_driving_path")},
        "p6_stored": d.get("stored"),
        "has_mismatch": d.get("has_mismatch", False),
        "actual_start": d.get("actual_start"),
        "actual_finish": d.get("actual_finish"),
    })
    preds, succs, constraints, calendar = [], [], [], None
    for u, v, ed in g.in_edges(activity_id, data=True):
        if ed.get("edge_type") == "PRECEDES":
            preds.append({"activity_id": u, "name": g.nodes[u].get("label", ""),
                          "rel_type": ed.get("rel_type"), "lag_days": ed.get("lag_days", 0)})
    for u, v, ed in g.out_edges(activity_id, data=True):
        et = ed.get("edge_type")
        if et == "PRECEDES":
            succs.append({"activity_id": v, "name": g.nodes[v].get("label", ""),
                          "rel_type": ed.get("rel_type"), "lag_days": ed.get("lag_days", 0)})
        elif et == "CONSTRAINED_BY":
            cn = g.nodes[v]
            constraints.append({"type": cn.get("cstr_type"), "date": cn.get("date")})
        elif et == "USES_CALENDAR":
            calendar = g.nodes[v].get("label")
    row["predecessors"] = preds[:_MAX_ROWS]
    row["successors"] = succs[:_MAX_ROWS]
    row["constraints"] = constraints
    row["calendar"] = calendar
    row["narrative_links"] = narrative_links(g, summary, activity_id).get("links", [])
    return row


def get_critical_path(g, summary) -> Dict[str, Any]:
    """The computed critical path chain(s) with per-activity dates and float."""
    chains = []
    for chain in (summary.get("critical_paths") or []):
        chains.append([_act_row(g, aid) for aid in chain[:_MAX_PATH_LEN]
                       if _is_activity(g, aid)])
    return {
        "project_finish": summary.get("project_finish"),
        "data_date": summary.get("data_date"),
        "critical_count": summary.get("critical_count"),
        "chains": chains,
        "note": "Computed deterministically by the CPM engine (total float ≤ 0).",
    }


def _neighbors(g, summary, activity_id: str, depth: int, direction: str) -> Dict[str, Any]:
    if not _is_activity(g, activity_id):
        return {"error": f"Activity '{activity_id}' not found"}
    depth = max(1, min(int(depth or 1), 5))
    rows: List[Dict[str, Any]] = []
    seen = {activity_id}
    frontier = [activity_id]
    for level in range(1, depth + 1):
        nxt: List[str] = []
        for node in frontier:
            edge_iter = (g.in_edges(node, data=True) if direction == "pred"
                         else g.out_edges(node, data=True))
            for u, v, ed in edge_iter:
                if ed.get("edge_type") != "PRECEDES":
                    continue
                other = u if direction == "pred" else v
                if other in seen:
                    continue
                seen.add(other)
                nxt.append(other)
                row = _act_row(g, other)
                row.update({"level": level, "rel_type": ed.get("rel_type"),
                            "lag_days": ed.get("lag_days", 0),
                            "via": node})
                rows.append(row)
                if len(rows) >= _MAX_ROWS:
                    return {"activity_id": activity_id, "direction": direction,
                            "depth": depth, "truncated": True, "activities": rows}
        frontier = nxt
    return {"activity_id": activity_id, "direction": direction, "depth": depth,
            "activities": rows}


def predecessors(g, summary, activity_id: str, depth: int = 1) -> Dict[str, Any]:
    """Predecessor activities (what must happen before), up to `depth` hops back."""
    return _neighbors(g, summary, activity_id, depth, "pred")


def successors(g, summary, activity_id: str, depth: int = 1) -> Dict[str, Any]:
    """Successor activities (what this drives), up to `depth` hops forward."""
    return _neighbors(g, summary, activity_id, depth, "succ")


def trace_path(g, summary, from_id: str, to_id: str) -> Dict[str, Any]:
    """Logic path(s) from one activity to another through PRECEDES edges."""
    for aid in (from_id, to_id):
        if not _is_activity(g, aid):
            return {"error": f"Activity '{aid}' not found"}
    sub = _precedes_subgraph(g)
    if from_id not in sub or to_id not in sub:
        return {"paths": [], "note": "No relationships recorded for one endpoint."}
    paths = []
    try:
        for path in nx.all_simple_paths(sub, from_id, to_id, cutoff=_MAX_PATH_LEN):
            paths.append([_act_row(g, aid) for aid in path])
            if len(paths) >= _MAX_PATHS:
                break
    except nx.NetworkXNoPath:
        pass
    if not paths:
        return {"paths": [],
                "note": f"No forward logic path from {from_id} to {to_id}; "
                        f"try the reverse direction."}
    return {"from": from_id, "to": to_id, "paths": paths}


def why_critical(g, summary, activity_id: str) -> Dict[str, Any]:
    """Explain an activity's criticality: float, driving chain back to a start, constraints."""
    if not _is_activity(g, activity_id):
        return {"error": f"Activity '{activity_id}' not found"}
    detail = _act_row(g, activity_id)
    d = g.nodes[activity_id]
    c = d.get("computed") or {}
    result: Dict[str, Any] = {"activity": detail}

    if not c:
        result["note"] = "No computed CPM data for this activity."
        return result
    if not c.get("is_critical"):
        result["note"] = (f"{activity_id} is NOT critical: computed total float is "
                          f"{c.get('total_float')} working days — it can slip that "
                          f"much before delaying the project finish.")
        return result

    # Walk driving PRECEDES edges backwards to an open start
    chain = [activity_id]
    cur = activity_id
    for _ in range(_MAX_PATH_LEN):
        step = None
        for u, v, ed in g.in_edges(cur, data=True):
            if ed.get("edge_type") == "PRECEDES" and ed.get("driving"):
                step = u
                break
        if step is None or step in chain:
            break
        chain.append(step)
        cur = step
    chain.reverse()
    result["driving_chain"] = [_act_row(g, aid) for aid in chain]

    constraints = [{"type": g.nodes[v].get("cstr_type"), "date": g.nodes[v].get("date")}
                   for _, v, ed in g.out_edges(activity_id, data=True)
                   if ed.get("edge_type") == "CONSTRAINED_BY"]
    if constraints:
        result["constraints"] = constraints
    for _, v, ed in g.out_edges(activity_id, data=True):
        if ed.get("edge_type") == "USES_CALENDAR":
            cal = g.nodes[v]
            result["calendar"] = {"name": cal.get("label"),
                                  "work_days": cal.get("work_days")}
    result["note"] = (f"{activity_id} has computed total float "
                      f"{c.get('total_float')} ≤ 0: any delay directly delays the "
                      f"project finish ({summary.get('project_finish')}).")
    return result


def find_activities(g, summary, name_contains: str = None, wbs_contains: str = None,
                    critical: bool = None, status: str = None,
                    float_lte: int = None, limit: int = _MAX_ROWS) -> Dict[str, Any]:
    """Filter activities by name/WBS substring, criticality, status, or max float."""
    limit = max(1, min(int(limit or _MAX_ROWS), _MAX_ROWS))
    name_q = (name_contains or "").lower()
    wbs_q = (wbs_contains or "").lower()
    rows = []
    for n, d in g.nodes(data=True):
        if d.get("node_type") != "Activity":
            continue
        if name_q and name_q not in (d.get("label", "") + " " + n).lower():
            continue
        if wbs_q and wbs_q not in " > ".join(d.get("wbs_path") or []).lower():
            continue
        if status and (d.get("status", "").lower() != status.lower()):
            continue
        c = d.get("computed") or {}
        if critical is not None and bool(c.get("is_critical")) != bool(critical):
            continue
        if float_lte is not None:
            tf = c.get("total_float")
            if tf is None or tf > int(float_lte):
                continue
        rows.append(_act_row(g, n))
        if len(rows) >= limit:
            break
    return {"count": len(rows), "activities": rows,
            "truncated": len(rows) >= limit}


def narrative_links(g, summary, activity_id: str) -> Dict[str, Any]:
    """Narrative entities (commitments, permits, deadlines…) linked to an activity."""
    if not _is_activity(g, activity_id):
        return {"error": f"Activity '{activity_id}' not found"}
    links = []
    for u, v, ed in g.in_edges(activity_id, data=True):
        if ed.get("edge_type") != "MENTIONS":
            continue
        ent = g.nodes[u]
        section_id = next((s for _, s, sd in g.out_edges(u, data=True)
                           if sd.get("edge_type") == "FROM_SECTION"), None)
        links.append({
            "entity_type": ent.get("entity_type"),
            "text": ent.get("text"),
            "quote": ent.get("quote"),
            "dates": ent.get("dates"),
            "match_method": ed.get("match_method"),
            "confidence": ed.get("confidence"),
            "narrative_section": section_id,
        })
    return {"activity_id": activity_id, "links": links[:_MAX_ROWS]}


def get_narrative_section(g, summary, section_id: str) -> Dict[str, Any]:
    """Full text of one narrative section (id like 'nsec:3')."""
    if not g.has_node(section_id) or \
            g.nodes[section_id].get("node_type") != "NarrativeSection":
        return {"error": f"Narrative section '{section_id}' not found"}
    d = g.nodes[section_id]
    return {"section_id": section_id, "heading": d.get("heading"),
            "page_pdf": d.get("page_pdf"), "text": d.get("text", "")}


def search_narrative(g, summary, keywords: str) -> Dict[str, Any]:
    """Keyword search over the designer narrative's section texts."""
    terms = [t for t in re.split(r"\W+", (keywords or "").lower()) if len(t) >= 3]
    if not terms:
        return {"error": "Provide at least one keyword of 3+ characters"}
    scored = []
    for n, d in g.nodes(data=True):
        if d.get("node_type") != "NarrativeSection":
            continue
        text = (d.get("heading", "") + "\n" + d.get("text", "")).lower()
        score = sum(text.count(t) for t in terms)
        if score > 0:
            scored.append((score, n, d))
    scored.sort(key=lambda x: -x[0])
    results = []
    for score, n, d in scored[:8]:
        text = d.get("text", "")
        low = text.lower()
        pos = min((low.find(t) for t in terms if low.find(t) >= 0), default=0)
        start = max(0, pos - 100)
        snippet = text[start:start + _SNIPPET_CHARS]
        results.append({"section_id": n, "heading": d.get("heading"),
                        "page_pdf": d.get("page_pdf"), "score": score,
                        "snippet": snippet})
    return {"query": keywords, "sections": results,
            "note": "Use get_narrative_section for the full text of a section."}


# ── Tool specs (provider-neutral) ──────────────────────────────────────────────

def _obj(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}

_ID = {"type": "string", "description": "Schedule activity ID, e.g. 'A1000' or 'M950'"}

GRAPH_TOOLS: List[ToolSpec] = [
    ToolSpec("get_activity",
             "Full detail for one schedule activity: computed dates/float, P6 stored "
             "values, predecessors, successors, constraints, calendar, narrative links.",
             _obj({"activity_id": _ID}, ["activity_id"]), get_activity),
    ToolSpec("get_critical_path",
             "The project's computed critical path chain(s) with dates and float, "
             "plus project finish and data date.",
             _obj({}, []), get_critical_path),
    ToolSpec("predecessors",
             "Predecessor activities of an activity (what must happen first), "
             "with relationship types and lags. depth 1-5 hops.",
             _obj({"activity_id": _ID,
                   "depth": {"type": "integer", "minimum": 1, "maximum": 5}},
                  ["activity_id"]), predecessors),
    ToolSpec("successors",
             "Successor activities of an activity (what it drives), with "
             "relationship types and lags. depth 1-5 hops.",
             _obj({"activity_id": _ID,
                   "depth": {"type": "integer", "minimum": 1, "maximum": 5}},
                  ["activity_id"]), successors),
    ToolSpec("trace_path",
             "Logic path(s) between two activities through schedule relationships.",
             _obj({"from_id": _ID, "to_id": _ID}, ["from_id", "to_id"]), trace_path),
    ToolSpec("why_critical",
             "Explain whether/why an activity is on the critical path: computed "
             "float, the driving chain back to a start, constraints, calendar.",
             _obj({"activity_id": _ID}, ["activity_id"]), why_critical),
    ToolSpec("find_activities",
             "Filter activities by name substring, WBS/phase substring, critical "
             "flag, status ('Not Started'|'In Progress'|'Complete'), or max total "
             "float. Returns up to 25 rows.",
             _obj({"name_contains": {"type": "string"},
                   "wbs_contains": {"type": "string"},
                   "critical": {"type": "boolean"},
                   "status": {"type": "string"},
                   "float_lte": {"type": "integer"},
                   "limit": {"type": "integer"}}, []), find_activities),
    ToolSpec("narrative_links",
             "Designer-narrative entities (commitments, permits, utility work, "
             "deadlines, restrictions) linked to a schedule activity, with quotes.",
             _obj({"activity_id": _ID}, ["activity_id"]), narrative_links),
    ToolSpec("get_narrative_section",
             "Full text of one designer-narrative section by id (e.g. 'nsec:3').",
             _obj({"section_id": {"type": "string"}}, ["section_id"]),
             get_narrative_section),
    ToolSpec("search_narrative",
             "Keyword search across the designer narrative's full text; returns "
             "matching sections with snippets.",
             _obj({"keywords": {"type": "string"}}, ["keywords"]), search_narrative),
]


def bind_graph_tools(graph: nx.MultiDiGraph, summary: Dict[str, Any]) -> List[ToolSpec]:
    """Return GRAPH_TOOLS with fns bound to a concrete (graph, summary)."""
    bound = []
    for spec in GRAPH_TOOLS:
        fn = spec.fn

        def _wrapped(fn=fn, **kwargs):
            return fn(graph, summary, **kwargs)

        bound.append(ToolSpec(spec.name, spec.description, spec.parameters, _wrapped))
    return bound
