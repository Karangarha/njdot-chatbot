"""backend/tests/test_chat_similarity_floor.py

FINDING 6: "the chat path must not regress" is a stated constraint of this
branch, and the only thing enforcing it is two explicit match_threshold=0.2
arguments -- build_sp_tool (app.retrieval_langchain.sp_retriever) and the
Special-Provision-only fallback in the session chat path
(app.api.session.session_query, has_graph=False branch). Neither had a
regression test. sp_retriever._DEFAULT_MATCH_THRESHOLD is 0.0 (right for a
compliance check's 150-word rule query, per that module's own docstring),
so if either call site ever drops its explicit override, chat silently
inherits the no-floor default and weak matches start surfacing as noise.

    python -m pytest backend/tests/test_chat_similarity_floor.py
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.retrieval_langchain.sp_retriever import build_sp_tool  # noqa: E402


class _FakeDB:
    """Same minimal rpc() capture as test_sp_search_parity.py."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.last_params = None

    def rpc(self, name, params):
        self.last_params = params
        outer = self

        class _R:
            def execute(self_inner):
                return type("X", (), {"data": outer.rows})()

        return _R()


def test_build_sp_tool_still_uses_the_0_2_floor():
    """Runtime proof, not just a source pin: invoking the tool the chat
    agent actually calls must reach match_session_chunks with 0.2, not
    sp_retriever's own no-floor default (0.0)."""
    db = _FakeDB(rows=[])
    tool = build_sp_tool(db, lambda q: [0.0, 0.0, 0.0], "proj-1")

    tool.func("does this project have gas work restrictions?")

    assert db.last_params["match_threshold"] == 0.2


def test_session_sp_only_fallback_still_passes_the_0_2_floor():
    """The has_graph=False branch of session_query is too deep in a live
    endpoint (get_db/get_neo4j/ChatOpenAI/ChatAnthropic/langfuse all sit in
    front of it) to exercise end-to-end from a unit test -- pin the literal
    call instead, via the AST rather than a whitespace-fragile regex, so a
    reformatted line doesn't produce a false failure."""
    from app.api.session import session_query

    tree = ast.parse(inspect.getsource(session_query))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "retrieve_sp_chunks"
    ]
    assert calls, "session_query must still call retrieve_sp_chunks for its SP-only fallback"

    thresholds = [
        kw.value.value
        for call in calls for kw in call.keywords
        if kw.arg == "match_threshold"
    ]
    assert thresholds == [0.2]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed!")
