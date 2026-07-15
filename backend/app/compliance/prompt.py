"""Shared rendering utility for compliance-check text blocks.

The old monolithic ``build_system_prompt`` (one LLM call covering all 56
checks) is retired — see ``app.compliance.eval_engine`` for its replacement,
a per-check batch loop with ``.with_structured_output()``.  ``_render_checks_block``
is kept as a utility (e.g. for a future "preview full checklist" debug view);
nothing in the live review path currently calls it.
"""

from __future__ import annotations

from typing import Iterable, List, Mapping


def _render_checks_block(checks: Iterable[Mapping]) -> str:
    """Render the dynamic CHECKS TO RUN block, grouped by category (first-seen order)."""
    groups: "dict[str, List[Mapping]]" = {}
    order: List[str] = []
    for c in checks:
        cat = (c.get("category") or "Uncategorized").strip()
        if cat not in groups:
            groups[cat] = []
            order.append(cat)
        groups[cat].append(c)

    lines: List[str] = []
    for cat in order:
        lines.append("")
        lines.append(f"CATEGORY: {cat}")
        for c in groups[cat]:
            key = (c.get("check_key") or c.get("id") or "").strip()
            name = (c.get("name") or "").strip()
            instruction = (c.get("instruction") or "").strip()
            line = f'- id: "{key}", name: "{name}"'
            if instruction:
                line += f" — {instruction}"
            lines.append(line)
    return "\n".join(lines).lstrip("\n")


def build_system_prompt(checks: Iterable[Mapping] | None = None) -> str:
    """Build the review system prompt for the given checks.

    ``checks`` is an iterable of mappings with keys ``check_key`` (or ``id``),
    ``category``, ``name``, and ``instruction``.  When ``None``, the full
    built-in catalog is used.  Passing an explicit list renders exactly those
    checks (an empty list yields an empty CHECKS TO RUN block — it does NOT
    fall back to the full catalog). Every check is evaluated together in one
    call; there is no separate engine or check type to filter out.
    """
    # Local import avoids a circular import at module load time.
    from app.compliance.catalog import builtin_checks_as_dicts

    checks_list = list(checks) if checks is not None else builtin_checks_as_dicts()

    return _TEMPLATE.format(checks_block=_render_checks_block(checks_list))
