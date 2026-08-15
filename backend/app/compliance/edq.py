"""EDQ line item -> schedule activity matching and coverage, computed
deterministically from the Neo4j graph.

Two responsibilities:
1. ``match_edq_items_to_activities`` — one LLM call that proposes which
   Activity node(s) each EDQ item corresponds to. An embedding pre-filter
   narrows each item's candidate set to its top-K most similar activities
   before the LLM sees them, so the model is structurally unable to propose
   an activity unrelated to the item -- this is the guard against
   hallucinated matches, not a prompt instruction alone.
2. ``evaluate_edq_coverage`` — deterministic, no LLM: reads the already-
   seeded EdqItem/COVERED_BY graph back and buckets each item into
   fully-covered / low-confidence / uncovered, based on
   ``MIN_COVERAGE_CONFIDENCE``.

``ReviewCheckResult.status`` (see ``app.models``) only accepts
Pass/Fail/Missing -- there is no separate "Warning" value at this layer (the
frontend renders "Missing" as an amber warning, see
``app.api.review._STATUS_MAP``). A low-confidence-but-present match is
therefore reported as "Missing" (not a confident "Pass"), with ``detail``
explaining it needs a human spot-check, distinct from the "no match at all"
case which is "Fail".
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import numpy as np
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import Tool
from pydantic import BaseModel, Field

from app.observability import get_langfuse_handler, new_trace_id

logger = logging.getLogger(__name__)

MIN_COVERAGE_CONFIDENCE = 0.6
_TOP_K_CANDIDATES = 15
_MAX_DETAIL_EXAMPLES = 10


def _cosine(a: List[float], b: List[float]) -> float:
    va, vb = np.array(a), np.array(b)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(np.dot(va, vb) / denom) if denom else 0.0


@dataclass
class EdqCoverageResult:
    """Outcome of reading the seeded EdqItem/COVERED_BY graph back."""

    total_items: int
    fully_covered: int
    low_confidence: int
    uncovered: int
    uncovered_items: List[Dict[str, Any]]
    low_confidence_items: List[Dict[str, Any]]
    status: Optional[str]   # "Pass" | "Fail" | "Missing", or None when no EdqItems exist at all
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


class _EdqActivityMatch(BaseModel):
    edq_item_id: str = Field(description="The id of the EDQ item this match is for.")
    task_ids: List[str] = Field(
        default_factory=list,
        description="Activity task IDs (from the candidate list shown for this item) that "
                    "cover it. Empty list is a legitimate answer when nothing in the "
                    "candidates genuinely corresponds -- never force a guess.",
    )
    confidence: float = Field(description="0.0-1.0, how confident this match is.")
    rationale: str = Field(description="One sentence: why these activities (or none) match.")


class _EdqMatchResult(BaseModel):
    matches: List[_EdqActivityMatch]


_MATCH_SYSTEM_PROMPT = """\
You match EDQ (Estimated Distribution Quantity) line items from a DBE Goal \
Memo's Job Estimate Report to the schedule activities that construct them.

For EACH EDQ item listed below, you are shown only its own candidate list of \
schedule activities (already narrowed to the most plausible ones by semantic \
similarity) -- pick zero, one, or several of THOSE candidates that genuinely \
correspond to the item's description, category and quantity.

