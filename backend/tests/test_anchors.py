"""backend/tests/test_anchors.py

Anchor extraction from a check instruction. Anchors do two jobs: they are the
exact keys for the metadata lookup, and they are the BM25 query. The BM25 part
is why only the first paragraph is read -- websearch_to_tsquery AND-chains
every token, so feeding it a 150-word rule matches nothing.

    python -m pytest backend/tests/test_anchors.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.anchors import extract_anchors  # noqa: E402


def test_extracts_subsection_and_sub_subsection_numbers():
    a = extract_anchors(
        "Special Provisions 105.07.01 Working in the Vicinity of Utilities and "
        "105.07.02 Work Performed by Utilities: \"Advance Notice Requirements\".\n\n"
        "Utilities on the Key Sheet sit within project limits."
    )
    assert a.sections == ("105.07.01", "105.07.02")


def test_extracts_a_table_caption():
    a = extract_anchors(
        "Special Provisions 105.05 WORKING DRAWINGS: \"TABLE 105.05-1 IS CHANGED TO\", "
        "\"Working Drawing Submission Category\", Certified and Approved columns."
    )
    assert a.sections == ("105.05",)
    assert a.tables == ("TABLE 105.05-1",)


def test_extracts_a_section_heading():
    a = extract_anchors(
        "Special Provisions SECTION 702 - TRAFFIC SIGNALS and SECTION 703 - HIGHWAY LIGHTING."
    )
    assert a.sections == ("SECTION 702", "SECTION 703")


def test_reads_only_the_first_paragraph():
    # A number mentioned deep in the rule body is not a retrieval anchor; the
    # first paragraph is where the v3 template puts the ones that are.
    a = extract_anchors("Restricted window: Dec 15 - Mar 15.\n\nStd Spec 504.03.02.C requires a plan.")
    assert a.sections == ()


def test_an_instruction_with_no_anchor_is_empty_not_an_error():
    a = extract_anchors("Confirm the narrative addresses community commitments.")
    assert a.is_empty
    assert a.as_query() == ""


def test_as_query_is_short_and_carries_every_anchor():
    a = extract_anchors(
        "Special Provisions 105.05 WORKING DRAWINGS: \"TABLE 105.05-1 IS CHANGED TO\"."
    )
    q = a.as_query()
    assert "105.05" in q and "TABLE 105.05-1" in q
    assert len(q.split()) <= 12, "the BM25 query must stay short; tsquery ANDs every token"


def test_deduplicates_and_preserves_first_appearance_order():
    a = extract_anchors("105.07 and 105.07.02 and 105.07 again.")
    assert a.sections == ("105.07", "105.07.02")


def test_table_followed_by_ordinary_prose_is_not_an_anchor():
    # "table" followed by a common noun/adverb is not a caption; a false
    # positive here (e.g. "CAREFULLY") would AND-poison the BM25 query.
    #
    # FINDING 7: the previous fixture, "table's two columns", already fails
    # to match on the apostrophe alone (no whitespace immediately after
    # "table", which \s+ requires) -- true under BOTH the pre-fix pattern
    # (re.compile(r"\bTABLE\s+[\dA-Z]+(?:\.[\dA-Z]+)*(?:-\d+)?\b",
    # re.IGNORECASE), commit 5d94940's "before") and the current one, so it
    # never actually exercised the digit/single-letter requirement this test
    # claims to pin. A plain sentence with a real space after "table" does:
    # confirmed the pre-fix pattern above DOES match "table carefully" (as
    # "TABLE CAREFULLY"), which is precisely the real bug commit 5d94940
    # fixed (working_drawing_review_time's actual instruction text produced
    # junk anchors TABLE CAREFULLY / TABLE GETS).
    a = extract_anchors("Read the table carefully.")
    assert a.tables == ()


def test_extracts_a_single_letter_table_from_construction_scheduling_manual():
    a = extract_anchors("Construction Scheduling Manual Table A; see the note.")
    assert a.tables == ("TABLE A",)


def test_pinnable_tables_excludes_bare_single_letter_captions():
    """FINDING 3: a bare "TABLE A" is ambiguous across documents -- three
    checks name the Construction Scheduling Manual's "Table A", and an
    unrelated Special Provision in the measured project happens to carry its
    own unrelated "TABLE A". Pinned rows bypass ranking entirely, so it must
    be excluded from pinnable_tables (used by the PIN filter) while staying
    in tables/as_query() so the keyword leg can still retrieve it and
    ranking can moderate the match. A numbered caption is document-specific
    by construction and stays pinnable."""
    a = extract_anchors(
        "Special Provisions 105.05 WORKING DRAWINGS: \"TABLE 105.05-1 IS CHANGED TO\" "
        "and Construction Scheduling Manual Table A."
    )
    assert a.tables == ("TABLE 105.05-1", "TABLE A")
    assert a.pinnable_tables == ("TABLE 105.05-1",)
    assert "TABLE A" in a.as_query()


def test_working_drawing_review_time_yields_no_prose_table_anchor():
    # Regression pin for the real bug: this check's actual catalog instruction
    # contains "Read the table carefully" and "table gets applied by mistake",
    # which the old case-insensitive, digit-optional _TABLE_RE matched as
    # "TABLE CAREFULLY" / "TABLE GETS". Run the real instruction text, not a
    # synthetic string, since that's what actually shipped the bad anchors.
    from app.compliance.catalog import BUILTIN_CHECKS

    check = next(c for c in BUILTIN_CHECKS if c.check_key == "working_drawing_review_time")
    a = extract_anchors(check.instruction)
    assert "TABLE 105.05-1" in a.tables  # the real caption still extracts
    for t in a.tables:
        identifier = t.split(" ", 1)[1]
        assert not identifier.isalpha(), f"prose leaked into table anchor: {t!r}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed!")
