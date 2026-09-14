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
``app.compliance.cost``), ``"edq_coverage"`` (EDQ line item -> schedule
activity graph coverage, ``app.compliance.edq``), ``"schedule_logic"``
(CSM Section 3.0 negative float / lag / open ends / mandatory constraints,
``app.compliance.schedule_logic``), and ``"date_rule"`` (milestone weekday
tests and holiday-aware business-day gaps, ``app.compliance.date_rule``).

Special Provision search closures (``sp_search_fn``) return a three-tuple
``(text, candidates, anchor_missing)`` rather than ``CitedSearch``'s plain
two-tuple -- carried as the third element of that call's own return value
(the chosen transport; see ``app.compliance.check_retrieval.RetrievalResult
.anchor_missing`` and ``app.api.review``'s SP closures for where it's
computed) rather than on a separate retrieval-log record. ``anchor_missing``
True means the project has section metadata, the check named a section/table
anchor, and it matched nothing -- a genuine gap. ``_evaluate_one_check``
short-circuits on that signal: no LLM call, an immediate "Missing" verdict
naming the absent anchor. False is the default/no-anchor/no-metadata case
and behaves exactly as before (evidence goes to the LLM as usual).
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Callable, Dict, List, Literal, Optional, Tuple

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_neo4j import Neo4jGraph

from app.compliance.anchors import extract_anchors
from app.compliance.catalog import CheckDef
from app.compliance.cost import CostGapResult
from app.compliance.date_rule import DateRuleResult
from app.compliance.edq import EdqCoverageResult
from app.compliance.geo import RegionResult
from app.compliance.schedule_logic import ScheduleLogicResult
from app.config import config
from app.models import EvaluationSchema, GroundingJudgment, ReviewCheckResult, ReviewCitation
from app.observability import get_langfuse_client, get_langfuse_handler, new_trace_id

logger = logging.getLogger(__name__)


@dataclass
class EvidenceCandidate:
    """One retrieved passage's citation metadata, keyed by the inline tag
    (e.g. ``"sp-0"``) a search function embeds in its returned evidence text
    -- lets ``_evaluate_one_check`` verify which passage (if any) the LLM's
    ``cited_chunk_ids`` actually refers to, mirroring how
    ``CitationSerializer`` validates chat citations against real retrieved
    chunks (see docs/superpowers/specs/2026-09-10-review-citations-design.md).
    """

    kind:       Literal["public", "private"]
    doc_type:   str
    label:      str
    page_pdf:   Optional[int] = None
    section_id: Optional[str] = None


# A citable search function returns (tagged_evidence_text, {tag: candidate})
# instead of a plain string, so the tag(s) the LLM copies into
# EvaluationSchema.cited_chunk_ids can be resolved back to real metadata.
CitedSearch = Callable[[str], Tuple[str, Dict[str, EvidenceCandidate]]]

# The Special Provision search closures additionally report whether the
# instruction named a section/table anchor this project's chunks genuinely
# lack (see check_retrieval.RetrievalResult.anchor_missing) -- a three-tuple,
# and its own alias distinct from CitedSearch. Every other source (spec,
# scheduling-manual, key-map, estimate) has no anchors and keeps the
# two-tuple CitedSearch shape unchanged.
SpCitedSearch = Callable[..., Tuple[str, Dict[str, EvidenceCandidate], bool]]

_MAX_FACT_ROWS = 40

