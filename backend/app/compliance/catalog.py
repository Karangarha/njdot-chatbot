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
CAT_UTILITY_RESTRICTIONS = "Utility Service Restrictions"
CAT_WEATHER_PAVING = "Weather & Paving Restrictions"
CAT_MATERIAL_LEAD = "Material Fabrication Lead Times"
CAT_NARRATIVE = "Designer's Narrative"


@dataclass(frozen=True)
class CheckDef:
    """One compliance check definition.

    ``instruction`` is the natural-language rule injected into the LLM prompt.
    Every check is evaluated the same way, in one ``/api/review`` LLM call that
    receives the schedule markdown, the full narrative PDF, the full Special
    Provision PDF (when uploaded), and keyword-searched Construction Scheduling
    Manual excerpts relevant to the enabled checks. ``check_type`` is kept as
    descriptive metadata (currently always ``"llm"``) for forward compatibility;
    nothing branches on it.
    """

    check_key: str
    category: str
    name: str
    instruction: str
    check_type: str = "llm"
    # Which document(s) app.compliance.eval_engine.evaluate_checks() should
    # draw evidence from for this check: any combination of "schedule"
    # (CPM facts + milestones + activity roster, from Neo4j), "narrative"
    # (full designer-narrative text, from Neo4j), and "sp" (Special
    # Provision retrieval). Explicit per-check — set by the user when they
    # add a custom check ("select the file(s)"), defaulted by category for
    # built-ins.
    source_files: List[str] = field(default_factory=lambda: ["schedule"])

    def as_dict(self) -> dict:
        return asdict(self)