Rules:
- Only pick from an item's own candidate list -- never invent a task id.
- An empty match (no covering activity) is a normal, expected, correct \
answer when nothing in the candidates is a real match. Do not force a match \
just to fill in an answer.
- confidence reflects how certain the correspondence is: 1.0 for an obvious, \
unambiguous match (e.g. the item is "Water Main Installation" and the \
candidate is literally named "Water Main Installation"); lower for a \
plausible-but-inferred correspondence.
"""


def _item_text(item: Dict[str, Any]) -> str:
    parts = [item.get("itemDescription") or ""]
    if item.get("category"):
        parts.append(f"category: {item['category']}")
    if item.get("unit"):
        parts.append(f"unit: {item['unit']}")
    return " | ".join(p for p in parts if p)


def _activity_text(activity: Dict[str, Any]) -> str:
    parts = [activity.get("name") or ""]
    wbs = activity.get("wbsPath")
    if wbs:
        parts.append(" > ".join(wbs) if isinstance(wbs, list) else str(wbs))
    return " | ".join(p for p in parts if p)


def match_edq_items_to_activities(
    items: List[Dict[str, Any]],
    activities: List[Dict[str, Any]],
    llm: Any,
    embeddings: Any,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    top_k: int = _TOP_K_CANDIDATES,
) -> List[Dict[str, Any]]:
    """One structured-output call: EDQ items, each paired with its own
    embedding-pre-filtered candidate activities, -> proposed matches.

    Returns flattened ``[{edqItemId, taskId, confidence, rationale}, ...]``
    rows (task_ids exploded; an item with an empty match list contributes no
    rows). ``[]`` on any failure -- fail-soft like every other LLM call in
    this codebase; an empty result just means every item ends up
    "uncovered" in ``evaluate_edq_coverage``.
    """
    if not items or not activities:
        return []

    try:
        item_vecs = embeddings.embed_documents([_item_text(it) for it in items])
        activity_vecs = embeddings.embed_documents([_activity_text(a) for a in activities])
    except Exception:
        logger.exception("match_edq_items_to_activities: embedding failed")
        return []

    prompt_lines: List[str] = []
    for item, ivec in zip(items, item_vecs):
        scored = sorted(zip(activities, activity_vecs), key=lambda av: -_cosine(ivec, av[1]))
        candidates = scored[:top_k]
        prompt_lines.append(
            f"\nEDQ item id={item['id']} | jobId={item.get('jobId')} | "
            f"category={item.get('category')} | description={item.get('itemDescription')} | "
            f"quantity={item.get('estimatedQuantity')} {item.get('unit') or ''}"
        )
        prompt_lines.append("Candidate activities (task_id: name):")
        for a, _score in candidates:
            prompt_lines.append(f"  - {a['taskId']}: {a.get('name')}")

    try:
        structured_llm = llm.with_structured_output(_EdqMatchResult)
        handler = get_langfuse_handler(trace_id=new_trace_id(seed=project_id)) if project_id else None
        invoke_config = {
            "callbacks": [handler] if handler else [],
            "run_name": "match-edq-items",
            "metadata": {
                "langfuse_session_id": project_id,
                "langfuse_user_id": user_id,
                "langfuse_tags": ["edq_matching"],
            },
        }
        result = structured_llm.invoke(
            [
                SystemMessage(content=_MATCH_SYSTEM_PROMPT),
                HumanMessage(content="\n".join(prompt_lines)),
            ],
            config=invoke_config,
        )
    except Exception:
        logger.exception("match_edq_items_to_activities: structured matching failed")
        return []

    rows: List[Dict[str, Any]] = []
    for m in result.matches:
        for task_id in m.task_ids:
            rows.append({
                "edqItemId": m.edq_item_id, "taskId": task_id,
                "confidence": m.confidence, "rationale": m.rationale,
            })
    return rows


def evaluate_edq_coverage(graph: Any, project_id: str) -> EdqCoverageResult:
    """Deterministic, no LLM. Reads EdqItem nodes + COVERED_BY edges (with
    their confidence) back from Neo4j -- already seeded earlier in the
    review pipeline -- and buckets each item into fully-covered /
    low-confidence / uncovered."""
    rows = graph.query(
        "MATCH (e:EdqItem {projectId: $pid}) "
        "OPTIONAL MATCH (e)-[r:COVERED_BY]->(a:Activity) "
        "WITH e, max(r.confidence) AS bestConfidence "
        "RETURN e.id AS id, e.jobId AS jobId, e.category AS category, "
        "       e.itemDescription AS itemDescription, bestConfidence "
        "ORDER BY e.id",
        params={"pid": project_id},
    ) or []

    if not rows:
        return EdqCoverageResult(
            total_items=0, fully_covered=0, low_confidence=0, uncovered=0,
            uncovered_items=[], low_confidence_items=[], status=None,
            detail="No EDQ items could be read from the uploaded estimate document.",
        )

    uncovered_items: List[Dict[str, Any]] = []
    low_confidence_items: List[Dict[str, Any]] = []
    fully_covered = 0
    for r in rows:
        conf = r.get("bestConfidence")
        entry = {k: r[k] for k in ("id", "jobId", "category", "itemDescription")}
        if conf is None:
            uncovered_items.append(entry)
        elif conf < MIN_COVERAGE_CONFIDENCE:
            low_confidence_items.append({**entry, "bestConfidence": conf})
        else:
            fully_covered += 1

    total = len(rows)
    uncovered = len(uncovered_items)
    low_confidence = len(low_confidence_items)

    def _examples(items: List[Dict[str, Any]]) -> str:
        shown = items[:_MAX_DETAIL_EXAMPLES]
        text = "; ".join(f"{it.get('jobId') or '?'}/{it.get('category') or '?'}: {it.get('itemDescription') or '?'}" for it in shown)
        if len(items) > _MAX_DETAIL_EXAMPLES:
            text += f"; and {len(items) - _MAX_DETAIL_EXAMPLES} more"
        return text

    if uncovered:
        status = "Fail"
        detail = (
            f"{uncovered} of {total} EDQ item(s) have no matching schedule activity: "
            f"{_examples(uncovered_items)}."
        )
    elif low_confidence:
        status = "Missing"
        detail = (
            f"All {total} EDQ item(s) have some matching activity, but {low_confidence} "
            f"matched only at low confidence (below {MIN_COVERAGE_CONFIDENCE}) and need "
            f"manual review: {_examples(low_confidence_items)}."
        )
    else:
        status = "Pass"
        detail = f"All {total} EDQ item(s) have a matching schedule activity at or above {MIN_COVERAGE_CONFIDENCE} confidence."

    return EdqCoverageResult(
        total_items=total, fully_covered=fully_covered, low_confidence=low_confidence,
        uncovered=uncovered, uncovered_items=uncovered_items,
        low_confidence_items=low_confidence_items, status=status, detail=detail,
    )


def get_edq_item_details(graph: Any, project_id: str, edq_item_id: str) -> Dict[str, Any]:
    """One EDQ item's own facts plus every activity matched to it, with
    confidence/rationale -- a raw lookup, not a Pass/Fail judgement. Unlike
    ``evaluate_edq_coverage``, nothing here is filtered by
    ``MIN_COVERAGE_CONFIDENCE`` -- callers see everything the matching pass
    proposed, including weak matches."""
    item_rows = graph.query(
        "MATCH (e:EdqItem {id: $id, projectId: $pid}) "
        "RETURN e.id AS id, e.jobId AS jobId, e.category AS category, "
        "       e.itemDescription AS itemDescription, "
        "       e.estimatedQuantity AS estimatedQuantity, e.unit AS unit",
        params={"id": edq_item_id, "pid": project_id},
    ) or []
    if not item_rows:
        return {"error": f"No EDQ item with id={edq_item_id!r} found for this project."}

    match_rows = graph.query(
        "MATCH (e:EdqItem {id: $id, projectId: $pid})-[r:COVERED_BY]->(a:Activity) "
        "RETURN a.taskId AS taskId, a.name AS name, "
        "       r.confidence AS confidence, r.rationale AS rationale "
        "ORDER BY r.confidence DESC",
        params={"id": edq_item_id, "pid": project_id},
    ) or []

    return {"item": item_rows[0], "matched_activities": match_rows}


def build_edq_coverage_tool(graph: Any, project_id: str = "default") -> Tool:
    """LangChain Tool wrapping EDQ coverage for a bound project. Empty input
    -> the overall coverage summary (evaluate_edq_coverage). Non-empty input
    -> one item's matched activities (get_edq_item_details), unfiltered by
    confidence -- lets the agent drill into why an item is uncovered or
    flagged low-confidence without falling back to generated Cypher."""

    def _tool(edq_item_id: str = "") -> str:
        edq_item_id = (edq_item_id or "").strip()
        if edq_item_id:
            return json.dumps(get_edq_item_details(graph, project_id, edq_item_id), default=str)
        return json.dumps(evaluate_edq_coverage(graph, project_id).as_dict(), default=str)

    return Tool.from_function(
        func=_tool,
        name="get_edq_coverage",
        description=(
            "EDQ (Estimated Distribution Quantity) line item to schedule "
            "activity coverage, read from the knowledge graph -- not an LLM "
            "judgement. Call with EMPTY input for the overall coverage "
            "summary (item counts and the list of uncovered items). Call "
            "with one EDQ item's id (e.g. 'edq:5', from a prior summary "
            "result) to see exactly which activities matched it, at what "
            "confidence, and why."
        ),
    )
