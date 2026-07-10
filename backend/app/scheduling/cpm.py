"""Deterministic Critical Path Method engine over the XER activity network.

Builds a networkx DiGraph from the extended activity dicts produced by
``xer_extract.parse_xer_to_json`` and runs a calendar-aware forward/backward
pass.  All numbers here are computed from network logic — never taken from
P6's stored values (those are compared separately in ``crosscheck``).

The pass runs in **day-boundary space** to match P6's instant semantics at
whole-day granularity: a boundary is a ``date`` meaning "morning of that
day".  An activity with duration d has a start boundary sb (morning of its
first work day) and finish boundary fb (morning after its last work day).
Start milestones bind at a morning boundary (an FS successor starts the SAME
day); finish milestones bind at an end-of-day boundary (displayed on the day
whose end they mark).  This removes the off-by-one-per-hop errors a naive
"+1 work day per FS link" convention accumulates.

Other conventions (documented for the cross-check tolerance):
- Lags elapse on the predecessor's calendar (P6 default setting).
- Completed activities are pinned to actual dates (retained logic) and do not
  constrain the backward pass; in-progress activities restart at the data date.
- Critical: total float ≤ 0 (P6 convention).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx

from app.scheduling.calendar import DEFAULT_CALENDAR, WorkCalendar, _to_date

logger = logging.getLogger(__name__)

_ONE_DAY = timedelta(days=1)

# LOE and WBS-summary activities never drive logic in P6
_EXCLUDED_TASK_TYPES = {"Level of Effort", "WBS Summary"}

_MANDATORY_START = {"CS_MANDSTART", "CS_MSO"}
_MANDATORY_FINISH = {"CS_MANDFIN", "CS_MEO"}


@dataclass
class CpmActivityResult:
    activity_id: str
    es: Optional[date] = None
    ef: Optional[date] = None
    ls: Optional[date] = None
    lf: Optional[date] = None
    total_float: Optional[int] = None
    free_float: Optional[int] = None
    is_critical: bool = False
    on_driving_path: bool = False
    excluded: bool = False          # LOE / WBS-summary: reported but not computed

    def to_dict(self) -> Dict[str, Any]:
        iso = lambda d: d.isoformat() if d else None
        return {
            "activity_id": self.activity_id,
            "es": iso(self.es), "ef": iso(self.ef),
            "ls": iso(self.ls), "lf": iso(self.lf),
            "total_float": self.total_float,
            "free_float": self.free_float,
            "is_critical": self.is_critical,
            "on_driving_path": self.on_driving_path,
            "excluded": self.excluded,
        }


@dataclass
class CpmResult:
    activities: Dict[str, CpmActivityResult] = field(default_factory=dict)
    critical_paths: List[List[str]] = field(default_factory=list)
    project_finish: Optional[date] = None
    data_date: Optional[date] = None
    cycles: List[List[str]] = field(default_factory=list)
    open_starts: List[str] = field(default_factory=list)
    open_ends: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary_dict(self) -> Dict[str, Any]:
        """JSON-safe project-level summary (per-activity results excluded)."""
        return {
            "critical_paths": self.critical_paths,
            "project_finish": self.project_finish.isoformat() if self.project_finish else None,
            "data_date": self.data_date.isoformat() if self.data_date else None,
            "cycles": self.cycles,
            "open_starts": self.open_starts,
            "open_ends": self.open_ends,
            "warnings": self.warnings,
            "critical_count": sum(1 for r in self.activities.values() if r.is_critical),
            "computed_count": sum(1 for r in self.activities.values() if not r.excluded),
        }


def build_network(activities: List[Dict[str, Any]]) -> nx.DiGraph:
    """Build the activity network.

    Nodes are keyed by activity_id and carry the full activity dict.  Edges
    run predecessor → successor; because P6 allows several relationships
    between one pair (e.g. SS+FF), each edge carries a ``links`` list of
    ``{"type", "lag_days"}`` dicts.
    """
    g = nx.DiGraph()
    for act in activities:
        aid = act.get("activity_id")
        if not aid:
            continue
        g.add_node(aid, **act,
                   _excluded=act.get("task_type") in _EXCLUDED_TASK_TYPES)

    dropped = 0
    for act in activities:
        aid = act.get("activity_id")
        for link in act.get("pred_links", []):
            pid = link.get("activity_id")
            if not pid or pid not in g or aid not in g:
                dropped += 1
                continue
            if g.has_edge(pid, aid):
                g[pid][aid]["links"].append(
                    {"type": link["type"], "lag_days": link.get("lag_days", 0)})
            else:
                g.add_edge(pid, aid, links=[
                    {"type": link["type"], "lag_days": link.get("lag_days", 0)}])
    if dropped:
        logger.warning("build_network: dropped %d relationships to unknown activities", dropped)
    return g


def _scheduling_duration(node: Dict[str, Any]) -> int:
    """Working-day duration used for the pass (remaining work for open activities)."""
    status = node.get("status", "Not Started")
    if status == "Not Started":
        dur = node.get("remaining_duration_days") or node.get("original_duration_days") \
            or node.get("duration_days") or 0
    else:
        dur = node.get("remaining_duration_days") or 0
    return max(int(dur), 0)


# ── Boundary arithmetic ────────────────────────────────────────────────────────
# A boundary is a date meaning "morning of that day".

def _badd(cal: WorkCalendar, b: date, n: int) -> date:
    """Advance a boundary by n working days (signed)."""
    if n > 0:
        for _ in range(n):
            b = cal.next_work_day(b) + _ONE_DAY
    elif n < 0:
        for _ in range(-n):
            b = cal.prev_work_day(b - _ONE_DAY)
    return b


def _bdist(cal: WorkCalendar, a: date, b: date) -> int:
    """Signed count of working days in the boundary interval [a, b)."""
    # Work days in [a, b) == work days in (a-1, b-1]
    return cal.work_days_between(a - _ONE_DAY, b - _ONE_DAY)


def _snap_morning_fwd(cal: WorkCalendar, b: date) -> date:
    """Earliest boundary ≥ b that is the morning of a working day."""
    return cal.next_work_day(b)


def _snap_morning_back(cal: WorkCalendar, b: date) -> date:
    """Latest boundary ≤ b that is the morning of a working day."""
    return b if cal.is_work_day(b) else cal.prev_work_day(b)


def _snap_evening_fwd(cal: WorkCalendar, b: date) -> date:
    """Earliest boundary ≥ b that is the end of a working day."""
    return cal.next_work_day(b - _ONE_DAY) + _ONE_DAY


def _snap_evening_back(cal: WorkCalendar, b: date) -> date:
    """Latest boundary ≤ b that is the end of a working day."""
    return cal.prev_work_day(b - _ONE_DAY) + _ONE_DAY


def run_cpm(
    graph: nx.DiGraph,
    calendars: Dict[str, WorkCalendar],
    project: Optional[Dict[str, Any]] = None,
) -> CpmResult:
    """Forward/backward pass over the activity network."""
    result = CpmResult()
    project = project or {}

    def cal_of(node_data: Dict[str, Any]) -> WorkCalendar:
        return calendars.get(str(node_data.get("calendar_id", ""))) or DEFAULT_CALENDAR

    def _constraints(nd: Dict[str, Any]):
        c = nd.get("constraints", {}) or {}
        for ctype, cdate_s in ((c.get("type"), c.get("date")),
                               (c.get("type2"), c.get("date2"))):
            cdate = _to_date(cdate_s)
            if ctype and ctype != "None" and cdate is not None:
                yield ctype, cdate

    # ── Working subgraph: drop LOE/WBS-summary from the pass ─────────────────
    active_nodes = [n for n, d in graph.nodes(data=True) if not d.get("_excluded")]
    sub = graph.subgraph(active_nodes).copy()

    for n, d in graph.nodes(data=True):
        if d.get("_excluded"):
            result.activities[n] = CpmActivityResult(
                activity_id=n, excluded=True,
                es=_to_date(d.get("start_date")),
                ef=_to_date(d.get("finish_date")))

    # ── Cycle handling: record and break so the pass can proceed ─────────────
    while not nx.is_directed_acyclic_graph(sub):
        try:
            cycle_edges = nx.find_cycle(sub)
        except nx.NetworkXNoCycle:
            break
        cycle_nodes = [u for u, _v in cycle_edges]
        result.cycles.append(cycle_nodes)
        u, v = cycle_edges[-1][0], cycle_edges[-1][1]
        sub.remove_edge(u, v)
        result.warnings.append(
            f"Relationship loop detected ({' → '.join(cycle_nodes)}); "
            f"removed {u} → {v} to compute dates")

    order = list(nx.topological_sort(sub))

    # ── Data date ─────────────────────────────────────────────────────────────
    data_date = _to_date(project.get("data_date"))
    if data_date is None:
        starts = [_to_date(sub.nodes[n].get("actual_start") or sub.nodes[n].get("start_date"))
                  for n in order]
        starts = [s for s in starts if s]
        data_date = min(starts) if starts else date.today()
    result.data_date = data_date
    plan_start = _to_date(project.get("plan_start_date")) or data_date
    start_floor_b = max(data_date, plan_start)

    # Per-node early boundaries and display dates
    sb: Dict[str, date] = {}   # start boundary (morning of ES)
    fb: Dict[str, date] = {}   # finish boundary (morning after EF)
    es: Dict[str, date] = {}
    ef: Dict[str, date] = {}
    # Per-edge forward candidate start boundary it imposed on the successor
    edge_cand: Dict[Tuple[str, str], date] = {}

    def _kind(nd: Dict[str, Any], dur: int) -> str:
        tt = nd.get("task_type", "")
        if tt == "Start Milestone":
            return "start_ms"
        if tt == "Finish Milestone":
            return "finish_ms"
        if dur <= 0:
            return "start_ms"   # zero-duration task: bind at morning boundary
        return "task"

    # ── Forward pass ──────────────────────────────────────────────────────────
    for n in order:
        nd = sub.nodes[n]
        cal = cal_of(nd)
        dur = _scheduling_duration(nd)
        kind = _kind(nd, dur)
        status = nd.get("status", "Not Started")
        a_start = _to_date(nd.get("actual_start"))
        a_finish = _to_date(nd.get("actual_finish"))

        if status == "Complete" and (a_start or a_finish):
            es[n] = a_start or a_finish
            ef[n] = a_finish or a_start
            sb[n] = es[n]
            fb[n] = ef[n] + _ONE_DAY
            continue

        if status == "In Progress" and a_start:
            es[n] = a_start
            sb[n] = a_start
            restart = _snap_morning_fwd(cal, data_date)
            fb[n] = _badd(cal, restart, dur) if dur > 0 else _snap_evening_fwd(cal, data_date)
            ef[n] = fb[n] - _ONE_DAY
            continue

        # Not started: max over incoming relationship candidates (boundary space)
        cands: List[date] = []
        for p in sub.predecessors(n):
            p_cal = cal_of(sub.nodes[p])
            best: Optional[date] = None
            for link in sub[p][n]["links"]:
                ltype, lag = link["type"], int(link.get("lag_days", 0))
                if ltype == "FS":
                    c = _badd(p_cal, fb[p], lag)
                elif ltype == "SS":
                    c = _badd(p_cal, sb[p], lag)
                elif ltype == "FF":
                    fb_req = _badd(p_cal, fb[p], lag)
                    c = _badd(cal, fb_req, -dur) if dur > 0 else fb_req
                else:  # SF
                    fb_req = _badd(p_cal, sb[p], lag)
                    c = _badd(cal, fb_req, -dur) if dur > 0 else fb_req
                if best is None or c > best:
                    best = c
            if best is not None:
                edge_cand[(p, n)] = best
                cands.append(best)

        node_sb = max(cands) if cands else start_floor_b
        node_sb = max(node_sb, data_date)

        # Start-side constraints
        for ctype, cdate in _constraints(nd):
            if ctype == "CS_MSOA":                       # Start On or After
                node_sb = max(node_sb, cdate)
            elif ctype in _MANDATORY_START:              # Mandatory Start / Start On
                if cdate < node_sb:
                    result.warnings.append(
                        f"{n}: mandatory start {cdate} violates network logic "
                        f"(logic-driven start {node_sb})")
                node_sb = cdate
            elif ctype == "CS_ALAP":
                result.warnings.append(
                    f"{n}: As-Late-As-Possible constraint scheduled at early dates "
                    f"(v1 simplification)")

        # Schedule at the boundary
        if kind == "task":
            node_sb = _snap_morning_fwd(cal, node_sb)
            node_fb = _badd(cal, node_sb, dur)
        elif kind == "start_ms":
            node_sb = _snap_morning_fwd(cal, node_sb)
            node_fb = node_sb
        else:  # finish_ms
            node_fb = _snap_evening_fwd(cal, node_sb)
            node_sb = node_fb

        # Finish-side constraints
        for ctype, cdate in _constraints(nd):
            if ctype == "CS_MEOA":                       # Finish On or After
                node_fb = max(node_fb, _snap_evening_fwd(cal, cdate + _ONE_DAY))
            elif ctype in _MANDATORY_FINISH:             # Mandatory Finish / Finish On
                pin_fb = cdate + _ONE_DAY                # end of the constraint day
                if pin_fb < node_fb:
                    result.warnings.append(
                        f"{n}: mandatory finish {cdate} violates network logic "
                        f"(logic-driven finish {node_fb - _ONE_DAY})")
                node_fb = pin_fb
                if kind == "task" and dur > 0:
                    node_sb = _badd(cal, node_fb, -dur)
                else:
                    node_sb = node_fb

        sb[n], fb[n] = node_sb, node_fb
        if kind == "task":
            es[n], ef[n] = node_sb, node_fb - _ONE_DAY
        elif kind == "start_ms":
            es[n] = ef[n] = node_sb
        else:
            es[n] = ef[n] = node_fb - _ONE_DAY

    if not es:
        result.warnings.append("No schedulable activities found")
        return result

    project_finish = max(ef.values())
    result.project_finish = project_finish
    project_finish_fb = max(fb.values())

    must_finish = _to_date(project.get("must_finish_date"))
    if must_finish is not None:
        late_anchor_fb = must_finish + _ONE_DAY   # may finish ON the must-finish date
        if must_finish < project_finish:
            result.warnings.append(
                f"Must-finish date {must_finish} is earlier than computed finish "
                f"{project_finish} — negative float expected")
    else:
        late_anchor_fb = project_finish_fb

    # ── Backward pass (late boundaries) ───────────────────────────────────────
    lsb: Dict[str, date] = {}
    lfb: Dict[str, date] = {}
    ls: Dict[str, date] = {}
    lf: Dict[str, date] = {}

    for n in reversed(order):
        nd = sub.nodes[n]
        cal = cal_of(nd)
        dur = _scheduling_duration(nd)
        kind = _kind(nd, dur)
        status = nd.get("status", "Not Started")

        if status == "Complete":
            lsb[n], lfb[n] = sb[n], fb[n]
            ls[n], lf[n] = es[n], ef[n]
            continue

        cands: List[date] = []
        for s in sub.successors(n):
            s_nd = sub.nodes[s]
            if s_nd.get("status") == "Complete":
                continue  # completed successors have no remaining work to drive
            for link in sub[n][s]["links"]:
                ltype, lag = link["type"], int(link.get("lag_days", 0))
                if ltype == "FS":
                    c = _badd(cal, lsb[s], -lag)                       # cap on fb
                elif ltype == "SS":
                    sb_cap = _badd(cal, lsb[s], -lag)
                    c = _badd(cal, sb_cap, dur) if dur > 0 else sb_cap
                elif ltype == "FF":
                    c = _badd(cal, lfb[s], -lag)
                else:  # SF
                    fb_cap = _badd(cal, lfb[s], -lag)
                    c = _badd(cal, fb_cap, dur) if dur > 0 else fb_cap
                cands.append(c)

        node_lfb = min(cands) if cands else late_anchor_fb

        mandatory = False
        for ctype, cdate in _constraints(nd):
            if ctype == "CS_MEOB":                       # Finish On or Before
                node_lfb = min(node_lfb, cdate + _ONE_DAY)
            elif ctype == "CS_MSOB":                     # Start On or Before
                cap = _badd(cal, _snap_morning_back(cal, cdate), dur) if dur > 0 else cdate
                node_lfb = min(node_lfb, cap)
            elif ctype in _MANDATORY_FINISH or ctype in _MANDATORY_START:
                mandatory = True

        if mandatory:
            # Mandatory constraints pin late dates to early dates
            lsb[n], lfb[n] = sb[n], fb[n]
            ls[n], lf[n] = es[n], ef[n]
            continue

        if kind == "task":
            node_lfb = _snap_evening_back(cal, node_lfb)
            node_lsb = _badd(cal, node_lfb, -dur)
            ls[n], lf[n] = node_lsb, node_lfb - _ONE_DAY
        elif kind == "start_ms":
            node_lsb = _snap_morning_back(cal, node_lfb)
            node_lfb = node_lsb
            ls[n] = lf[n] = node_lsb
        else:  # finish_ms
            node_lfb = _snap_evening_back(cal, node_lfb)
            node_lsb = node_lfb
            ls[n] = lf[n] = node_lfb - _ONE_DAY

        lsb[n], lfb[n] = node_lsb, node_lfb

    # ── Floats, criticality, driving edges ────────────────────────────────────
    min_tf: Optional[int] = None
    for n in order:
        nd = sub.nodes[n]
        cal = cal_of(nd)
        status = nd.get("status", "Not Started")
        r = CpmActivityResult(activity_id=n, es=es[n], ef=ef[n], ls=ls[n], lf=lf[n])

        if status != "Complete":
            r.total_float = _bdist(cal, sb[n], lsb[n])
            # Free float: slack before delaying any successor's early start
            slacks: List[int] = []
            for s in sub.successors(n):
                c = edge_cand.get((n, s))
                if c is not None:
                    slacks.append(_bdist(cal_of(sub.nodes[s]), c, sb[s]))
            r.free_float = min(slacks) if slacks else r.total_float
            r.is_critical = r.total_float is not None and r.total_float <= 0
            if r.total_float is not None:
                min_tf = r.total_float if min_tf is None else min(min_tf, r.total_float)
        result.activities[n] = r

    # Open ends among schedulable activities
    result.open_starts = [n for n in order if sub.in_degree(n) == 0
                          and sub.nodes[n].get("status") != "Complete"]
    result.open_ends = [n for n in order if sub.out_degree(n) == 0
                        and sub.nodes[n].get("status") != "Complete"]

    # ── Critical path chains via driving edges ────────────────────────────────
    critical_tf = 0 if (min_tf is None or min_tf <= 0) else min_tf
    crit_nodes = {n for n, r in result.activities.items()
                  if r.total_float is not None and r.total_float <= critical_tf}
    driving = nx.DiGraph()
    driving.add_nodes_from(crit_nodes)
    for (p, n), cand in edge_cand.items():
        if p in crit_nodes and n in crit_nodes and cand == sb.get(n):
            driving.add_edge(p, n)

    remaining = driving.copy()
    for _ in range(3):
        if remaining.number_of_nodes() == 0:
            break
        try:
            chain = nx.dag_longest_path(remaining)
        except Exception:
            break
        if len(chain) < 2:
            break
        result.critical_paths.append(chain)
        remaining.remove_nodes_from(chain)

    on_path = {n for chain in result.critical_paths for n in chain}
    for n in on_path:
        if n in result.activities:
            result.activities[n].on_driving_path = True

    return result
