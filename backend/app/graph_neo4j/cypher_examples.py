"""Few-shot NL -> Cypher examples for GraphCypherQAChain.

Covers the query shapes the retired ``app/graph/tools.py`` functions used to
answer via networkx traversal (predecessors/successors, negative float,
mandatory constraints, WBS-phase filtering, narrative mentions) — now
answered generically by the LLM-generated Cypher against this schema.

Every example filters by ``projectId`` on every node pattern (multi-project
isolation — see ``graph_neo4j.tools.build_cypher_chain``). Query strings
carry a ``{project_id}`` format placeholder rather than a generic
"PROJECT_ID" token: ``format_examples()`` substitutes the *actual*
project_id for the request being served, so the LLM sees worked examples
using its literal real target value, not a placeholder it has to
generalize from.
"""

from typing import List

CYPHER_EXAMPLES = [
    {
        "question": "What activities are in the Bridge phase?",
        "query": (
            "MATCH (a:Activity {{projectId: '{project_id}'}})-[:PART_OF]->(w:WBS {{projectId: '{project_id}'}}) "
            "WHERE w.path CONTAINS 'Bridge' "
            "RETURN a.taskId, a.name, a.startDate, a.finishDate, a.computedTotalFloat "
            "ORDER BY a.startDate LIMIT 25"
        ),
    },
    {
        "question": "Which activities have negative float?",
        "query": (
            "MATCH (a:Activity {{projectId: '{project_id}'}}) WHERE a.computedTotalFloat < 0 "
            "RETURN a.taskId, a.name, a.computedTotalFloat "
            "ORDER BY a.computedTotalFloat LIMIT 25"
        ),
    },
    {
        "question": "What are the predecessors of activity A1000, two hops back?",
        "query": (
            "MATCH path = (p:Activity {{projectId: '{project_id}'}})-[:PRECEDES*1..2]->"
            "(a:Activity {{taskId: 'A1000', projectId: '{project_id}'}}) "
            "RETURN p.taskId, p.name, length(path) AS hops "
            "ORDER BY hops LIMIT 25"
        ),
    },
    {
        "question": "What are the successors of activity M100?",
        "query": (
            "MATCH (a:Activity {{taskId: 'M100', projectId: '{project_id}'}})-[:PRECEDES]->"
            "(s:Activity {{projectId: '{project_id}'}}) "
            "RETURN s.taskId, s.name, s.startDate LIMIT 25"
        ),
    },
    {
        "question": "Which activities have mandatory constraints applied?",
        "query": (
            "MATCH (a:Activity {{projectId: '{project_id}'}})-[:CONSTRAINED_BY]->"
            "(c:Constraint {{projectId: '{project_id}'}}) "
            "WHERE c.type CONTAINS 'MAND' "
            "RETURN a.taskId, a.name, c.type, c.date LIMIT 25"
        ),
    },
    {
        "question": "Which narrative entities mention activity M950?",
        "query": (
            "MATCH (e:NarrativeEntity {{projectId: '{project_id}'}})-[m:MENTIONS]->"
            "(a:Activity {{taskId: 'M950', projectId: '{project_id}'}}) "
            "RETURN e.entityType, e.text, e.quote, m.confidence "
            "ORDER BY m.confidence DESC LIMIT 25"
        ),
    },
    {
        "question": "Are there any open-ended activities (no predecessors or no successors)?",
        "query": (
            "MATCH (a:Activity {{projectId: '{project_id}'}}) "
            "WHERE a.isOpenStart = true OR a.isOpenEnd = true "
            "RETURN a.taskId, a.name, a.isOpenStart, a.isOpenEnd LIMIT 25"
        ),
    },
    {
        "question": "What activities does A2000 immediately precede, with relationship type and lag?",
        "query": (
            "MATCH (a:Activity {{taskId: 'A2000', projectId: '{project_id}'}})-[r:PRECEDES]->"
            "(s:Activity {{projectId: '{project_id}'}}) "
            "RETURN s.taskId, s.name, r.relType, r.lagDays LIMIT 25"
        ),
    },
    {
        "question": "How many activities are in this project in total?",
        "query": (
            "MATCH (a:Activity {{projectId: '{project_id}'}}) "
            "RETURN count(a) AS activityCount"
        ),
    },
]


def format_examples(project_id: str) -> List[dict]:
    """Render ``CYPHER_EXAMPLES`` with the real ``project_id`` substituted in."""
    return [
        {"question": ex["question"], "query": ex["query"].format(project_id=project_id)}
        for ex in CYPHER_EXAMPLES
    ]
