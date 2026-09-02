"""LangChain tools over the Neo4j knowledge graph.

Most of the retired ``app/graph/tools.py`` (10 hand-rolled traversal
functions) collapses into generic Cypher via ``GraphCypherQAChain`` — reliable
graph-shaped Q&A no longer needs bespoke Python once Cypher generation works
against this schema (see ``cypher_examples.py``).

Two functions stay deterministic Python, not LLM-generated Cypher:
  - ``get_critical_path`` — critical path is a precomputed CPM concept (see
    the ``Project`` node written by ``graph_neo4j.seed.seed_schedule``); an
    LLM should read it, not re-derive it via ad-hoc Cypher.
  - ``search_narrative`` — in-process cosine ranking over
    ``NarrativeChunk.embedding`` (no native Neo4j vector index — see the
    Phase 0 decision: the narrative corpus is small enough that an index is
    unnecessary complexity).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import numpy as np
from langchain_core.language_models import BaseLanguageModel
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import Tool
from langchain_neo4j import GraphCypherQAChain, Neo4jGraph

from app.graph_neo4j.cypher_examples import format_examples

logger = logging.getLogger(__name__)

_MAX_ROWS = 25


def _format_examples(examples: List[Dict[str, str]]) -> str:
    return "\n\n".join(
        f"Question: {ex['question']}\nCypher: {ex['query']}" for ex in examples
    )


def _build_cypher_prompt(project_id: str) -> PromptTemplate:
    """A from-scratch Cypher-generation prompt, not ``langchain_neo4j``'s
    stock ``CYPHER_GENERATION_PROMPT``.

    The stock template buries few-shot examples under a heading literally
    reading "Examples (**optional**):" — measured empirically (see
    ``_run_review_pipeline``'s multi-project isolation tests) that GPT-4o
    reliably (repeatedly, 4/4 in testing) drops the projectId filter when the
    fencing instruction is smuggled into that slot, apparently reading
    "optional" as license to ignore it even with an explicit few-shot example
    for the exact question asked. This template puts the fencing instruction
    in the mandatory instruction block instead, and repeats it once more
    immediately before the question (recency helps LLMs weight instructions).
    The ``_cypher_tool`` guard in ``build_tools`` still backstops this — an
    LLM can still slip — but a prompt that actually works most of the time
    means most queries succeed instead of erroring out via the guard.
    """
    fence = (
        f"MANDATORY: this is a multi-tenant graph database holding many "
        f"projects' data under identical node labels and relationship types. "
        f"Every single node pattern in the Cypher you generate MUST include "
        f"projectId: '{project_id}' — e.g. MATCH (a:Activity {{projectId: "
        f"'{project_id}'}}) — for every node variable, including in "
        f"aggregate/count queries and multi-hop traversals. A query with even "
        f"one un-fenced node pattern is wrong and will be rejected."
    )
    examples_text = _format_examples(format_examples(project_id))
    # Every example/fence string above contains literal Cypher property maps
    # like {projectId: '...'} — PromptTemplate's f-string formatter would
    # otherwise treat those braces as template variables. Build the template
    # with placeholder tokens for the two REAL variables, escape all braces,
    # then swap the tokens in for the real `{schema}`/`{question}` slots.
    _SCHEMA_TOKEN, _QUESTION_TOKEN = "\x00SCHEMA\x00", "\x00QUESTION\x00"
    raw = (
        "Task: Generate a Cypher statement to query a graph database.\n\n"
        f"{fence}\n\n"
        "Instructions:\n"
        "Use only the provided relationship types and properties in the schema.\n"
        "Do not use any other relationship types or properties that are not provided.\n"
        f"Schema:\n{_SCHEMA_TOKEN}\n\n"
        "Examples of correctly-fenced queries against this schema:\n"
        f"{examples_text}\n\n"
        "Note: Do not include any explanations or apologies in your responses.\n"
        "Do not respond to any questions that might ask anything else than for "
        "you to construct a Cypher statement.\n"
        "Do not include any text except the generated Cypher statement.\n\n"
        f"REMINDER: every node pattern below MUST include projectId: "
        f"'{project_id}'. Verify this before responding.\n\n"
        f"The question is:\n{_QUESTION_TOKEN}"
    )
    escaped = raw.replace("{", "{{").replace("}", "}}")
    template = escaped.replace(_SCHEMA_TOKEN, "{schema}").replace(_QUESTION_TOKEN, "{question}")
    return PromptTemplate(input_variables=["schema", "question"], template=template)


def build_cypher_chain(graph: Neo4jGraph, llm: BaseLanguageModel, project_id: str) -> GraphCypherQAChain:
    """Build the NL -> Cypher -> answer chain, few-shot primed for this schema
    AND fenced to one project — every few-shot example and the instruction
    preamble bake in the real ``project_id`` value (see
    ``_build_cypher_prompt``), not a generic placeholder.

    Refreshes the graph's cached schema first — ``Neo4jGraph`` snapshots
    node/relationship properties at construction time, which is stale once
    new data has been seeded since (e.g. the app-wide singleton client).
    """
    graph.refresh_schema()
    cypher_prompt = _build_cypher_prompt(project_id)
    return GraphCypherQAChain.from_llm(
        llm=llm,
        graph=graph,
        cypher_prompt=cypher_prompt,
        allow_dangerous_requests=True,
        return_intermediate_steps=True,
        return_direct=True,   # skip QA-synthesis LLM call — callers consume raw
                               # Cypher records directly (agent/eval loop has its
                               # own LLM); observed the synthesis step unreliably
                               # answering "I don't know" despite correct, non-empty
                               # context during implementation testing.
        top_k=_MAX_ROWS,
        verbose=False,
    )


def _generated_cypher(chain_result: Dict[str, Any]) -> str:
    """Pull the actual generated Cypher string out of a chain invoke() result
    (``return_intermediate_steps=True`` puts it in ``intermediate_steps[0]['query']``).
    """
    steps = chain_result.get("intermediate_steps") or []
    if not steps or not isinstance(steps[0], dict):
        return ""
    return steps[0].get("query", "") or ""


# ── Deterministic tool: critical path ───────────────────────────────────────

def get_critical_path(graph: Neo4jGraph, project_id: str = "default") -> Dict[str, Any]:
    """The project's precomputed critical path chain(s), read from the
    ``Project`` node written at seed time — never re-derived by the LLM."""
    rows = graph.query(
        "MATCH (p:Project {projectId: $projectId}) RETURN p LIMIT 1",
        params={"projectId": project_id},
    )
    if not rows:
        return {"error": f"No project data seeded for projectId={project_id}"}
    p = rows[0]["p"]
    chains: List[List[str]] = json.loads(p.get("criticalPathsJson") or "[]")

    act_rows = graph.query(
        "UNWIND $ids AS tid MATCH (a:Activity {taskId: tid, projectId: $projectId}) "
        "RETURN a.taskId AS taskId, a.name AS name, a.computedEarlyStart AS es, "
        "       a.computedEarlyFinish AS ef, a.computedTotalFloat AS totalFloat",
        params={"ids": [aid for chain in chains for aid in chain], "projectId": project_id},
    )
    by_id = {r["taskId"]: r for r in act_rows}

    return {
        "project_finish": p.get("projectFinish"),
        "data_date": p.get("dataDate"),
        "critical_count": p.get("criticalCount"),
        "chains": [
            [by_id.get(aid, {"taskId": aid}) for aid in chain]
            for chain in chains
        ],
        "note": "Computed deterministically by the CPM engine (total float <= 0).",
    }


# ── Deterministic tool: narrative search (in-process cosine ranking) ───────

def _cosine(a: List[float], b: List[float]) -> float:
    va, vb = np.array(a), np.array(b)
    denom = (np.linalg.norm(va) * np.linalg.norm(vb))
    return float(np.dot(va, vb) / denom) if denom else 0.0


def search_narrative(
    graph: Neo4jGraph,
    query_embedding: List[float],
    project_id: str = "default",
    top_k: int = 5,
) -> Dict[str, Any]:
    """Rank NarrativeChunk nodes by cosine similarity to a query embedding.

    In-process ranking (not a native Neo4j vector index) — see module docstring.
    """
    rows = graph.query(
        "MATCH (c:NarrativeChunk {projectId: $projectId}) "
        "RETURN c.id AS id, c.heading AS heading, c.pagePdf AS pagePdf, "
        "       c.text AS text, c.embedding AS embedding",
        params={"projectId": project_id},
    )
    scored = [
        (r, _cosine(query_embedding, r["embedding"]))
        for r in rows if r.get("embedding")
    ]
    scored.sort(key=lambda x: -x[1])
    results = [
        {"section_id": r["id"], "heading": r["heading"], "page_pdf": r["pagePdf"],
         "score": round(score, 4), "text": r["text"]}
        for r, score in scored[:top_k]
    ]
    return {"sections": results,
            "note": "Ranked by in-process cosine similarity over NarrativeChunk embeddings."}


# ── LangChain Tool wrappers ──────────────────────────────────────────────────

def build_tools(
    graph: Neo4jGraph,
    llm: BaseLanguageModel,
    embed_fn: Any,
    project_id: str = "default",
) -> List[Tool]:
    """Assemble the tool set for a Document Q&A agent (Phase 6).

    ``embed_fn`` embeds a query string to a vector (e.g.
    ``OpenAIEmbeddings().embed_query``), used by the narrative-search tool.
    """
    cypher_chain = build_cypher_chain(graph, llm, project_id)

    def _cypher_tool(question: str) -> str:
        result = cypher_chain.invoke({"query": question})
        # Defense-in-depth: prompt engineering alone can't guarantee the LLM
        # never forgets the projectId filter. Refuse to hand back records
        # from a generated query that doesn't literally mention this
        # project's id, rather than risk silently leaking other projects'
        # data mixed into the answer.
        cypher = _generated_cypher(result) if isinstance(result, dict) else ""
        if cypher and project_id not in cypher:
            logger.warning(
                "Refusing Cypher result missing projectId filter for project_id=%s: %s",
                project_id, cypher,
            )
            return json.dumps({
                "error": (
                    "Generated Cypher did not include the required projectId "
                    "filter — refusing to return potentially cross-project results."
                )
            })
        # return_direct=True -> "result" is the raw list of Cypher records.
        records = result.get("result", []) if isinstance(result, dict) else result
        return json.dumps(records, default=str)

    def _critical_path_tool(_: str = "") -> str:
        return json.dumps(get_critical_path(graph, project_id), default=str)

    def _search_narrative_tool(keywords: str) -> str:
        embedding = embed_fn(keywords)
        return json.dumps(search_narrative(graph, embedding, project_id), default=str)

    return [
        Tool.from_function(
            func=_cypher_tool,
            name="query_schedule_graph",
            description=(
                "Answer schedule-logic questions (predecessors/successors, "
                "negative float, mandatory constraints, WBS/phase filtering, "
                "narrative-entity mentions) by generating and running Cypher "
                "against the project's knowledge graph. Input: a natural-"
                "language question about the schedule."
            ),
        ),
        Tool.from_function(
            func=_critical_path_tool,
            name="get_critical_path",
            description=(
                "The project's precomputed critical path chain(s), with "
                "dates and float, plus project finish and data date. Use "
                "this instead of query_schedule_graph for critical-path "
                "questions — it reads deterministic CPM output, not "
                "LLM-generated Cypher."
            ),
        ),
        Tool.from_function(
            func=_search_narrative_tool,
            name="search_narrative",
            description=(
                "Semantic search across the designer narrative's full text; "
                "returns the most relevant sections with scores. Input: "
                "keywords or a short question about narrative content "
                "(commitments, permits, utility work, restrictions, etc.)."
            ),
        ),
    ]
