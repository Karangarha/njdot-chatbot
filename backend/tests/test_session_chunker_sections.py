"""backend/tests/test_session_chunker_sections.py

Special Provision chunks used to carry only doc_type, page_pdf and
chunk_index, cut wherever a 600-token window landed, so a chunk routinely
began inside 105.05 and ended inside 105.07 and nothing knew which clause any
passage belonged to. section_detector already recognises every NJDOT heading
form and already feeds the static specs ingestion; it was never pointed at
per-project uploads.

    python -m pytest backend/tests/test_session_chunker_sections.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.ingestion.session_chunker import chunk_special_provision  # noqa: E402


def _pages(*texts):
    return [{"page_num": i + 1, "text": t} for i, t in enumerate(texts)]


def test_each_chunk_carries_the_section_it_belongs_to():
    pages = _pages(
        "105.05 WORKING DRAWINGS\n"
        "Submit working drawings as specified. TABLE 105.05-1 IS CHANGED TO the following.\n"
        "105.06 COOPERATION WITH OTHERS\n"
        "Cooperate with other contractors working within the project limits.\n"
    )
    chunks = chunk_special_provision(pages)
    by_section = {c["metadata"].get("section_id") for c in chunks}
    assert "105.05" in by_section
    assert "105.06" in by_section


def test_a_chunk_never_spans_two_detected_sections():
    pages = _pages(
        "105.05 WORKING DRAWINGS\nFirst clause body.\n"
        "105.06 COOPERATION WITH OTHERS\nSecond clause body.\n"
    )
    for c in chunk_special_provision(pages):
        assert "105.06 COOPERATION" not in c["content"] or c["metadata"]["section_id"] == "105.06"


def test_table_captions_are_recorded_in_metadata():
    pages = _pages("105.05 WORKING DRAWINGS\nTABLE 105.05-1 IS CHANGED TO the following.\n")
    chunks = chunk_special_provision(pages)
    assert any("TABLE 105.05-1" in c["metadata"].get("tables", []) for c in chunks)


def test_section_title_is_captured():
    chunks = chunk_special_provision(_pages("105.05 WORKING DRAWINGS\nBody text here.\n"))
    assert any(c["metadata"].get("section_title") == "WORKING DRAWINGS" for c in chunks)


def test_text_before_any_heading_still_becomes_a_chunk_with_no_section():
    chunks = chunk_special_provision(_pages("Cover page boilerplate with no heading at all.\n"))
    assert chunks
    assert chunks[0]["metadata"].get("section_id") is None


def test_existing_metadata_fields_are_preserved():
    chunks = chunk_special_provision(_pages("105.05 WORKING DRAWINGS\nBody.\n"))
    m = chunks[0]["metadata"]
    assert m["doc_type"] == "special_provision"
    assert "page_pdf" in m and "chunk_index" in m


def test_a_long_section_still_splits_into_overlapping_windows():
    body = " ".join(f"word{i}" for i in range(4000))
    chunks = chunk_special_provision(_pages(f"105.05 WORKING DRAWINGS\n{body}\n"))
    assert len(chunks) > 1
    assert all(c["metadata"]["section_id"] == "105.05" for c in chunks)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed!")
