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

from app.ingestion.section_detector import TABLE_RE as _TABLE_RE

# Mirrors app.ingestion.section_detector's dialect rather than inventing a
# second one: 105.07, 105.07.02, and "SECTION 703".
_SECTION_RE = re.compile(r"\b\d{3,4}\.\d{2}(?:\.\d{2})?\b|\bSECTION\s+\d{3}\b", re.IGNORECASE)
# Table-caption pattern: defined in app.ingestion.section_detector.TABLE_RE
# (that module is the project's authority on heading dialect and imports
# nothing but `re`); imported here under the old private name so the rest of
# this module is unchanged. See that module for why the identifier after
# "TABLE" must be digit-led or a lone letter.

# A bare single-letter caption ("TABLE A") carries no document-specific
# information -- extract_anchors has no notion of which document a table
# belongs to, so "TABLE A" in a check meant for the Construction Scheduling
# Manual matches an unrelated "TABLE A" just as well in a project's Special
# Provision. A numbered caption ("TABLE 105.05-1") is document-specific by
# construction and stays safe to pin.
_AMBIGUOUS_TABLE_RE = re.compile(r"^TABLE\s+[A-Z]$", re.IGNORECASE)


@dataclass(frozen=True)
class Anchors:
    sections: Tuple[str, ...] = ()
    tables: Tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.sections and not self.tables

    @property
    def pinnable_tables(self) -> Tuple[str, ...]:
        """Table anchors safe to PIN by exact metadata match -- excludes
        bare single-letter captions like "TABLE A", which are ambiguous
        across documents and would pin an irrelevant passage at position 0
        (pinned rows bypass ranking entirely, so a bad pin is worse than no
        pin). Still included in ``tables``/``as_query()`` so the keyword leg
        can retrieve it and ranking can moderate it normally."""
        return tuple(t for t in self.tables if not _AMBIGUOUS_TABLE_RE.match(t))

    @property
    def pinnable_is_empty(self) -> bool:
        """True when there is nothing pin_by_anchors could ever pin -- no
        section anchors and no pinnable table anchors. Differs from
        ``is_empty`` exactly when the only anchor found is a bare
        single-letter table caption ("TABLE A"): that's still a real anchor
        for the keyword query (``is_empty`` is False), but pinning never
        attempts it (``pinnable_is_empty`` is True), so a check whose only
        anchor is "TABLE A" must not be reported as a missing-anchor gap --
        pinning never had a chance to fill it. Both ``check_retrieval.
        retrieve_for_check`` and ``app.api.review``'s in-process closure use
        this (not ``is_empty``) to gate ``anchor_missing``."""
        return not self.sections and not self.pinnable_tables

    def as_query(self) -> str:
        """The BM25 query: anchors only, never the rule body."""
        return " ".join((*self.sections, *self.tables))

    def matches_section(self, section_id: str | None) -> bool:
        """Whether a chunk's section_id is pinned by these section anchors --
        either an exact match, or a child one level or more deeper, bounded
        by a literal dot ("105.07" matches "105.07.02" but not "105.070").
        A check naming the parent section must also pin chunks headed by its
        children -- exact match alone misses every document whose headings
        only go one level deeper than the anchor.

        This is the single Python-side implementation of the same rule
        check_retrieval.pin_by_anchors applies server-side via a
        ``metadata->>section_id.like.<anchor>.*`` PostgREST filter (that
        path has no in-memory rows to test against, so it can't call this
        directly) -- in-process callers holding already-fetched chunk
        metadata (app.api.review._sp_chunk_matches_anchors) use this so the
        two never diverge on the boundary rule again.
        """
        if not section_id:
            return False
        if section_id in self.sections:
            return True
        return any(section_id.startswith(f"{s}.") for s in self.sections)


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
