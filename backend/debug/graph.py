"""Debug script: knowledge-graph inspection (schedule + narrative).

Modes
-----
Build a graph ad-hoc from the sample files (no DB writes; runs LLM extraction
when --extract is given):

    python -m debug.graph --files [--extract] [--entities]

Inspect a stored session graph:

    python -m debug.graph SESSION_ID [--entities]
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

# ── Bootstrap sys.path and env ────────────────────────────────────────────────
import debug  # noqa: F401 — side-effect: adds backend/ to sys.path

from debug import bold, cyan, dim, green, hr, red, section, subsection, yellow

_XER = Path(__file__).parent / "Schedule.xer"
_NARRATIVE = Path(__file__).parent / "Narrative.pdf"


def _print_graph(g, summary, show_entities: bool) -> None:
    section("GRAPH SUMMARY")
    node_types = Counter(d.get("node_type", "?") for _, d in g.nodes(data=True))
    edge_types = Counter(d.get("edge_type", "?") for _, _, d in g.edges(data=True))
    print(f"Nodes: {bold(str(g.number_of_nodes()))}  {dict(node_types)}")
    print(f"Edges: {bold(str(g.number_of_edges()))}  {dict(edge_types)}")
    if summary:
        print(f"CPM: finish {summary.get('project_finish')}, data date "
              f"{summary.get('data_date')}, {summary.get('critical_count')} critical")
    nar = g.graph.get("narrative")
    if nar:
        print(f"Narrative: {nar}")

    mentions = [(u, v, d) for u, v, d in g.edges(data=True)
                if d.get("edge_type") == "MENTIONS"]
    if mentions:
        subsection(f"MENTIONS EDGES ({len(mentions)})")
        for u, v, d in sorted(mentions, key=lambda x: -x[2].get("confidence", 0)):
            ent = g.nodes[u]
            act = g.nodes[v]
            conf = d.get("confidence", 0)
            colour = green if conf >= 0.9 else (yellow if conf >= 0.7 else dim)
            print(f"  {colour(f'{conf:.2f}')} [{d.get('match_method','?'):12s}] "
                  f"{ent.get('entity_type','?'):12s} "
                  f"\"{ent.get('text','')[:45]}\" → {cyan(v)} {act.get('label','')[:40]}")

    if show_entities:
        ents = [(n, d) for n, d in g.nodes(data=True)
                if d.get("node_type") == "NarrativeEntity"]
        subsection(f"NARRATIVE ENTITIES ({len(ents)})")
        for n, d in ents:
            print(f"\n  {bold(n)} [{d.get('entity_type')}] {d.get('text','')}")
            if d.get("dates"):
                print(f"    dates: {d['dates']}")
            print(f"    quote: {dim(d.get('quote','')[:120])}")

        secs = [(n, d) for n, d in g.nodes(data=True)
                if d.get("node_type") == "NarrativeSection"]
        subsection(f"NARRATIVE SECTIONS ({len(secs)})")
        for n, d in secs:
            print(f"  {cyan(n):12s} p.{d.get('page_pdf')} "
                  f"{d.get('heading','')[:50]} ({len(d.get('text',''))} chars)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Knowledge-graph debug inspection")
    ap.add_argument("session_id", nargs="?", help="stored session to inspect")
    ap.add_argument("--files", action="store_true",
                    help="build ad-hoc from debug/Schedule.xer + debug/Narrative.pdf")
    ap.add_argument("--extract", action="store_true",
                    help="run LLM entity extraction in --files mode")
    ap.add_argument("--entities", action="store_true", help="print entity detail")
    args = ap.parse_args()

    if args.files:
        from app.scheduling import (
            build_calendars, build_network, cross_check, parse_xer_all, run_cpm)
        from app.graph import build_graph
        from app.graph.narrative_graph import add_narrative_to_graph
        from app.ingestion.pdf_parser import PDFParser
        from app.ingestion.session_chunker import chunk_narrative

        xer_text = _XER.read_text(encoding="utf-8", errors="ignore")
        parsed = parse_xer_all(xer_text)
        cpm = run_cpm(build_network(parsed["activities"]),
                      build_calendars(parsed["calendars"]), parsed["project"])
        xc = cross_check(parsed["activities"], cpm)
        g = build_graph(parsed["activities"], parsed["calendars"],
                        cpm=cpm, crosscheck=xc, project=parsed["project"])

        nar_chunks = chunk_narrative(PDFParser(str(_NARRATIVE)).extract_text())
        llm = None
        if args.extract:
            from app.generation.llm_client import LLMClient
            llm = LLMClient()
            print(dim(f"extraction via {llm.provider}:{llm.model}"))
        add_narrative_to_graph(g, nar_chunks, parsed["activities"], llm=llm)
        _print_graph(g, g.graph.get("cpm", {}), args.entities)

    elif args.session_id:
        from app.database import get_db
        from app.graph import load_graph

        loaded = load_graph(get_db(), args.session_id)
        if loaded is None:
            print(red(f"No stored graph for session {args.session_id}"))
            return
        g, summary = loaded
        _print_graph(g, summary, args.entities)

    else:
        ap.print_help()

    hr()
    print(bold("Done."))


if __name__ == "__main__":
    main()
