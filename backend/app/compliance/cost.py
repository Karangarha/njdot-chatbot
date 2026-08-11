"""Substantial-to-Final completion gap, computed deterministically from cost.

NJDOT requires a minimum calendar-day gap between the Substantial Completion
and Final Completion milestones, and the minimum depends on the project's
value: 60 days for projects of $50M or less, 90 days for projects over $50M
(see ``substantial_to_final`` in ``app.compliance.catalog``).

Both inputs are available exactly, so nothing here is an LLM judgement: the
Engineer's Estimate comes from the uploaded DBE Goal Memo
(``app.ingestion.estimate_extractor`` — a vision transcription, parsed to
``Decimal`` here) and the milestone dates come straight from the schedule
graph. All money comparisons use ``Decimal``; a project priced at exactly
$50,000,000.00 must not land on the wrong side of the threshold because of
binary floating-point rounding.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# The rule reads "60 Days <= $50M", so the threshold itself takes the 60-day
# branch — the comparison is inclusive.
COST_THRESHOLD_USD = Decimal("50000000")
GAP_DAYS_AT_OR_BELOW = 60
GAP_DAYS_ABOVE = 90

# Sanity bounds on a construction estimate. Outside these the value is almost
# certainly a mis-transcription (a phone number, a project id, a line item),
# so the check reports Missing rather than a confident wrong answer.
MIN_PLAUSIBLE_USD = Decimal("10000")
MAX_PLAUSIBLE_USD = Decimal("5000000000")

# NJDOT's conventional milestone ids — the catalog already relies on M100
# (Advertisement) and M950 elsewhere.
SUBSTANTIAL_MILESTONE_IDS = ("M900",)
FINAL_MILESTONE_IDS = ("M950",)

_SUBSTANTIAL_NAME_RE = re.compile(r"substantial\s+completion", re.IGNORECASE)
_FINAL_NAME_RE = re.compile(r"(?:final|contract)?\s*completion", re.IGNORECASE)
_SUBSTANTIAL_WORD_RE = re.compile(r"substantial", re.IGNORECASE)

_MONEY_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MILLIONS_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:M\b|MM\b|million)", re.IGNORECASE)


@dataclass
class CostGapResult:
    """Outcome of the Substantial-to-Final gap computation, carrying enough
    intermediate detail that ``detail`` can serve as check evidence verbatim."""

    cost_usd: Optional[str]              # str, not Decimal — JSON-serializable
    required_gap_days: Optional[int]
    actual_gap_days: Optional[int]
    substantial: Optional[Dict[str, Any]]   # {id, name, date}
    final: Optional[Dict[str, Any]]
    satisfied: Optional[bool]
    suspect: bool                        # cost outside plausible bounds
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


def parse_money(raw: Optional[str]) -> Optional[Decimal]:
    """Parse a currency string to ``Decimal`` dollars.

    Handles ``$14,973,645.25``, ``14973645``, ``USD 14,973,645.25`` and the
    shorthand ``$14.97M`` / ``14.97 million``. Returns ``None`` when the
    string carries no digits or can't be interpreted.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    is_millions = bool(_MILLIONS_RE.search(s))
    match = _MONEY_RE.search(s)
    if not match:
        return None
    try:
        value = Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None
    if is_millions:
        value *= Decimal("1000000")
    return value


def required_gap_days(cost: Decimal) -> int:
    """Minimum calendar-day gap for a project of this value (inclusive at
    the $50M threshold)."""
    return GAP_DAYS_AT_OR_BELOW if cost <= COST_THRESHOLD_USD else GAP_DAYS_ABOVE


def _parse_iso_date(raw: Any) -> Optional[date]:
    """Milestone dates are ISO strings throughout the graph (see
    ``scheduling.cpm.CpmActivityResult.to_dict`` and ``xer_extract._iso``);
    tolerate a trailing time component."""
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _milestone_rows(graph: Any, project_id: str) -> List[Dict[str, Any]]:
    """Milestone activities with a finish-first date coalesce. Zero-duration
    milestones make start and finish equal, but finish-first is the correct
    reading for a Finish Milestone. Same task-type filter as
    ``eval_engine.build_milestones``."""
    return graph.query(
        "MATCH (a:Activity {projectId: $pid}) WHERE a.taskType CONTAINS 'Milestone' "
        "RETURN a.taskId AS id, a.name AS name, "
        "       coalesce(a.computedEarlyFinish, a.computedEarlyStart, "
        "                a.finishDate, a.startDate) AS date",
        params={"pid": project_id},
    ) or []


def _pick(rows: List[Dict[str, Any]], ids: Tuple[str, ...], is_final: bool) -> Optional[Dict[str, Any]]:
    """Resolve one milestone: exact NJDOT id first, then a name match.
    Ambiguity (more than one candidate at a tier) resolves to ``None`` — a
    wrong milestone silently yields a nonsense gap, so declining is safer.
    """
    by_id = [r for r in rows if (r.get("id") or "").strip().upper() in ids]
    if len(by_id) == 1:
        return by_id[0]
    if len(by_id) > 1:
        return None

    if is_final:
        # "Substantial Completion" also contains "Completion" — it must be
        # excluded explicitly or it gets picked as the final milestone and
        # the computed gap collapses to zero.
        named = [
            r for r in rows
            if _FINAL_NAME_RE.search(r.get("name") or "")
            and not _SUBSTANTIAL_WORD_RE.search(r.get("name") or "")
        ]
    else:
        named = [r for r in rows if _SUBSTANTIAL_NAME_RE.search(r.get("name") or "")]
    return named[0] if len(named) == 1 else None


