"""Section header detector for NJDOT PDF documents.

Classifies individual text lines as one of six heading levels:

    division / section / subsection / sub_subsection / appendix / decimal_section

and extracts a canonical (section_id, title) pair from each match.

Usage
-----
    from app.ingestion.section_detector import detect

    match = detect("SECTION 101 – DESCRIPTION")
    # → {"level": "section", "section_id": "SECTION 101", "title": "DESCRIPTION"}

    match = detect("202.03 MATERIALS")
    # → {"level": "subsection", "section_id": "202.03", "title": "MATERIALS"}

    match = detect("7.0 Introduction")
    # → {"level": "decimal_section", "section_id": "7.0", "title": "Introduction"}

    match = detect("Some ordinary sentence.")
    # → None
"""

from __future__ import annotations

import re
from typing import Optional, TypedDict


# ── Exact patterns specified by the project ──────────────────────────────────

DIVISION        = r'^DIVISION\s+\d+\s+[–—-]\s+.+$'
SECTION         = r'^SECTION\s+\d+\s+[–—-]\s+.+$'
SUBSECTION      = r'^\d{3,4}\.\d{2}\s+[A-Z][A-Z\s,/()-]+$'
SUB_SUB         = r'^\d{3,4}\.\d{2}\.\d{2}\s+[A-Z].+$'
APPENDIX        = r'^NJDOT\s+[A-Z]-\d+\s+[–—-]\s+.+$'
# Decimal-chapter format used by the Construction Scheduling Manual
# (e.g. "1.0 Introduction", "7.2 Final Submission").
# Intentionally placed AFTER SUB_SUB and SUBSECTION in _PATTERNS so that
# the stricter three-digit specs patterns always win when they match.
DECIMAL_SECTION = r'^\d+\.\d+\s+[A-Z].+$'


# ── Compiled patterns (most-specific first to prevent false matches) ──────────
#
#   Priority order:
#     sub_subsection  (^\d{3}\.\d{2}\.\d{2}…)   most specific
#     subsection      (^\d{3}\.\d{2}…  all-caps)
#     decimal_section (^\d+\.\d+…)               general — catches scheduling headings
#                                                 without stealing specs subsections
#     division / section / appendix

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("sub_subsection",  re.compile(SUB_SUB,         re.MULTILINE)),
    ("subsection",      re.compile(SUBSECTION,      re.MULTILINE)),
    ("decimal_section", re.compile(DECIMAL_SECTION, re.MULTILINE)),
    ("division",        re.compile(DIVISION,        re.MULTILINE)),
    ("section",         re.compile(SECTION,         re.MULTILINE)),
    ("appendix",        re.compile(APPENDIX,        re.MULTILINE)),
]

# Matches the em-dash / en-dash / ASCII-hyphen separator used in
# DIVISION, SECTION, and APPENDIX headings.
_DASH_RE = re.compile(r'\s+[–—-]\s+')

# Table-caption dialect shared with app.compliance.anchors and
# app.ingestion.session_chunker: a caption recorded in chunk metadata must be
# matchable by an anchor extracted from a check instruction later, so both
# sides use this same pattern object rather than two textually-similar but
# different ones. The identifier after "TABLE" must be digit-led
# ("105.05-1") or a lone letter ("Table A") -- not just any word -- so
# ordinary prose like "read the table carefully" is never misread as a
# caption. Public: this module is the project's authority on heading
# dialect and imports nothing but `re`, so it is the dependency-safe home
# for this pattern (compliance may depend on ingestion; not the reverse).
TABLE_RE = re.compile(
    r"\bTABLE\s+(?:\d[\dA-Z]*(?:\.[\dA-Z]+)*(?:-\d+)?|[A-Z]\b)", re.IGNORECASE
)


# ── Public types ──────────────────────────────────────────────────────────────

class SectionMatch(TypedDict):
    level:      str   # division | section | subsection | sub_subsection | appendix | decimal_section
    section_id: str   # e.g. "SECTION 101", "202.03", "202.03.01", "NJDOT A-1", "7.0"
    title:      str   # e.g. "DESCRIPTION", "MATERIALS", "Scope", "Introduction"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_heading(level: str, line: str) -> tuple[str, str]:
    """
    Return ``(section_id, title)`` for an already-matched heading line.

    Format rules
    ------------
    division / section / appendix
        ``<keyword + number> [–—-] <title>``
        Split on the dash separator; left side is the id, right side is the title.

    subsection / sub_subsection / decimal_section
        ``<numeric_code>  <title>``
        The first whitespace-delimited token is the id; the rest is the title.
    """
    line = line.strip()

    if level in ("division", "section", "appendix"):
        parts = _DASH_RE.split(line, maxsplit=1)
        section_id = parts[0].strip()
        title      = parts[1].strip() if len(parts) > 1 else ""
        return section_id, title

    # subsection / sub_subsection / decimal_section  →  "202.03 MATERIALS" or "7.0 Introduction"
    parts = line.split(maxsplit=1)
    section_id = parts[0]
    title      = parts[1] if len(parts) > 1 else ""
    return section_id, title


