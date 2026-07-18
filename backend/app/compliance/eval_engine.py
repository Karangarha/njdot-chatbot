"""Batch compliance-checklist evaluation engine.

Replaces the retired ``app.compliance.prompt.build_system_prompt`` (one LLM
call covering all 56 checks, manually parsed from free-text JSON) with a
per-check LLM call using ``.with_structured_output(EvaluationSchema)`` —
Pydantic-enforced, no manual JSON parsing.

Each check names the document(s) it needs via ``CheckDef.source_files``
(any combination of ``"schedule"``, ``"narrative"``, ``"sp"``):
  - ``"schedule"`` — deterministic schedule/CPM facts + milestones + the
    full activity roster, read from Neo4j (``build_compliance_facts`` +
    ``build_milestones`` + ``build_activity_roster``).
  - ``"narrative"`` — full designer-narrative text (``build_narrative_text``).
  - ``"sp"`` — Special Provision retrieval, queried per-check.
Each of ``schedule``/``narrative`` is computed ONCE and shared verbatim
across every check requesting it, so checks with the same source set get an
identical prefix (OpenAI's automatic prefix caching applies within each
group). ``sp`` evidence is always queried fresh per check — no caching
benefit, but it's a minority of the catalog.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Tuple

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_neo4j import Neo4jGraph

from app.compliance.catalog import CheckDef
from app.config import config
from app.models import EvaluationSchema, ReviewCheckResult

logger = logging.getLogger(__name__)

_MAX_FACT_ROWS = 40

_STATIC_SYSTEM_PROMPT = """\
You are an expert NJDOT Construction Schedule Compliance Agent evaluating ONE \
compliance check at a time against the evidence provided in the user message \
(precomputed schedule/CPM facts, or Special Provision excerpts, depending on \
the check).

