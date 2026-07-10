"""Compare CPM-engine results against P6's stored values.

P6 writes its own CPM outputs into the XER (total/free float, early/late
dates, longest-path flag).  Comparing them against our recomputation serves
two purposes: it validates the engine, and — once the engine is trusted —
mismatches become review findings (schedule exported without recalculation,
float sequestration via constraints, etc.).

A systematic offset (nearly every date shifted by the same amount) indicates
a convention difference between our whole-day math and P6's hour math, not a
project problem, and is reported once instead of per-activity.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from app.scheduling.calendar import _to_date
from app.scheduling.cpm import CpmResult

_MAX_MISMATCH_ROWS = 40


def _delta_days(computed, stored) -> Optional[int]:
    """Calendar-day delta computed − stored, or None if either side missing."""
    c, s = _to_date(computed), _to_date(stored)
    if c is None or s is None:
        return None
    return (c - s).days


def cross_check(
    activities: List[Dict[str, Any]],
    cpm: CpmResult,
    tolerance_days: int = 1,
) -> Dict[str, Any]:
    """Per-activity comparison of computed vs P6-stored CPM values."""
    mismatches: List[Dict[str, Any]] = []
    date_deltas: Counter = Counter()
    float_mismatch_ids: List[str] = []
    critical_flag_diffs: List[str] = []
    max_float_delta = 0
    checked = 0

    for act in activities:
        aid = act.get("activity_id")
        r = cpm.activities.get(aid)
        if r is None or r.excluded or act.get("status") == "Complete":
            continue
        checked += 1
        act_clean = True

        # Total float (working days)
        stored_tf = act.get("stored_total_float_days")
        if stored_tf is not None and r.total_float is not None:
            delta = r.total_float - stored_tf
            if abs(delta) > tolerance_days:
                act_clean = False
                float_mismatch_ids.append(aid)
                max_float_delta = max(max_float_delta, abs(delta))
                mismatches.append({
                    "activity_id": aid, "field": "total_float",
                    "computed": r.total_float, "stored": stored_tf,
                    "delta_days": delta,
                })

        # Free float
        stored_ff = act.get("stored_free_float_days")
        if stored_ff is not None and r.free_float is not None:
            delta = r.free_float - stored_ff
            if abs(delta) > tolerance_days:
                act_clean = False
                mismatches.append({
                    "activity_id": aid, "field": "free_float",
                    "computed": r.free_float, "stored": stored_ff,
                    "delta_days": delta,
                })

        # Early/late dates (calendar-day deltas)
        for fld, computed, stored_key in (
            ("early_start", r.es, "early_start"),
            ("early_finish", r.ef, "early_finish"),
            ("late_start", r.ls, "late_start"),
            ("late_finish", r.lf, "late_finish"),
        ):
            delta = _delta_days(computed, act.get(stored_key))
            if delta is None:
                continue
            date_deltas[delta] += 1
            if abs(delta) > tolerance_days:
                act_clean = False
                mismatches.append({
                    "activity_id": aid, "field": fld,
                    "computed": computed.isoformat() if computed else None,
                    "stored": act.get(stored_key),
                    "delta_days": delta,
                })

        # Critical-flag agreement (computed TF≤0 vs stored TF≤0 vs longest path)
        if stored_tf is not None and r.total_float is not None:
            stored_critical = stored_tf <= 0
            if r.is_critical != stored_critical:
                critical_flag_diffs.append(aid)
                if act_clean:
                    mismatches.append({
                        "activity_id": aid, "field": "is_critical",
                        "computed": r.is_critical, "stored": stored_critical,
                        "delta_days": None,
                    })

    # Systematic offset: one delta value covering ≥80% of all date comparisons
    systematic_offset = None
    total_date_cmp = sum(date_deltas.values())
    if total_date_cmp >= 10:
        top_delta, top_count = date_deltas.most_common(1)[0]
        if top_delta != 0 and top_count / total_date_cmp >= 0.8:
            systematic_offset = top_delta

    date_mismatch_count = sum(
        1 for m in mismatches
        if m["field"] in ("early_start", "early_finish", "late_start", "late_finish"))

    assessment: List[str] = []
    if systematic_offset is not None:
        assessment.append(
            f"Nearly all computed dates differ from P6-stored dates by a uniform "
            f"{systematic_offset:+d} day(s) — this is a calculation-convention "
            f"difference (whole-day vs hour math), not a schedule problem.")
    if float_mismatch_ids and systematic_offset is None:
        assessment.append(
            f"Stored total float differs from recomputed float on "
            f"{len(float_mismatch_ids)} activities (max {max_float_delta}d) — "
            f"the schedule may not have been recalculated after edits, or float "
            f"is being sequestered via constraints. Examples: "
            f"{', '.join(float_mismatch_ids[:5])}.")
    if critical_flag_diffs:
        assessment.append(
            f"Critical-path membership disagrees on {len(critical_flag_diffs)} "
            f"activities: {', '.join(critical_flag_diffs[:5])}"
            f"{' …' if len(critical_flag_diffs) > 5 else ''}.")
    if cpm.cycles:
        assessment.append(
            f"{len(cpm.cycles)} relationship loop(s) found and broken to compute "
            f"dates — schedule logic must be corrected.")
    if cpm.open_ends:
        assessment.append(
            f"{len(cpm.open_ends)} activities have no successors (open ends): "
            f"{', '.join(cpm.open_ends[:5])}"
            f"{' …' if len(cpm.open_ends) > 5 else ''}.")
    if not assessment:
        assessment.append(
            "Computed CPM values agree with P6-stored values within tolerance — "
            "the schedule appears properly recalculated.")

    clean = checked - len({m["activity_id"] for m in mismatches})
    return {
        "summary": {
            "checked": checked,
            "clean": clean,
            "float_mismatches": len(float_mismatch_ids),
            "date_mismatches": date_mismatch_count,
            "critical_flag_diffs": len(critical_flag_diffs),
            "max_float_delta_days": max_float_delta,
            "systematic_offset_days": systematic_offset,
            "tolerance_days": tolerance_days,
        },
        "mismatches": mismatches[:_MAX_MISMATCH_ROWS],
        "assessment": assessment,
    }
