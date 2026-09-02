"""Narrative entity extraction and schedule-activity linking.

Ported unchanged (pure Python, backend-agnostic) from the retired
``app.graph.narrative_graph`` module — only the final graph-write step differs
(Cypher ``UNWIND``/``MERGE`` in ``seed.py`` instead of networkx mutation).

Activity linking runs three tiers:
  id_exact (1.0)   — activity ID appears verbatim in the entity quote/text
  name_fuzzy (>=.75)— difflib ratio between entity text and an activity name
  date_keyword (.5)— entity date within +/-3 days of activity start/finish AND
                     a shared content keyword

Extraction is fail-safe: any LLM/JSON error skips that batch and the session
continues with chunks only.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ENTITY_TYPES = (
    "Commitment", "Permit", "UtilityWork", "Deadline",
    "Stakeholder", "PhaseMention", "Restriction",
)

_BATCH_SIZE = 4          # narrative sections per extraction call
_FUZZY_THRESHOLD = 0.75
_DATE_WINDOW_DAYS = 3
_KEYWORD_MIN_LEN = 5

# Matches P6-style activity IDs (M100, A1000, PS350, C0910, F9000, …);
# hits are validated against the real activity-id set before use.
_ACT_ID_RE = re.compile(r"\b[A-Z]{1,3}\d{2,6}\b")

_EXTRACTION_SYSTEM = """\
You extract structured entities from NJDOT designer-narrative sections for a
construction-schedule knowledge graph.

Entity types (use exactly these strings):
- Commitment: a promise or obligation stated in the narrative (e.g. maintain
  access, complete work by a date, coordinate with a party)
- Permit: an environmental or regulatory permit/approval and its conditions
- UtilityWork: utility relocation/interruption work (gas, water, electric,
  telecom) and any timing restrictions on it
- Deadline: a specific date or timeframe the narrative commits to
- Stakeholder: an organization or party (utility owner, agency, municipality)
- PhaseMention: a construction stage/phase the narrative describes
- Restriction: a work restriction (seasonal, environmental, traffic, hours)

Return ONLY a valid JSON object (no markdown fences):
{
  "entities": [
    {
      "type": "<one of the types above>",
      "text": "<short canonical description, max 25 words>",
      "quote": "<verbatim supporting sentence from the section>",
      "section_id": "<the [section id] the quote came from>",
      "dates": ["YYYY-MM-DD", ...],
      "activity_ids": ["<schedule activity IDs mentioned verbatim, e.g. A1000>"],
      "activity_names": ["<schedule activity/work names referenced, if any>"]
    }
  ],
  "relations": [
    {"from": <entity index>, "to": <entity index>, "label": "<short verb phrase>"}
  ]
}

Rules:
- Extract only what the text states; do not infer beyond it.
- dates: only dates you can resolve to a specific day; otherwise omit.
- Keep quotes under 40 words.
- If a section has no extractable entities, contribute nothing for it.
"""


def _parse_json(raw: str) -> Optional[dict]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        stripped = raw.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None


# ── Narrative chunks ─────────────────────────────────────────────────────────

def narrative_to_sections(nar_chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Turn ``chunk_narrative`` output into NarrativeChunk node payloads."""
    sections = []
    for chunk in nar_chunks:
        meta = chunk.get("metadata", {})
        sections.append({
            "id": f"nsec:{meta.get('chunk_index', len(sections))}",
            "text": chunk.get("content", ""),
            "heading": meta.get("section_heading", ""),
            "page_pdf": meta.get("page_pdf"),
        })
    return sections


# ── Entity extraction ──────────────────────────────────────────────────────────

