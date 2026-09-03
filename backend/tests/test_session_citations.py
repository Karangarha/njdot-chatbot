"""backend/tests/test_session_citations.py

Unit tests for chat citation-extraction helpers in session.py:
  _build_tool_sources, _tool_message_to_sources, _flatten_activities, etc.

Tests confirm that tool payloads are converted to richer citation dicts with
page numbers, activity lists, and doc_type info (when available).

Runnable two ways:
    python tests/test_session_citations.py
    python -m pytest tests/test_session_citations.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.api.session import (  # noqa: E402
    _build_tool_sources,
    _flatten_activities,
    _looks_like_activity_row,
    _normalize_key,
    _tool_message_to_sources,
)


# Create a class with __name__ == "ToolMessage" for type(m).__name__ checks
class ToolMessage:
    """Minimal ToolMessage stand-in for testing."""
    def __init__(self, name: str, content: str):
        self.name = name
        self.content = content


def _FakeToolMessage(name: str, content: str) -> ToolMessage:
    """Factory to create ToolMessage instances for testing."""
    return ToolMessage(name, content)


def test_search_special_provisions_with_page_pdf():
    """search_special_provisions payload with page_pdf -> doc_type + page_pdf present."""
    payload = {
        "chunks": [
            {
                "page_pdf": 5,
                "heading": "Section 2.1",
                "section_id": "sp_2_1",
                "similarity": 0.95,
                "content": "Some provision text",
            }
        ]
    }
    m = _FakeToolMessage("search_special_provisions", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    assert len(sources) == 1
    assert sources[0]["label"] == "Special Provision"
    assert sources[0]["tool"] == "search_special_provisions"
    assert sources[0]["doc_type"] == "special_provision"
    assert sources[0]["page_pdf"] == 5
    assert sources[0]["similarity"] == 0.95
    assert sources[0]["heading"] == "Section 2.1"
    assert sources[0]["section_id"] == "sp_2_1"


def test_search_narrative_with_sections_and_page_pdf():
    """search_narrative payload with sections (not chunks) + page_pdf -> doc_type="narrative".

    search_narrative's real items key their match score "score" (see
    graph_neo4j/tools.py's search_narrative), not "similarity" like the
    Supabase-backed retrievers -- this fixture matches that real shape so the
    test actually exercises the score->similarity normalization.
    """
    payload = {
        "sections": [
            {
                "page_pdf": 12,
                "heading": "Project Overview",
                "section_id": "nar_1",
                "score": 0.88,
                "content": "Designer narrative text",
            }
        ]
    }
    m = _FakeToolMessage("search_narrative", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    assert len(sources) == 1
    assert sources[0]["label"] == "Designer Narrative"
    assert sources[0]["tool"] == "search_narrative"
    assert sources[0]["doc_type"] == "narrative"
    assert sources[0]["page_pdf"] == 12
    assert sources[0]["similarity"] == 0.88


def test_search_utility_plans_no_page_pdf_fallback():
    """search_utility_plans chunks without page_pdf -> fallback (label-only, no doc_type)."""
    payload = {
        "chunks": [
            {
                "similarity": 0.75,
                "content": "Utility plan text (no page_pdf)",
            }
        ]
    }
    m = _FakeToolMessage("search_utility_plans", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    # Fallback: label-only, no doc_type, no page_pdf
    assert len(sources) == 1
    assert sources[0] == {
        "label": "Utility Agreement Plan",
        "tool": "search_utility_plans",
    }
    assert "doc_type" not in sources[0]
    assert "page_pdf" not in sources[0]


def test_search_utility_plans_empty_chunks():
    """search_utility_plans with empty chunks list -> fallback."""
    payload = {"chunks": []}
    m = _FakeToolMessage("search_utility_plans", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    assert len(sources) == 1
    assert sources[0] == {
        "label": "Utility Agreement Plan",
        "tool": "search_utility_plans",
    }


def test_special_provisions_empty_chunks_falls_back():
    """search_special_provisions with empty chunks list (tool in _TOOL_DOC_TYPE) -> fallback, no doc_type."""
    payload = {"chunks": []}
    m = _FakeToolMessage("search_special_provisions", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    assert len(sources) == 1
    assert sources[0] == {
        "label": "Special Provision",
        "tool": "search_special_provisions",
    }
    # When no page_items are found, fallback is used (no doc_type)
    assert "doc_type" not in sources[0]


def test_get_critical_path_with_chains():
    """get_critical_path payload with chains -> flattened, deduplicated activities.

    Uses real get_critical_path field names (taskId/name/es/ef -- see
    graph_neo4j/tools.py's `RETURN a.taskId AS taskId, ..., a.computedEarlyStart
    AS es, a.computedEarlyFinish AS ef`) rather than already-canonical
    start/finish, so this exercises the es->start / ef->finish alias mapping
    instead of bypassing it.
    """
    payload = {
        "chains": [
            [
                {"taskId": "A1000", "name": "Mobilization", "es": "2024-01-01", "ef": "2024-01-05"},
                {"taskId": "A2000", "name": "Excavation", "es": "2024-01-06", "ef": "2024-02-15"},
            ],
            [
                {"taskId": "A2000", "name": "Excavation", "es": "2024-01-06", "ef": "2024-02-15"},  # Duplicate
                {"taskId": "A3000", "name": "Paving", "es": "2024-02-16", "ef": "2024-03-30"},
            ],
        ]
    }
    m = _FakeToolMessage("get_critical_path", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    assert len(sources) == 1
    assert sources[0]["label"] == "Schedule Graph"
    assert sources[0]["tool"] == "get_critical_path"
    assert "activities" in sources[0]

    activities = sources[0]["activities"]
    # Should have 3 unique activities (A2000 deduplicated)
    assert len(activities) == 3
    task_ids = {a["taskId"] for a in activities}
    assert task_ids == {"A1000", "A2000", "A3000"}

    # Check structure
    for act in activities:
        assert "taskId" in act
        assert "name" in act
        assert "start" in act
        assert "finish" in act


def test_query_schedule_graph_with_dotted_keys():
    """query_schedule_graph raw list with dotted keys (a.taskId, a.startDate) -> normalized."""
    payload = [
        {
            "a.taskId": "A1000",
            "a.name": "Setup",
            "a.computedEarlyStart": "2024-01-01",
            "a.computedEarlyFinish": "2024-01-07",
        },
        {
            "a.taskId": "A2000",
            "a.name": "Main Work",
            "a.startDate": "2024-01-08",  # Different key variant
            "a.finishDate": "2024-03-15",
        },
    ]
    m = _FakeToolMessage("query_schedule_graph", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    assert len(sources) == 1
    assert sources[0]["label"] == "Schedule Graph"
    assert sources[0]["tool"] == "query_schedule_graph"
    assert "activities" in sources[0]

    activities = sources[0]["activities"]
    assert len(activities) == 2
    assert activities[0]["taskId"] == "A1000"
    assert activities[0]["name"] == "Setup"
    assert activities[0]["start"] == "2024-01-01"
    assert activities[0]["finish"] == "2024-01-07"

    assert activities[1]["taskId"] == "A2000"
    assert activities[1]["start"] == "2024-01-08"
    assert activities[1]["finish"] == "2024-03-15"


def test_query_schedule_graph_non_activity_fallback():
    """query_schedule_graph with aggregate/non-activity rows (no taskId/name) -> fallback."""
    payload = [
        {"totalCost": 1000000, "count": 15},  # Aggregate, not an activity
    ]
    m = _FakeToolMessage("query_schedule_graph", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    # Fallback because rows don't look like activities
    assert len(sources) == 1
    assert sources[0] == {
        "label": "Schedule Graph",
        "tool": "query_schedule_graph",
    }
    assert "activities" not in sources[0]


def test_get_edq_coverage_fallback():
    """get_edq_coverage payload -> no special-casing, fallback to label-only."""
    payload = {
        "status": "Pass",
        "items": [
            {"id": "edq:1", "jobId": "0001", "description": "Mobilization"},
        ],
    }
    m = _FakeToolMessage("get_edq_coverage", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    # No special handling for get_edq_coverage; should fall back to label-only
    assert len(sources) == 1
    assert sources[0] == {
        "label": "EDQ Coverage",
        "tool": "get_edq_coverage",
    }
    assert "doc_type" not in sources[0]
    assert "activities" not in sources[0]


def test_malformed_json_fallback():
    """Malformed/non-JSON m.content -> fallback, no exception raised."""
    m = _FakeToolMessage("search_special_provisions", "{ invalid json }")
    sources = _tool_message_to_sources(m)

    # Should gracefully fall back to label-only
    assert len(sources) == 1
    assert sources[0] == {
        "label": "Special Provision",
        "tool": "search_special_provisions",
    }


def test_build_tool_sources_multiple_messages():
    """_build_tool_sources processes multiple ToolMessages and non-ToolMessages."""

    class _FakeNonToolMessage:
        """Message that is not a ToolMessage."""
        def __init__(self, content):
            self.content = content

    messages = [
        _FakeNonToolMessage("some non-tool message"),
        _FakeToolMessage("search_special_provisions", json.dumps({
            "chunks": [{"page_pdf": 5, "content": "text"}]
        })),
        _FakeNonToolMessage("another non-tool"),
        _FakeToolMessage("search_narrative", json.dumps({
            "sections": [{"page_pdf": 10, "content": "narrative text"}]
        })),
    ]
    sources = _build_tool_sources(messages)

    # Should have 2 entries (one per ToolMessage), skipping non-ToolMessages
    assert len(sources) == 2
    assert sources[0]["tool"] == "search_special_provisions"
    assert sources[1]["tool"] == "search_narrative"


def test_normalize_key_strips_prefix_and_lowercases():
    """_normalize_key removes a.prefix and lowercases."""
    assert _normalize_key("taskId") == "taskid"
    assert _normalize_key("a.taskId") == "taskid"
    assert _normalize_key("b.StartDate") == "startdate"
    assert _normalize_key("some.deeply.nested.key") == "key"
    assert _normalize_key("UPPERCASE") == "uppercase"


def test_looks_like_activity_row_detects_valid_activity():
    """_looks_like_activity_row checks for taskId + name."""
    assert _looks_like_activity_row({
        "a.taskId": "A1000",
        "a.name": "Setup",
        "duration": 5,
    }) is True

    assert _looks_like_activity_row({
        "taskId": "A1000",
        "name": "Setup",
    }) is True

    # Missing taskId
    assert _looks_like_activity_row({
        "name": "Setup",
        "duration": 5,
    }) is False

    # Missing name
    assert _looks_like_activity_row({
        "taskId": "A1000",
        "duration": 5,
    }) is False

    # Empty dict
    assert _looks_like_activity_row({}) is False


def test_flatten_activities_deduplicates_and_normalizes():
    """_flatten_activities deduplicates by taskId, normalizes field names."""
    records = [
        {
            "a.taskId": "A1000",
            "a.name": "Mobilization",
            "a.computedEarlyStart": "2024-01-01",
            "ef": "2024-01-07",  # Alias for finish
        },
        {
            "taskId": "A1000",  # Duplicate
            "name": "Mobilization",
            "start": "2024-01-02",  # Different value, but already filled
            "finish": "2024-01-08",  # Different value, but already filled
        },
        {
            "a.taskId": "A2000",
            "a.name": "Excavation",
            "es": "2024-01-08",  # Alias for start
            "computedEarlyFinish": "2024-02-15",  # Alias for finish
        },
    ]
    activities = _flatten_activities(records)

    # Should have 2 unique (by taskId)
    assert len(activities) == 2
    task_ids = [a["taskId"] for a in activities]
    assert task_ids == ["A1000", "A2000"]

    # First value should be from first occurrence
    a1 = activities[0]
    assert a1["taskId"] == "A1000"
    assert a1["name"] == "Mobilization"
    assert a1["start"] == "2024-01-01"  # From first record
    assert a1["finish"] == "2024-01-07"  # From first record (ef alias)


def test_flatten_activities_skips_none_taskid():
    """_flatten_activities skips records with None taskId."""
    records = [
        {
            "taskId": None,
            "name": "No ID",
            "start": "2024-01-01",
        },
        {
            "taskId": "A1000",
            "name": "With ID",
            "start": "2024-01-01",
        },
    ]
    activities = _flatten_activities(records)

    assert len(activities) == 1
    assert activities[0]["taskId"] == "A1000"


def test_flatten_activities_with_non_dict_records():
    """_flatten_activities skips non-dict items gracefully."""
    records = [
        {"taskId": "A1000", "name": "Valid"},
        "not a dict",
        None,
        42,
        {"taskId": "A2000", "name": "Also Valid"},
    ]
    activities = _flatten_activities(records)

    assert len(activities) == 2
    assert activities[0]["taskId"] == "A1000"
    assert activities[1]["taskId"] == "A2000"


def test_search_estimate_with_page_pdf():
    """search_estimate payload with page_pdf -> doc_type="estimate"."""
    payload = {
        "chunks": [
            {
                "page_pdf": 8,
                "heading": "DBE Goal",
                "similarity": 0.92,
                "content": "DBE goal memo text",
            }
        ]
    }
    m = _FakeToolMessage("search_estimate", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    assert len(sources) == 1
    assert sources[0]["doc_type"] == "estimate"
    assert sources[0]["page_pdf"] == 8


def test_search_key_map_with_page_pdf():
    """search_key_map payload with page_pdf -> doc_type="key_map"."""
    payload = {
        "chunks": [
            {
                "page_pdf": 3,
                "heading": "Key Map",
                "similarity": 0.85,
                "content": "Key sheet text",
            }
        ]
    }
    m = _FakeToolMessage("search_key_map", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    assert len(sources) == 1
    assert sources[0]["doc_type"] == "key_map"
    assert sources[0]["page_pdf"] == 3


def test_multiple_page_items_in_single_tool_response():
    """Tool response with multiple page items -> multiple source entries."""
    payload = {
        "chunks": [
            {"page_pdf": 5, "heading": "Section A", "similarity": 0.95, "content": "text 1"},
            {"page_pdf": 12, "heading": "Section B", "similarity": 0.88, "content": "text 2"},
            {"page_pdf": 18, "heading": "Section C", "similarity": 0.76, "content": "text 3"},
        ]
    }
    m = _FakeToolMessage("search_special_provisions", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    assert len(sources) == 3
    page_nums = [s["page_pdf"] for s in sources]
    assert page_nums == [5, 12, 18]


def test_optional_fields_omitted_when_absent():
    """Optional fields (similarity, heading, section_id) omitted when not in payload."""
    payload = {
        "chunks": [
            {
                "page_pdf": 7,
                "content": "text (no similarity, heading, or section_id)",
            }
        ]
    }
    m = _FakeToolMessage("search_special_provisions", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    assert len(sources) == 1
    source = sources[0]
    assert "similarity" not in source
    assert "heading" not in source
    assert "section_id" not in source
    assert source["page_pdf"] == 7


def test_page_pdf_zero_is_not_dropped():
    """page_pdf: 0 is a valid page number and should not be filtered out."""
    payload = {
        "chunks": [
            {
                "page_pdf": 0,
                "heading": "Cover Page",
                "similarity": 0.90,
                "content": "text on page 0",
            }
        ]
    }
    m = _FakeToolMessage("search_special_provisions", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    assert len(sources) == 1
    assert sources[0]["page_pdf"] == 0
    assert sources[0]["heading"] == "Cover Page"


def test_duplicate_pages_deduped_and_capped():
    """Multiple chunks sharing the same page_pdf collapse to one entry (the
    highest-similarity one for that page), and the result is capped to 3
    entries even when more than 3 distinct pages are present."""
    payload = {
        "chunks": [
            {"page_pdf": 12, "heading": "Special Provision p.12 (weak match)", "similarity": 0.70, "content": "a"},
            {"page_pdf": 12, "heading": "Special Provision p.12 (strong match)", "similarity": 0.92, "content": "b"},
            {"page_pdf": 5,  "heading": "Section A", "similarity": 0.85, "content": "c"},
            {"page_pdf": 18, "heading": "Section B", "similarity": 0.80, "content": "d"},
            {"page_pdf": 27, "heading": "Section C", "similarity": 0.60, "content": "e"},
        ]
    }
    m = _FakeToolMessage("search_special_provisions", json.dumps(payload))
    sources = _tool_message_to_sources(m)

    # Capped to 3, one entry per page.
    assert len(sources) == 3
    page_nums = [s["page_pdf"] for s in sources]
    assert len(page_nums) == len(set(page_nums))  # no duplicate pages

    # Page 12's surviving entry is the higher-similarity one.
    p12 = next(s for s in sources if s["page_pdf"] == 12)
    assert p12["similarity"] == 0.92
    assert p12["heading"] == "Special Provision p.12 (strong match)"

    # The lowest-similarity page (27) was dropped by the cap.
    assert 27 not in page_nums

    # Sorted by similarity, descending.
    similarities = [s["similarity"] for s in sources]
    assert similarities == sorted(similarities, reverse=True)


def test_looks_like_activity_row_rejects_cross_node_splice():
    """taskId and name from two different aliased nodes (e.g. a predecessor/
    successor join) must NOT be treated as one activity's identity."""
    assert _looks_like_activity_row({"p.taskId": "A050", "s.name": "Final Paving"}) is False
    # Same alias for both -> still valid.
    assert _looks_like_activity_row({"p.taskId": "A050", "p.name": "Final Paving"}) is True


def test_looks_like_activity_row_recognizes_as_aliased_columns():
    """An LLM-authored `RETURN a.taskId AS id, a.name AS activityName` should
    still be recognized as an activity row instead of silently vanishing."""
    assert _looks_like_activity_row({"id": "A1000", "activityName": "Setup"}) is True


def test_flatten_activities_prefers_computed_dates_over_raw():
    """When a record carries both a raw/reported date and a CPM-computed one,
    the computed value wins regardless of which key iterates first."""
    records = [{
        "taskId": "A1000", "name": "Mobilization",
        "startDate": "2024-01-01", "computedEarlyStart": "2024-01-03",
        "finishDate": "2024-01-10", "computedEarlyFinish": "2024-01-12",
    }]
    activities = _flatten_activities(records)
    assert activities[0]["start"] == "2024-01-03"
    assert activities[0]["finish"] == "2024-01-12"


def test_flatten_activities_capped():
    """Activity lists are capped so a long critical-path chain can't flood
    the chat UI (mirrors the page-citation path's top-3 cap)."""
    records = [{"taskId": f"A{i}", "name": f"Activity {i}"} for i in range(40)]
    activities = _flatten_activities(records)
    assert len(activities) == 25


if __name__ == "__main__":
    # Allow running as a script
    import pytest
    pytest.main([__file__, "-v"])