# ── Public API ────────────────────────────────────────────────────────────────

def detect(line: str) -> Optional[SectionMatch]:
    """
    Test a single text line against all heading patterns.

    Patterns are evaluated in priority order (most-specific first):

        sub_subsection → subsection → decimal_section → division → section → appendix

    Parameters
    ----------
    line : str
        A single line of text.  Leading / trailing whitespace is stripped
        internally before matching.

    Returns
    -------
    SectionMatch or None
        A typed dict with ``level``, ``section_id``, and ``title`` when the
        line matches any pattern; ``None`` otherwise.

    Examples
    --------
    >>> detect("DIVISION 100 – GENERAL REQUIREMENTS")
    {'level': 'division', 'section_id': 'DIVISION 100', 'title': 'GENERAL REQUIREMENTS'}

    >>> detect("SECTION 101 – DESCRIPTION")
    {'level': 'section', 'section_id': 'SECTION 101', 'title': 'DESCRIPTION'}

    >>> detect("202.03 MATERIALS")
    {'level': 'subsection', 'section_id': '202.03', 'title': 'MATERIALS'}

    >>> detect("202.03.01 Scope of supply")
    {'level': 'sub_subsection', 'section_id': '202.03.01', 'title': 'Scope of supply'}

    >>> detect("NJDOT A-1 – Appendix Title")
    {'level': 'appendix', 'section_id': 'NJDOT A-1', 'title': 'Appendix Title'}

    >>> detect("7.0 Introduction")
    {'level': 'decimal_section', 'section_id': '7.0', 'title': 'Introduction'}

    >>> detect("1.1 Definitions")
    {'level': 'decimal_section', 'section_id': '1.1', 'title': 'Definitions'}

    >>> detect("This is ordinary body text.") is None
    True
    """
    line = line.strip()
    if not line:
        return None

    for level, pattern in _PATTERNS:
        if pattern.match(line):
            section_id, title = _parse_heading(level, line)
            return SectionMatch(level=level, section_id=section_id, title=title)

    return None


# ── Quick self-test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    _CASES: list[tuple[str, str | None]] = [
        # (input line,  expected level or None)

        # ── specs patterns (unchanged behaviour) ──────────────────────────────
        ("DIVISION 100 – GENERAL REQUIREMENTS",     "division"),
        ("DIVISION 200 — MATERIALS",                "division"),
        ("SECTION 101 – DESCRIPTION",               "section"),
        ("SECTION 202 - MATERIALS",                 "section"),
        ("202.03 MATERIALS",                        "subsection"),
        ("101.05 SCOPE, PURPOSE AND APPLICABILITY", "subsection"),
        ("202.03.01 General requirements",          "sub_subsection"),
        ("NJDOT A-1 – Special Provisions",          "appendix"),

        # ── decimal_section (scheduling manual format) ────────────────────────
        ("7.0 Designer Contract Time Determination", "decimal_section"),
        ("1.0 Introduction",                         "decimal_section"),
        ("1.1 Definitions",                          "decimal_section"),
        ("7.2 Final Submission",                     "decimal_section"),
        # specs subsection must NOT be reclassified as decimal_section
        ("202.03 MATERIALS",                         "subsection"),

        # ── non-matches ───────────────────────────────────────────────────────
        ("This is ordinary body text.",             None),
        ("",                                        None),
        ("1.  Introduction",                        None),   # dot + spaces, not decimal
        ("4.2.1 RESP Responsibility",               None),   # two dots → no match
    ]

    print("\n🧪 section_detector self-test")
    print("=" * 60)
    passed = 0
    for line, expected_level in _CASES:
        result = detect(line)
        got_level = result["level"] if result else None
        ok = got_level == expected_level
        status = "✅" if ok else "❌"
        if ok:
            passed += 1
        preview = repr(line)[:50]
        print(f"  {status}  {preview:<52} → {got_level}")
        if not ok:
            print(f"       expected: {expected_level}")

    print("=" * 60)
    print(f"\n  {passed}/{len(_CASES)} tests passed\n")
