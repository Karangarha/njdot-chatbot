"""backend/tests/integration/test_local_retrieval.py

Local Supabase only. Seeds a synthetic Special Provision, then asserts the
behaviour the whole design exists to produce: a check naming TABLE 105.05-1
receives it, first, every single time.

    python -m pytest backend/tests/integration -q -m integration
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration

_SP_TEXT = """105.05 WORKING DRAWINGS
Submit working drawings to the Engineer. TABLE 105.05-1 IS CHANGED TO the following.
Working Drawing Submission Category: Certified 30 days, Approved 45 days.

105.06 COOPERATION WITH OTHERS
Cooperate with other contractors working within the project limits.

105.07.02 WORK PERFORMED BY UTILITIES
Provide advance notice prior to the start of gas work. Safe-time applies to
fiber optic cable splicing between 12:00am and 6:00am.
"""

_INSTRUCTION = (
    "Special Provisions 105.05 WORKING DRAWINGS: \"TABLE 105.05-1 IS CHANGED TO\", "
    "\"Working Drawing Submission Category\", Certified and Approved columns.\n\n"
    "Classify each submittal by the governing table, not the WBS folder."
)


@pytest.fixture(scope="module")
def seeded_project(local_db):
    from app.ingestion.session_chunker import chunk_special_provision
    from app.ingestion.chunk_store import insert_session_chunks
    from langchain_openai import OpenAIEmbeddings
    from app.config import config

    project_id = str(uuid.uuid4())
    pages = [{"page_num": 1, "text": _SP_TEXT}]
    chunks = chunk_special_provision(pages)
    emb = OpenAIEmbeddings(model=config.EMBEDDING_MODEL, api_key=config.OPENAI_API_KEY)
    vectors = emb.embed_documents([c["content"] for c in chunks])
    insert_session_chunks(local_db, project_id, [{**c, "embedding": v} for c, v in zip(chunks, vectors)])
    yield project_id, emb
    local_db.table("session_chunks").delete().eq("session_id", project_id).execute()


def test_section_metadata_survives_the_round_trip(local_db, seeded_project):
    project_id, _ = seeded_project
    rows = local_db.table("session_chunks").select("metadata").eq("session_id", project_id).execute().data
    sections = {r["metadata"].get("section_id") for r in rows}
    assert {"105.05", "105.06", "105.07.02"} <= sections


def test_the_named_table_is_pinned_first_on_ten_consecutive_runs(local_db, seeded_project):
    from app.compliance.check_retrieval import retrieve_for_check

    project_id, emb = seeded_project
    firsts = []
    for _ in range(10):
        out = retrieve_for_check(local_db, emb.embed_query, project_id, _INSTRUCTION, top_k=8)
        assert out.pinned >= 1
        firsts.append("TABLE 105.05-1" in out.rows[0]["content"])
    assert all(firsts), "the named table must be first on every run, not most runs"


def test_websearch_tsquery_actually_matches_a_hyphenated_section_number(local_db, seeded_project):
    # The single riskiest assumption in the design. If Postgres tokenisation
    # mangles "105.05-1", BM25 contributes nothing and only pinning works.
    project_id, _ = seeded_project
    rows = local_db.rpc("keyword_search_session_chunks", {
        "search_query": "105.05 TABLE 105.05-1",
        "p_session_id": project_id,
        "p_doc_type": "special_provision",
        "match_count": 8,
    }).execute().data
    assert rows, "keyword search returned nothing for a literal section anchor"
    assert any("TABLE 105.05-1" in r["content"] for r in rows)


def test_a_parent_section_anchor_pins_its_child_heading(local_db, seeded_project):
    """FINDING 2 regression: a check naming the parent section "105.07" must
    still pin a document whose actual heading is the child "105.07.02" --
    exact match alone misses this (see test_check_retrieval.py's unit-level
    coverage of the same boundary with a fake DB). Proven here against the
    real PostgREST `like` filter, not a fake that can't disagree with the
    code under test."""
    from app.compliance.anchors import Anchors
    from app.compliance.check_retrieval import pin_by_anchors

    project_id, _ = seeded_project
    rows = pin_by_anchors(local_db, project_id, Anchors(sections=("105.07",)), limit=8)
    assert rows, "parent-section anchor '105.07' must pin the child heading '105.07.02'"
    assert all(r["metadata"].get("section_id") == "105.07.02" for r in rows)


def test_a_bare_string_prefix_does_not_pin_an_unrelated_section(local_db, seeded_project):
    """The boundary must be a literal dot, not a bare string prefix: "105.0"
    is a plausible-looking but WRONG parent of "105.07" (it isn't one -- 0
    and 7 are siblings' leading digits, not a section/subsection
    relationship), and must not pin it."""
    from app.compliance.anchors import Anchors
    from app.compliance.check_retrieval import pin_by_anchors

    project_id, _ = seeded_project
    rows = pin_by_anchors(local_db, project_id, Anchors(sections=("105.0",)), limit=8)
    assert rows == [], "\"105.0\" must not match \"105.07.02\" -- the shared prefix isn't a dot boundary"


def test_naming_a_section_retrieves_the_passage_it_names(local_db, seeded_project):
    """An instruction naming section 105.07.02 gets, first, the passage
    containing the safe-time clause -- checked case-insensitively, since the
    fixture text capitalizes "Safe-time" at the start of a sentence.

    This does NOT exercise a "keyword leg finds a quoted phrase" path --
    no such path exists. app.compliance.anchors.extract_anchors only ever
    extracts section/table identifiers; a quoted phrase like "safe-time" is
    never captured and never reaches keyword_search_session_chunks as a
    query term. The passage surfaces here because 105.07.02 is itself a
    section anchor, pinned ahead of ranking by pin_by_anchors -- the same
    mechanism test_the_named_table_is_pinned_first_on_ten_consecutive_runs
    already covers. This test asserts only the end-to-end retrieval outcome,
    not which layer produced it. See "Phrase anchors are not implemented" in
    docs/superpowers/specs/2026-09-14-hybrid-check-retrieval-design.md.
    """
    from app.compliance.check_retrieval import retrieve_for_check

    project_id, emb = seeded_project
    out = retrieve_for_check(
        local_db, emb.embed_query, project_id,
        "Special Provisions 105.07.02: \"safe-time\", \"fiber optic cable splicing\".\n\nCheck night work.",
        top_k=8,
    )
    assert out.rows, "no rows retrieved"
    assert "safe-time" in out.rows[0]["content"].lower()
