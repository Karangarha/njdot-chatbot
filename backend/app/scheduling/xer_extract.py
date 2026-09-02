"""Canonical XER parsing for the scheduling pipeline.

Moved here from ``app/api/review.py`` (which still re-exports the public
functions for backward compatibility) and extended to capture the fields the
CPM engine and knowledge graph need: typed relationships with lags, P6's own
computed float/critical values, early/late/actual dates, and the PROJECT
table (data date, must-finish date).

All duration/float day-conversions use each calendar's real ``day_hr_cnt``
rather than xerparser's hardcoded 8 h/day.
"""

from __future__ import annotations

import logging
from datetime import date as _date, timedelta as _td
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from xerparser import Xer

logger = logging.getLogger(__name__)

_DEFAULT_HOURS_PER_DAY = 8.0


def _iso(dt) -> Optional[str]:
    """Format a datetime/date as YYYY-MM-DD, or None."""
    return dt.strftime("%Y-%m-%d") if dt else None


def _hours_to_days(hours: Optional[float], hours_per_day: float) -> Optional[int]:
    if hours is None:
        return None
    hpd = hours_per_day or _DEFAULT_HOURS_PER_DAY
    return round(hours / hpd)


# ── Activities ─────────────────────────────────────────────────────────────────

def _activities_from_xer(xer: Xer, cal_hours: Dict[str, float]) -> list:
    """Build extended activity dicts from a parsed Xer object.

    ``cal_hours`` maps clndr_id → hours-per-day for that calendar (used to
    convert raw *_hr_cnt fields to days).
    """
    task_id_map = {}
    activities_data = {}

    def get_wbs_path(wbs_id):
        path = []
        if hasattr(xer, 'wbs_nodes'):
            current_wbs = xer.wbs_nodes.get(wbs_id)
            while current_wbs:
                wbs_name = getattr(current_wbs, 'wbs_name', getattr(current_wbs, 'name', ''))
                if wbs_name:
                    path.insert(0, wbs_name)
                parent_id = getattr(current_wbs, 'parent_wbs_id', None)
                current_wbs = xer.wbs_nodes.get(parent_id) if parent_id else None
        return path

    def _hpd(clndr_id) -> float:
        return cal_hours.get(str(clndr_id or ""), _DEFAULT_HOURS_PER_DAY)

    for activity in xer.tasks.values():
        internal_db_key = activity.uid
        human_readable_id = getattr(activity, 'task_code', '')
        task_id_map[internal_db_key] = human_readable_id

        wbs_id = getattr(activity, 'wbs_id', None)
        hierarchy_path = get_wbs_path(wbs_id) if wbs_id else []

        try:
            task_type = activity.type.value
        except Exception:
            task_type = "Task Dependent"
        try:
            status = activity.status.value
        except Exception:
            status = "Not Started"

        hpd = _hpd(getattr(activity, 'clndr_id', ''))
        total_float_hrs = getattr(activity, 'total_float_hr_cnt', None)
        free_float_hrs = getattr(activity, 'free_float_hr_cnt', None)
        target_hrs = getattr(activity, 'target_drtn_hr_cnt', None)
        remain_hrs = getattr(activity, 'remain_drtn_hr_cnt', None)

        activities_data[human_readable_id] = {
            # ── original fields (unchanged shape, consumed by xer_to_markdown) ──
            "activity_id": human_readable_id,
            "activity_name": getattr(activity, 'name', ''),
            "wbs_path": hierarchy_path,
            "start_date": _iso(getattr(activity, 'start', None)),
            "finish_date": _iso(getattr(activity, 'finish', None)),
            "duration_days": int(getattr(activity, 'duration', 0) or 0),
            "total_float": int(getattr(activity, 'total_float', 0) or 0),
            "calendar_id": getattr(activity, 'clndr_id', ''),
            "predecessors": [],
            "successors": [],
            "constraints": {
                "type": getattr(activity, 'cstr_type', None) or "None",
                "date": _iso(getattr(activity, 'cstr_date', None)),
                "type2": getattr(activity, 'cstr_type2', None),
                "date2": _iso(getattr(activity, 'cstr_date2', None)),
            },
            # ── extended fields for CPM + knowledge graph ──
            "task_type": task_type,
            "status": status,
            "original_duration_days": _hours_to_days(target_hrs, hpd) or 0,
            "remaining_duration_days": _hours_to_days(remain_hrs, hpd) or 0,
            "total_float_hrs": total_float_hrs,
            "free_float_hrs": free_float_hrs,
            "stored_total_float_days": _hours_to_days(total_float_hrs, hpd),
            "stored_free_float_days": _hours_to_days(free_float_hrs, hpd),
            "is_longest_path": bool(getattr(activity, 'is_longest_path', False)),
            "float_path": getattr(activity, 'float_path', None),
            "float_path_order": getattr(activity, 'float_path_order', None),
            "early_start": _iso(getattr(activity, 'early_start_date', None)),
            "early_finish": _iso(getattr(activity, 'early_end_date', None)),
            "late_start": _iso(getattr(activity, 'late_start_date', None)),
            "late_finish": _iso(getattr(activity, 'late_end_date', None)),
            "actual_start": _iso(getattr(activity, 'act_start_date', None)),
            "actual_finish": _iso(getattr(activity, 'act_end_date', None)),
            "pred_links": [],
            "succ_links": [],
        }

    if hasattr(xer, 'relationships'):
        for relation in xer.relationships.values():
            pred_internal_key = getattr(relation, 'pred_task_id', None)
            succ_internal_key = getattr(relation, 'task_id', None)

            pred_readable_id = task_id_map.get(pred_internal_key)
            succ_readable_id = task_id_map.get(succ_internal_key)

            rel_type = (getattr(relation, 'pred_type', '') or '')[-2:].upper()
            if rel_type not in ("FS", "SS", "FF", "SF"):
                rel_type = "FS"
            lag_hrs = getattr(relation, 'lag_hr_cnt', 0) or 0
            # Lag elapses on the predecessor's calendar (P6 default setting)
            pred_task = getattr(relation, 'predecessor', None)
            lag_hpd = _hpd(getattr(pred_task, 'clndr_id', '') if pred_task else '')
            lag_days = round(lag_hrs / (lag_hpd or _DEFAULT_HOURS_PER_DAY))

            if pred_readable_id and pred_readable_id in activities_data:
                activities_data[pred_readable_id]["successors"].append(succ_readable_id)
                activities_data[pred_readable_id]["succ_links"].append({
                    "activity_id": succ_readable_id,
                    "type": rel_type,
                    "lag_days": lag_days,
                    "lag_hrs": lag_hrs,
                })

            if succ_readable_id and succ_readable_id in activities_data:
                activities_data[succ_readable_id]["predecessors"].append(pred_readable_id)
                activities_data[succ_readable_id]["pred_links"].append({
                    "activity_id": pred_readable_id,
                    "type": rel_type,
                    "lag_days": lag_days,
                    "lag_hrs": lag_hrs,
                })

    return list(activities_data.values())