# NOTE: order matters — this array's order follows the NJDOT source checklist
# document line-by-line. Checks render in this order; only the four groups
# below that the source document actually nests under a shared parent line
# (Utility Service Restrictions, Weather & Paving Restrictions, Material
# Fabrication Lead Times, Designer's Narrative) carry a category, which the
# checklist editor renders as a header. Everything else is uncategorized.
BUILTIN_CHECKS: List[CheckDef] = [
    # ── Manual Review: utility alignment vs. Key-Map / Special Provisions ──────
    CheckDef(
        "utility_alignment", CAT_NONE,
        "Utility Alignments Match Key Sheet and Special Provisions",
        "Cross-check utility alignments against the Special Provisions attached "
        "above. Mark warning if the Special Provision was not provided.",
        source_files=["sp"],
    ),

    # ── Administrative Dates ──────────────────────────────────────────────────
    CheckDef(
        "schedule_duration", CAT_NONE,
        "Schedule Duration Under 3 Years (Exclude Weekends and Holidays)",
        "Locate the start date of the Advertisement milestone (M100) and the end "
        "date of the Contract Completion milestone (M950). Compute the duration in "
        "business days, excluding weekends and holidays. Pass if it is under 3 years. "
        "Pass If Designer narrative give the reason for the exceeding the timeframe. "
        "Else Fail",
        source_files=["schedule", "narrative"],
    ),
    CheckDef(
        "ad_date_day", CAT_NONE,
        "Advertisement Date Falls on Tuesday or Thursday",
        "Inspect the weekday of the Advertisement milestone (M100) date. Pass if it "
        "is a Tuesday or Thursday; any other weekday is a fail.",
    ),
    CheckDef(
        "bid_date_day", CAT_NONE,
        "Bid Date Falls on Tuesday or Thursday",
        "Inspect the weekday of the Bid Opening milestone date. Pass if it is a "
        "Tuesday or Thursday; any other weekday is a fail.",
    ),
    CheckDef(
        "ad_to_bid_gap", CAT_NONE,
        "15 Business Days: Advertisement to Bid",
        "Verify at least 15 business days (excluding weekends and holidays) between "
        "the Advertisement date and the Bid date.",
    ),
    CheckDef(
        "bid_to_award_gap", CAT_NONE,
        "15 Business Days: Bid to Award",
        "Verify at least 15 business days (excluding weekends and holidays) between "
        "the Bid date and the Award date.",
    ),
    CheckDef(
        "award_to_construction", CAT_NONE,
        "Award to Construction Start Timeframe (40 days State / 55 days Federal / 25-55 days Pavement)",
        "Determine the project type, then verify the business-day gap from Award to "
        "Construction Start: exactly 40 business days (State), 55 business days "
        "(Federal), or 25-55 business days (Pavement Preservation). Mark warning if "
        "the project type is unknown.",
    ),

    # ── Environmental, Landscape & ROW ────────────────────────────────────────
    CheckDef(
        "row_availability", CAT_NONE,
        "ROW Availability Date Precedes Parcel Work",
        "Verify that each Right-of-Way (ROW) availability date precedes the start of "
        "work on the corresponding parcel.",
    ),
    CheckDef(
        "environmental_permit", CAT_NONE,
        "Environmental Permit Compliance Beyond Narrative",
        "Assess environmental permit compliance using the narrative and the "
        "attached Special Provision / Scheduling Manual excerpts beyond what the "
        "narrative alone asserts.",
        source_files=["sp"],
    ),
    CheckDef(
        "landscape_season", CAT_NONE,
        "Landscape/Planting Limited to Mar 1-May 15 or Aug 15-Dec 1",
        "Verify that landscape and planting activities are scheduled only within "
        "March 1 - May 15 or August 15 - December 1.",
    ),

    # ── Utility Service Restrictions (nested under one parent line in the
    # source document: gas/water/electric interruptions, railroad, work hours) ─
    CheckDef(
        "gas_interruption", CAT_UTILITY_RESTRICTIONS,
        "No Gas Service Interruptions Oct 1 to Apr 1",
        "Check if any gas activity is scheduled, if yes, verify that no gas service "
        "interruption activities are scheduled between October 1 and April 1. Pass, "
        "if no gas activity is scheduled.",
    ),
    CheckDef(
        "water_interruption", CAT_UTILITY_RESTRICTIONS,
        "No Water Service Interruptions Apr 1 to Sep 30",
        "Check if any water activity is scheduled, if yes, then verify that no water "
        "service interruption activities are scheduled between April 1 and "
        "September 30. Pass, if no water activity scheduled.",
    ),
    CheckDef(
        "electric_interruption", CAT_UTILITY_RESTRICTIONS,
        "No Electric Service Interruptions Jun 1 to Sep 30",
        "Check if any Electric activity is scheduled, if yes, then verify that no "
        "electric service interruption activities are scheduled between June 1 and "
        "September 30. if no electricity activity is scheduled then pass.",
    ),
    CheckDef(
        "utility_work_hours", CAT_UTILITY_RESTRICTIONS,
        "Utility Relocation Work Accounts for No Night/Weekend Work",
        "Utility companies typically do not work nights or weekends. Where utility "
        "relocations or other utility work are proposed, verify the schedule accounts "
        "for this (no assumed night/weekend utility production) and allows adequate "
        "duration.",
    ),
    CheckDef(
        "railroad_restrictions", CAT_UTILITY_RESTRICTIONS,
        "Railroad Work Restrictions Reflected in Schedule and Special Provisions",
        "Check for any railroad-related work restrictions (flagging, windows, "
        "coordination) and verify they are reflected in the schedule and the "
        "Special Provisions. Mark Pass if the project has no railroad involvement.",
    ),

    # ── Weather & Paving Restrictions (nested under one parent line in the
    # source document: temperature windows, winter concrete/paving, cure time) ─
    CheckDef(
        "temp_50_window", CAT_WEATHER_PAVING,
        "Work Requiring >50°F Scheduled May 1 - Sep 30",
        "Work requiring temperatures above 50°F must be scheduled between May 1 and "
        "September 30. Verify temperature-sensitive activities fall within this window.",
    ),
    CheckDef(
        "temp_60_window", CAT_WEATHER_PAVING,
        "Work Requiring >60°F Scheduled Jun 1 - Sep 15",
        "Work requiring temperatures above 60°F must be scheduled between June 1 and "
        "September 15. Verify temperature-sensitive activities fall within this window.",
    ),
    CheckDef(
        "no_concrete_winter", CAT_WEATHER_PAVING,
        "No Concrete Activities Dec 15 - Mar 15",
        "Verify that no concrete-placement activities are scheduled between "
        "December 15 and March 15.",
    ),
    CheckDef(
        "cold_weather_concreting", CAT_WEATHER_PAVING,
        "Winter Concrete Uses Cold Weather Concreting (504.03.02.C)",
        "If any concrete work is scheduled between December 15 and March 15, verify "
        "Cold Weather Concreting per Section 504.03.02.C is applied and the winter "
        "work is identified in the narrative. Mark warning if no winter concrete work "
        "is present.",
    ),
    CheckDef(
        "concrete_cure_time", CAT_WEATHER_PAVING,
        "Concrete Cure Time Accounted For (507.03.02.J)",
        "Verify the schedule accounts for concrete cure time per Section 507.03.02.J "
        "(cure durations reflected before dependent activities begin).",
    ),
    CheckDef(
        "no_paving_winter", CAT_WEATHER_PAVING,
        "No Paving Activities Dec 15 - Mar 15",
        "Verify that no paving activities are scheduled between December 15 and "
        "March 15.",
    ),

    # ── Completion Milestones ─────────────────────────────────────────────────
    CheckDef(
        "no_completion_in_winter", CAT_NONE,
        "Completion Dates Not Between Dec 15 and Mar 15",
        "Verify that no substantial or final completion milestone dates fall between "
        "December 15 and March 15.",
    ),
    CheckDef(
        "substantial_regional_deadlines", CAT_NONE,
        "Substantial Completion Before Oct 1 (North/Central NJ) or Oct 15 (South NJ)",
        "Determine the project geography. Substantial Completion must be before "
        "October 1 for projects North or Central Jersey, and before October 15 for "
        "projects South Jersey. Mark warning if the geography is unknown.",
    ),

    # ── Working Drawings, Materials & ITS ─────────────────────────────────────
    CheckDef(
        "working_drawing_review_time", CAT_NONE,
        "Working Drawing Review Durations (30/45 Days)",
        "Verify that working-drawing review activities allow the required review "
        "durations (30 or 45 days, as applicable to the submittal type).",
    ),

    # ── Material Fabrication Lead Times (nested under one parent line in the
    # source document: steel/aluminum poles, controllers) ────────────────────
    CheckDef(
        "steel_pole_lead_time", CAT_MATERIAL_LEAD,
        "Steel Traffic Signal Pole Fabrication Lead Time (4 Months)",
        "Verify that steel traffic-signal pole fabrication allows a lead time of at "
        "least 4 months.",
    ),
    CheckDef(
        "aluminum_pole_lead_time", CAT_MATERIAL_LEAD,
        "Aluminum Lighting/Signal Pole Fabrication Lead Time (2 Months)",
        "Verify that aluminum lighting/signal pole fabrication allows a lead time of "
        "at least 2 months.",
    ),
    CheckDef(
        "controller_lead_time", CAT_MATERIAL_LEAD,
        "Traffic Signal Controller Fabrication Lead Time (4 Months)",
        "Verify that traffic-signal controller fabrication allows a lead time of at "
        "least 4 months.",
    ),

    # ── Schedule Logic ────────────────────────────────────────────────────────
    CheckDef(
        "no_negative_float", CAT_NONE,
        "No Negative Float Present",
        "Read the precomputed '## Activities with Negative Float' section. Pass if "
        "it is empty; otherwise fail and cite the affected activity IDs.",
    ),
    CheckDef(
        "no_lag", CAT_NONE,
        "No Lag Present",
        "Verify that no relationship lags are present in the schedule logic.",
    ),
    CheckDef(
        "no_open_ends", CAT_NONE,
        "No Open Ends Present",
        "Read the precomputed open-ends information. Pass if there are no open ends "
        "(activities missing a predecessor or successor); otherwise fail and cite "
        "the affected activity IDs.",
    ),
    CheckDef(
        "no_mandatory_constraints", CAT_NONE,
        "No Mandatory Constraints Applied",
        "Read the '## Activities with Mandatory Constraints' section. Pass if it is "
        "empty; otherwise fail and cite the constrained activities.",
    ),
    CheckDef(
        "cpm_consistency", CAT_NONE,
        "P6 Stored Values Match Recomputed CPM (Schedule Recalculated)",
        "Pass when the '## CPM Validation' section reports zero or tolerance-only "
        "mismatches; fail when it reports float or date mismatches beyond tolerance "
        "(cite the affected activity IDs); warning when the '## CPM Validation' "
        "section is absent.",
    ),

    # ── Completion Milestones (continued) ─────────────────────────────────────
    CheckDef(
        "substantial_to_final", CAT_NONE,
        "Substantial to Final Completion Gap (60 Days <= $50M / 90 Days > $50M)",
        "Determine the project budget, then verify the calendar-day gap between "
        "Substantial Completion and Final Completion: at least 60 days for projects "
        "$50M or less, at least 90 days for projects over $50M. Mark warning if the "
        "budget is unknown.",
    ),

    # ── Manual Review: EDQ items ───────────────────────────────────────────────
    CheckDef(
        "edq_items", CAT_NONE,
        "EDQ Items Cross-Referenced for Missing Construction Activities",
        "Cross-reference EDQ items mentioned in the Special Provision or narrative "
        "against the schedule to detect missing construction activities.",
        source_files=["sp"],
    ),

    # ── Designer's Narrative (required narrative sections per the CSM) ─────────
    # The Construction Scheduling Manual requires the schedule narrative to
    # contain each of these sections; every check confirms the section is present
    # and substantive in the narrative PDF.
    CheckDef(
        "narrative_production_rates", CAT_NARRATIVE,
        "Narrative States Anticipated Production Rates",
        "Confirm the schedule narrative states anticipated production rates.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_workforce", CAT_NARRATIVE,
        "Narrative Describes Anticipated Workforce",
        "Confirm the narrative describes the anticipated workforce (number of crews, "
        "crew size, crew type, etc.).",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_winter_work", CAT_NARRATIVE,
        "Narrative Describes Winter-Season Work and Workdays",
        "Confirm the narrative describes anticipated work during the Winter Season "
        "(December through March inclusive), including the number of workdays for "
        "Bridge and Roadwork.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_permit_requirements", CAT_NARRATIVE,
        "Narrative Addresses Permit Requirements",
        "Confirm the narrative addresses permit requirements.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_utility_requirements", CAT_NARRATIVE,
        "Narrative Addresses Utility Requirements",
        "Confirm the narrative addresses utility requirements.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_row_requirements", CAT_NARRATIVE,
        "Narrative Addresses ROW Requirements",
        "Confirm the narrative addresses ROW (Right-of-Way) requirements.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_community_commitments", CAT_NARRATIVE,
        "Narrative Addresses Community Commitments",
        "Confirm the narrative addresses community commitments.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_material_lead_time", CAT_NARRATIVE,
        "Narrative States Lead Time for Special Materials",
        "Confirm the narrative states lead time for special materials.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_detours", CAT_NARRATIVE,
        "Detours and Timeframes Included in Schedule/Narrative",
        "Confirm any necessary detours and their anticipated timeframes are included "
        "in the schedule and narrative.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_critical_milestones", CAT_NARRATIVE,
        "Narrative Identifies Critical Milestones",
        "Confirm the narrative identifies critical milestones (e.g., road/ramp "
        "openings, critical stages).",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_schedule_problems", CAT_NARRATIVE,
        "Narrative Describes Anticipated Schedule Problems",
        "Confirm the narrative describes any anticipated problems meeting the "
        "schedule (ROW, utilities, etc.).",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_acceleration", CAT_NARRATIVE,
        "Narrative Describes Any Acceleration Applied",
        "Confirm the narrative describes any acceleration applied to the project's "
        "schedule.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_winter_extension_reason", CAT_NARRATIVE,
        "Reason Given if Substantial-to-Final Extends Through Winter",
        "If the period between Substantial and Final Completion extends through "
        "December to March, confirm the narrative describes the reason and the "
        "reasonable methods used to avoid it (multiple crews, extended hours, etc.).",
        source_files=["schedule", "narrative"],
    ),
    CheckDef(
        "narrative_work_hour_restrictions", CAT_NARRATIVE,
        "Narrative Addresses Work-Hour Restrictions",
        "Confirm the narrative and Traffic Control Plans address work-hour "
        "restrictions such as marine, bridge openings, railroad traffic, special "
        "events, or municipal/county restrictions. Pass if no such events are "
        "scheduled.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_emergency_routes", CAT_NARRATIVE,
        "Emergency Routes Determined and Included if Required",
        "Confirm whether emergency routes are required and, if required, they are "
        "included in the schedule narrative. Pass if emergency routes are scheduled.",
        source_files=["narrative"],
    ),
    CheckDef(
        "narrative_night_work", CAT_NARRATIVE,
        "Night Work Explained in the Narrative",
        "Confirm night work activities are explained in the schedule narrative. "
        "Pass, if no night work activities are scheduled",
        source_files=["narrative"],
    ),

    # ── Manual Review (cross-references the Special Provision + Scheduling
    # Manual excerpts, both attached directly in the same review call) ─────────
    CheckDef(
        "traffic_control_staging", CAT_NONE,
        "Traffic Control Staging Sequences Match Schedule Narrative",
        "Verify traffic-control staging sequences match the activities and stages in "
        "the schedule and narrative.",
        source_files=["sp"],
    ),
    CheckDef(
        "summer_shutdown", CAT_NONE,
        "Summer Shutdown Restrictions Applied (NJ Shore Routes)",
        "Confirm summer shutdown restrictions are applied on NJ Shore routes per "
        "the attached Special Provision. Mark warning if the Special Provision "
        "was not provided or does not address this.",
        source_files=["sp"],
    ),
    CheckDef(
        "required_activities_present", CAT_NONE,
        "Applicable Roadside Activities Included in Schedule",
        "Verify that activities such as guide rail, rumble strip, concrete curb, "
        "sidewalk, and island are included in the schedule where applicable to the "
        "project scope.",
        source_files=["sp"],
    ),
    CheckDef(
        "multi_year_funding", CAT_NONE,
        "Multi-Year Funding Logic Applied (SP 108.10)",
        "Check the attached Special Provision for section 108.10 (multi-year "
        "funding) and confirm its logic is reflected in the schedule. Mark warning "
        "if 108.10 is not present in the attached documents.",
        source_files=["sp"],
    ),
    CheckDef(
        "nearby_projects", CAT_NONE,
        "No Conflicts with Nearby Construction Projects (105.06)",
        "Check the attached Special Provision for section 105.06 (Cooperation with "
        "Others) for conflicts with nearby construction projects that could affect "
        "the schedule. Mark warning if 105.06 is not present in the attached "
        "documents.",
        source_files=["sp"],
    ),

    # ── Working Drawings, Materials & ITS (continued) ─────────────────────────
    CheckDef(
        "its_burn_in", CAT_NONE,
        "ITS New Installation Observation Period (6 Months)",
        "Verify ITS testing and burn-in are accounted for. New ITS installations "
        "require a 6-month observation period post-verification-testing (other "
        "activities may overlap it). Non-CTSS systems typically need no burn-in "
        "unless brand-new to DOT; adaptive systems typically waive the burn-in to "
        "coincide with construction. Mark warning if it is unclear whether a burn-in "
        "period is required.",
    ),
]


# check_keys that make up the "Manual Review" bucket reported in the /api/review
# summary (checks that cross-reference the Special Provision / Scheduling Manual
# excerpts rather than relying on the schedule/narrative alone). These checks
# carry no `category` of their own (see CAT_NONE above) since the checklist UI
# no longer groups by category except for the four true nested sub-check
# groups — this set is check_key-based so it stays correct independent of that.
MANUAL_REVIEW_KEYS = frozenset({
    "utility_alignment", "environmental_permit", "edq_items",
    "traffic_control_staging", "summer_shutdown", "required_activities_present",
    "multi_year_funding", "nearby_projects",
})


def builtin_checks_as_dicts() -> List[dict]:
    """Return the full built-in catalog as plain dicts (for seeding / the UI /
    the main review prompt — every check is evaluated the same way now)."""
    return [c.as_dict() for c in BUILTIN_CHECKS]
