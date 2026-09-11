"""Canonical catalog of built-in NJDOT compliance checks.

This is the SINGLE SOURCE OF TRUTH for the default checklist.  It was extracted
verbatim (ids, names, categories) from the old ``_SYSTEM_PROMPT`` "CHECKS TO RUN"
block in ``app.api.review`` — with the per-check logic that used to live in the
"RIGID EVALUATION RULES" block moved into each check's ``instruction`` field so
the prompt can be assembled dynamically from any selected subset.

Two consumers read this catalog:
  * ``app.compliance.eval_engine.evaluate_checks`` — when a review request
    arrives with no explicit ``checks`` selection, the full catalog is used
    (backward compatible with the original behaviour).
  * ``scripts.seed_compliance_checks`` — seeds these rows into the Supabase
    ``compliance_checks`` table (``user_id IS NULL``) so the frontend can list
    them and users can fork/customise their own copies.

Keep ``check_key`` values stable — they are the ids echoed back in the review
report and are what the frontend/Supabase rows key on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List

# Category labels. Most checks are uncategorized (category="") — the source
# NJDOT checklist document is a flat list with no headings. Only checks that
# the source document actually nests under one shared parent line keep a
# category, which the frontend's checklist editor renders as a header
# (see ChecklistManager.tsx). The Compliance Results view groups by
# check_key (SECTION_CHECK_KEYS in DocumentReview.tsx), not by category.
CAT_NONE = ""
CAT_ADMIN_DATES = "Administrative Dates"
CAT_ENV_LANDSCAPE_UTILITIES = "Environmental, Landscape & Utilities"
CAT_WEATHER = "Weather Restrictions"
CAT_WINTER = "Winter Restrictions"
CAT_WEATHER_PAVING = "Weather & Paving Restrictions"
CAT_COMPLETION = "Completion Milestones"
CAT_WORKING_DRAWINGS = "Working Drawings, Materials & ITS"
CAT_SCHEDULE_LOGIC = "Schedule Logic"
CAT_NARRATIVE = "Designer's Narrative"


@dataclass(frozen=True)
class CheckDef:
    """One compliance check definition.

    ``instruction`` is the natural-language rule injected into the LLM prompt.
    ``check_type`` selects the evaluation path in
    ``app.compliance.eval_engine._evaluate_one_check``: ``"llm"`` (default,
    one structured-output LLM call over the check's evidence) or one of the
    deterministic types in that module's ``_DETERMINISTIC_EVALUATORS``
    registry, which compute their result in Python and make no LLM call —
    ``"geo"`` (north/south of I-195 from the key map's coordinates, see
    ``app.compliance.geo``), ``"cost_gap"`` (Substantial-to-Final day gap
    from the Engineer's Estimate, see ``app.compliance.cost``), and
    ``"edq_coverage"`` (EDQ line item -> schedule activity graph coverage,
    see ``app.compliance.edq``).
    """

    check_key: str
    category: str
    name: str
    instruction: str
    check_type: str = "llm"
    # Which document(s) app.compliance.eval_engine.evaluate_checks() should
    # draw evidence from for this check: any combination of "schedule"
    # (CPM facts + milestones + activity roster, from Neo4j), "narrative"
    # (full designer-narrative text, from Neo4j), "sp" (Special Provision
    # retrieval), "keymap" (key map facts extracted from the key sheet),
    # "spec"/"csm" (static reference collections). Explicit per-check — set
    # by the user when they add a custom check ("select the file(s)"),
    # defaulted by category for built-ins.
    source_files: List[str] = field(default_factory=lambda: ["schedule"])
    # Number of Special Provision chunks to retrieve for checks with "sp" in
    # source_files (ignored otherwise). Default matches chunk_special_provision's
    # 600-token/100-token-overlap window; table-heavy checks (a table spanning
    # more chunks than the default retrieves) raise this explicitly.
    sp_top_k: int = 8

    def as_dict(self) -> dict:
        return asdict(self)


# NOTE: order matters — this array's order follows the NJDOT source checklist
# document line-by-line. Checks render in this order; category is set only
# where the source document nests a run of checks under one shared header
# (Administrative Dates; Environmental, Landscape & Utilities; Weather
# Restrictions; Winter Restrictions; Weather & Paving Restrictions;
# Completion Milestones; Working Drawings, Materials & ITS; Schedule Logic;
# Designer's Narrative). Everything else is uncategorized.
BUILTIN_CHECKS: List[CheckDef] = [
    # ── Utility alignment: key map vs. Special Provisions ─────────────────────
    CheckDef(
        "utility_alignment", CAT_NONE,
        "Utility Alignments Match Key Sheet and Special Provisions",
        "Utilities on the Key Sheet sit within project limits but many have no "
        "work here. A key-map utility missing from the Special Provisions is "
        "NOT a failure. The failure is the reverse: a utility the SP assigns "
        "work to with no activity in the schedule. Allow name variants (SJG / "
        "South Jersey Gas). FAIL only for SP-scoped utilities absent from the "
        "schedule. Look in: Key Sheet utility list; Special Provisions "
        "105.07.01 and 105.07.02 (search: \"Advance Notice Requirements\", "
        "\"Work to be Performed by Utility\"); schedule activity list. If the "
        "retrieved Special Provision text does not contain 105.07.01 or "
        "105.07.02, say so and state whether you believe the section is "
        "absent from this contract or simply was not retrieved — these are "
        "different findings.",
        source_files=["keymap", "sp", "schedule"],
    ),
    CheckDef(
        "schedule_duration", CAT_NONE,
        "Schedule Duration Under 3 Years (Exclude Weekends and Holidays)",
        "Measure Advertisement (M100) to Completion (M950) in CALENDAR days. "
        "State the exact day count first. Under 1,095 days passes. Do not "
        "apply the 3-year test to a business-day count. If over 3 years, a "
        "narrative justification converts it to PASS. Look in: schedule "
        "milestones M100 and M950; designer's narrative.",
        source_files=["schedule", "narrative"],
    ),

    # ── Administrative Dates ──────────────────────────────────────────────────
    CheckDef(
        "ad_date_day", CAT_ADMIN_DATES,
        "Advertisement Date Falls on Tuesday or Thursday",
        "Deterministic: computed from the Advertisement milestone's (M100) "
        "date — no AI judgement involved. FAIL states the actual weekday.",
        check_type="date_rule",
    ),
    CheckDef(
        "bid_date_day", CAT_ADMIN_DATES,
        "Bid Date Falls on Tuesday or Thursday",
        "Deterministic: computed from the Bid milestone's (M200) date — no "
        "AI judgement involved. FAIL states the actual weekday.",
        check_type="date_rule",
    ),
    CheckDef(
        "ad_to_bid_gap", CAT_ADMIN_DATES,
        "15 Business Days: Advertisement to Bid",
        "Deterministic: computed from the Advertisement (M100) and Bid "
        "(M200) milestone dates using the Advertisement milestone's own "
        "calendar (holiday-aware) — no AI judgement involved. FAIL states "
        "the actual business-day count against the 15-day minimum.",
        check_type="date_rule",
    ),
    CheckDef(
        "bid_to_award_gap", CAT_ADMIN_DATES,
        "15 Business Days: Bid to Award",
        "Deterministic: computed from the Bid (M200) and Award (M300) "
        "milestone dates using the Bid milestone's own calendar "
        "(holiday-aware) — no AI judgement involved. FAIL states the "
        "actual business-day count against the 15-day minimum.",
        check_type="date_rule",
    ),
    CheckDef(
        "award_to_construction", CAT_ADMIN_DATES,
        "Award to Construction Start Timeframe (40 days State / 55 days Federal / 25-55 days Pavement)",
        "Deterministic: computed from the Award (M300) and Construction "
        "Start (M500) milestone dates using the Award milestone's own "
        "calendar (holiday-aware) — no AI judgement involved. Project type "
        "is a Federal Project Number lookup (key map or DBE Goal Memo) "
        "falling back to an EDQ item-mix classification for Pavement "
        "Preservation, defaulting to State otherwise; conflicting Federal "
        "Project Numbers between the two sources report Missing rather than "
        "guess. FAIL states the actual business-day count against the "
        "type's minimum (55 Federal / 40 State / 25 Pavement Preservation).",
        check_type="date_rule",
        source_files=["schedule", "keymap", "estimate"],
    ),

    # ── Environmental, Landscape & Utilities ──────────────────────────────────
    CheckDef(
        "row_availability", CAT_ENV_LANDSCAPE_UTILITIES,
        "ROW Availability Date Precedes Parcel Work",
        "No parcel work may start before that parcel's availability date. "
        "Mobilization, submittals and notice activities do not occupy a "
        "parcel. Also compare the narrative's ROW claim against the actual "
        "dates - a narrative saying all ROW is acquired before Construction "
        "Start, while parcels become available later, is a contradiction "
        "worth reporting. If the Special Provisions were not supplied, say "
        "'SP 108.12 not supplied.' Look in: Special Provisions 108.12 "
        "RIGHT-OF-WAY RESTRICTIONS (search: \"has not obtained the following "
        "ROW parcels\", \"anticipated availability dates\"); narrative ROW "
        "Requirements section; schedule activity dates. If the retrieved "
        "text does not contain 108.12, say so and state whether you believe "
        "it is absent from this contract or simply was not retrieved.",
        source_files=["narrative", "schedule", "sp"],
    ),

    # ── Manual Review (cross-references the Special Provision + Scheduling
    # Manual excerpts, both attached directly in the same review call) ─────────
    CheckDef(
        "environmental_permit", CAT_NONE,
        "Environmental Permit Compliance Beyond Narrative",
        "Check the SCHEDULE OBEYS the permit restrictions, not that the "
        "narrative mentions them. Typical: in-water work barred Mar 1 - Jun "
        "30 and Oct 1 - Nov 30; tree clearing barred Apr 1 - Aug 30. These "
        "windows recur every year the project spans - test cofferdam, "
        "in-water and clearing activities against every occurrence, not "
        "just the first. Calendar exception ranges are not currently "
        "included in the schedule evidence you receive - do not claim to "
        "have checked them; rely on activity dates only. This contract's "
        "Special Provisions may contain no dedicated environmental permit "
        "section - permit conditions normally appear in the designer's "
        "narrative instead. The closest related section, if present, is "
        "158 SOIL EROSION AND SEDIMENT CONTROL AND WATER QUALITY, not a "
        "permit section by that name. If no SP permit text is retrieved, "
        "say so explicitly and evaluate from the narrative and schedule "
        "alone rather than treating the absence as a gap in retrieval. "
        "Look in: narrative Permit Requirements section; schedule activity "
        "dates; Special Provisions 158.",
        source_files=["sp", "narrative", "schedule"],
    ),

    # ── Environmental, Landscape & Utilities (continued) ──────────────────────
    CheckDef(
        "landscape_season", CAT_ENV_LANDSCAPE_UTILITIES,
        "Landscape/Planting Limited to Mar 1-May 15 or Aug 15-Dec 1",
        "Two different windows apply - evaluate both. PLANTING (plant, tree, "
        "shrub, bulb, transplant): Mar 1 - May 15, Aug 15 - Dec 1. "
        "SEEDING/TURF (seed, topsoil, fertiliz, sod, turf): Mar 1 - May 15, "
        "Aug 15 - OCT 15. Both windows recur every year the project spans - "
        "check every activity against every year's occurrence, not just "
        "the first. Any activity starting or finishing outside its "
        "window is a violation. If the list is non-empty the status is FAIL "
        "- 'Tree Plantings' is planting, and 'topsoiling, fertilizing and "
        "seeding' is seeding. Look in: schedule activity list; Standard "
        "Specifications Table 811.03.01-1 (planting) and Section 810 "
        "(seeding).",
        source_files=["narrative", "schedule", "spec"],
    ),
    CheckDef(
        "gas_interruption", CAT_ENV_LANDSCAPE_UTILITIES,
        "No Gas Service Interruptions Oct 1 to Apr 1",
        "Restricted window: Oct 1 - Apr 1, recurring every year the project "
        "spans - check every occurrence, not just the first. Search activity "
        "names for: gas, gas main, gas line, gas valve, gas service, and "
        "named gas utilities. List every activity your search terms "
        "matched, including notice/submittal activities, before concluding. "
        "If the list of physical gas work is empty, PASS, showing the full "
        "list you found (even if only notices). Installation, tie-in, "
        "connection, abandonment and valve work can interrupt service; "
        "notices and submittals cannot. WARNING if such work falls in the "
        "window; check the SP utility notes first - 'facilities remain in "
        "service at all times' is mitigation, quote it. Look in: schedule "
        "activity list; Special Provisions 105.07.02 (search: \"gas main\", "
        "\"advance notice prior to the start of gas work\", \"remain in "
        "service at all times\"). If the retrieved SP text does not contain "
        "105.07.02, say so and state whether you believe it is absent from "
        "this contract or simply was not retrieved.",
        source_files=["narrative", "schedule", "sp"],
    ),
    CheckDef(
        "water_interruption", CAT_ENV_LANDSCAPE_UTILITIES,
        "No Water Service Interruptions Apr 1 to Sep 30",
        "Restricted window: Apr 1 - Sep 30, recurring every year the project "
        "spans - check every occurrence, not just the first. Search activity "
        "names for: water, water main, water service, water valve, hydrant, "
        "named water owners. List every matching activity before "
        "concluding. If the list of physical water work is empty, PASS, "
        "showing the full list you found. A live TIE-IN OR CONNECTION TO AN "
        "EXISTING MAIN IS a service interruption - do not dismiss it as "
        "'just a construction activity.' Installing new pipe not yet in "
        "service is not. WARNING if an interruption activity falls in the "
        "window. Look in: schedule activity list; Special Provisions "
        "105.07.02 and 651.03.01.A (search: \"Scheduling of Work and "
        "Interruption to Water Service\", \"tie-in to existing main\"); "
        "Standard Specifications 651. If the retrieved SP text does not "
        "contain 105.07.02 or 651.03.01.A, say so.",
        source_files=["narrative", "schedule", "sp", "spec"],
    ),
    CheckDef(
        "electric_interruption", CAT_ENV_LANDSCAPE_UTILITIES,
        "No Electric Service Interruptions Jun 1 to Sep 30",
        "Restricted window: Jun 1 - Sep 30, recurring every year the project "
        "spans - check every occurrence, not just the first. Search activity "
        "names for: electric, power, service, meter, lighting, signal, ITS, "
        "conduit, junction box, named electric owners. List every matching "
        "activity before concluding; if the list of live-service work is "
        "empty, PASS, showing the full list you found. Service cut-over, "
        "meter changeover, de-energizing and resetting a live signal can "
        "interrupt; installing unenergized conduit or boxes cannot. Watch "
        "for installation activities with a live operation buried in the "
        "name (e.g. '...and Reset Traffic Light'). Look in: schedule "
        "activity list; Special Provisions 105.07.02 (search: \"electric "
        "service inquiry\", \"meter cabinet\"). If the retrieved SP text "
        "does not contain 105.07.02, say so.",
        source_files=["narrative", "schedule", "sp"],
    ),
    CheckDef(
        "utility_work_hours", CAT_ENV_LANDSCAPE_UTILITIES,
        "Utility Relocation Work Accounts for No Night/Weekend Work",
        "Utility owners work daytime weekday hours. Check utility activity "
        "durations do not assume nights or weekends. Then check the SP for "
        "work that REQUIRES off-hours - telecom fiber splicing during "
        "overnight windows is the common one. If the SP mandates off-hours "
        "utility work the schedule omits, that is a FAIL. Look in: schedule "
        "activity durations (calendar assignment is not in the evidence you "
        "receive - infer off-hours only from named night/weekend activities "
        "or an explicit SP mandate); Special Provisions 105.07.02 (search: "
        "\"day shift, night shift, or on weekends\", \"safe-time\", "
        "\"advance notice for nighttime work\"). If the retrieved SP text "
        "does not contain 105.07.02, say so.",
        source_files=["narrative", "schedule", "sp"],
    ),
    CheckDef(
        "railroad_restrictions", CAT_ENV_LANDSCAPE_UTILITIES,
        "Railroad Work Restrictions Reflected in Schedule and Special Provisions",
        "Most projects have no railroad. If the Key Sheet utility list and "
        "the SP utility sections name none, and no activity mentions track, "
        "rail, flagging or fouling, PASS with 'no railroad involvement' and "
        "name what you checked. If a railroad is involved, verify flagging "
        "lead time, track windows and coordination appear in both schedule "
        "and SP. Look in: Key Sheet utility list; Special Provisions 105.07; "
        "schedule activity names. If the retrieved SP text does not contain "
        "a railroad-specific subsection under 105.07, say so and state "
        "whether you believe none exists in this contract or it simply was "
        "not retrieved.",
        source_files=["keymap", "narrative", "schedule", "sp"],
    ),

    # ── Weather Restrictions ───────────────────────────────────────────────────
    CheckDef(
        "temp_50_window", CAT_WEATHER,
        "Work Requiring >50°F Scheduled May 1 - Sep 30",
        "Work requiring above 50 F must fall between May 1 and Sep 30, "
        "checking every year the project spans, not just the first. You "
        "must classify which work carries the threshold - the schedule will "
        "not label it. Typical: pavement markings and thermoplastic, epoxy "
        "and latex-modified materials, waterproofing membranes, joint and "
        "crack sealers, coatings, landscape establishment. Cite the spec you "
        "relied on. Exclude HMA paving (governed by base temperature "
        "instead). If no activity carries a >50 F requirement, PASS and list "
        "what you evaluated. Look in: schedule activity list; Standard "
        "Specifications for the item types you classify.",
        source_files=["schedule", "narrative", "spec"],
    ),
    CheckDef(
        "temp_60_window", CAT_WEATHER,
        "Work Requiring >60°F Scheduled Jun 1 - Sep 15",
        "Work requiring above 60 F must fall between Jun 1 and Sep 15, "
        "checking every year the project spans, not just the first. "
        "Narrower set than the 50 F rule - typically epoxy and polymer "
        "overlays, thin bonded overlays, and materials whose specification "
        "states a 60 F minimum. Cite the spec. Exclude HMA paving. If no "
        "activity carries a >60 F requirement, PASS and list what you "
        "evaluated. A schedule with none is a normal result, not a data "
        "gap. Look in: schedule activity list; Standard Specifications for "
        "the item types you classify.",
        source_files=["schedule", "narrative", "spec"],
    ),

    # ── Winter Restrictions ────────────────────────────────────────────────────
    CheckDef(
        "no_concrete_winter", CAT_WINTER,
        "No Concrete Activities Dec 15 - Mar 15",
        "Temperature-sensitive concrete placement is barred Dec 15 - Mar 15, "
        "recurring every winter the project spans - a multi-year project "
        "has more than one such window; check every one of them, not just "
        "the first. INCLUDE only placement: 'Place and cure', 'Pour', 'Cast'. EXCLUDE "
        "these, which get miscounted: 'Remove concrete', 'Demolish', 'Cut', "
        "'Saw' -> demolition; 'Form', 'Install reinforcement', 'Install deck "
        "pans' -> precede placement; 'Install new bearings', 'Erect "
        "superstructure' -> steel erection; 'Strip forms', 'Backfill' -> "
        "follow placement. A placement on a winter-restricted bridge "
        "calendar was scheduled deliberately - say so rather than calling it "
        "an oversight. Empty list after exclusions -> PASS. Look in: "
        "schedule activity names, dates and calendar assignments.",
    ),
    CheckDef(
        "cold_weather_concreting", CAT_WINTER,
        "Winter Concrete Uses Cold Weather Concreting (504.03.02.C)",
        "Only applies if genuine concrete PLACEMENT falls in Dec 15 - Mar 15 "
        "(apply the same exclusions as the winter concrete check), checking "
        "every winter the project spans - a multi-year project has more "
        "than one, and a placement in a LATER winter still needs a "
        "cold-weather plan even if the first winter had none. No "
        "winter placement in ANY year -> WARNING, 'no winter concrete work present.' "
        "Otherwise confirm three things per winter with a placement: a "
        "cold-weather concreting plan submittal finishing at least 30 days "
        "before that winter's first placement; durations allowing 7 days "
        "protection for decks and approaches, 5 for other placements; and "
        "the narrative identifying the winter work. Look in: schedule submittal activities and "
        "placement dates; Standard Specifications 504.03.02.C; designer's "
        "narrative.",
        source_files=["narrative", "schedule", "spec"],
    ),
    CheckDef(
        "concrete_cure_time", CAT_NONE,
        "Concrete Cure Time Accounted For (507.03.02.J)",
        "Deck cure is 14 days minimum with nothing on the deck; sawcutting "
        "no earlier than 15 days after placement. Check each deck and slab "
        "placement has that time either inside its own duration or in the "
        "gap before its successors. Work that must wait: sawcutting, "
        "grooving, membrane, overlay, loading the deck. Overlapping work at "
        "a DIFFERENT location or in another stage is not a violation - "
        "check location before calling it one. Give your reasoning already "
        "resolved; do not leave a 'however' unanswered. Look in: schedule "
        "placement activities and successor gaps; Standard Specifications "
        "507.03.02.J and 507.03.02.L.",
        source_files=["narrative", "schedule", "spec"],
    ),

    # ── Weather & Paving Restrictions ──────────────────────────────────────────
    CheckDef(
        "no_paving_winter", CAT_WEATHER_PAVING,
        "No Paving Activities Dec 15 - Mar 15",
        "No paving between Dec 15 and Mar 15, recurring every winter the "
        "project spans - a multi-year project has more than one such "
        "window; check every one, not just the first. Identify paving: pave, "
        "paving, mill, overlay, HMA, asphalt, surface course, base course, "
        "pavement box, stripe. List them all, then filter to those "
        "intersecting any occurrence of the window. THE STATUS FOLLOWS THE FILTERED LIST. If "
        "it is empty, PASS - however many paving activities exist "
        "elsewhere. Never cite an activity outside the window as evidence "
        "of a violation inside it. Look in: schedule activity list and "
        "dates.",
    ),

    # ── Completion Milestones ──────────────────────────────────────────────────
    CheckDef(
        "no_completion_in_winter", CAT_COMPLETION,
        "Completion Dates Not Between Dec 15 and Mar 15",
        "Deterministic: computed from the Substantial (M900) and Final "
        "(M950) Completion milestone dates against the fixed Dec 15 - Mar "
        "15 window — no AI judgement involved. FAIL states whichever "
        "milestone(s) fall in the window.",
        check_type="date_rule",
    ),
    CheckDef(
        "project_region_i195", CAT_NONE,
        "Project Location North/South of I-195 (from Key Map Coordinates)",
        "Deterministic: computed from the key map's latitude/longitude "
        "against the I-195 alignment — no AI judgement involved. Reports "
        "whether the project is NORTH or SOUTH of I-195.",
        check_type="geo",
        source_files=["keymap"],
    ),
    CheckDef(
        "substantial_regional_deadlines", CAT_COMPLETION,
        "Substantial Completion Before Oct 1 (North/Central NJ) or Oct 15 (South NJ)",
        "Deterministic: computed from the Substantial Completion milestone "
        "(M900) date against the region-dependent deadline (Oct 1 NORTH of "
        "I-195, Oct 15 SOUTH), using the same region determination as "
        "project_region_i195 — no AI judgement involved. Governs SUBSTANTIAL "
        "Completion only; a later Final Completion is expected and not a "
        "failure here.",
        check_type="date_rule",
        source_files=["schedule", "keymap"],
    ),

    # ── Working Drawings, Materials & ITS ──────────────────────────────────────
    CheckDef(
        "working_drawing_review_time", CAT_WORKING_DRAWINGS,
        "Working Drawing Review Durations (30/45 Days)",
        "Certified submittals get 30 days review; Approved get 45. Category "
        "is set by Table 105.05-1 - but the Special Provisions often "
        "REPLACE that table (search for the literal heading \"TABLE "
        "105.05-1 IS CHANGED TO\" and the column header \"Working Drawing "
        "Submission Category\"); the replacement governs. Read the table "
        "carefully: two columns of unequal length, and the tail rows of the "
        "longer one are easily misread as the other. For each submittal "
        "give: label in schedule, column in the governing table, scheduled "
        "duration, required duration, match? FAIL on each mismatch. Do not "
        "explain a long duration away as material lead time unless a "
        "document says so - check the narrative's special materials "
        "section first. If the retrieved SP text does not contain a Table "
        "105.05-1 replacement, say so explicitly before falling back to the "
        "base spec table - falling back silently is how a superseded table "
        "gets applied by mistake. Look in: Special Provisions Table "
        "105.05-1; Standard Specifications 105.05; schedule submittal "
        "(PS-series) activities; narrative Lead Time section.",
        source_files=["narrative", "schedule", "sp", "spec"],
        sp_top_k=12,
    ),
    CheckDef(
        "steel_pole_lead_time", CAT_WORKING_DRAWINGS,
        "Steel Traffic Signal Pole Fabrication Lead Time (4 Months)",
        "Steel traffic signal poles need 4 months fabrication and delivery. "
        "Decide applicability first: does this project install NEW poles, "
        "mast arms or pole foundations? A project that only resets or "
        "rewires an existing signal does not. If none, PASS with 'no new "
        "steel signal pole work' and name what you checked. If new poles "
        "exist, verify at least 4 months between fabrication and "
        "installation. Look in: schedule activity and submittal list; "
        "Special Provisions SECTION 702 - TRAFFIC SIGNALS and SECTION 703 - "
        "HIGHWAY LIGHTING; Construction Scheduling Manual Table A; traffic "
        "signal plans if available. If no SP text relevant to pole lead "
        "times was retrieved, say so rather than treating silence as "
        "confirmation there is no requirement.",
        source_files=["schedule", "narrative", "sp", "csm"],
    ),
    CheckDef(
        "aluminum_pole_lead_time", CAT_WORKING_DRAWINGS,
        "Aluminum Lighting/Signal Pole Fabrication Lead Time (2 Months)",
        "Aluminum lighting and signal poles need 2 months fabrication and "
        "delivery. Decide applicability first: look for lighting standards, "
        "luminaire poles or aluminum signal poles. If none, PASS and name "
        "what you checked. If present, verify at least 2 months between "
        "submittal or fabrication and installation. Report installation "
        "activities that have no procurement time before them. Look in: "
        "schedule activity and submittal list; Special Provisions SECTION "
        "703 - HIGHWAY LIGHTING, particularly 703.03.07 Temporary Highway "
        "Lighting System (search: \"Lighting mast arms and standards\"); "
        "Construction Scheduling Manual Table A. If no SP text relevant to "
        "pole lead times was retrieved, say so rather than treating silence "
        "as confirmation there is no requirement.",
        source_files=["schedule", "narrative", "sp", "csm"],
    ),
    CheckDef(
        "controller_lead_time", CAT_WORKING_DRAWINGS,
        "Traffic Signal Controller Fabrication Lead Time (4 Months)",
        "Traffic signal controllers need 4 months fabrication and delivery. "
        "Decide applicability first: is a NEW controller, cabinet or CTSS "
        "equipment procured? A reset signal or temporary signal system may "
        "need none. If none, PASS and name what you checked. If present, "
        "verify at least 4 months before installation, and that "
        "installation leaves time for testing before Substantial "
        "Completion. Look in: schedule activity and submittal list; Special "
        "Provisions 702.03.01 Controller (search: \"Submit catalog cuts for "
        "the time synchronized GPS unit to the RE for approval before "
        "installation\"); Construction Scheduling Manual Table A. If no SP "
        "text relevant to controller lead times was retrieved, say so "
        "rather than treating silence as confirmation there is no "
        "requirement.",
        source_files=["schedule", "narrative", "sp", "csm"],
    ),

    # ── Schedule Logic ─────────────────────────────────────────────────────────
    CheckDef(
        "no_negative_float", CAT_SCHEDULE_LOGIC,
        "No Negative Float Present",
        "Deterministic: computed from each activity's recomputed total "
        "float in the schedule graph — no AI judgement involved. FAIL lists "
        "every activity with negative float.",
        check_type="schedule_logic",
    ),
    CheckDef(
        "no_lag", CAT_SCHEDULE_LOGIC,
        "No Lag Present",
        "Deterministic: computed from every relationship's type and lag in "
        "the schedule graph — no AI judgement involved. FAIL lists any "
        "Finish-to-Start relationship with lag, and any relationship of any "
        "type with negative lag, per Construction Scheduling Manual "
        "Section 3.0.",
        check_type="schedule_logic",
    ),
    CheckDef(
        "no_open_ends", CAT_SCHEDULE_LOGIC,
        "No Open Ends Present",
        "Deterministic: computed from each activity's incoming/outgoing "
        "relationship types in the schedule graph — no AI judgement "
        "involved. Open start = no Finish-to-Start or Start-to-Start "
        "predecessor; open finish = no Finish-to-Start or Finish-to-Finish "
        "successor. The project's own start/finish milestones (M100, M950) "
        "are exempt per Construction Scheduling Manual Section 3.0.",
        check_type="schedule_logic",
    ),
    CheckDef(
        "no_mandatory_constraints", CAT_SCHEDULE_LOGIC,
        "No Mandatory Constraints Applied",
        "Deterministic: computed from each activity's constraint type in "
        "the schedule graph — no AI judgement involved. FAIL lists every "
        "activity with a Mandatory Start or Mandatory Finish constraint, "
        "per Construction Scheduling Manual Section 3.0.",
        check_type="schedule_logic",
    ),
    CheckDef(
        "cpm_consistency", CAT_SCHEDULE_LOGIC,
        "P6 Stored Values Match Recomputed CPM (Schedule Recalculated)",
        "Confirm the schedule was recalculated: stored dates and float "
        "should match what the logic and calendars produce. Mismatches "
        "usually mean the file was edited and not rescheduled, which makes "
        "every other date-based finding unreliable. PASS on zero or "
        "tolerance-only mismatches; FAIL beyond tolerance citing IDs; "
        "WARNING if the section is absent - say stored values could not be "
        "verified rather than implying failure. Look in: precomputed 'CPM "
        "Validation' section.",
    ),

    # ── Completion Milestones (continued) ─────────────────────────────────────
    CheckDef(
        "substantial_to_final", CAT_COMPLETION,
        "Substantial to Final Completion Gap (60 Days <= $50M / 90 Days > $50M)",
        "Deterministic: the Engineer's Estimate is read from the uploaded "
        "DBE Goal Memo and the Substantial/Final Completion milestone dates "
        "from the schedule; the calendar-day gap is computed in code and "
        "compared against 60 days ($50M or less) or 90 days (over $50M). "
        "No AI judgement involved.",
        check_type="cost_gap",
        source_files=["schedule", "estimate"],
    ),

    # ── Manual Review (continued) — EDQ items (deterministic — graph coverage) ─
    CheckDef(
        "edq_items", CAT_NONE,
        "EDQ Items Cross-Referenced for Missing Construction Activities",
        "Every pay item implies work that needs an activity. Group the "
        "items by operation - earthwork, drainage, structures, paving, "
        "curb and sidewalk, guide rail, signing and striping, signals and "
        "lighting, landscaping, SESC - and find the activity performing "
        "each. Watch for the commonly omitted: SESC measures, temporary "
        "pavement and its removal, utility resets, testing and acceptance, "
        "final cleanup. If you were not given the EDQ or estimate, say "
        "exactly which document is missing. Look in: EDQ / estimate item "
        "list; Special Provisions; schedule activity list.",
        check_type="edq_coverage",
        source_files=["schedule", "estimate", "sp"],
    ),

    # ── Designer's Narrative (required narrative sections per the CSM) ─────────
    # The Construction Scheduling Manual requires the schedule narrative to
    # contain each of these sections; every check confirms the section is present
    # and substantive in the narrative PDF.
    CheckDef(
        "narrative_production_rates", CAT_NARRATIVE,
        "Narrative States Anticipated Production Rates",
        "Confirm the narrative states anticipated production rates AND "
        "their source - typically the Construction Scheduling Manual rate "
        "tables - plus the assumptions behind them (hours per working day, "
        "crews per activity). FAIL if rates are asserted as reasonable with "
        "no named basis. Look in: narrative, Anticipated Production Rates "
        "section.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_workforce", CAT_NARRATIVE,
        "Narrative Describes Anticipated Workforce",
        "Confirm the narrative lists crew types with their trade makeup and "
        "size. Check the stated crew count against the list actually given "
        "- if the narrative says one number and enumerates another, report "
        "it. Look in: narrative, Anticipated Workforce section.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_winter_work", CAT_NARRATIVE,
        "Narrative Describes Winter-Season Work and Workdays",
        "Confirm the narrative covers December through March: how many "
        "winter seasons, which work types are restricted and by which "
        "calendars, and THE NUMBER OF WORKDAYS for Bridge and Roadwork. A "
        "citation to Appendix B or Table 108.11.01-1 instead of actual "
        "numbers is partial - the requirement is the counts. If you can see "
        "calendar workdays per month, compare them to the cited allowance "
        "and report any excess. Look in: narrative, Anticipated Winter "
        "Season Work section; schedule calendars; Construction Scheduling "
        "Manual Appendix B; Standard Specifications Table 108.11.01-1.",
        source_files=["narrative", "csm", "schedule", "spec"],
    ),
    CheckDef(
        "narrative_permit_requirements", CAT_NARRATIVE,
        "Narrative Addresses Permit Requirements",
        "Confirm the narrative lists anticipated permits by issuing agency "
        "AND the restrictions each imposes, with dates. Permits listed "
        "without their restrictions is a FAIL. Look in: narrative, Permit "
        "Requirements section.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_utility_requirements", CAT_NARRATIVE,
        "Narrative Addresses Utility Requirements",
        "Confirm the narrative names the utility owners, describes each "
        "one's scope, and states the TIMING dependencies between utility "
        "work and construction stages. The timing statements matter most - "
        "they drive schedule logic. Owners listed without scope or "
        "sequencing is a FAIL. Look in: narrative, Utility Requirements "
        "section.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_row_requirements", CAT_NARRATIVE,
        "Narrative Addresses ROW Requirements",
        "Confirm the narrative identifies the acquisitions by parcel "
        "designation, what each is needed for, and when they become "
        "available. Scrutinise any claim that all ROW is acquired before "
        "construction start - check it against the parcel dates in the "
        "Special Provisions. Look in: narrative, Right-of-Way Requirements "
        "section; Special Provisions 108.12. If the retrieved SP text does "
        "not contain 108.12, say so.",
        source_files=["narrative", "sp"],
    ),
    CheckDef(
        "narrative_community_commitments", CAT_NARRATIVE,
        "Narrative Addresses Community Commitments",
        "Confirm the narrative addresses commitments made during public "
        "outreach - access, noise, events, promised dates. An explicit "
        "statement that no specific commitments were made IS a valid answer "
        "and passes. FAIL only if the topic is absent. Look in: narrative, "
        "Community Commitments section.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_material_lead_time", CAT_NARRATIVE,
        "Narrative States Lead Time for Special Materials",
        "Confirm the narrative either identifies special materials with "
        "their lead times, or states affirmatively that none are required. "
        "Both pass. If it claims none while the schedule carries long "
        "procurement durations, note the tension. Look in: narrative, Lead "
        "Time for Special Materials section; schedule submittal durations.",
        source_files=["narrative", "schedule"],
    ),
    CheckDef(
        "narrative_detours", CAT_NARRATIVE,
        "Detours and Timeframes Included in Schedule/Narrative",
        "Confirm detours and their timeframes appear in the narrative and "
        "the schedule. State whether long-term detours are required, and "
        "describe short-term detours, lane closures and overnight shutdowns "
        "with the roadways and stages affected. 'No long-term detours' "
        "passes provided short-term closures are still described. Look in: "
        "narrative, Detour section; schedule activity list.",
        source_files=["narrative", "schedule"],
    ),
    CheckDef(
        "narrative_critical_milestones", CAT_NARRATIVE,
        "Narrative Identifies Critical Milestones",
        "Confirm the narrative lists critical milestones with dates - road "
        "and ramp openings, stage completions, substantial and final "
        "completion. Compare each against the schedule milestone. Report "
        "any disagreement with both values. Look in: narrative, Critical "
        "Milestones section; schedule milestones.",
        source_files=["narrative", "schedule"],
    ),
    CheckDef(
        "narrative_schedule_problems", CAT_NARRATIVE,
        "Narrative Describes Anticipated Schedule Problems",
        "Confirm the narrative names project-specific risks - ROW "
        "acquisition, utility coordination, permit conditions, sequencing "
        "dependencies - and where possible what each would delay. Generic "
        "construction risk with nothing specific to this project is a "
        "FAIL. Look in: narrative, Anticipated Schedule Problems section.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_acceleration", CAT_NARRATIVE,
        "Narrative Describes Any Acceleration Applied",
        "Confirm the narrative states whether acceleration was applied and "
        "by what means - additional crews, multiple shifts, extended "
        "hours, resequencing. A narrative saying none was applied but "
        "explaining how it could be achieved also passes. FAIL only if "
        "unaddressed. Look in: narrative, Project Schedule Acceleration "
        "section.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_winter_extension_reason", CAT_NARRATIVE,
        "Reason Given if Substantial-to-Final Extends Through Winter",
        "Applies only if the Substantial-to-Final period runs through "
        "December to March. Check the two milestone dates first - if it "
        "does not, PASS and state the dates. If it does, the narrative must "
        "explain why and show that multiple crews, extended hours or "
        "resequencing were considered. Look in: schedule milestones M900 "
        "and M950; designer's narrative.",
        source_files=["schedule", "narrative"],
    ),
    CheckDef(
        "narrative_work_hour_restrictions", CAT_NARRATIVE,
        "Narrative Addresses Work-Hour Restrictions",
        "Confirm the narrative addresses work-hour restrictions: marine and "
        "navigation windows, movable bridge openings, railroad traffic, "
        "special events, municipal or county ordinances. A narrative that "
        "considers these and states none apply passes. But check whether "
        "the project's conditions imply one - a bridge over a navigable "
        "waterway suggests marine considerations. Also check the Special "
        "Provisions for an enforceable closure schedule - 108.08 OCCUPANCY "
        "CHARGES (search: \"The closure schedule shown in the plans "
        "indicates the time periods for allowable closures\", \"Overrun of "
        "'Alternating Traffic Pattern/Traffic Shift' Time Limits\") and "
        "SECTION 159 - TRAFFIC CONTROL are work-hour restrictions in their "
        "own right even when framed as a cost provision, and 108.06 can "
        "carry a night-operations lighting requirement. A closure schedule "
        "with a per-minute occupancy charge that the narrative never "
        "mentions is a FAIL, not a pass by omission - the narrative's "
        "silence is itself the finding. Look in: narrative; Traffic Control "
        "Plans; Special Provisions 108.08, 108.06, Section 159.",
        source_files=["narrative", "schedule", "sp"],
    ),
    CheckDef(
        "narrative_emergency_routes", CAT_NARRATIVE,
        "Emergency Routes Determined and Included if Required",
        "Decide whether emergency routes are required. They become a "
        "concern when the project closes or narrows a route serving "
        "emergency response, detours traffic, or shuts a crossing overnight "
        "with no parallel route. Narrative addresses emergency access -> "
        "PASS. Conditions imply it matters and the narrative is silent -> "
        "FAIL, naming the condition. No condition implies it -> PASS, "
        "saying why. Look in: narrative, Detour and Traffic Control "
        "sections; schedule closure activities.",
        source_files=["narrative", "schedule"],
    ),
    CheckDef(
        "narrative_night_work", CAT_NARRATIVE,
        "Night Work Explained in the Narrative",
        "Night work can be REQUIRED by contract even when the schedule "
        "shows none. First check the SP for mandated night work - telecom "
        "fiber splicing during overnight windows is the common case, "
        "typically under 105.07.02 (search: \"day shift, night shift, or "
        "on weekends\", \"safe-time\", \"advance notice for nighttime "
        "work\"). Then check the schedule for activities named night or "
        "overnight, and night-related submittals (a Night Lighting Plan "
        "means night operations were contemplated) - calendar assignment "
        "is not in the evidence you receive, so do not claim to have "
        "checked it. FAIL if the SP requires night work the narrative does "
        "not explain or the schedule cannot perform. Do not pass on 'no "
        "night work scheduled' without addressing whether any is required. "
        "An SP mandate, if found, is Special Provisions text - attribute it "
        "to \"Special Provisions 105.07.02\", never to the narrative, even "
        "if the narrative is silent on the topic; silence in the narrative "
        "is itself a finding, not a reason to relocate the SP's own "
        "language into it. Look in: Special Provisions 105.07.02 utility "
        "sections; schedule submittals; designer's narrative.",
        source_files=["narrative", "schedule", "sp"],
    ),

    # ── Manual Review (continued) ─────────────────────────────────────────────
    CheckDef(
        "traffic_control_staging", CAT_NONE,
        "Traffic Control Staging Sequences Match Schedule Narrative",
        "Staging on the plans must match the schedule and narrative. "
        "Compare three things: the stage sequence and numbering; the stage "
        "completion milestones against the narrative's dates; and the "
        "activities within each stage. Report every date or sequence "
        "disagreement with both values - a single mismatched milestone "
        "date is a finding even when the sequence is right. No staging "
        "plan -> the narrative must outline the assumed order of work. "
        "Separately and always: search the Special Provisions for "
        "\"TEMPORARY HIGHWAY LIGHTING SYSTEM (STAGE\" pay items and for "
        "\"Pre-Stage\", \"Post-Stage\", \"during Stage 1\", \"during Stage "
        "2\", and section 703 - list every stage a temporary lighting item "
        "is required for, then check the schedule for a matching lighting "
        "activity in each of those stages. A required stage with no "
        "matching activity is a FAIL, cited separately from any "
        "staging-sequence finding. Also search for reset/work-verification "
        "notes (e.g. \"WV #\" item numbers) tied to a specific stage and "
        "confirm a matching activity exists in that stage. If the "
        "retrieved SP text does not contain 703 or a lighting pay-item "
        "list, say so rather than concluding no lighting requirement "
        "exists. Look in: Traffic Control Plans; Special Provisions 703 "
        "and any Traffic Control Stage notes; narrative Critical Milestones "
        "section; schedule milestones and stage WBS.",
        source_files=["sp", "narrative", "schedule"],
        sp_top_k=12,
    ),
    CheckDef(
        "summer_shutdown", CAT_NONE,
        "Summer Shutdown Restrictions Applied (NJ Shore Routes)",
        "Some contracts restrict work during peak travel season, usually "
        "on shore routes around Memorial Day to Labor Day. If the SP was "
        "supplied and contains no summer, seasonal, shutdown or moratorium "
        "provision, and the project is not on a shore route, PASS - naming "
        "what you searched and where the project is. That is a "
        "determination, not a gap. WARNING only if the SP was not supplied "
        "at all. Look in: Special Provisions (search: \"summer\", "
        "\"seasonal restriction\", \"shutdown\", \"moratorium\", \"shore\"); "
        "Key Sheet route and location.",
        source_files=["sp", "keymap"],
    ),
    CheckDef(
        "required_activities_present", CAT_NONE,
        "Applicable Roadside Activities Included in Schedule",
        "Check for guide rail, rumble strips, concrete curb, sidewalk and "
        "ADA ramps, islands, driveways, topsoil and seeding, signing, "
        "striping, SESC measures and their removal, and demolition. For "
        "each, either find the activity or establish it is not in scope. "
        "Absence is only a finding where the scope includes the item. If "
        "you were not given the schedule activity list, say so - do not "
        "report the items missing when what is missing is the schedule. "
        "The Special Provisions' pay-item list is usually under headings "
        "like \"MEASUREMENT AND PAYMENT\" and an \"Item / Pay Unit\" table - "
        "search those rather than assuming scope from item names alone. "
        "Name which parts of the Special Provisions you actually searched - "
        "a generic \"Special Provisions\" evidence block may not cover "
        "every relevant section; say so if coverage looks partial. Look "
        "in: schedule activity list; Special Provisions; estimate items; "
        "plan sheet index.",
        source_files=["sp", "schedule", "estimate"],
    ),
    CheckDef(
        "multi_year_funding", CAT_NONE,
        "Multi-Year Funding Logic Applied (SP 108.10)",
        "Multi-year funding shows as language splitting work across State "
        "fiscal years, capping value in a period, or setting an interim "
        "completion tied to a funding year. Where present, the schedule "
        "must show the matching constraint. Most 108.10 sections contain "
        "only completion dates. If you read 108.10 and it holds only "
        "Substantial Completion and Completion dates, PASS - quoting what "
        "it says. That is a determination. WARNING only if 108.10 was not "
        "supplied. Look in: Special Provisions 108.10 CONTRACT TIME "
        "(search: \"Substantial Completion on or before\"); schedule "
        "milestones and activity dates.",
        source_files=["sp", "schedule"],
    ),
    CheckDef(
        "nearby_projects", CAT_NONE,
        "No Conflicts with Nearby Construction Projects (105.06)",
        "The base 105.06 language is a generic duty to cooperate with "
        "Others. What matters is whether the Special Provisions AMEND it to "
        "name specific adjacent projects or coordination requirements. "
        "Generic base form with no project-specific additions -> PASS, "
        "stating you read 105.06 and it names no adjacent work. That is a "
        "determination, not a gap. Where adjacent work IS named, check the "
        "schedule accounts for it. WARNING only if the SP was not supplied. "
        "Look in: Special Provisions 105.06 (search: \"Cooperation with "
        "Others\"); Standard Specifications 105.06; schedule. If retrieval "
        "returns no 105.06 text at all (not even the base form), say so "
        "distinctly from '105.06 exists and adds nothing' - those are "
        "different findings.",
        source_files=["schedule", "sp", "spec"],
    ),

    # ── Working Drawings, Materials & ITS (continued) ─────────────────────────
    CheckDef(
        "its_burn_in", CAT_WORKING_DRAWINGS,
        "ITS New Installation Observation Period (6 Months)",
        "Decide what ITS work exists before applying any rule: reset or "
        "relocation of existing equipment only -> PASS, 'no new ITS "
        "installation,' naming the activities inspected; new ITS devices -> "
        "6-month observation after verification testing; may overlap other "
        "work but must be represented; adaptive CTSS -> burn-in typically "
        "waived to run concurrent with construction. Non-CTSS systems need "
        "no burn-in unless brand new to the Department. Separately and "
        "always: if any electrical or ITS installation exists, verify some "
        "testing or acceptance activity sits between the last installation "
        "and Substantial Completion. Report its absence as a finding of "
        "its own. Look in: schedule electrical and ITS activities; Special "
        "Provisions 701.03.15 and 704 (search: \"interim acceptance\", "
        "\"verification testing\", \"burn-in\"); Standard Specifications "
        "Section 704.",
        source_files=["narrative", "schedule", "sp", "spec"],
    ),
]


# check_keys that make up the "Manual Review" bucket reported in the /api/review
# summary (checks that cross-reference the Special Provision / Scheduling Manual
# excerpts rather than relying on the schedule/narrative alone). These checks
# carry no `category` of their own (see CAT_NONE above) since the checklist UI
# no longer groups by category except for the nested sub-check groups above —
# this set is check_key-based so it stays correct independent of that.
# "utility_alignment" left this set when the key map upload was added — it is
# now a real LLM-evaluated comparison (key map utilities vs. Special
# Provisions) rather than a cross-reference the reviewer must do by hand.
# "edq_items" left this set for the same reason: it's now a deterministic
# graph-coverage check (check_type="edq_coverage"), not a human cross-reference.
MANUAL_REVIEW_KEYS = frozenset({
    "environmental_permit",
    "traffic_control_staging", "summer_shutdown", "required_activities_present",
    "multi_year_funding", "nearby_projects",
})


def builtin_checks_as_dicts() -> List[dict]:
    """Return the full built-in catalog as plain dicts (for seeding / the UI /
    the main review prompt — every check is evaluated the same way now)."""
    return [c.as_dict() for c in BUILTIN_CHECKS]
