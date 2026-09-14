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


def test_a_phrase_anchor_is_found_by_the_keyword_leg(local_db, seeded_project):
    from app.compliance.check_retrieval import retrieve_for_check

    project_id, emb = seeded_project
    out = retrieve_for_check(
        local_db, emb.embed_query, project_id,
        "Special Provisions 105.07.02: \"safe-time\", \"fiber optic cable splicing\".\n\nCheck night work.",
        top_k=8,
    )
    assert any("safe-time" in r["content"] for r in out.rows)
