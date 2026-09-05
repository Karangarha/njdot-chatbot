"""Batch compliance-checklist evaluation engine.

Replaces the retired ``app.compliance.prompt.build_system_prompt`` (one LLM
call covering all 56 checks, manually parsed from free-text JSON) with
per-check LLM calls using ``.with_structured_output(EvaluationSchema)`` —
Pydantic-enforced, no manual JSON parsing. A check can now cost up to 4 LLM
calls: the original answer, a grounding judge, and — if the judge finds it
ungrounded — a corrective retry plus a re-judge of that retry.

Each check names the document(s) it needs via ``CheckDef.source_files``
(any combination of ``"schedule"``, ``"narrative"``, ``"sp"``, ``"keymap"``,
``"spec"``, ``"csm"``):
  - ``"schedule"`` — deterministic schedule/CPM facts + milestones + the
    full activity roster, read from Neo4j (``build_compliance_facts`` +
    ``build_milestones`` + ``build_activity_roster``).
  - ``"narrative"`` — full designer-narrative text (``build_narrative_text``).
  - ``"sp"`` — Special Provision retrieval, queried per-check.
  - ``"keymap"`` — the precomputed "KEY MAP FACTS" block
    (``ingestion.keymap_extractor.render_keymap_facts``) — whole-doc, like
    ``narrative``: key sheets are 1–3 pages, so no retrieval is needed.
  - ``"estimate"`` — the precomputed "PROJECT COST FACTS" block
    (``ingestion.estimate_extractor.render_estimate_facts``), likewise
    whole-doc: only page 1 of the DBE Goal Memo is read.
Each of ``schedule``/``narrative``/``keymap``/``estimate`` is computed ONCE
and shared verbatim across every check requesting it, so checks with the same
source set get an identical prefix (OpenAI's automatic prefix caching applies
within each group). ``sp`` evidence is always queried fresh per check — no
caching benefit, but it's a minority of the catalog.

Checks whose ``check_type`` appears in ``_DETERMINISTIC_EVALUATORS``
short-circuit the LLM entirely and are computed in Python: ``"geo"``
(north/south of I-195 from key map coordinates, ``app.compliance.geo``),
``"cost_gap"`` (Substantial-to-Final day gap from the Engineer's Estimate,
``app.compliance.cost``), and ``"edq_coverage"`` (EDQ line item -> schedule
activity graph coverage, ``app.compliance.edq``).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_neo4j import Neo4jGraph

from app.compliance.catalog import CheckDef
from app.compliance.cost import CostGapResult
from app.compliance.edq import EdqCoverageResult
from app.compliance.geo import RegionResult
from app.config import config
from app.models import EvaluationSchema, GroundingJudgment, ReviewCheckResult
from app.observability import get_langfuse_client, get_langfuse_handler, new_trace_id

logger = logging.getLogger(__name__)

_MAX_FACT_ROWS = 40

_STATIC_SYSTEM_PROMPT = """\
You are an expert NJDOT Construction Schedule Compliance Agent evaluating ONE \
compliance check at a time against the evidence provided in the user message \
(precomputed schedule/CPM facts, key map facts, or Special Provision \
excerpts, depending on the check).

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