def resolve_completion_milestones(
    graph: Any, project_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """``(substantial, final)`` milestones, each ``{id, name, date}`` or
    ``None`` when unresolvable."""
    rows = _milestone_rows(graph, project_id)
    return _pick(rows, SUBSTANTIAL_MILESTONE_IDS, is_final=False), \
        _pick(rows, FINAL_MILESTONE_IDS, is_final=True)


def _found_ids(graph: Any, project_id: str) -> str:
    try:
        rows = _milestone_rows(graph, project_id)
    except Exception:
        return "none"
    if not rows:
        return "none"
    return ", ".join(f"{r.get('id')} {r.get('name')}" for r in rows[:12])


def _missing(detail: str, *, cost: Optional[Decimal] = None, suspect: bool = False,
             substantial=None, final=None, required: Optional[int] = None) -> CostGapResult:
    return CostGapResult(
        cost_usd=str(cost) if cost is not None else None,
        required_gap_days=required, actual_gap_days=None,
        substantial=substantial, final=final,
        satisfied=None, suspect=suspect, detail=detail,
    )


def evaluate_cost_gap(graph: Any, project_id: str, extraction: Any) -> CostGapResult:
    """Compute the Substantial-to-Final gap and compare it against the
    cost-derived requirement.

    ``extraction`` is duck-typed (an ``EstimateExtraction``) to avoid
    importing ``app.ingestion.estimate_extractor``, which imports this
    module. The Python-parsed ``engineers_estimate_raw`` string wins over any
    numeric field the model produced.
    """
    cost = parse_money(getattr(extraction, "engineers_estimate_raw", None)) if extraction else None
    if cost is None and extraction is not None:
        fallback = getattr(extraction, "engineers_estimate_usd", None)
        if fallback is not None:
            try:
                cost = Decimal(str(fallback))
            except (InvalidOperation, ValueError):
                cost = None

    if cost is None:
        return _missing(
            "No Engineer's Estimate could be read from the uploaded estimate document.",
        )
    if not (MIN_PLAUSIBLE_USD <= cost <= MAX_PLAUSIBLE_USD):
        return _missing(
            f"The estimated cost read from the document (${cost:,}) falls outside the "
            f"plausible range ${MIN_PLAUSIBLE_USD:,}-${MAX_PLAUSIBLE_USD:,} - likely a "
            "transcription error; manual verification needed.",
            cost=cost, suspect=True,
        )

    required = required_gap_days(cost)
    # ASCII only in evidence strings: they are rendered verbatim into the
    # jsPDF report export (DocumentReview.handleDownloadPdf), whose default
    # font is WinAnsi-encoded and drops characters like U+2264 / U+2192.
    band = "<= $50M" if cost <= COST_THRESHOLD_USD else "> $50M"
    cost_phrase = f"Engineer's Estimate ${cost:,} ({band}) requires a {required}-day gap."

    substantial, final = resolve_completion_milestones(graph, project_id)
    unresolved = [
        label for label, m in (("Substantial Completion", substantial), ("Final Completion", final))
        if m is None
    ]
    if unresolved:
        return _missing(
            f"{cost_phrase} Could not identify the {' and '.join(unresolved)} milestone(s) "
            f"in the schedule. Milestones found: {_found_ids(graph, project_id)}.",
            cost=cost, required=required, substantial=substantial, final=final,
        )

    sub_date = _parse_iso_date(substantial.get("date"))
    fin_date = _parse_iso_date(final.get("date"))
    missing_dates = [
        f"{m['id']} {m['name']}" for m, d in ((substantial, sub_date), (final, fin_date)) if d is None
    ]
    if missing_dates:
        return _missing(
            f"{cost_phrase} No usable date on milestone(s): {', '.join(missing_dates)}.",
            cost=cost, required=required, substantial=substantial, final=final,
        )

    gap = (fin_date - sub_date).days
    milestone_phrase = (
        f"{substantial['name']} {substantial['id']} {sub_date.isoformat()} -> "
        f"{final['name']} {final['id']} {fin_date.isoformat()} = {gap} calendar days."
    )

    if gap < 0:
        return CostGapResult(
            cost_usd=str(cost), required_gap_days=required, actual_gap_days=gap,
            substantial=substantial, final=final, satisfied=False, suspect=False,
            detail=(f"{cost_phrase} {milestone_phrase} Final Completion precedes Substantial "
                    "Completion — the schedule's milestone order is invalid."),
        )

    satisfied = gap >= required
    verdict = (
        f"Requirement met ({gap - required} days of margin)." if satisfied
        else f"Short by {required - gap} days."
    )
    return CostGapResult(
        cost_usd=str(cost), required_gap_days=required, actual_gap_days=gap,
        substantial=substantial, final=final, satisfied=satisfied, suspect=False,
        detail=f"{cost_phrase} {milestone_phrase} {verdict}",
    )