def extract_entities(
    sections: List[Dict[str, Any]],
    llm: Any,
    batch_size: int = _BATCH_SIZE,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """LLM entity extraction over narrative sections (batched, fail-safe).

    Returns ``(entities, relations)``; entities carry ``id`` (``nar:<n>``),
    relations reference those ids.  Any batch failure is logged and skipped.
    ``llm`` must expose ``.complete(system, user) -> str``.
    """
    entities: List[Dict[str, Any]] = []
    relations: List[Dict[str, Any]] = []

    for start in range(0, len(sections), batch_size):
        batch = sections[start:start + batch_size]
        blocks = [
            f"[section {s['id']}] {s['heading']} (p.{s['page_pdf']})\n{s['text']}"
            for s in batch
        ]
        user_msg = (
            "Extract entities from these narrative sections:\n\n"
            + "\n\n---\n\n".join(blocks)
        )
        try:
            raw = llm.complete(_EXTRACTION_SYSTEM, user_msg)
            parsed = _parse_json(raw)
            if not parsed:
                logger.warning("narrative extraction: unparseable JSON for batch %d", start)
                continue
            base = len(entities)
            batch_ids = {s["id"] for s in batch}
            for ent in parsed.get("entities", []):
                etype = ent.get("type")
                if etype not in ENTITY_TYPES or not ent.get("text"):
                    continue
                sec_id = ent.get("section_id", "")
                entities.append({
                    "id": f"nar:{len(entities)}",
                    "entity_type": etype,
                    "text": str(ent.get("text", ""))[:300],
                    "quote": str(ent.get("quote", ""))[:400],
                    "section_id": sec_id if sec_id in batch_ids else (
                        batch[0]["id"] if batch else ""),
                    "dates": [d for d in ent.get("dates", []) if _safe_date(d)],
                    "activity_ids": list(ent.get("activity_ids", []) or []),
                    "activity_names": list(ent.get("activity_names", []) or []),
                })
            for rel in parsed.get("relations", []):
                try:
                    f_idx, t_idx = base + int(rel["from"]), base + int(rel["to"])
                except (KeyError, TypeError, ValueError):
                    continue
                if base <= f_idx < len(entities) and base <= t_idx < len(entities) \
                        and f_idx != t_idx:
                    relations.append({
                        "from": entities[f_idx]["id"],
                        "to": entities[t_idx]["id"],
                        "label": str(rel.get("label", "relates to"))[:60],
                    })
        except Exception as exc:
            logger.warning("narrative extraction batch %d failed: %s", start, exc)

    logger.info("narrative extraction: %d entities, %d relations from %d sections",
                len(entities), len(relations), len(sections))
    return entities, relations


def _safe_date(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


# ── Activity linking ───────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(text).lower()).strip()


def link_entities(
    entities: List[Dict[str, Any]],
    activities: List[Dict[str, Any]],
) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Match narrative entities to schedule activities.

    Returns ``(entity_id, activity_id, edge_attrs)`` tuples for MENTIONS
    edges; at most one edge per (entity, activity) pair, keeping the
    highest-confidence method.
    """
    act_ids = {a["activity_id"] for a in activities if a.get("activity_id")}
    act_names = [(a["activity_id"], _normalize(a.get("activity_name", "")))
                 for a in activities if a.get("activity_name")]

    links: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def _propose(eid: str, aid: str, attrs: Dict[str, Any]) -> None:
        key = (eid, aid)
        if key not in links or attrs["confidence"] > links[key]["confidence"]:
            links[key] = attrs

    for ent in entities:
        eid = ent["id"]
        scan_text = f"{ent.get('text', '')} {ent.get('quote', '')}"

        # 1. Exact activity-ID mentions (extractor output + regex over text)
        candidates = set(ent.get("activity_ids", []))
        candidates.update(_ACT_ID_RE.findall(scan_text))
        for aid in candidates & act_ids:
            _propose(eid, aid, {
                "match_method": "id_exact", "confidence": 1.0,
                "evidence": ent.get("quote", "")[:200],
            })

        # 2. Fuzzy name match
        name_candidates = [_normalize(n) for n in ent.get("activity_names", [])]
        name_candidates.append(_normalize(ent.get("text", "")))
        for cand in name_candidates:
            if len(cand) < _KEYWORD_MIN_LEN:
                continue
            for aid, aname in act_names:
                if not aname or len(aname) < _KEYWORD_MIN_LEN:
                    continue
                # Containment (e.g. "Pre-Stage 1A Complete January 21, 2025"
                # vs activity "Pre-Stage 1A Complete") beats a raw ratio that
                # gets diluted by extra words like dates.  The length-fraction
                # guard stops generic short names ("Completion") from matching
                # inside every longer phrase; the reverse direction (entity
                # term inside an activity name, e.g. "Verizon") is the
                # stakeholder-mention signal.
                if aname in cand and len(aname) / len(cand) >= 0.45:
                    _propose(eid, aid, {
                        "match_method": "name_fuzzy", "confidence": 0.9,
                        "evidence": ent.get("quote", "")[:200],
                    })
                    continue
                if cand in aname:
                    _propose(eid, aid, {
                        "match_method": "name_fuzzy", "confidence": 0.85,
                        "evidence": ent.get("quote", "")[:200],
                    })
                    continue
                ratio = difflib.SequenceMatcher(None, cand, aname).ratio()
                if ratio >= _FUZZY_THRESHOLD:
                    _propose(eid, aid, {
                        "match_method": "name_fuzzy", "confidence": round(ratio, 3),
                        "evidence": ent.get("quote", "")[:200],
                    })

        # 3. Date proximity + shared keyword
        ent_dates = [_safe_date(d) for d in ent.get("dates", [])]
        ent_dates = [d for d in ent_dates if d]
        if ent_dates:
            ent_words = {w for w in _normalize(scan_text).split()
                         if len(w) >= _KEYWORD_MIN_LEN}
            for act in activities:
                aid = act.get("activity_id")
                if not aid:
                    continue
                act_dates = [_safe_date(act.get("start_date")),
                             _safe_date(act.get("finish_date"))]
                act_dates = [d for d in act_dates if d]
                if not act_dates:
                    continue
                close = any(abs((ed - ad).days) <= _DATE_WINDOW_DAYS
                            for ed in ent_dates for ad in act_dates)
                if not close:
                    continue
                act_words = {w for w in _normalize(act.get("activity_name", "")).split()
                             if len(w) >= _KEYWORD_MIN_LEN}
                if ent_words & act_words:
                    _propose(eid, aid, {
                        "match_method": "date_keyword", "confidence": 0.5,
                        "evidence": ent.get("quote", "")[:200],
                    })

    return [(eid, aid, attrs) for (eid, aid), attrs in links.items()]
