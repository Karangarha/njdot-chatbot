"""backend/tests/test_review_frontend_shape.py

Tests that _to_frontend_shape passes each check's citations through to the
frontend's JSON contract. See
docs/superpowers/specs/2026-09-10-review-citations-design.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.api.review import _to_frontend_shape  # noqa: E402
from app.models import ReviewCheckResult, ReviewCitation, ReviewResponse  # noqa: E402


def test_to_frontend_shape_includes_citations_as_plain_dicts():
    response = ReviewResponse(
        project_name="Route 49", project_duration_days=365,
        summary={"passed": 1, "warnings": 0, "failed": 0, "manual_review": 0},
        checks=[
            ReviewCheckResult(
                id="c1", category="Cat", name="Check 1", status="Pass",
                evidence="e", source="s",
                citations=[
                    ReviewCitation(
                        kind="private", doc_type="special_provision", label="Special Provision",
                        page_pdf=7, verified=True,
                    ),
                ],
            ),
        ],
        model_used="claude-sonnet-5",
        project_id="proj1",
    )

    shaped = _to_frontend_shape(response)

    citations = shaped["checks"][0]["citations"]
    assert citations == [{
        "kind": "private", "doc_type": "special_provision", "label": "Special Provision",
        "page_pdf": 7, "section_id": None, "verified": True,
    }]


def test_to_frontend_shape_empty_citations_list_when_none_attached():
    response = ReviewResponse(
        project_name="Route 49", project_duration_days=365,
        summary={"passed": 1, "warnings": 0, "failed": 0, "manual_review": 0},
        checks=[
            ReviewCheckResult(id="c1", category="Cat", name="Check 1", status="Pass", evidence="e", source="s"),
        ],
        model_used="claude-sonnet-5",
        project_id="proj1",
    )

    shaped = _to_frontend_shape(response)

    assert shaped["checks"][0]["citations"] == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    total = sum(1 for n in globals() if n.startswith("test_"))
    print(f"\n{total - failures}/{total} passed")
    sys.exit(1 if failures else 0)
