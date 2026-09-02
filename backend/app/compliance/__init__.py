"""Configurable compliance-checklist support for the schedule review.

- ``catalog``     — the canonical built-in check definitions (56 checks,
                    single source of truth), each with a ``source_hint``
                    ("graph" or "sp") routing it to an evidence source.
- ``eval_engine`` — the batch evaluation loop: one structured-output LLM
                    call per check (Neo4j facts or Special Provision
                    retrieval), replacing the old single-mega-prompt design.
"""

from app.compliance.catalog import (
    BUILTIN_CHECKS,
    CheckDef,
    builtin_checks_as_dicts,
)
from app.compliance.eval_engine import (
    build_activity_roster,
    build_compliance_facts,
    build_milestones,
    build_narrative_text,
    evaluate_checks,
)

__all__ = [
    "BUILTIN_CHECKS",
    "CheckDef",
    "builtin_checks_as_dicts",
    "build_compliance_facts",
    "build_milestones",
    "build_activity_roster",
    "build_narrative_text",
    "evaluate_checks",
]