def parse_xer_to_json(xer_text: str) -> list:
    """Parse XER text into a list of extended activity dicts."""
    xer = _build_xer(xer_text)
    cal_hours = {c["id"]: c.get("day_hr_cnt") or _DEFAULT_HOURS_PER_DAY
                 for c in parse_xer_calendars(xer_text)}
    return _activities_from_xer(xer, cal_hours)


def _build_xer(xer_text: str) -> Xer:
    try:
        return Xer(xer_text)
    except Exception as e:
        logger.error(f"Error parsing XER content: {e}")
        raise HTTPException(status_code=400, detail="Failed to parse XER file.")


# ── Project ────────────────────────────────────────────────────────────────────

def parse_xer_project(xer_or_text) -> Optional[Dict[str, Any]]:
    """Extract project-level fields (data date, must-finish, planned finish).

    Accepts either raw XER text or an already-constructed ``Xer`` object.
    When the file holds several export-flagged projects, picks the one that
    owns the most tasks and lists the others in ``other_projects``.
    """
    xer = xer_or_text if isinstance(xer_or_text, Xer) else _build_xer(xer_or_text)
    projects = getattr(xer, 'projects', {}) or {}
    if not projects:
        return None

    task_counts: Dict[str, int] = {}
    for task in getattr(xer, 'tasks', {}).values():
        pid = getattr(task, 'proj_id', None)
        if pid:
            task_counts[pid] = task_counts.get(pid, 0) + 1

    main_id = max(projects, key=lambda pid: task_counts.get(pid, 0))
    proj = projects[main_id]

    try:
        name = proj.name
    except Exception:
        name = ""

    result = {
        "proj_id": getattr(proj, 'uid', main_id),
        "project_code": getattr(proj, 'short_name', ''),
        "project_name": name or getattr(proj, 'short_name', ''),
        "data_date": _iso(getattr(proj, 'data_date', None)),
        "plan_start_date": _iso(getattr(proj, 'plan_start_date', None)),
        "scd_finish_date": _iso(getattr(proj, 'finish_date', None)),
        "must_finish_date": _iso(getattr(proj, 'must_finish_date', None)),
        "last_schedule_date": _iso(getattr(proj, 'last_schedule_date', None)),
        "other_projects": [
            getattr(p, 'short_name', pid)
            for pid, p in projects.items() if pid != main_id
        ],
    }
    if result["other_projects"]:
        logger.warning(
            "XER contains %d export-flagged projects; using %s (most tasks)",
            len(projects), result["project_code"],
        )
    return result