_STATIC_SYSTEM_PROMPT = """\
You are an expert NJDOT Construction Schedule Compliance Agent evaluating ONE \
compliance check at a time against the evidence provided in the user message \
(precomputed schedule/CPM facts, key map facts, or Special Provision \
excerpts, depending on the check).

You do NOT decide Pass/Fail/Missing directly -- there is no status field. \
Instead you report two lists and one flag, and the verdict is computed \
from them mechanically:

- considered_items: every activity ID, milestone ID, or SP/spec section \
number the rule governs, that you actually found in the evidence. Literal \
IDs only (e.g. "B1020", "M900", "105.07.02") -- never a prose description \
in place of an ID. If the rule's subject is absent from this project \
entirely (no railroad, no summer-shutdown clause, etc.), leave this empty \
and say so in evidence -- that is a Pass, not a reason to invent an item.
- breaching_items: the subset of considered_items that actually breaches \
the rule. Every entry here MUST also appear in considered_items, and every \
entry must be an ID you can point to verbatim in the evidence you were \
given -- never an item you merely suspect might exist. Empty list -> the \
check passes. Non-empty -> the check fails on exactly those items and no \
others.
- insufficient_evidence: set true when the material the rule needs is NOT \
in the evidence -- a section/table the check names was not retrieved, a \
required narrative element or count is absent, a precomputed section is \
missing, or the excerpt is truncated before the relevant text. This is \
reported as "needs review", never as a Pass. Where a check instruction \
says WARNING for missing or unretrieved material, that is this flag. Do \
not set it merely because the rule's subject does not apply to this \
project (that is a Pass); it is ignored when breaching_items is non-empty.

Getting this right means: list every candidate item first (considered_items), \
THEN decide which of those breach (breaching_items) -- never work backwards \
from a conclusion to a list that supports it. If your reasoning contains an \
unresolved "however" or a question mark that reverses your conclusion, \
resolve it before answering.

Every quotation must be attributed to the document it came from. Do not \
attribute Special Provision text to the narrative or vice versa.

Rules:
- Base your answer only on the evidence provided — do not assume facts not shown.
- evidence: quote the specific fact(s) used (activity IDs, dates, SP section \
number, narrative text) — keep it concise, and make sure it explains WHY \
each breaching_items entry breaches (not just that it exists).
- source: cite where the evidence came from (e.g. an activity ID, an SP \
section number, "narrative", or "no data provided").
- Some passages above are tagged like "[cite:sp-0]" immediately before their \
text. NEVER write that bracket syntax, or any "[cite:...]" text, into your \
evidence or source fields — those must read as plain prose with no tag \
markup in them at all.
- cited_chunk_ids: list a tag id (the part after "cite:", e.g. "sp-0" — no \
brackets, no "cite:" prefix) here ONLY if your evidence field directly \
quotes or closely paraphrases that specific tagged passage's own text. A \
passage you merely consulted, or that is generally relevant to the rule but \
not what your evidence actually quotes, does NOT get listed — leave \
cited_chunk_ids empty rather than over-citing. Passages without a tag \
(schedule facts, key map facts, estimate facts) never go in cited_chunk_ids.
"""