_JUDGE_SYSTEM_PROMPT = """\
You are a strict fact-checker reviewing ONE compliance-check verdict for \
internal consistency. You will be shown the same evidence the original \
check saw, the rule being checked, and the verdict that was produced.

Your only job: does the cited evidence actually support the stated status? \
Look specifically for:
- A status that contradicts what the evidence itself shows (e.g. dates in \
the evidence indicate compliance, but status is "Fail").
- Evidence that does not appear to describe what it claims to (a quote \
attributed to a document that doesn't match the material shown).

A "Pass" that rests on a well-scoped absence (e.g. "searched for water, \
water main, hydrant, valve — none appear in the evidence") is grounded, \
provided the search terms are named, the named terms genuinely do not \
appear, and the evidence shown is the right material to have searched. \
Absence of a quote is not the same as a fabricated quote.

grounded: true if the evidence genuinely supports the status. false if the \
status contradicts its own evidence, or the evidence looks fabricated.
reason: one sentence explaining your grounded/not-grounded call.
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


def _result(check: CheckDef, status: str, evidence: str, source: str) -> ReviewCheckResult:
    return ReviewCheckResult(
        id=check.check_key, category=check.category, name=check.name,
        status=status, evidence=evidence, source=source,
    )


def _evaluate_geo_check(check: CheckDef, ctx: "_DeterministicContext") -> ReviewCheckResult:
    """Deterministic north/south-of-I-195 check — maps the precomputed
    ``RegionResult`` straight onto a ``ReviewCheckResult``, no LLM call.
    ``geo`` is None when the key map yielded no extraction at all (the
    missing-source path normally catches that first)."""
    geo = ctx.keymap_geo
    if geo is None or (geo.region is None and not geo.suspect):
        return _result(
            check, "Missing",
            geo.detail if geo is not None
            else "Latitude/longitude could not be found or parsed on the key map.",
            "key map",
        )
    if geo.suspect:
        return _result(check, "Missing", geo.detail, "key map")
    return _result(check, "Pass", geo.detail, "key map (deterministic geo computation)")


def _evaluate_cost_gap_check(check: CheckDef, ctx: "_DeterministicContext") -> ReviewCheckResult:
    """Deterministic Substantial-to-Final completion gap — maps the
    precomputed ``CostGapResult`` onto a ``ReviewCheckResult``, no LLM call.
    ``satisfied is None`` means the inputs were incomplete (no readable
    estimate, an implausible amount, or an unresolvable milestone), which is
    reported as "Missing" with the reason rather than a guess."""
    gap = ctx.cost_gap
    source = "DBE Goal Memo + schedule milestones (deterministic computation)"
    if gap is None:
        return _result(
            check, "Missing",
            "No Engineer's Estimate could be read from the uploaded estimate document.",
            "cost estimate",
        )
    if gap.satisfied is None:
        return _result(check, "Missing", gap.detail, "cost estimate")
    return _result(check, "Pass" if gap.satisfied else "Fail", gap.detail, source)


def _evaluate_edq_coverage_check(check: CheckDef, ctx: "_DeterministicContext") -> ReviewCheckResult:
    """Deterministic EDQ item -> Activity graph coverage — maps the
    precomputed ``EdqCoverageResult`` onto a ``ReviewCheckResult``, no LLM
    call. ``status is None`` means no EDQ items could be read from the
    estimate document at all (reported as "Missing"); otherwise ``status``
    is already one of Pass/Fail/Missing (a low-confidence-but-present match
    reports "Missing" too, distinct from "Fail" for a genuinely uncovered
    item — see ``app.compliance.edq``'s module docstring)."""
    edq = ctx.edq_coverage
    if edq is None or edq.status is None:
        return _result(
            check, "Missing",
            edq.detail if edq is not None
            else "No EDQ items could be read from the uploaded estimate document.",
            "estimate document",
        )
    return _result(
        check, edq.status, edq.detail,
        "estimate document + schedule graph (deterministic EDQ coverage)",
    )


@dataclass
class _DeterministicContext:
    """Precomputed inputs for the non-LLM check types. Grouping them keeps
    every deterministic evaluator to one signature as more are added."""

    keymap_geo: Optional[RegionResult] = None
    cost_gap: Optional[CostGapResult] = None
    edq_coverage: Optional[EdqCoverageResult] = None


# check_type -> evaluator. A check_type absent from this registry takes the
# ordinary LLM path. Keep in sync with ``CheckDef.check_type``'s docstring.
_DETERMINISTIC_EVALUATORS: Dict[str, Callable[[CheckDef, _DeterministicContext], ReviewCheckResult]] = {
    "geo": _evaluate_geo_check,
    "cost_gap": _evaluate_cost_gap_check,
    "edq_coverage": _evaluate_edq_coverage_check,
}


def _usage_from_raw(raw_result: dict) -> Dict[str, int]:
    """Extract input/output/cached token counts from a structured-output
    call's raw response — shared by the original check call, the grounding
    judge, and the corrective retry, all of which return the same
    ``{"raw": ..., "parsed": ..., "parsing_error": ...}`` shape."""
    usage = getattr(raw_result.get("raw"), "usage_metadata", None) or {}
    return {
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cached_tokens": (usage.get("input_token_details") or {}).get("cache_read", 0) or 0,
    }


def _accumulate_usage(totals: Dict[str, int], call_usage: Dict[str, int]) -> None:
    """Add one call's token usage into the running totals and bump the call
    count — shared by every call site in _evaluate_one_check (the original
    check, the judge, and the retry), so that logic isn't repeated at each."""
    totals["llm_call_count"] += 1
    totals["input_tokens"] += call_usage["input_tokens"]
    totals["output_tokens"] += call_usage["output_tokens"]
    totals["cached_tokens"] += call_usage["cached_tokens"]


def _judge_grounding(
    check: CheckDef,
    evidence_blob: str,
    result: EvaluationSchema,
    structured_judge_llm: Runnable,
    invoke_config: dict,
) -> Tuple[GroundingJudgment, Dict[str, int]]:
    """Second-pass check: does `result`'s evidence actually support its
    status? Only called for Pass/Fail verdicts — Missing already means "not
    enough evidence," nothing to judge. Fails open (grounded=True) if the
    judge call itself errors, so an unreachable judge never blocks an
    otherwise-reasonable answer."""
    judge_msg = (
        f"{evidence_blob}\n\nCHECK: {check.name}\n{check.instruction}\n\n"
        f"VERDICT TO REVIEW:\nstatus: {result.status}\n"
        f"evidence: {result.evidence}\nsource: {result.source}"
    )
    try:
        raw = structured_judge_llm.invoke(
            [SystemMessage(content=_JUDGE_SYSTEM_PROMPT), HumanMessage(content=judge_msg)],
            config=invoke_config,
        )
        judgment: Optional[GroundingJudgment] = raw.get("parsed")
        if judgment is None:
            raise ValueError(f"judge structured output parsing failed: {raw.get('parsing_error')}")
        return judgment, _usage_from_raw(raw)
    except Exception:
        logger.exception("evaluate_checks: grounding judge failed for check %s", check.check_key)
        return (
            GroundingJudgment(grounded=True, reason="Judge call failed; not blocking on it."),
            {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0},
        )


def _retry_with_correction(
    structured_llm: Runnable,
    user_msg: str,
    correction: str,
    invoke_config: dict,
) -> Tuple[Optional[EvaluationSchema], Dict[str, int]]:
    """Re-run the original check once with a corrective note appended, after
    the first answer failed grounding verification. Returns (None, zeroed
    usage) if the retry call itself errors — the caller falls back to
    Missing either way, so a broken retry isn't a crash."""
    try:
        raw = structured_llm.invoke(
            [
                SystemMessage(content=_STATIC_SYSTEM_PROMPT),
                HumanMessage(content=user_msg),
                HumanMessage(
                    content=f"Your previous answer could not be verified: {correction} "
                    "Re-examine the evidence above and provide a corrected answer, "
                    "quoting only text that actually appears in it."
                ),
            ],
            config=invoke_config,
        )
        parsed: Optional[EvaluationSchema] = raw.get("parsed")
        if parsed is None:
            raise ValueError(f"retry structured output parsing failed: {raw.get('parsing_error')}")
        return parsed, _usage_from_raw(raw)
    except Exception:
        logger.exception("evaluate_checks: grounding retry failed")
        return None, {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}


def _evaluate_one_check(
    check: CheckDef,
    structured_llm: Runnable,
    structured_judge_llm: Runnable,
    schedule_facts: str,
    narrative_text: str,
    sp_search_fn: Optional[Callable[[str], str]],
    spec_search_fn: Optional[Callable[[str], str]],
    csm_search_fn: Optional[Callable[[str], str]],
    keymap_facts: Optional[str],
    estimate_facts: Optional[str],
    utility_plan_search_fn: Optional[Callable[[str], str]],
    deterministic: "_DeterministicContext",
    project_id: str,
    user_id: Optional[str],
    langfuse_handler,
) -> Tuple[ReviewCheckResult, Dict[str, int]]:
    """Evaluate a single check and return its result plus the token usage of
    all the LLM calls this check made (original answer, plus the grounding
    judge and any retry/re-judge). Touches no shared state — safe to run
    concurrently in a worker thread (see ``evaluate_checks``'s
    ``ThreadPoolExecutor``).
    """
    usage_totals = {
        "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "llm_call_count": 0,
        "judged": 0, "ungrounded": 0, "downgraded": 0,
    }
    sources = check.source_files or ["schedule"]

    # "sp"/"keymap"/"estimate" are per-review (only present if that document
    # was uploaded); "spec"/"csm" are static, pre-ingested reference
    # collections (Standard Specifications / Construction Scheduling Manual —
    # see backend/scripts/ingest_specs.py) that should always be available, so
    # a missing search_fn for either signals an infra problem rather than a
    # normal "not uploaded" case — still degrades to "Missing" either way
    # rather than silently evaluating with partial evidence.
    #
    # "utility_plan" is deliberately NOT included here: it's a cross-reference
    # bonus (checks like gas_interruption worked fine off "schedule" alone
    # before this source existed), not a requirement — a review without one
    # uploaded must keep evaluating exactly as it did before, not degrade to
    # Missing. See the evidence-gathering block below for how it's included
    # only when actually available.
    missing_sources = []
    if "sp" in sources and sp_search_fn is None:
        missing_sources.append("Special Provision (not uploaded for this review)")
    if "keymap" in sources and keymap_facts is None:
        missing_sources.append("Key Map (not uploaded for this review)")
    if "estimate" in sources and estimate_facts is None:
        missing_sources.append("Cost Estimate (not uploaded for this review)")
    if "spec" in sources and spec_search_fn is None:
        missing_sources.append("Standard Specifications")
    if "csm" in sources and csm_search_fn is None:
        missing_sources.append("Construction Scheduling Manual")
    if missing_sources:
        return ReviewCheckResult(
            id=check.check_key, category=check.category, name=check.name,
            status="Missing", evidence=f"Not available for this review: {', '.join(missing_sources)}.",
            source="no data provided",
        ), usage_totals

    # Deterministic checks bypass the LLM entirely.
    deterministic_fn = _DETERMINISTIC_EVALUATORS.get(check.check_type)
    if deterministic_fn is not None:
        return deterministic_fn(check, deterministic), usage_totals

    evidence_parts: List[str] = []
    if "schedule" in sources:
        evidence_parts.append(schedule_facts)
    if "narrative" in sources:
        evidence_parts.append(narrative_text)
    if "sp" in sources:
        evidence_parts.append(sp_search_fn(check.instruction))
    if "keymap" in sources:
        evidence_parts.append(keymap_facts)
    if "estimate" in sources:
        evidence_parts.append(estimate_facts)
    if "spec" in sources:
        evidence_parts.append(spec_search_fn(check.instruction))
    if "csm" in sources:
        evidence_parts.append(csm_search_fn(check.instruction))
    if "utility_plan" in sources and utility_plan_search_fn is not None:
        evidence_parts.append(utility_plan_search_fn(check.instruction))
    evidence = "\n\n".join(evidence_parts) if evidence_parts else schedule_facts

    user_msg = f"{evidence}\n\nCHECK: {check.name}\n{check.instruction}"
    invoke_config = {
        "callbacks": [langfuse_handler] if langfuse_handler else [],
        "run_name": f"evaluate-check:{check.check_key}",
        "metadata": {
            "langfuse_session_id": project_id,
            "langfuse_user_id": user_id,
            "langfuse_tags": ["compliance_review", check.check_key],
        },
    }
    try:
        raw_result = structured_llm.invoke(
            [SystemMessage(content=_STATIC_SYSTEM_PROMPT), HumanMessage(content=user_msg)],
            config=invoke_config,
        )
        _accumulate_usage(usage_totals, _usage_from_raw(raw_result))

        result: Optional[EvaluationSchema] = raw_result.get("parsed")
        if result is None:
            raise ValueError(f"structured output parsing failed: {raw_result.get('parsing_error')}")
    except Exception:
        logger.exception("evaluate_checks: check %s failed", check.check_key)
        result = EvaluationSchema(
            status="Missing", evidence="Evaluation failed due to an internal error.",
            source="error",
        )

    if result.status in ("Pass", "Fail"):
        judge_config = {**invoke_config, "run_name": f"{invoke_config['run_name']}:judge"}
        judgment, judge_usage = _judge_grounding(check, evidence, result, structured_judge_llm, judge_config)
        _accumulate_usage(usage_totals, judge_usage)
        usage_totals["judged"] = 1

        if not judgment.grounded:
            logger.warning(
                "evaluate_checks: check %s judged ungrounded (%s)", check.check_key, judgment.reason,
            )
            usage_totals["ungrounded"] = 1
            retry_config = {**invoke_config, "run_name": f"{invoke_config['run_name']}:retry"}
            retried, retry_usage = _retry_with_correction(structured_llm, user_msg, judgment.reason, retry_config)
            _accumulate_usage(usage_totals, retry_usage)

            re_judgment = None
            if retried is not None:
                re_judge_config = {**invoke_config, "run_name": f"{invoke_config['run_name']}:rejudge"}
                re_judgment, re_judge_usage = _judge_grounding(
                    check, evidence, retried, structured_judge_llm, re_judge_config,
                )
                _accumulate_usage(usage_totals, re_judge_usage)

            if retried is not None and re_judgment is not None and re_judgment.grounded:
                # Note the asymmetry: if the FIRST judge call errors, _judge_grounding
                # fails open and we keep the original answer unverified (safe — it's
                # what we already had). If the RE-judge errors, it also fails open,
                # and we accept the RETRIED answer unverified here — a different
                # answer we've never actually verified. Both follow "an unreachable
                # judge never blocks a check," and accepting the correction is the
                # better of the two options, but it's worth being explicit that this
                # branch can be reached without the retry ever being judged.
                result = retried
            else:
                failure_reason = re_judgment.reason if re_judgment is not None else judgment.reason
                logger.warning(
                    "evaluate_checks: check %s downgraded to Missing after retry (%s)",
                    check.check_key, failure_reason,
                )
                usage_totals["downgraded"] = 1
                result = EvaluationSchema(
                    status="Missing",
                    evidence=f"Could not verify grounding after retry: {failure_reason}",
                    source="grounding verification failed",
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
    spec_search_fn: Optional[Callable[[str], str]] = None,
    csm_search_fn: Optional[Callable[[str], str]] = None,
    keymap_facts: Optional[str] = None,
    keymap_geo: Optional[RegionResult] = None,
    estimate_facts: Optional[str] = None,
    cost_gap: Optional[CostGapResult] = None,
    edq_coverage: Optional[EdqCoverageResult] = None,
    utility_plan_search_fn: Optional[Callable[[str], str]] = None,
    project_id: str = "default",
    user_id: Optional[str] = None,
) -> List[ReviewCheckResult]:
    """Run the batch checklist: at least one structured-output LLM call per
    check (up to 4 — original answer, grounding judge, and a corrective
    retry plus re-judge if the judge finds it ungrounded), up to
    ``config.REVIEW_CHECK_CONCURRENCY`` running at once.

    Each check's evidence is built from exactly the document(s) named in
    ``check.source_files`` — ``"schedule"`` and ``"narrative"`` are each
    computed once and shared verbatim across every check that requests
    them, as are ``"keymap"``/``"estimate"`` (the precomputed
    ``keymap_facts``/``estimate_facts`` blocks; pass ``keymap_geo`` and
    ``cost_gap`` alongside them so the deterministic check types resolve
    without an LLM call). ``"sp"`` (Special Provision,
    per-review) is queried fresh per
    check via ``sp_search_fn``; ``"spec"``/``"csm"`` (Standard
    Specifications / Construction Scheduling Manual — static, pre-ingested
    reference collections, see ``backend/scripts/ingest_specs.py``) are
    likewise queried fresh per check via ``spec_search_fn``/``csm_search_fn``.
    A check requesting a source whose search_fn is ``None`` (e.g. no Special
    Provision uploaded, or the reference collections aren't reachable) is
    marked "Missing" without an LLM call — see ``_evaluate_one_check``.

    Checks are dispatched to a thread pool (bounded by
    ``REVIEW_CHECK_CONCURRENCY`` to stay under LLM provider rate limits) and
    reassembled in the original catalog order regardless of completion
    order, since callers (the frontend's checklist grouping) depend on that
    order.

    Logs a token-usage summary (input/output/total, plus any cached input
    tokens, plus how many checks were judged/ungrounded/downgraded) to the
    server console when done — ``include_raw=True`` is needed to see each
    call's ``usage_metadata``; the plain parsed Pydantic object doesn't
    carry it.
    """
    structured_llm = llm.with_structured_output(EvaluationSchema, include_raw=True)
    structured_judge_llm = llm.with_structured_output(GroundingJudgment, include_raw=True)
    deterministic_ctx = _DeterministicContext(
        keymap_geo=keymap_geo, cost_gap=cost_gap, edq_coverage=edq_coverage,
    )
    total_input_tokens = 0
    total_output_tokens = 0
    total_cached_tokens = 0
    llm_call_count = 0
    total_judged = 0
    total_ungrounded = 0
    total_downgraded = 0
    # Each computed once; shared verbatim across every check requesting it,
    # so checks with the same source_files set get an identical, cacheable
    # prefix.
    schedule_facts = "\n\n".join([
        build_compliance_facts(graph, project_id),
        build_milestones(graph, project_id),
        build_activity_roster(graph, project_id),
    ])
    narrative_text = build_narrative_text(graph, project_id)

    # ── Langfuse: one trace per review, with every concurrent check nested
    # as a child generation under one root span — see app.observability's
    # module docstring for why a deterministic trace id (rather than
    # ThreadPoolExecutor-crossing context propagation) is what makes this
    # work across worker threads. Fails soft: if Langfuse is unavailable,
    # review_span/langfuse_handler stay None and checks just don't trace.
    langfuse_client = get_langfuse_client()
    trace_id = new_trace_id(seed=project_id)
    review_span_cm = (
        langfuse_client.start_as_current_observation(
            name="compliance-review", as_type="span", trace_context={"trace_id": trace_id},
            input={"num_checks": len(checks)},
            metadata={
                "langfuse_session_id": project_id,
                "langfuse_user_id": user_id,
                "langfuse_tags": ["compliance_review"],
            },
        )
        if langfuse_client is not None and trace_id
        else nullcontext(None)
    )

    results_by_index: Dict[int, ReviewCheckResult] = {}
    with review_span_cm as review_span:
        langfuse_handler = get_langfuse_handler(
            trace_id=trace_id, parent_span_id=getattr(review_span, "id", None),
        )
        with ThreadPoolExecutor(max_workers=config.REVIEW_CHECK_CONCURRENCY) as executor:
            future_to_index = {
                executor.submit(
                    _evaluate_one_check, check, structured_llm, structured_judge_llm,
                    schedule_facts, narrative_text,
                    sp_search_fn, spec_search_fn, csm_search_fn, keymap_facts, estimate_facts,
                    utility_plan_search_fn,
                    deterministic_ctx, project_id, user_id, langfuse_handler,
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
                total_judged += usage["judged"]
                total_ungrounded += usage["ungrounded"]
                total_downgraded += usage["downgraded"]

        if review_span is not None:
            try:
                statuses = [r.status for r in results_by_index.values()]
                review_span.update(output={
                    "checks_evaluated": len(results_by_index),
                    "passed": statuses.count("Pass"),
                    "failed": statuses.count("Fail"),
                    "missing": statuses.count("Missing"),
                })
            except Exception:
                logger.warning("Failed to update Langfuse review span output", exc_info=True)

    results = [results_by_index[i] for i in range(len(checks))]

    logger.info(
        "evaluate_checks: %d checks evaluated (%d LLM calls, concurrency=%d) | tokens: %d in "
        "(%d cached) / %d out / %d total | judge: %d checks judged, %d ungrounded, %d downgraded",
        len(results), llm_call_count, config.REVIEW_CHECK_CONCURRENCY, total_input_tokens,
        total_cached_tokens, total_output_tokens, total_input_tokens + total_output_tokens,
        total_judged, total_ungrounded, total_downgraded,
    )
    return results
