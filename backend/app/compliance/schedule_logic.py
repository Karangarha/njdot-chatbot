"""Deterministic CSM Section 3.0 schedule-logic checks -- negative float,
prohibited lag, open ends, mandatory constraints -- computed straight from
the Neo4j graph, no LLM call.

All four read data that ``app.scheduling.cpm.run_cpm`` already computed at
ingest time (``computedTotalFloat``, ``isOpenStart``/``isOpenEnd``,
``PRECEDES.relType``/``lagDays``, ``CONSTRAINED_BY`` -> ``Constraint.type``)
and seeded onto the graph in ``app.graph_neo4j.seed`` -- this module only
reads it back and applies the pass/fail rule, the same "precompute once,
judge in Python" split ``app.compliance.cost``/``edq`` already use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from app.scheduling.cpm import _MANDATORY_FINISH, _MANDATORY_START

_MAX_ROWS = 40

# NJDOT catalog convention (see app.compliance.catalog): every built-in
# schedule uses these fixed milestone ids for the project's own start and
# finish. CSM Section 3.0 exempts them from the open-ends rule by name.
EXEMPT_MILESTONE_IDS = {"M100", "M950"}


@dataclass
class ScheduleLogicResult:
    violations: List[Dict[str, Any]]
    detail: str
    # False when the CPM engine never produced computed values for this
    # schedule -- the check can't be evaluated and must report Missing, not
    # the Pass an empty violation list would otherwise imply.
    cpm_ran: bool = True

    def as_dict(self) -> dict:
        return {"violations": self.violations, "detail": self.detail, "cpm_ran": self.cpm_ran}


def _result(
    violations: List[Dict[str, Any]],
    empty_detail: str,
    fmt: Callable[[Dict[str, Any]], str],
) -> ScheduleLogicResult:
    if not violations:
        return ScheduleLogicResult(violations=[], detail=empty_detail)
    shown = violations[:_MAX_ROWS]
    text = "; ".join(fmt(v) for v in shown)
    if len(violations) > _MAX_ROWS:
        text += f"; and {len(violations) - _MAX_ROWS} more"
    return ScheduleLogicResult(
        violations=violations, detail=f"{len(violations)} violation(s): {text}."
    )


def evaluate_no_negative_float(graph: Any, project_id: str) -> ScheduleLogicResult:
    rows = graph.query(
        "MATCH (a:Activity {projectId: $pid}) WHERE a.computedTotalFloat < 0 "
        "RETURN a.taskId AS id, a.name AS name, a.computedTotalFloat AS totalFloat "
        "ORDER BY a.computedTotalFloat",
        params={"pid": project_id},
    ) or []
    return _result(
        rows,
        "No activities with negative float.",
        lambda r: f"{r['id']} ({r['name']}): total_float={r['totalFloat']}",
    )


def evaluate_no_mandatory_constraints(graph: Any, project_id: str) -> ScheduleLogicResult:
    # No LIMIT: the mandatory-type filter below runs in Python, so a fetch cap
    # would let 40 ordinary constraints crowd out a real CS_MANDSTART (same
    # truncation bug evaluate_no_lag documents). _result() caps the display.
    rows = graph.query(
        "MATCH (a:Activity {projectId: $pid})-[:CONSTRAINED_BY]->(c:Constraint) "
        "RETURN a.taskId AS id, c.type AS type, c.date AS date",
        params={"pid": project_id},
    ) or []
    mandatory_types = _MANDATORY_START | _MANDATORY_FINISH
    violations = [r for r in rows if r.get("type") in mandatory_types]
    return _result(
        violations,
        "No mandatory (Mandatory Start / Mandatory Finish) constraints present.",
        lambda r: f"{r['id']}: {r['type']} on {r['date']}",
    )


def evaluate_no_lag(graph: Any, project_id: str) -> ScheduleLogicResult:
    # Unfiltered -- CSM's rule depends on relType, so every relationship
    # (not just the already-lagged ones) has to be inspected to classify it.
    # No LIMIT here: this is a classification pass over the whole graph, not
    # a display list -- a real schedule has several relationships per
    # activity (P6 allows SS+FF+FS between one pair), so a 136-activity
    # project can easily carry 240+ PRECEDES edges. An earlier LIMIT of
    # _MAX_ROWS*4=160 silently truncated below that on the Route 49 fixture
    # and dropped one real violation (D1220 -> D1250) from the result --
    # confirmed by re-querying without a limit and getting all 243 edges.
    # _result() below still caps how many violations are ever *displayed*.
    rows = graph.query(
        "MATCH (p:Activity {projectId: $pid})-[r:PRECEDES]->(s:Activity {projectId: $pid}) "
        "RETURN p.taskId AS pred, s.taskId AS succ, r.relType AS relType, "
        "       coalesce(r.lagDays, 0) AS lagDays",
        params={"pid": project_id},
    ) or []
    # Lag is barred outright on Finish-to-Start; on any other type, only a
    # negative lag is barred (a positive SS/FF lag is ordinary schedule logic).
    violations = [
        r for r in rows
        if (r.get("relType") == "FS" and r["lagDays"] != 0) or r["lagDays"] < 0
    ]
    return _result(
        violations,
        "No prohibited lag (any Finish-to-Start lag, or negative lag on any relationship type).",
        lambda r: f"{r['pred']} -> {r['succ']} ({r.get('relType') or '?'}): lag={r['lagDays']} days",
    )


def evaluate_no_open_ends(graph: Any, project_id: str) -> ScheduleLogicResult:
    rows = graph.query(
        "MATCH (a:Activity {projectId: $pid}) "
        "WHERE a.isOpenStart = true OR a.isOpenEnd = true "
        "RETURN a.taskId AS id, a.name AS name, "
        "       a.isOpenStart AS isOpenStart, a.isOpenEnd AS isOpenEnd",
        params={"pid": project_id},
    ) or []
    violations = [r for r in rows if r["id"] not in EXEMPT_MILESTONE_IDS]
    return _result(
        violations,
        "No open-ended activities (excluding the project start/finish "
        "milestones, exempt per CSM Section 3.0).",
        lambda r: f"{r['id']} ({r['name']}): open_start={r['isOpenStart']}, open_end={r['isOpenEnd']}",
    )


# check_key -> evaluator, one entry per schedule_logic-typed check in the catalog.
_EVALUATORS: Dict[str, Callable[[Any, str], ScheduleLogicResult]] = {
    "no_negative_float": evaluate_no_negative_float,
    "no_mandatory_constraints": evaluate_no_mandatory_constraints,
    "no_lag": evaluate_no_lag,
    "no_open_ends": evaluate_no_open_ends,
}


# Checks that read values only run_cpm produces (computedTotalFloat,
# isOpenStart/isOpenEnd). no_lag/no_mandatory_constraints come straight
# from the XER's relationships and constraints and are valid regardless.
_CPM_DEPENDENT_KEYS = ("no_negative_float", "no_open_ends")

_CPM_NOT_RUN_DETAIL = (
    "The CPM engine did not run for this schedule (no computed float or "
    "open-end values in the graph), so this rule could not be evaluated."
)


def evaluate_schedule_logic(graph: Any, project_id: str) -> Dict[str, ScheduleLogicResult]:
    """Run all four schedule_logic checks once per review -- each is a cheap
    read of the already-seeded graph, so there's no reason to defer any of
    them until their specific check is selected. If run_cpm failed at seed
    time (Project.computedCount unset/0), the CPM-dependent checks report
    ``cpm_ran=False`` (-> Missing) instead of a Pass built on absent data."""
    results = {key: fn(graph, project_id) for key, fn in _EVALUATORS.items()}
    rows = graph.query(
        "MATCH (p:Project {projectId: $pid}) RETURN p.computedCount AS computedCount LIMIT 1",
        params={"pid": project_id},
    ) or []
    if not rows or not rows[0].get("computedCount"):
        for key in _CPM_DEPENDENT_KEYS:
            results[key] = ScheduleLogicResult([], _CPM_NOT_RUN_DETAIL, cpm_ran=False)
    return results
