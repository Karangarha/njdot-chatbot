"""Compact NL digest of the knowledge graph for Q&A context.

The digest is the "global summary" half of GraphRAG: a ~1200-token block
injected into every session query so the LLM always sees the project's
shape — data date, computed finish, critical path, milestones, phases,
cross-check findings, and high-confidence narrative links — and can decide
what to drill into with the graph tools.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

import networkx as nx

_MAX_CHAIN_ROWS = 12
_MAX_MILESTONES = 20
_MAX_PHASES = 15
_MAX_NARRATIVE_LINKS = 12
_MAX_FINDINGS = 6


def build_digest(graph: nx.MultiDiGraph, cpm_summary: Dict[str, Any]) -> str:
    """Render the always-injected schedule/narrative digest."""
    lines: List[str] = []
    project = graph.graph.get("project") or {}

    # ── Header ────────────────────────────────────────────────────────────────
    name = project.get("project_name") or project.get("project_code") or "Project"
    bits = [f"Schedule digest for {name}."]
    if cpm_summary.get("data_date"):
        bits.append(f"Data date: {cpm_summary['data_date']}.")
    if cpm_summary.get("project_finish"):
        bits.append(f"Computed project finish: {cpm_summary['project_finish']}.")
    if project.get("must_finish_date"):
        bits.append(f"Must-finish date: {project['must_finish_date']}.")
    if cpm_summary.get("critical_count") is not None:
        bits.append(f"{cpm_summary['critical_count']} of "
                    f"{cpm_summary.get('computed_count', '?')} activities are "
                    f"critical (computed total float ≤ 0).")
    bits.append("All float/critical values were computed deterministically by "
                "a CPM engine, not by an LLM.")
    lines += [" ".join(bits), ""]

    acts = {n: d for n, d in graph.nodes(data=True)
            if d.get("node_type") == "Activity"}

    # ── Critical path ─────────────────────────────────────────────────────────
    chains = cpm_summary.get("critical_paths") or []
    if chains:
        lines.append("Critical path (computed):")
        for i, chain in enumerate(chains, 1):
            shown = chain[:_MAX_CHAIN_ROWS]
            parts = []
            for aid in shown:
                d = acts.get(aid, {})
                c = d.get("computed") or {}
                parts.append(f"{aid} {d.get('label', '')} "
                             f"(finish {c.get('ef', '?')})")
            suffix = f" … +{len(chain) - len(shown)} more" if len(chain) > len(shown) else ""
            lines.append(f"  Chain {i}: " + " → ".join(parts) + suffix)
        lines.append("")

    # ── Milestones ────────────────────────────────────────────────────────────
    milestones = [(n, d) for n, d in acts.items()
                  if "Milestone" in (d.get("task_type") or "")]
    if milestones:
        milestones.sort(key=lambda x: (x[1].get("computed") or {}).get("es")
                        or x[1].get("start_date") or "9999")
        lines.append("Milestones (computed dates; Critical Yes/No):")
        for n, d in milestones[:_MAX_MILESTONES]:
            c = d.get("computed") or {}
            crit = "Yes" if c.get("is_critical") else "No"
            lines.append(f"  {n} {d.get('label', '')}: {c.get('es') or d.get('start_date', '?')}"
                         f" (float {c.get('total_float', '?')}, critical {crit})")
        lines.append("")

    # ── Phases ────────────────────────────────────────────────────────────────
    by_phase: Dict[str, List[str]] = defaultdict(list)
    for n, d in acts.items():
        phase = " > ".join(d.get("wbs_path") or ["General"])
        by_phase[phase].append(n)
    if by_phase:
        lines.append("Phases (activity count, computed date range, critical count):")
        for phase, ids in sorted(by_phase.items())[:_MAX_PHASES]:
            cs = [acts[i].get("computed") or {} for i in ids]
            starts = sorted(c.get("es") for c in cs if c.get("es"))
            ends = sorted(c.get("ef") for c in cs if c.get("ef"))
            crit = sum(1 for c in cs if c.get("is_critical"))
            rng = f"{starts[0]} → {ends[-1]}" if starts and ends else "dates n/a"
            lines.append(f"  {phase}: {len(ids)} activities, {rng}, {crit} critical")
        lines.append("")

    # ── Cross-check / logic findings ──────────────────────────────────────────
    findings = list(graph.graph.get("crosscheck_assessment") or [])
    for w in (cpm_summary.get("warnings") or [])[:3]:
        findings.append(w)
    if findings:
        lines.append("Schedule findings (computed):")
        for f in findings[:_MAX_FINDINGS]:
            lines.append(f"  - {f}")
        lines.append("")

    # ── Narrative links ───────────────────────────────────────────────────────
    mentions = [(u, v, d) for u, v, d in graph.edges(data=True)
                if d.get("edge_type") == "MENTIONS" and d.get("confidence", 0) >= 0.7]
    if mentions:
        mentions.sort(key=lambda x: -x[2].get("confidence", 0))
        lines.append("Narrative commitments/entities linked to schedule activities:")
        for u, v, d in mentions[:_MAX_NARRATIVE_LINKS]:
            ent = graph.nodes[u]
            act = graph.nodes[v]
            lines.append(f"  - {ent.get('entity_type', 'Entity')} \"{ent.get('text', '')}\""
                         f" → {v} {act.get('label', '')}")
        lines.append("")

    nar_secs = sum(1 for _, d in graph.nodes(data=True)
                   if d.get("node_type") == "NarrativeSection")
    if nar_secs:
        lines.append(f"The designer narrative ({nar_secs} sections) is stored in "
                     f"the graph — use search_narrative / get_narrative_section "
                     f"to read it.")

    return "\n".join(lines).strip()
