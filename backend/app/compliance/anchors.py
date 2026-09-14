"""Retrieval anchors extracted from a check instruction.

An anchor is a section or table the check names explicitly: "105.07.02",
"SECTION 703", "TABLE 105.05-1". Anchors do two jobs.

1. They are exact keys for the metadata lookup in check_retrieval, which pins
   the named clause into the evidence with no ranking involved.
2. They are the BM25 query. That is why only the FIRST PARAGRAPH is read:
   websearch_to_tsquery AND-chains every surviving token, so handing it a
   150-word rule body matches zero rows. The v3 instruction template puts the
   anchors in the opening sentence for exactly this reason.

Pure: no database, no engine import, so the probe script can use it directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Tuple

# Mirrors app.ingestion.section_detector's dialect rather than inventing a
# second one: 105.07, 105.07.02, and "SECTION 703".
_SECTION_RE = re.compile(r"\b\d{3,4}\.\d{2}(?:\.\d{2})?\b|\bSECTION\s+\d{3}\b", re.IGNORECASE)
# The identifier after "TABLE" must be digit-bearing ("105.05-1") or a lone
# letter not itself the start of a longer word ("A" in "Table A", but not the
# "c" in "table carefully"); otherwise ordinary prose like "table gets" reads
# as a caption.
_TABLE_RE = re.compile(r"\bTABLE\s+(?:\d[\dA-Z]*(?:\.[\dA-Z]+)*(?:-\d+)?|[A-Z]\b)", re.IGNORECASE)


@dataclass(frozen=True)
class Anchors:
    sections: Tuple[str, ...] = ()
    tables: Tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.sections and not self.tables

    def as_query(self) -> str:
        """The BM25 query: anchors only, never the rule body."""
        return " ".join((*self.sections, *self.tables))


def _dedupe(values) -> Tuple[str, ...]:
    seen, out = set(), []
    for v in values:
        norm = v.upper() if v.upper().startswith(("SECTION", "TABLE")) else v
        norm = re.sub(r"\s+", " ", norm).strip()
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return tuple(out)


def extract_anchors(instruction: str) -> Anchors:
    """Pull section and table anchors from an instruction's first paragraph."""
    head = (instruction or "").split("\n\n", 1)[0]
    table_matches = list(_TABLE_RE.finditer(head))
    tables = _dedupe(m.group(0) for m in table_matches)
    # A table caption embeds a section-shaped number (e.g. "105.05" inside
    # "TABLE 105.05-1"); skip only the section match that overlaps a table
    # match's span, not every occurrence of that same text elsewhere in the
    # paragraph -- a standalone "105.05" section reference is still real.
    table_spans = [m.span() for m in table_matches]
    sections = _dedupe(
        m.group(0)
        for m in _SECTION_RE.finditer(head)
        if not any(a <= m.start() and m.end() <= b for a, b in table_spans)
    )
    return Anchors(sections=sections, tables=tables)