Rules:
- Base your answer only on the evidence provided — do not assume facts not shown.
- status "Pass": the evidence clearly satisfies the rule.
- status "Fail": the evidence clearly violates the rule.
- status "Missing": the evidence needed to evaluate the rule is not present in \
what was provided.
- evidence: quote the specific fact(s) used (activity IDs, dates, SP section \
number, narrative text) — keep it concise.
- source: cite where the evidence came from (e.g. an activity ID, an SP \
section number, "narrative", or "no data provided").
"""


def build_compliance_facts(graph: Neo4jGraph, project_id: str = "default") -> str:
    """Deterministic ground truth for graph-sourced checks — negative float,
    mandatory constraints, relationship lag, open ends, P6/CPM cross-check
    mismatches — read directly from Neo4j so the LLM doesn't have to
    rediscover them via Cypher generation.
    """
    neg_float = graph.query(
        "MATCH (a:Activity {projectId: $pid}) WHERE a.computedTotalFloat < 0 "
        "RETURN a.taskId AS id, a.name AS name, a.computedTotalFloat AS totalFloat "
        "ORDER BY a.computedTotalFloat LIMIT $limit",
        params={"pid": project_id, "limit": _MAX_FACT_ROWS},
    )
    constrained = graph.query(
        "MATCH (a:Activity {projectId: $pid})-[:CONSTRAINED_BY]->(c:Constraint) "
        "RETURN a.taskId AS id, c.type AS type, c.date AS date LIMIT $limit",
        params={"pid": project_id, "limit": _MAX_FACT_ROWS},
    )
    lagged = graph.query(
        "MATCH (p:Activity {projectId: $pid})-[r:PRECEDES]->(s:Activity) "
        "WHERE coalesce(r.lagDays, 0) <> 0 "
        "RETURN p.taskId AS pred, s.taskId AS succ, r.lagDays AS lagDays LIMIT $limit",
        params={"pid": project_id, "limit": _MAX_FACT_ROWS},
    )
    open_ends = graph.query(
        "MATCH (a:Activity {projectId: $pid}) "
        "WHERE a.isOpenStart = true OR a.isOpenEnd = true "
        "RETURN a.taskId AS id, a.name AS name, "
        "       a.isOpenStart AS isOpenStart, a.isOpenEnd AS isOpenEnd LIMIT $limit",
        params={"pid": project_id, "limit": _MAX_FACT_ROWS},
    )
    mismatches = graph.query(
        "MATCH (a:Activity {projectId: $pid}) WHERE a.hasMismatch = true "
        "RETURN a.taskId AS id, a.name AS name LIMIT $limit",
        params={"pid": project_id, "limit": _MAX_FACT_ROWS},
    )
    project_rows = graph.query(
        "MATCH (p:Project {projectId: $pid}) "
        "RETURN p.warnings AS warnings, p.projectFinish AS projectFinish, "
        "       p.dataDate AS dataDate, p.criticalCount AS criticalCount LIMIT 1",
        params={"pid": project_id},
    )
    project_row = project_rows[0] if project_rows else {}
    warnings = project_row.get("warnings") or []

    lines = ["PRECOMPUTED COMPLIANCE FACTS (deterministic — read these, do not recompute):"]

    lines.append(
        f"\nProject: finish={project_row.get('projectFinish', '?')}, "
        f"data_date={project_row.get('dataDate', '?')}, "
        f"critical_count={project_row.get('criticalCount', '?')}."
    )

    if neg_float:
        lines.append(f"\nActivities with Negative Float ({len(neg_float)}):")
        lines += [f"  - {r['id']} {r['name']}: total_float={r['totalFloat']}" for r in neg_float]
    else:
        lines.append("\nActivities with Negative Float: none.")

    if constrained:
        lines.append(f"\nActivities with Mandatory Constraints ({len(constrained)}):")
        lines += [f"  - {r['id']}: {r['type']} on {r['date']}" for r in constrained]
    else:
        lines.append("\nActivities with Mandatory Constraints: none.")

    if lagged:
        lines.append(f"\nRelationships with Lag ({len(lagged)}):")
        lines += [f"  - {r['pred']} -> {r['succ']}: lag={r['lagDays']} days" for r in lagged]
    else:
        lines.append("\nRelationships with Lag: none.")

    if open_ends:
        lines.append(f"\nOpen-Ended Activities ({len(open_ends)}):")
        lines += [
            f"  - {r['id']} {r['name']}: open_start={r['isOpenStart']}, open_end={r['isOpenEnd']}"
            for r in open_ends
        ]
    else:
        lines.append("\nOpen-Ended Activities: none.")

    if mismatches:
        lines.append(f"\nP6/CPM Cross-Check Mismatches ({len(mismatches)}):")
        lines += [f"  - {r['id']} {r['name']}" for r in mismatches]
    else:
        lines.append("\nP6/CPM Cross-Check Mismatches: none — schedule is fully recalculated.")

    if warnings:
        lines.append("\nCPM Engine Warnings:")
        lines += [f"  - {w}" for w in warnings]

    return "\n".join(lines)


def build_milestones(graph: Neo4jGraph, project_id: str = "default") -> str:
    """All milestone activities (zero-duration Start/Finish Milestone task
    types) with dates and float — the Administrative Dates and Completion
    Milestones checks need these (Advertisement, Bid, Award, Substantial/
    Final Completion, etc.) and nothing else in this module provides them.
    """
    rows = graph.query(
        "MATCH (a:Activity {projectId: $pid}) WHERE a.taskType CONTAINS 'Milestone' "
        "RETURN a.taskId AS id, a.name AS name, "
        "       coalesce(a.computedEarlyStart, a.startDate) AS date, "
        "       a.computedTotalFloat AS totalFloat, a.isCritical AS isCritical "
        "ORDER BY date",
        params={"pid": project_id},
    )
    if not rows:
        return "MILESTONES: none found."
    lines = ["MILESTONES (id | name | date | float | critical):"]
    lines += [
        f"  - {r['id']} | {r['name']} | {r['date']} | {r['totalFloat']} | {r['isCritical']}"
        for r in rows
    ]
    return "\n".join(lines)


def build_activity_roster(graph: Neo4jGraph, project_id: str = "default") -> str:
    """Every schedule activity (id, name, phase, dates, duration, float,
    critical) — most checks (weather/seasonal windows, utility interruptions,
    material lead times, roadside-activity presence) need to find activities
    by name/keyword; without a roster the LLM has nothing to search.
    """
    rows = graph.query(
        "MATCH (a:Activity {projectId: $pid}) "
        "RETURN a.taskId AS id, a.name AS name, a.wbsPath AS wbsPath, "
        "       a.startDate AS start, a.finishDate AS finish, "
        "       a.durationDays AS dur, a.computedTotalFloat AS totalFloat, "
        "       a.isCritical AS isCritical "
        "ORDER BY a.wbsPath, a.startDate",
        params={"pid": project_id},
    )
    if not rows:
        return "ACTIVITIES: none found."
    lines = ["ALL SCHEDULE ACTIVITIES (id | name | phase | start | finish | duration_days | float | critical):"]
    lines += [
        f"  - {r['id']} | {r['name']} | {' > '.join(r['wbsPath'] or [])} | "
        f"{r['start']} | {r['finish']} | {r['dur']} | {r['totalFloat']} | {r['isCritical']}"
        for r in rows
    ]
    return "\n".join(lines)


def build_narrative_text(graph: Neo4jGraph, project_id: str = "default") -> str:
    """Full designer-narrative text, concatenated in chunk order.

    The narrative is small (~10 pages) — cheaper and more reliable to include
    it whole (shared prefix, still cacheable across every check that
    requests ``"narrative"``) than to build per-check retrieval for it.
    """
    rows = graph.query(
        "MATCH (c:NarrativeChunk {projectId: $pid}) "
        "RETURN c.id AS id, c.heading AS heading, c.text AS text "
        "ORDER BY c.id",
        params={"pid": project_id},
    )
    if not rows:
        return "DESIGNER'S NARRATIVE: not provided."
    parts = [f"[{r['heading'] or r['id']}]\n{r['text']}" for r in rows]
    return "DESIGNER'S NARRATIVE:\n\n" + "\n\n".join(parts)


def _evaluate_one_check(
    check: CheckDef,
    structured_llm: Runnable,
    schedule_facts: str,
    narrative_text: str,
    sp_search_fn: Optional[Callable[[str], str]],
) -> Tuple[ReviewCheckResult, Dict[str, int]]:
    """Evaluate a single check and return its result plus that call's token
    usage. Touches no shared state — safe to run concurrently in a worker
    thread (see ``evaluate_checks``'s ``ThreadPoolExecutor``).
    """
    usage_totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "llm_call_count": 0}
    sources = check.source_files or ["schedule"]

    if "sp" in sources and sp_search_fn is None:
        return ReviewCheckResult(
            id=check.check_key, category=check.category, name=check.name,
            status="Missing", evidence="No Special Provision was uploaded for this review.",
            source="no data provided",
        ), usage_totals

    evidence_parts: List[str] = []
    if "schedule" in sources:
        evidence_parts.append(schedule_facts)
    if "narrative" in sources:
        evidence_parts.append(narrative_text)
    if "sp" in sources:
        evidence_parts.append(sp_search_fn(check.instruction))
    evidence = "\n\n".join(evidence_parts) if evidence_parts else schedule_facts

    user_msg = f"{evidence}\n\nCHECK: {check.name}\n{check.instruction}"
    try:
        raw_result = structured_llm.invoke([
            SystemMessage(content=_STATIC_SYSTEM_PROMPT),
            HumanMessage(content=user_msg),
        ])
        usage_totals["llm_call_count"] = 1
        usage = getattr(raw_result.get("raw"), "usage_metadata", None) or {}
        usage_totals["input_tokens"] = usage.get("input_tokens", 0) or 0
        usage_totals["output_tokens"] = usage.get("output_tokens", 0) or 0
        usage_totals["cached_tokens"] = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0

        result: Optional[EvaluationSchema] = raw_result.get("parsed")
        if result is None:
            raise ValueError(f"structured output parsing failed: {raw_result.get('parsing_error')}")
    except Exception:
        logger.exception("evaluate_checks: check %s failed", check.check_key)
        result = EvaluationSchema(
            status="Missing", evidence="Evaluation failed due to an internal error.",
            source="error",
        )

    return ReviewCheckResult(
        id=check.check_key, category=check.category, name=check.name,
        status=result.status, evidence=result.evidence, source=result.source,
    ), usage_totals


def evaluate_checks(
    checks: List[CheckDef],
    graph: Neo4jGraph,
    llm: BaseChatModel,
    sp_search_fn: Optional[Callable[[str], str]] = None,
    project_id: str = "default",
) -> List[ReviewCheckResult]:
    """Run the batch checklist: one structured-output LLM call per check, up
    to ``config.REVIEW_CHECK_CONCURRENCY`` running at once.

    Each check's evidence is built from exactly the document(s) named in
    ``check.source_files`` — ``"schedule"`` and ``"narrative"`` are each
    computed once and shared verbatim across every check that requests
    them; ``"sp"`` is queried fresh per check via ``sp_search_fn`` (e.g.
    ``retrieval_langchain.sp_retriever.retrieve_sp_chunks`` bound to a
    session). A check requesting ``"sp"`` when ``sp_search_fn`` is ``None``
    (no Special Provision uploaded) is marked "Missing" without an LLM call.

    Checks are dispatched to a thread pool (bounded by
    ``REVIEW_CHECK_CONCURRENCY`` to stay under LLM provider rate limits) and
    reassembled in the original catalog order regardless of completion
    order, since callers (the frontend's checklist grouping) depend on that
    order.

    Logs a token-usage summary (input/output/total, plus any cached input
    tokens) to the server console when done — ``include_raw=True`` is needed
    to see each call's ``usage_metadata``; the plain parsed Pydantic object
    doesn't carry it.
    """
    structured_llm = llm.with_structured_output(EvaluationSchema, include_raw=True)
    total_input_tokens = 0
    total_output_tokens = 0
    total_cached_tokens = 0
    llm_call_count = 0
    # Each computed once; shared verbatim across every check requesting it,
    # so checks with the same source_files set get an identical, cacheable
    # prefix.
    schedule_facts = "\n\n".join([
        build_compliance_facts(graph, project_id),
        build_milestones(graph, project_id),
        build_activity_roster(graph, project_id),
    ])
    narrative_text = build_narrative_text(graph, project_id)

    results_by_index: Dict[int, ReviewCheckResult] = {}
    with ThreadPoolExecutor(max_workers=config.REVIEW_CHECK_CONCURRENCY) as executor:
        future_to_index = {
            executor.submit(
                _evaluate_one_check, check, structured_llm, schedule_facts, narrative_text, sp_search_fn,
            ): i
            for i, check in enumerate(checks)
        }
        for future in as_completed(future_to_index):
            i = future_to_index[future]
            result, usage = future.result()
            results_by_index[i] = result
            total_input_tokens += usage["input_tokens"]
            total_output_tokens += usage["output_tokens"]
            total_cached_tokens += usage["cached_tokens"]
            llm_call_count += usage["llm_call_count"]

    results = [results_by_index[i] for i in range(len(checks))]

    logger.info(
        "evaluate_checks: %d checks evaluated (%d LLM calls, concurrency=%d) | tokens: %d in "
        "(%d cached) / %d out / %d total",
        len(results), llm_call_count, config.REVIEW_CHECK_CONCURRENCY, total_input_tokens,
        total_cached_tokens, total_output_tokens, total_input_tokens + total_output_tokens,
    )
    return results
