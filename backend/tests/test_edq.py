"""backend/tests/test_edq.py

No LLM, no network, no Neo4j (graph is a fake dispatching by a substring
unique to each real query in app.compliance.edq).
Runnable two ways:
    python tests/test_edq.py
    python -m pytest tests/test_edq.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.edq import build_edq_coverage_tool, get_edq_item_details   # noqa: E402
from app.compliance.edq import evaluate_edq_coverage   # noqa: E402


class _FakeGraph:
    """Minimal graph.query() stand-in, dispatching by a substring unique to
    each real query in app.compliance.edq -- no real Neo4j involved."""

    def __init__(self, coverage_rows=None, item_rows=None, match_rows=None):
        self.coverage_rows = coverage_rows or []
        self.item_rows = item_rows or []
        self.match_rows = match_rows or []
        self.calls = []

    def query(self, cypher, params=None):
        self.calls.append((cypher, params))
        if "bestConfidence" in cypher:
            return self.coverage_rows
        if "a.taskId AS taskId" in cypher:
            return self.match_rows
        return self.item_rows


def test_get_edq_item_details_found_with_matches():
    graph = _FakeGraph(
        item_rows=[{
            "id": "edq:5", "jobId": "0006", "category": "0001",
            "itemDescription": "MOBILIZATION", "estimatedQuantity": "1", "unit": "LS",
        }],
        match_rows=[
            {"taskId": "A1000", "name": "Mobilize Site", "confidence": 0.9, "rationale": "exact description match"},
        ],
    )
    result = get_edq_item_details(graph, "proj-1", "edq:5")
    assert result["item"]["itemDescription"] == "MOBILIZATION"
    assert result["item"]["jobId"] == "0006"
    assert len(result["matched_activities"]) == 1
    assert result["matched_activities"][0]["taskId"] == "A1000"
    assert result["matched_activities"][0]["confidence"] == 0.9


def test_get_edq_item_details_not_found():
    graph = _FakeGraph(item_rows=[], match_rows=[])
    result = get_edq_item_details(graph, "proj-1", "edq:999")
    assert result == {"error": "No EDQ item with id='edq:999' found for this project."}


def test_get_edq_item_details_found_no_matches():
    graph = _FakeGraph(
        item_rows=[{
            "id": "edq:1", "jobId": "0001", "category": "0001",
            "itemDescription": "PERFORMANCE BOND AND PAYMENT BOND", "estimatedQuantity": "1", "unit": "DOLL",
        }],
        match_rows=[],
    )
    result = get_edq_item_details(graph, "proj-1", "edq:1")
    assert result["item"]["itemDescription"] == "PERFORMANCE BOND AND PAYMENT BOND"
    assert result["matched_activities"] == []


def test_get_edq_item_details_scopes_query_to_project_and_item_id():
    graph = _FakeGraph(item_rows=[{"id": "edq:5"}], match_rows=[])
    get_edq_item_details(graph, "proj-42", "edq:5")
    # Both calls (item facts, then matches) must be fenced by projectId AND
    # the requested item id -- a query missing either param is a cross-
    # project or wrong-item leak.
    assert len(graph.calls) == 2
    for _cypher, params in graph.calls:
        assert params["pid"] == "proj-42"
        assert params["id"] == "edq:5"


def test_edq_coverage_tool_summary_on_empty_input():
    graph = _FakeGraph(coverage_rows=[
        {"id": "edq:1", "jobId": "0001", "category": "0001", "itemDescription": "MOBILIZATION", "bestConfidence": 0.9},
        {"id": "edq:2", "jobId": "0002", "category": "0001", "itemDescription": "TRAINEES", "bestConfidence": None},
    ])
    tool = build_edq_coverage_tool(graph, project_id="proj-1")
    result = json.loads(tool.func(""))
    assert result["total_items"] == 2
    assert result["status"] == "Fail"
    assert result["uncovered"] == 1


def test_edq_coverage_tool_per_item_on_nonempty_input():
    graph = _FakeGraph(
        item_rows=[{"id": "edq:5", "jobId": "0006", "category": "0001",
                    "itemDescription": "MOBILIZATION", "estimatedQuantity": "1", "unit": "LS"}],
        match_rows=[{"taskId": "A1000", "name": "Mobilize Site", "confidence": 0.9, "rationale": "exact match"}],
    )
    tool = build_edq_coverage_tool(graph, project_id="proj-1")
    result = json.loads(tool.func("edq:5"))
    assert result["item"]["itemDescription"] == "MOBILIZATION"
    assert result["matched_activities"][0]["taskId"] == "A1000"


def test_edq_coverage_tool_strips_whitespace_input():
    graph = _FakeGraph(item_rows=[{"id": "edq:5"}], match_rows=[])
    tool = build_edq_coverage_tool(graph, project_id="proj-1")
    result = json.loads(tool.func("  edq:5  "))
    assert "error" not in result


def test_evaluate_edq_coverage_all_covered_is_pass():
    graph = _FakeGraph(coverage_rows=[
        {"id": "edq:1", "jobId": "0001", "category": "0001", "itemDescription": "MOBILIZATION", "bestConfidence": 0.9},
        {"id": "edq:2", "jobId": "0002", "category": "0001", "itemDescription": "TRAINEES", "bestConfidence": 0.75},
    ])
    result = evaluate_edq_coverage(graph, "proj-1")
    assert result.status == "Pass"
    assert result.uncovered == 0
    assert result.low_confidence == 0
    assert result.fully_covered == 2


def test_evaluate_edq_coverage_low_confidence_is_missing():
    graph = _FakeGraph(coverage_rows=[
        {"id": "edq:1", "jobId": "0001", "category": "0001", "itemDescription": "MOBILIZATION", "bestConfidence": 0.9},
        {"id": "edq:2", "jobId": "0002", "category": "0001", "itemDescription": "TRAINEES", "bestConfidence": 0.3},
    ])
    result = evaluate_edq_coverage(graph, "proj-1")
    assert result.status == "Missing"
    assert result.uncovered == 0
    assert result.low_confidence == 1
    assert "TRAINEES" in result.detail


def test_evaluate_edq_coverage_no_rows_is_none_status():
    graph = _FakeGraph(coverage_rows=[])
    result = evaluate_edq_coverage(graph, "proj-1")
    assert result.status is None
    assert result.total_items == 0


if __name__ == "__main__":
    test_get_edq_item_details_found_with_matches()
    test_get_edq_item_details_not_found()
    test_get_edq_item_details_found_no_matches()
    test_get_edq_item_details_scopes_query_to_project_and_item_id()
    test_edq_coverage_tool_summary_on_empty_input()
    test_edq_coverage_tool_per_item_on_nonempty_input()
    test_edq_coverage_tool_strips_whitespace_input()
    test_evaluate_edq_coverage_all_covered_is_pass()
    test_evaluate_edq_coverage_low_confidence_is_missing()
    test_evaluate_edq_coverage_no_rows_is_none_status()
    print("All tests passed!")
