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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed!")