# ── Calendars ──────────────────────────────────────────────────────────────────

def parse_xer_calendars(xer_text: str) -> list:
    """Extract calendar definitions from raw XER text by parsing the CALENDAR table directly.

    xerparser exposes clndr_data as a scalar float (day_hr_cnt) rather than the
    nested parenthetical string we need, so we bypass it and read the raw rows.

    clndr_data encodes working days in a DaysOfWeek section where day numbers
    1–7 map to Sun–Sat.  A day is working when its entry contains at least one
    work-period marker (s|HH:MM), otherwise it is empty (non-working).
    Exception dates are Primavera serials (days since Dec 30, 1899).
    """
    import re as _re

    _P6_EPOCH = _date(1899, 12, 30)
    _ABBREV   = {1: "Sun", 2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat"}

    # ── Parse CALENDAR table rows from raw XER text ───────────────────────────
    in_calendar   = False
    clndr_id_idx  = clndr_name_idx = clndr_data_idx = day_hr_idx = -1
    raw_calendars: dict = {}   # clndr_id (str) → {name, clndr_data, day_hr_cnt}

    for line in xer_text.splitlines():
        if line.startswith("%T\t"):
            in_calendar = (line.strip() == "%T\tCALENDAR")
            clndr_id_idx = clndr_name_idx = clndr_data_idx = day_hr_idx = -1
            continue
        if not in_calendar:
            continue
        if line.startswith("%F\t"):
            fields = line[3:].split("\t")
            try:
                clndr_id_idx   = fields.index("clndr_id")
                clndr_name_idx = fields.index("clndr_name")
                clndr_data_idx = fields.index("clndr_data")
            except ValueError:
                pass
            try:
                day_hr_idx = fields.index("day_hr_cnt")
            except ValueError:
                day_hr_idx = -1
            continue
        if line.startswith("%R\t") and clndr_id_idx != -1:
            fields = line[3:].split("\t")
            try:
                cal_id   = fields[clndr_id_idx].strip()
                cal_name = fields[clndr_name_idx].strip() if clndr_name_idx < len(fields) else ""
                cal_data = fields[clndr_data_idx].strip() if clndr_data_idx < len(fields) else ""
            except IndexError:
                continue
            day_hr_cnt = None
            if 0 <= day_hr_idx < len(fields):
                try:
                    day_hr_cnt = float(fields[day_hr_idx].strip().replace(",", "."))
                except (ValueError, AttributeError):
                    day_hr_cnt = None
            raw_calendars[cal_id] = {
                "name": cal_name, "clndr_data": cal_data, "day_hr_cnt": day_hr_cnt,
            }

    # ── Helpers to parse a clndr_data string ─────────────────────────────────

    def _working_days(clndr_data: str) -> list:
        """Return abbreviated day names for days that have work periods."""
        if not clndr_data:
            return []
        dow_start = clndr_data.find("DaysOfWeek()(")
        if dow_start == -1:
            return []
        exc_pos  = clndr_data.find("(0||Exceptions", dow_start)
        view_pos = clndr_data.find("(0||VIEW",       dow_start)
        end = min(p for p in [exc_pos, view_pos, len(clndr_data)] if p != -1)
        dow_text = clndr_data[dow_start:end]

        working = []
        for day_num in range(1, 8):
            # Non-working days have exactly (0||N()()) — empty children list
            non_working = f"(0||{day_num}()())" in dow_text
            has_entry   = f"(0||{day_num}()("  in dow_text
            if has_entry and not non_working:
                working.append(_ABBREV[day_num])
        return working

    def _exception_dates(clndr_data: str) -> tuple:
        """Split the Exceptions section into (non-working, working) date lists.

        P6 encodes two flavors: ``(d|serial)()`` is a non-working holiday,
        while ``(d|serial)(<shift data>)`` is a working exception that
        overrides the weekly pattern (e.g. in-water work windows painted onto
        specific dates).  Treating both as holidays wrecks calendar math.
        """
        if not clndr_data:
            return [], []
        exc_start = clndr_data.find("(0||Exceptions()(")
        if exc_start == -1:
            return [], []
        exc_text = clndr_data[exc_start:]
        non_working, working = [], []
        for m in _re.finditer(r'd\|(\d+)\)\((.)', exc_text, _re.DOTALL):
            try:
                d = _P6_EPOCH + _td(days=int(m.group(1)))
                entry = {"date": d.strftime("%Y-%m-%d"), "name": ""}
                if m.group(2) == ")":
                    non_working.append(entry)
                else:
                    working.append(entry)
            except Exception:
                pass
        return non_working, working

    # ── Build output ──────────────────────────────────────────────────────────
    calendars = []
    for cal_id, cal in raw_calendars.items():
        work_days = _working_days(cal["clndr_data"])
        non_working, working = _exception_dates(cal["clndr_data"])
        calendars.append({
            "id":         cal_id,
            "name":       cal["name"] or "Project Calendar",
            "work_days":  work_days,
            "exceptions": sorted(non_working, key=lambda e: e["date"]),
            "work_exceptions": sorted(working, key=lambda e: e["date"]),
            "day_hr_cnt": cal["day_hr_cnt"] or _DEFAULT_HOURS_PER_DAY,
        })

    return calendars


# ── Combined entry point ───────────────────────────────────────────────────────

def parse_xer_all(xer_text: str) -> Dict[str, Any]:
    """Parse everything needed from an XER in one pass (single Xer construction).

    Returns ``{"activities", "calendars", "project"}`` — the preferred entry
    point for the review and session pipelines.
    """
    xer = _build_xer(xer_text)
    calendars = parse_xer_calendars(xer_text)
    cal_hours = {c["id"]: c.get("day_hr_cnt") or _DEFAULT_HOURS_PER_DAY
                 for c in calendars}
    return {
        "activities": _activities_from_xer(xer, cal_hours),
        "calendars": calendars,
        "project": parse_xer_project(xer),
    }
