"""backend/tests/test_edq.py

No LLM, no network, no Neo4j (graph is a fake dispatching by a substring
unique to each real query in app.compliance.edq).
Runnable two ways:
    python tests/test_edq.py
    python -m pytest tests/test_edq.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.edq import get_edq_item_details   # noqa: E402


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


if __name__ == "__main__":
    test_get_edq_item_details_found_with_matches()
    test_get_edq_item_details_not_found()
    test_get_edq_item_details_found_no_matches()
    test_get_edq_item_details_scopes_query_to_project_and_item_id()
    print("All tests passed!")