_JUDGE_SYSTEM_PROMPT = """\
You are a strict fact-checker reviewing ONE compliance-check verdict. You
will be shown the same evidence the original check saw, the rule being
checked, and the items it reported: which activities/clauses it considered,
which of those it flagged as breaching, and its evidence text.

Item existence and internal list consistency (breaching_items all being a
subset of considered_items, every item appearing verbatim somewhere in the
evidence) have ALREADY been checked mechanically before this ever reaches
you -- do not re-derive those. Your only job is the part a mechanical check
can't do: does the evidence text actually SUPPORT treating each breaching
item as a genuine breach, and not, say, an item that exists but doesn't
actually violate the rule (e.g. an activity is real and is named, but its
own date shows it complies, not breaches)?

A "Pass" (empty breaching_items) that rests on a well-scoped absence (e.g.
"searched for water, water main, hydrant, valve — none appear in the
evidence") is grounded, provided the search terms are named, the named
terms genuinely do not appear, and the evidence shown is the right material
to have searched.

grounded: true if the evidence genuinely supports treating the listed
breaching items (if any) as real breaches. false if the evidence for a
breaching item actually shows compliance, or attributes a quote to the
wrong document.
reason: one sentence explaining your grounded/not-grounded call -- if false,
state what the evidence actually shows for the specific item in question,
since that sentence may be used to correct the verdict directly.
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
    free_float_notes = graph.query(
        "MATCH (a:Activity {projectId: $pid}) WHERE a.hasFreeFloatNote = true "
        "RETURN a.taskId AS id, a.name AS name LIMIT $limit",
        params={"pid": project_id, "limit": _MAX_FACT_ROWS},
    )
    project_rows = graph.query(
        "MATCH (p:Project {projectId: $pid}) "
        "RETURN p.warnings AS warnings, p.projectFinish AS projectFinish, "
        "       p.dataDate AS dataDate, p.criticalCount AS criticalCount, "
        "       p.computedCount AS computedCount LIMIT 1",
        params={"pid": project_id},
    )
    project_row = project_rows[0] if project_rows else {}
    warnings = project_row.get("warnings") or []
    # computedCount is None/0 when run_cpm raised and the graph was seeded
    # without any computed values -- "no mismatches" would then be a lie.
    cpm_ran = bool(project_row.get("computedCount"))

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

    if not cpm_ran:
        lines.append(
            "\nP6/CPM Cross-Check Mismatches: NOT AVAILABLE — the CPM engine did not "
            "run for this schedule, so stored values could not be verified."
        )
    elif mismatches:
        lines.append(f"\nP6/CPM Cross-Check Mismatches ({len(mismatches)}):")
        lines += [f"  - {r['id']} {r['name']}" for r in mismatches]
    else:
        lines.append("\nP6/CPM Cross-Check Mismatches: none — schedule is fully recalculated.")

    if free_float_notes:
        lines.append(
            f"\nFree Float Notes ({len(free_float_notes)}) — informational only, "
            f"does NOT indicate the schedule was not recalculated (total float "
            f"and dates for these activities already agree with P6; only free "
            f"float differs, often because a successor's own calendar has an "
            f"extended non-working stretch such as an in-water-work season):"
        )
        lines += [f"  - {r['id']} {r['name']}" for r in free_float_notes]

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


def build_narrative_text(
    graph: Neo4jGraph, project_id: str = "default",
) -> Tuple[str, Dict[str, EvidenceCandidate]]:
    """Full designer-narrative text, concatenated in chunk order and tagged
    per-chunk for citation verification (e.g. ``[cite:narrative-0]``).

    The narrative is small (~10 pages) — cheaper and more reliable to include
    it whole (shared prefix, still cacheable across every check that
    requests ``"narrative"``) than to build per-check retrieval for it.
    """
    rows = graph.query(
        "MATCH (c:NarrativeChunk {projectId: $pid}) "
        "RETURN c.id AS id, c.heading AS heading, c.text AS text, c.pagePdf AS pagePdf "
        "ORDER BY c.id",
        params={"pid": project_id},
    )
    if not rows:
        return "DESIGNER'S NARRATIVE: not provided.", {}
    parts: List[str] = []
    candidates: Dict[str, EvidenceCandidate] = {}
    for i, r in enumerate(rows):
        tag = f"narrative-{i}"
        heading = r["heading"] or "Narrative"
        parts.append(f"[cite:{tag}] [{heading}]\n{r['text']}")
        candidates[tag] = EvidenceCandidate(
            kind="private", doc_type="narrative", label=heading, page_pdf=r.get("pagePdf"),
        )
    return "DESIGNER'S NARRATIVE:\n\n" + "\n\n".join(parts), candidates


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


def _evaluate_date_rule_check(check: CheckDef, ctx: "_DeterministicContext") -> ReviewCheckResult:
    """Deterministic milestone-date/business-day-gap check — dispatches by
    ``check.check_key`` into the precomputed ``date_rule`` dict (one entry
    per check_key, see ``app.compliance.date_rule``), no LLM call.
    ``satisfied is None`` means an input milestone date (or, for
    substantial_regional_deadlines, the key-map region) couldn't be
    resolved — reported as Missing with the reason rather than a guess."""
    results = ctx.date_rule
    if results is None or check.check_key not in results:
        return _result(check, "Missing", "Date-rule facts were not computed for this review.", "schedule")
    r: DateRuleResult = results[check.check_key]
    if r.satisfied is None:
        return _result(check, "Missing", r.detail, "schedule milestones")
    return _result(
        check, "Pass" if r.satisfied else "Fail", r.detail,
        "schedule milestones (deterministic computation)",
    )


def _evaluate_schedule_logic_check(check: CheckDef, ctx: "_DeterministicContext") -> ReviewCheckResult:
    """Deterministic CSM Section 3.0 schedule-logic check — dispatches by
    ``check.check_key`` into the precomputed ``schedule_logic`` dict (one
    entry per check_key, see ``app.compliance.schedule_logic``), no LLM
    call. ``schedule_logic is None`` only if the graph read never ran
    (shouldn't happen — schedule is always available), reported as Missing
    rather than silently defaulting to Pass."""
    results = ctx.schedule_logic
    if results is None or check.check_key not in results:
        return _result(check, "Missing", "Schedule-logic facts were not computed for this review.", "schedule")
    r: ScheduleLogicResult = results[check.check_key]
    if not r.cpm_ran:
        return _result(check, "Missing", r.detail, "schedule graph")
    status = "Fail" if r.violations else "Pass"
    return _result(check, status, r.detail, "schedule graph (deterministic CSM Section 3.0 computation)")


@dataclass
class _DeterministicContext:
    """Precomputed inputs for the non-LLM check types. Grouping them keeps
    every deterministic evaluator to one signature as more are added."""

    keymap_geo: Optional[RegionResult] = None
    cost_gap: Optional[CostGapResult] = None
    edq_coverage: Optional[EdqCoverageResult] = None
    schedule_logic: Optional[Dict[str, ScheduleLogicResult]] = None
    date_rule: Optional[Dict[str, DateRuleResult]] = None


# check_type -> evaluator. A check_type absent from this registry takes the
# ordinary LLM path. Keep in sync with ``CheckDef.check_type``'s docstring.
_DETERMINISTIC_EVALUATORS: Dict[str, Callable[[CheckDef, _DeterministicContext], ReviewCheckResult]] = {
    "geo": _evaluate_geo_check,
    "cost_gap": _evaluate_cost_gap_check,
    "edq_coverage": _evaluate_edq_coverage_check,
    "schedule_logic": _evaluate_schedule_logic_check,
    "date_rule": _evaluate_date_rule_check,
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


def _derive_status(result: EvaluationSchema) -> str:
    """Status is never a model output (see EvaluationSchema's docstring) --
    Fail iff breaching_items is non-empty; else Missing iff the model
    flagged insufficient_evidence; else Pass. Makes "Fail with evidence
    that describes a Pass" structurally impossible: there is no field left
    for the two to disagree in."""
    if result.breaching_items:
        return "Fail"
    if result.insufficient_evidence:
        return "Missing"
    return "Pass"


_SECTION_NUMBER_RE = re.compile(r"[\d.]+")


def _item_pattern(item: str) -> re.Pattern:
    """Whole-token match for an item ID. Alphanumeric boundaries stop "M1"
    matching inside "M100"; dotted section numbers tolerate leading zeros
    per segment so the model's "105.07.01" matches an SP that prints
    "105.07.1" (and vice versa)."""
    if _SECTION_NUMBER_RE.fullmatch(item) and "." in item:
        body = r"\.".join(
            rf"0*{int(p)}" if p.isdigit() else re.escape(p) for p in item.split(".")
        )
    else:
        body = re.escape(item)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def _validate_items(result: EvaluationSchema, evidence_blob: str) -> Optional[str]:
    """Mechanical (no LLM) consistency check on the item lists, run before
    the grounding judge ever sees the answer. Returns None if valid, or a
    corrective message identifying exactly what's wrong, fed into the same
    retry path a grounding-judge rejection uses.

    This catches pure ID fabrication -- an item that never appears anywhere
    in the evidence at all (e.g. a mistyped or invented activity ID). It
    does NOT catch a real ID being semantically misclassified (a genuine
    activity cited for a rule it doesn't actually govern, e.g. "B1020
    Place Advance Warning Signs" cited as a paving activity) -- that class
    of error has no fabricated token to catch mechanically; it's still the
    grounding judge's job below, which is exactly why that judge exists
    alongside this check rather than instead of it.
    """
    orphaned = [b for b in result.breaching_items if b not in result.considered_items]
    if orphaned:
        return (
            f"These breaching_items were never listed in considered_items: "
            f"{', '.join(orphaned)}. Every breaching item must also appear in "
            f"considered_items."
        )
    hallucinated = [
        item for item in result.considered_items
        if item and not _item_pattern(item).search(evidence_blob)
    ]
    if hallucinated:
        return (
            f"These items do not appear anywhere in the evidence you were "
            f"given: {', '.join(hallucinated)}. Only list items you can point "
            f"to verbatim in the evidence."
        )
    return None


def _judge_grounding(
    check: CheckDef,
    evidence_blob: str,
    result: EvaluationSchema,
    structured_judge_llm: Runnable,
    invoke_config: dict,
) -> Tuple[GroundingJudgment, Dict[str, int]]:
    """Second-pass check: does the evidence actually support treating
    breaching_items as genuine breaches? Item existence/list-consistency is
    already handled by _validate_items before this is ever called; this is
    the narrower, genuinely subjective remainder (see _JUDGE_SYSTEM_PROMPT).
    Fails open (grounded=True) if the judge call itself errors, so an
    unreachable judge never blocks an otherwise-reasonable answer."""
    judge_msg = (
        f"{evidence_blob}\n\nCHECK: {check.name}\n{check.instruction}\n\n"
        f"VERDICT TO REVIEW:\nconsidered_items: {result.considered_items}\n"
        f"breaching_items: {result.breaching_items}\n"
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
    narrative_result: Tuple[str, Dict[str, EvidenceCandidate]],
    sp_search_fn: Optional[SpCitedSearch],
    spec_search_fn: Optional[CitedSearch],
    csm_search_fn: Optional[CitedSearch],
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

    # Deterministic checks bypass the LLM entirely -- and the missing-sources
    # gate below, which only describes what the LLM path needs. Each
    # evaluator reports its own Missing when an input it genuinely needs is
    # absent (e.g. no key map -> region unresolved), so a check like
    # award_to_construction still gets its computed verdict when an optional
    # upload it merely lists in source_files was skipped.
    deterministic_fn = _DETERMINISTIC_EVALUATORS.get(check.check_type)
    if deterministic_fn is not None:
        return deterministic_fn(check, deterministic), usage_totals

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

    citation_lookup: Dict[str, EvidenceCandidate] = {}

    evidence_parts: List[str] = []
    if "schedule" in sources:
        evidence_parts.append(schedule_facts)
    if "narrative" in sources:
        narrative_text, narrative_candidates = narrative_result
        evidence_parts.append(narrative_text)
        citation_lookup.update(narrative_candidates)
    if "sp" in sources:
        sp_text, sp_candidates, anchor_missing = sp_search_fn(check.instruction, top_k=check.sp_top_k)
        if anchor_missing:
            # The project HAS section metadata and the check's own named
            # anchor still matched nothing -- a genuine, provable gap, not a
            # pre-section-aware project where pinning simply can't work (see
            # this module's docstring). No point spending an LLM call asking
            # the model about text it was never given: construct
            # ReviewCheckResult(status="Missing") directly, naming the
            # anchor for the reviewer. This does NOT go through
            # _derive_status -- there is no EvaluationSchema here (no LLM
            # call was made for this check), so there is nothing for
            # _derive_status to inspect. Direct construction is the same
            # idiom the missing_sources short-circuit above already uses for
            # the same reason.
            anchors = extract_anchors(check.instruction)
            named = ", ".join((*anchors.sections, *anchors.tables))
            return ReviewCheckResult(
                id=check.check_key, category=check.category, name=check.name,
                status="Missing",
                evidence=(
                    f"{named} was not found anywhere in this project's Special "
                    "Provisions, which do have section-level metadata -- this "
                    "check's named clause appears to be genuinely absent."
                ),
                source="special provision",
            ), usage_totals
        evidence_parts.append(sp_text)
        citation_lookup.update(sp_candidates)
    if "keymap" in sources:
        evidence_parts.append(keymap_facts)
    if "estimate" in sources:
        evidence_parts.append(estimate_facts)
    if "spec" in sources:
        spec_text, spec_candidates = spec_search_fn(check.instruction)
        evidence_parts.append(spec_text)
        citation_lookup.update(spec_candidates)
    if "csm" in sources:
        csm_text, csm_candidates = csm_search_fn(check.instruction)
        evidence_parts.append(csm_text)
        citation_lookup.update(csm_candidates)
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
        return ReviewCheckResult(
            id=check.check_key, category=check.category, name=check.name,
            status="Missing", evidence="Evaluation failed due to an internal error.",
            source="error", citations=[],
        ), usage_totals

    grounding_note: Optional[str] = None
    # An empty breaching list the judge could not ground twice carries no
    # information either way -- unlike an ungrounded Fail, whose item list
    # is often still right. Forces Missing instead of a green Pass.
    ungrounded_pass = False

    # Mechanical item-consistency check, before any LLM judge sees the
    # answer -- catches pure ID fabrication for free (no LLM call) and only
    # spends a retry when it actually finds something wrong. Validated
    # against user_msg (evidence + the check text) so a section number the
    # instruction itself names -- which the model is told to mention when
    # it was NOT retrieved -- counts as present.
    item_validation_failed = False
    item_error = _validate_items(result, user_msg)
    if item_error is not None:
        logger.warning(
            "evaluate_checks: check %s failed item validation (%s)", check.check_key, item_error,
        )
        retry_config = {**invoke_config, "run_name": f"{invoke_config['run_name']}:item_retry"}
        retried, retry_usage = _retry_with_correction(structured_llm, user_msg, item_error, retry_config)
        _accumulate_usage(usage_totals, retry_usage)
        if retried is not None:
            result = retried
            if _validate_items(retried, user_msg) is not None:
                grounding_note = "item consistency could not be fully verified after retry"
                item_validation_failed = True
        else:
            # Retry call itself errored -- the original's fabrication is
            # already proven (item_error), so flag it rather than shipping
            # it unmarked to a judge that is told not to re-check existence.
            grounding_note = f"item consistency could not be verified — {item_error}"
            item_validation_failed = True

    # A result whose items are known-fabricated is already known-bad -- skip
    # the judge rather than spend another call (and possibly another
    # retry) verifying support for items we already know aren't real.
    if config.REVIEW_GROUNDING_JUDGE and not item_validation_failed:
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

            # The re-judge is told item existence was already checked, so
            # check it: a corrective retry that introduces a fabricated ID
            # is not a usable correction -- treat it like a failed retry
            # call (keep the validated original, flagged) below.
            if retried is not None:
                retry_item_error = _validate_items(retried, user_msg)
                if retry_item_error is not None:
                    logger.warning(
                        "evaluate_checks: check %s grounding retry failed item validation (%s)",
                        check.check_key, retry_item_error,
                    )
                    retried = None

            re_judgment = None
            if retried is not None:
                re_judge_config = {**invoke_config, "run_name": f"{invoke_config['run_name']}:rejudge"}
                re_judgment, re_judge_usage = _judge_grounding(
                    check, evidence, retried, structured_judge_llm, re_judge_config,
                )
                _accumulate_usage(usage_totals, re_judge_usage)

            if retried is not None and re_judgment is not None and re_judgment.grounded:
                result = retried
            elif retried is not None:
                # Double-failure. Deliberately NOT collapsed to Missing: that
                # used to discard a verdict whose item list was often already
                # correct (confirmed on Route 49's narrative_winter_work --
                # the judge's own rejection reasoning WAS the right finding,
                # word for word). Keep the retried answer's mechanically-
                # derived status, since breaching_items still says exactly
                # what it says, and flag it in the evidence for a human to
                # weigh instead of hiding it behind an amber "Missing" pill
                # indistinguishable from "the file wasn't uploaded."
                result = retried
                failure_reason = re_judgment.reason if re_judgment is not None else judgment.reason
                logger.warning(
                    "evaluate_checks: check %s grounding unresolved after retry (%s)",
                    check.check_key, failure_reason,
                )
                usage_totals["downgraded"] = 1
                grounding_note = f"grounding could not be independently confirmed after retry — {failure_reason}"
                ungrounded_pass = not retried.breaching_items
            else:
                # Retry call itself errored -- keep the original, same
                # fail-open posture as an unreachable judge.
                usage_totals["downgraded"] = 1
                grounding_note = f"grounding could not be independently confirmed — {judgment.reason}"
                ungrounded_pass = not result.breaching_items

    # Known-fabricated items must not drive a red Fail (or a green Pass), and
    # an empty-breach answer the judge rejected twice must not render green:
    # both become Missing / needs-review, with the note explaining why.
    if item_validation_failed or ungrounded_pass:
        status = "Missing"
    else:
        status = _derive_status(result)
    evidence_text = result.evidence
    if grounding_note:
        evidence_text = f"{evidence_text} [Automated note: {grounding_note} — recommend human review.]"

    try:
        citations: List[ReviewCitation] = []
        for tag in result.cited_chunk_ids:
            candidate = citation_lookup.get(tag)
            if candidate is not None:
                citations.append(ReviewCitation(
                    kind=candidate.kind, doc_type=candidate.doc_type, label=candidate.label,
                    page_pdf=candidate.page_pdf, section_id=candidate.section_id, verified=True,
                ))
            else:
                citations.append(ReviewCitation(
                    kind="private", doc_type="unknown", label=f"Unverified citation ({tag})",
                    verified=False,
                ))
        if "keymap" in sources and keymap_facts is not None:
            citations.append(ReviewCitation(
                kind="private", doc_type="key_map", label="Key Map", page_pdf=1, verified=True,
            ))
        if "estimate" in sources and estimate_facts is not None:
            citations.append(ReviewCitation(
                kind="private", doc_type="estimate", label="Estimate", page_pdf=1, verified=True,
            ))
    except Exception:
        logger.exception("evaluate_checks: citation construction failed for check %s", check.check_key)
        citations = []

    return ReviewCheckResult(
        id=check.check_key, category=check.category, name=check.name,
        status=status, evidence=evidence_text, source=result.source,
        citations=citations,
    ), usage_totals


def evaluate_checks(
    checks: List[CheckDef],
    graph: Neo4jGraph,
    llm: BaseChatModel,
    sp_search_fn: Optional[SpCitedSearch] = None,
    spec_search_fn: Optional[CitedSearch] = None,
    csm_search_fn: Optional[CitedSearch] = None,
    keymap_facts: Optional[str] = None,
    keymap_geo: Optional[RegionResult] = None,
    estimate_facts: Optional[str] = None,
    cost_gap: Optional[CostGapResult] = None,
    edq_coverage: Optional[EdqCoverageResult] = None,
    schedule_logic: Optional[Dict[str, ScheduleLogicResult]] = None,
    date_rule: Optional[Dict[str, DateRuleResult]] = None,
    utility_plan_search_fn: Optional[Callable[[str], str]] = None,
    project_id: str = "default",
    user_id: Optional[str] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
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

    If given, ``on_progress(completed_count, total_count)`` is called once
    per finished check, from the same thread that called ``evaluate_checks``
    (the completion loop below, never a worker thread) -- safe to use for
    UI progress reporting without any locking.

    Logs a token-usage summary (input/output/total, plus any cached input
    tokens, plus how many checks were judged/ungrounded/left unresolved
    after a retry) to the server console when done — ``include_raw=True``
    is needed to see each
    call's ``usage_metadata``; the plain parsed Pydantic object doesn't
    carry it.
    """
    structured_llm = llm.with_structured_output(EvaluationSchema, include_raw=True)
    structured_judge_llm = llm.with_structured_output(GroundingJudgment, include_raw=True)
    deterministic_ctx = _DeterministicContext(
        keymap_geo=keymap_geo, cost_gap=cost_gap, edq_coverage=edq_coverage,
        schedule_logic=schedule_logic, date_rule=date_rule,
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
    narrative_result = build_narrative_text(graph, project_id)

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
                    schedule_facts, narrative_result,
                    sp_search_fn, spec_search_fn, csm_search_fn, keymap_facts, estimate_facts,
                    utility_plan_search_fn,
                    deterministic_ctx, project_id, user_id, langfuse_handler,
                ): i
                for i, check in enumerate(checks)
            }
            completed = 0
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
                completed += 1
                if on_progress is not None:
                    on_progress(completed, len(checks))

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
        "(%d cached) / %d out / %d total | judge: %d checks judged, %d ungrounded, %d unresolved after retry",
        len(results), llm_call_count, config.REVIEW_CHECK_CONCURRENCY, total_input_tokens,
        total_cached_tokens, total_output_tokens, total_input_tokens + total_output_tokens,
        total_judged, total_ungrounded, total_downgraded,
    )
    return results
