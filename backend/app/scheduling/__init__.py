"""Deterministic schedule computation: XER extraction, calendars, CPM, cross-check.

This package owns everything computed from a Primavera P6 .xer file so that
LLM prompts never have to do network math themselves:

- ``xer_extract``  — canonical XER parsing (activities, calendars, project)
- ``calendar``     — working-day date arithmetic per P6 calendar
- ``cpm``          — network build + forward/backward pass + floats + critical paths
- ``crosscheck``   — computed-vs-P6-stored comparison report
"""

from app.scheduling.xer_extract import (
    parse_xer_all,
    parse_xer_calendars,
    parse_xer_project,
    parse_xer_to_json,
)
from app.scheduling.calendar import WorkCalendar, build_calendars
from app.scheduling.cpm import CpmActivityResult, CpmResult, build_network, run_cpm
from app.scheduling.crosscheck import cross_check

__all__ = [
    "parse_xer_all",
    "parse_xer_calendars",
    "parse_xer_project",
    "parse_xer_to_json",
    "WorkCalendar",
    "build_calendars",
    "CpmActivityResult",
    "CpmResult",
    "build_network",
    "run_cpm",
    "cross_check",
]
