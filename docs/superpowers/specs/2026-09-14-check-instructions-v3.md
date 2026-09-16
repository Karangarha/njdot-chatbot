# Check Instructions v3 — Architecture-Aware (Spec)

Source document supplied by the user on 2026-09-14. This file is the verbatim
reference the implementation plan draws from. Do not paraphrase these
instruction blocks when applying them: copy them.

---

## What report 23 tells us

`working_drawing_review_time` is **fixed** — the WBS-override line landed and it
correctly fails PS340 (60 days) and PS350 (80 days) against the SP's 45-day
Approved requirement.

But three checks now contradict their own evidence:

| check | evidence says | verdict |
|---|---|---|
| `environmental_permit` | "…all scheduled 2025-12-15 through 2026-07-15, i.e. **outside the cited in-water blackout windows**" | **Fail** |
| `utility_alignment` | "The schedule contains **matching activity IDs for each** SP-scoped utility work item" | **Warning** |
| `no_concrete_winter` | lists "D1120 … (2026-01-06 …)" among placements | **Pass** |

And `no_concrete_winter` has now flipped Fail → Pass → Fail → Pass across four
runs on identical input.

**The remaining failure mode is not knowledge. It's that the decision rule isn't
binding.** Longer instructions won't help — the model already has the facts and
writes them down correctly. It needs a mechanical test it cannot reason past.

---

## The template

Four architecture facts drive the shape of every instruction below. All four
were verified against the code on 2026-09-14; see the plan's "Verified
architecture facts" section for file and line references.

**1. The instruction is the literal last line of the prompt.**

```python
user_msg = f"{evidence}\n\nCHECK: {check.name}\n{check.instruction}"
```

Whatever ends the instruction is the most recent thing the model reads before
answering. **Put the decision rule last.** Every instruction below ends with a
three-line verdict table.

**2. For `sp` checks, the instruction *is* the vector query.**

```python
sp_text, sp_candidates = sp_search_fn(check.instruction, top_k=check.sp_top_k)
```

Section numbers and distinctive quoted phrases must appear **in the first
sentence**, where they carry the most weight in a 600-token-chunk cosine match.
A 200-word rule with anchors in a trailing "Look in:" line embeds as a rule, not
as a query. Every `sp` instruction below opens with its anchors.

**3. Evidence blocks have literal names.** Reference them exactly:
`ALL SCHEDULE ACTIVITIES`, `MILESTONES`, `PRECOMPUTED COMPLIANCE FACTS`. The
model wastes reasoning locating data when the block isn't named.

**4. There is no calendar data in the evidence.** The activity roster is
`id | name | phase | start | finish | duration_days | float | critical`. Never
ask for calendar assignment, working days, or holiday exceptions.

### Shared addition to `_STATIC_SYSTEM_PROMPT`

The spec's original wording, which names a three-state Pass/Fail/Warning
verdict:

```
DECISION DISCIPLINE

Populate `breaching_items` before you write `evidence`. Then:
  breaching_items empty        -> Pass
  breaching_items non-empty    -> Fail
  required evidence absent, or you cannot resolve a contradiction -> Warning

Your evidence text may not contradict your verdict. If your evidence states
that items fall outside a restricted window, the verdict is Pass. If it names
an item inside the window, that item belongs in breaching_items and the verdict
is Fail. Do not write a sentence beginning "however" that reverses a conclusion
you have already supported.

Quote only text present in the evidence blocks. Attribute every quotation to the
block it came from: Special Provisions, narrative, schedule, or specification.
```

**Adaptation required.** This codebase has no `Warning` status and the model
authors no status field at all. The verdict is derived mechanically in
`_derive_status`, and the third state is expressed by the `insufficient_evidence`
boolean on `EvaluationSchema`, which renders as the amber MISSING pill. Task 4
of the plan adapts the block above accordingly. Everywhere an instruction below
says "-> Warning", it means "set `insufficient_evidence` true".

---

## Instructions

Deterministic checks (`geo`, `cost_gap`, `edq_coverage`, `date_rule`,
`schedule_logic`) never read `instruction` at eval time — their text below is UI
copy only.

### `utility_alignment` — `sp`, `schedule`, `keymap`

```
Special Provisions 105.07.01 Working in the Vicinity of Utilities and 105.07.02
Work Performed by Utilities: "Advance Notice Requirements", "Work to be Performed
by Utility", "Work to be Performed by the Contractor".

Utilities on the Key Sheet sit within project limits but many have NO WORK here.
A key-map utility absent from the Special Provisions is normal and is not a
breach.

From the SP, list every utility the Department assigns work or notice to. For
each, find the matching activity in ALL SCHEDULE ACTIVITIES. Advance-notice,
relocation, installation, support and tie-in activities all count as coverage.
Allow name variants (SJG / South Jersey Gas, ACE / Atlantic City Electric).

breaching_items = SP-scoped utilities with NO matching schedule activity.
Empty -> Pass.  Non-empty -> Fail.  SP not retrieved -> Warning.
```

### Administrative dates — all `date_rule`, UI copy only

Applies to `schedule_duration`, `ad_date_day`, `bid_date_day`, `ad_to_bid_gap`,
`bid_to_award_gap`, `award_to_construction`.

```
Deterministic: computed in Python from the schedule milestones and the project's
business-day calendar. No LLM judgement involved.
```

### `row_availability` — `sp`, `narrative`, `schedule`

```
Special Provisions 108.12 RIGHT-OF-WAY RESTRICTIONS: "The Department has not
obtained the following ROW parcels", "anticipated availability dates", parcel
designations and baseline stations.

For each parcel with an availability date, identify which work needs it (the
narrative's ROW section maps parcels to scope), then find that work in ALL
SCHEDULE ACTIVITIES. Mobilization, submittals, notices and MPT do not occupy a
parcel - exclude them.

Report separately, without changing the verdict: whether the narrative's ROW
acquisition claim agrees with the 108.12 dates and the Construction Start
milestone.

breaching_items = activities occupying a parcel before that parcel's date.
Empty -> Pass.  Non-empty -> Fail.  108.12 not retrieved -> Warning.
```

### `environmental_permit` — `narrative`, `schedule`, `sp`, `csm`

```
The narrative's Permit Requirements section states the restricted windows.
Typical: in-water work barred Mar 1 - Jun 30 and Oct 1 - Nov 30; tree clearing
barred Apr 1 - Aug 30.

WORK INSIDE AN INSTALLED COFFERDAM IS NOT IN-WATER WORK. The narrative says so:
"Cofferdams will be installed outside the restricted period, such that work can
be conducted within these enclosures during the restricted period." Only
cofferdam installation and removal, and work below the waterline outside an
enclosure, are governed by an in-water restriction. Deck, abutment, pier,
backwall and superstructure activities performed within an enclosure are not.

Tree clearing means activities named clearing, grubbing or site clearing.

An activity intersects a window if start <= window_end AND finish >=
window_start, in ANY project year. Test intersection, not start date alone.

breaching_items = governed activities intersecting their window.
Empty -> Pass.  Non-empty -> Fail.  No restriction stated -> Warning.
```

### `landscape_season` — `schedule`, `spec`

```
Two windows apply. Evaluate each activity against the one for its operation.
  PLANTING (plant, planting, tree, shrub, bulb, transplant):
    Mar 1 - May 15 and Aug 15 - Dec 1   (Std Spec Table 811.03.01-1)
  SEEDING / TURF (seed, seeding, topsoil, fertiliz, sod, turf):
    Mar 1 - May 15 and Aug 15 - Oct 15  (Std Spec 810)

An activity complies only if its ENTIRE duration falls inside a window.

breaching_items = activities starting or finishing outside their window.
Empty -> Pass.  Non-empty -> Fail.
```

### `gas_interruption` — `schedule`, `sp`

```
Special Provisions 105.07.02, South Jersey Gas section: "Work to be Performed by
Utility", "advance notice prior to the start of gas work", "facilities will
remain in service at all times except as allowed".

Restricted window: Oct 1 - Apr 1, recurring every project year.

Search ALL SCHEDULE ACTIVITIES for: gas, gas main, gas line, gas valve, gas
service, and named gas utilities. List every match, including notice activities.
Interruption-capable: installation, tie-in, connection, cut-over, abandonment,
removal, valve work. Not interruption-capable: notice, submittal, inspection.

If the SP states the utility's facilities remain in service, quote it - that is
mitigation and the verdict is Pass even where in-window work exists.

breaching_items = interruption-capable activities inside the window with no
service-continuity provision.
Empty -> Pass.  Non-empty -> Fail.  Terms unsearched or SP absent -> Warning.
```

### `water_interruption` — `schedule`, `sp`, `spec`

```
Special Provisions 105.07.02 water/sewer section and Standard Specification
651.03.01.A "Scheduling of Work and Interruption to Water Service": "tie-in to
existing main", "valve turning".

Restricted window: Apr 1 - Sep 30, recurring every project year.

Search for: water, water main, water service, water valve, hydrant, named water
owners. A LIVE TIE-IN OR CONNECTION TO AN EXISTING MAIN IS AN INTERRUPTION -
also cut-over, shutdown, valve turning, and abandonment of a main still in
service. Installing new pipe not yet in service is not.

breaching_items = interruption activities inside the window with no provision
permitting them.
Empty -> Pass.  Non-empty -> Fail.  Terms unsearched or SP absent -> Warning.
```

### `electric_interruption` — `schedule`, `sp`

```
Special Provisions 105.07.02, Atlantic City Electric section: "electric service
inquiry", "meter cabinet", "construction permit".

Restricted window: Jun 1 - Sep 30, recurring every project year.

Search for: electric, power, service, meter, lighting, signal, ITS, conduit,
junction box, named electric owners. Interruption-capable: service cut-over,
meter changeover, de-energizing, resetting or relocating a live signal or
circuit. Not: installing unenergized conduit, boxes or standards.

Read compound names carefully - a live operation can sit inside an installation
activity, e.g. "Install Electrical Features ... and Reset Traffic Light".

Note if the SP section for this owner contains NO service-continuity language;
unlike gas and water, there may be no mitigation to quote.

breaching_items = interruption activities inside the window.
Empty -> Pass.  Non-empty -> Fail.  SP absent -> Warning.
```

### `utility_work_hours` — `schedule`, `sp`

```
Special Provisions 105.07.02: "day shift, night shift, or on weekends",
"safe-time", "advance notice for nighttime work".

Utility owners work daytime weekday hours. Check that utility activity durations
in ALL SCHEDULE ACTIVITIES are consistent with single daytime shifts. Calendar
assignment is NOT in your evidence - do not claim to have checked it.

Then check whether the SP REQUIRES off-hours utility work the schedule does not
show. Telecom fiber splicing in an overnight window is the common case.

breaching_items = SP-mandated off-hours utility work with no corresponding
schedule activity, plus any utility duration only achievable with night or
weekend shifts.
Empty -> Pass.  Non-empty -> Fail.  SP absent -> Warning.
```

### `railroad_restrictions` — `schedule`, `sp`, `keymap`

```
Special Provisions 105.07 and the Key Sheet utility list. Search for: railroad,
rail, track, flagging, fouling, right-of-entry.

Most projects have none. If the Key Sheet utility list names no railroad, the SP
utility sections name none, and no activity mentions track or flagging, that is
a determination: Pass, naming what you searched.

If a railroad is involved, verify flagging lead time, track windows and railroad
review durations appear in both the schedule and the SP.

breaching_items = railroad requirements with no schedule representation.
Empty or no railroad on project -> Pass.  Non-empty -> Fail.
```

### `temp_50_window` — `schedule`, `spec`

```
Work requiring ambient temperature above 50 F must fall between May 1 and Sep 30.

You must classify which work carries the threshold; the schedule does not label
it. Typical: pavement markings and thermoplastic, epoxy and latex-modified
materials, waterproofing membranes, joint and crack sealers, coatings, landscape
establishment. Name the specification you relied on for each.

EXCLUDE HMA paving - governed separately by base temperature.

breaching_items = >50 F activities outside May 1 - Sep 30.
Empty -> Pass, listing what you evaluated.  Non-empty -> Fail.
```

### `temp_60_window` — `schedule`, `spec`

```
Work requiring ambient temperature above 60 F must fall between Jun 1 and Sep 15.
A narrower set than the 50 F rule: certain epoxy and polymer overlays, thin
bonded overlays, and materials whose specification states a 60 F minimum.

This is an AMBIENT AIR temperature rule. Do not use concrete internal-temperature
clauses (e.g. "maintain the concrete between 60 and 160 F") as the basis - those
govern curing, not scheduling.

EXCLUDE HMA paving.

breaching_items = >60 F activities outside Jun 1 - Sep 15.
Empty -> Pass, naming the specifications consulted.  Non-empty -> Fail.
```

### `no_concrete_winter` — `schedule`

```
Restricted window: Dec 15 - Mar 15, recurring every project year.

Step 1. From ALL SCHEDULE ACTIVITIES, select concrete placements. INCLUDE names
containing "Place and cure", "Place & cure", "Pour", "Cast". EXCLUDE: "Remove
concrete", "Demolish", "Cut", "Saw" (demolition); "Form", "Install
reinforcement", "Install deck pans" (precede placement); "Install new bearings",
"Erect superstructure" (steel); "Strip forms", "Backfill" (follow placement).

Step 2. For EACH selected activity apply this test exactly:
  intersects = (start <= Mar 15 of year Y) AND (finish >= Dec 15 of year Y-1)
  for every year the project spans.
An activity beginning Jan 6 and ending Feb 25 intersects. Test the whole
duration; a start date outside the window does not exempt an activity, and a
start date inside it is sufficient on its own.

Step 3. breaching_items = every activity passing the Step 2 test.
Empty -> Pass.  Non-empty -> Fail.

Do not exempt an activity because it is "winter-adjacent", because it is the
only one, or because you cannot see its calendar. Calendars are not in your
evidence.
```

### `cold_weather_concreting` — `schedule`, `spec`, `narrative`

```
Applies only if a genuine concrete PLACEMENT falls in Dec 15 - Mar 15. Use the
same include/exclude list and intersection test as the winter concrete check.
No winter placement -> Pass, stating none was found.

Where winter placement exists, verify three things:
  1. A cold-weather concreting plan submittal (Std Spec 504.03.02.C requires it
     at least 30 days before placing) finishing 30+ days before the first winter
     placement.
  2. Placement durations allow protection: 7 days for decks and approaches,
     5 days for other placements.
  3. The narrative identifies the winter work.

breaching_items = each of the three that is absent.
Empty -> Pass.  Non-empty -> Fail.
```

### `concrete_cure_time` — `schedule`, `spec`

```
Std Spec 507.03.02.J: wet burlap and polyethylene maintained at least 14 days on
a bridge deck. 507.03.02.L: no sawcutting earlier than 15 days after placement.

For each deck and slab placement, check the cure time sits either inside the
activity's own duration or in the gap before its dependent successors.
Dependent: sawcutting, grooving, membrane, overlay, loading the deck.

Work overlapping a cure AT A DIFFERENT LOCATION or in another stage is not a
breach. Check location before treating an overlap as one.

breaching_items = placements whose dependent work starts inside the cure period
at the same location.
Empty -> Pass.  Non-empty -> Fail.
```

### `no_paving_winter` — `schedule`

```
Restricted window: Dec 15 - Mar 15, recurring every project year.

From ALL SCHEDULE ACTIVITIES select paving: pave, paving, mill, overlay, HMA,
asphalt, surface course, base course, intermediate course, pavement box.
EXCLUDE sidewalk, curb and ADA ramp activities - those are concrete flatwork.

Apply the same intersection test as the winter concrete check.

breaching_items = paving activities intersecting the window.
Empty -> Pass, however many paving activities exist elsewhere.  Non-empty -> Fail.
An activity outside the window is never evidence of a breach inside it.
```

### Completion milestones — `date_rule`, UI copy only

Applies to `no_completion_in_winter`, `project_region_i195`,
`substantial_regional_deadlines`, `substantial_to_final`.

```
Deterministic: computed in Python from the schedule milestones (and, where
applicable, the key map geography and the Engineer's Estimate). No LLM judgement.
```

### `working_drawing_review_time` — `schedule`, `sp`, `spec`, `narrative`

```
Special Provisions 105.05 WORKING DRAWINGS: "TABLE 105.05-1 IS CHANGED TO",
"Working Drawing Submission Category", Certified and Approved columns.

Certified = 30 days review. Approved = 45 days. Where the SP replaces Table
105.05-1, the replacement governs over the base specification.

Classify each submittal by the GOVERNING TABLE, not by the WBS folder it sits
in. A schedule grouping such as "Material Submittals" or "Long Lead Items" is
the contractor's organization, not a contractual category, and does not exempt
an item from its review duration. Do not explain a long duration away as
procurement or material lead time - the narrative's special-materials section
usually states that no special materials are used.

Read the table's two columns carefully; they are of unequal length and the tail
rows of the longer one are easily misattributed.

For every PS-series activity produce: id, name, category the schedule labels it,
column in the governing table, scheduled duration, required duration.

breaching_items = activities whose category or duration disagrees with the table.
Empty -> Pass.  Non-empty -> Fail.  Replacement table not retrieved -> Warning,
saying so rather than falling back silently to the base spec.
```

### `steel_pole_lead_time` — `schedule`, `sp`, `csm`

```
Special Provisions SECTION 702 - TRAFFIC SIGNALS and SECTION 703 - HIGHWAY
LIGHTING. Construction Scheduling Manual Table A: steel traffic signal poles
require 4 months manufacturing and delivery.

Decide applicability first. Does this project install NEW signal poles, mast arms
or pole foundations? A project that resets, relocates or rewires an existing
signal procures none. If none, Pass, naming the activities and documents checked.

Where new poles exist, verify at least 4 months between the fabrication or
procurement activity and installation.

breaching_items = pole installations with under 4 months of lead time.
Empty or not applicable -> Pass.  Non-empty -> Fail.
```

### `aluminum_pole_lead_time` — `schedule`, `sp`, `csm`

```
Special Provisions SECTION 703 - HIGHWAY LIGHTING, 703.03.07 Temporary Highway
Lighting System, "Lighting mast arms and standards". CSM Table A: aluminum
lighting and signal poles require 2 months.

Decide applicability first. Look for lighting standards, luminaire poles or
aluminum signal poles among installation activities and submittals. A certified
submittal alone (e.g. "Lighting Standards (Certified)") does not establish new
pole procurement. If no new aluminum pole work exists, Pass, naming what you
checked - do not Warning because applicability was merely hard to confirm.

breaching_items = aluminum pole installations with under 2 months of lead time.
Empty or not applicable -> Pass.  Non-empty -> Fail.
```

### `controller_lead_time` — `schedule`, `sp`, `csm`

```
Special Provisions 702.03.01 Controller: "Submit catalog cuts for the time
synchronized GPS unit to the RE for approval before installation". CSM Table A:
traffic signal controllers require 4 months.

Decide applicability first. Is a NEW controller, controller cabinet or CTSS
equipment procured? A reset signal or a temporary signal system may need none.
If none, Pass, naming what you checked.

Where a new controller exists, verify at least 4 months before installation and
that installation leaves time for testing before Substantial Completion.

breaching_items = controller installations with under 4 months of lead time.
Empty or not applicable -> Pass.  Non-empty -> Fail.
```

### `its_burn_in` — `schedule`, `sp`, `spec`

```
Special Provisions 701.03.15 and Standard Specification Section 704: "interim
acceptance", "verification testing", "burn-in", "observational and functional
test".

Decide scope first:
  reset or relocation of existing equipment only -> Pass, "no new ITS
    installation", naming the activities inspected
  new non-adaptive ITS devices -> Section 704 testing applies, including the
    Department's 14-day observational and functional test
  adaptive CTSS -> up-to-6-month burn-in, typically waived to run concurrent
    with construction; confirm it is accommodated

Separately and always: if any electrical or ITS installation activity exists,
check that a testing or acceptance activity sits between the last installation
and the Substantial Completion milestone.

breaching_items = a required observation period with no representation, plus a
missing testing activity before Substantial Completion.
Empty -> Pass.  Non-empty -> Fail.
```

### Schedule logic — `schedule_logic`, UI copy only

Applies to `no_negative_float`, `no_lag`, `no_open_ends`,
`no_mandatory_constraints`.

```
Deterministic: computed in Python from the schedule graph against Construction
Scheduling Manual Section 3.0, including the project start/finish milestone
exemption and relationship-type handling. No LLM judgement.
```

### `cpm_consistency` — `schedule`

```
Read the PRECOMPUTED COMPLIANCE FACTS "CPM Validation" section. It answers one
question: was the schedule recalculated before submission?

Total float and early/late date agreement establish that. Free-float deltas are
reported for context and are NOT a breach - P6's free-float semantics on a
multi-calendar project are implementation-specific.

breaching_items = activities with total-float or date mismatches beyond tolerance.
Empty -> Pass.  Non-empty -> Fail.  CPM Validation section absent -> Warning,
stating that stored values could not be verified.
```

### `edq_items` — `edq_coverage`, UI copy only

```
Deterministic: EDQ pay items are matched against schedule activities in Python,
with non-physical items excluded. No LLM judgement.
```

### Designer's narrative — general rule

These are section-presence checks. The requirement is that the narrative
**addresses** the topic. Do not fail or warn because a sub-element is absent from
a project that has none, or because the narrative contains an internal
inconsistency — report that as a note on a passing check.

### `narrative_production_rates` — `narrative`

```
Confirm the narrative states anticipated production rates, names their source,
and gives the working assumptions (hours per day, crews per activity).
breaching_items = "no rate source named" if rates are asserted with no basis.
Empty -> Pass.  Non-empty -> Fail.
```

### `narrative_workforce` — `narrative`

```
Confirm the narrative lists crew types with trade makeup and size.
If the stated crew count disagrees with the enumerated list, report it as a note
- it does not breach this check, which asks whether the workforce is described.
breaching_items = "workforce not described" only if crews are not enumerated.
Empty -> Pass.  Non-empty -> Fail.
```

### `narrative_winter_work` — `narrative`, `schedule`, `csm`, `spec`

```
Confirm the narrative addresses December through March AND gives the NUMBER OF
WORKDAYS for Bridge and Roadwork.

A citation to CSM Appendix B or Std Spec Table 108.11.01-1 in place of actual
counts does NOT satisfy this - the requirement is the numbers.

Also compare the narrative's winter-work claims against ALL SCHEDULE ACTIVITIES.
A narrative stating no winter roadway work is anticipated, while roadway or site
activities are scheduled December through March, is a contradiction and belongs
in breaching_items.

breaching_items = "workday counts not provided" and/or each contradicted claim.
Empty -> Pass.  Non-empty -> Fail.
```

### `narrative_permit_requirements` — `narrative`

```
Confirm the narrative lists the anticipated permits by issuing agency AND states
the restrictions they impose, with dates.

Restrictions stated for the project as a whole satisfy this. Do NOT require each
restriction to be mapped to an individual permit - that is not part of the rule.

breaching_items = "permits listed with no restrictions" or "permits not
addressed".
Empty -> Pass.  Non-empty -> Fail.
```

### `narrative_utility_requirements` — `narrative`

```
Confirm the narrative names the utility owners, describes each one's scope, and
states timing dependencies between utility work and construction stages. The
timing statements are what matter - they drive schedule logic.

Naming the owners collectively ("four utility companies") and then describing
each one's work satisfies this.

breaching_items = "owners listed with no scope or sequencing".
Empty -> Pass.  Non-empty -> Fail.
```

### `narrative_row_requirements` — `narrative`, `sp`

```
Special Provisions 108.12 for cross-reference.
Confirm the narrative identifies the ROW acquisitions by parcel designation, what
each is needed for, and when they are expected.

Where the narrative's acquisition timing disagrees with 108.12 or the
Construction Start milestone, that IS a breach of this check - it is the
narrative's accuracy under test here, not the schedule's sequencing.

breaching_items = "parcels or purpose not identified", plus each timing conflict.
Empty -> Pass.  Non-empty -> Fail.
```

### `narrative_community_commitments` — `narrative`

```
Confirm the narrative addresses community commitments. An explicit statement that
no specific commitments were made IS a valid answer.
breaching_items = "topic not addressed".
Empty -> Pass.  Non-empty -> Fail.
```

### `narrative_material_lead_time` — `narrative`, `schedule`

```
Confirm the narrative identifies special materials with lead times, or states
affirmatively that none are required. Both satisfy the rule.
breaching_items = "topic not addressed".
Empty -> Pass.  Non-empty -> Fail.
```

### `narrative_detours` — `narrative`, `schedule`

```
Confirm detours and their timeframes appear in the narrative, with the roadways
and stages affected. "No long-term detours" satisfies this provided short-term
closures are described.
breaching_items = "detours not addressed".
Empty -> Pass.  Non-empty -> Fail.
```

### `narrative_critical_milestones` — `narrative`, `schedule`

```
Confirm the narrative lists critical milestones with dates and that they match
MILESTONES in the schedule.

Categories the project does not contain (road openings, ramp openings) are not
required. Their absence is not a gap and must not produce a Warning.

breaching_items = each milestone whose narrative date differs from the schedule,
or "milestones not listed".
Empty -> Pass.  Non-empty -> Fail.
```

### `narrative_schedule_problems` — `narrative`

```
Confirm the narrative names project-specific risks - ROW, utility coordination,
permits, sequencing - rather than generic construction risk.
breaching_items = "no project-specific risk identified".
Empty -> Pass.  Non-empty -> Fail.
```

### `narrative_acceleration` — `narrative`

```
Confirm the narrative states whether acceleration was applied and by what means.
A narrative saying none was applied but explaining how it could be achieved
satisfies this.
breaching_items = "topic not addressed".
Empty -> Pass.  Non-empty -> Fail.
```

### `narrative_winter_extension_reason` — `schedule`, `narrative`

```
Applies only if the Substantial-to-Final period runs through December to March.
Check both milestone dates first; if it does not, Pass, stating the dates.
Where it does, the narrative must explain why and show that multiple crews,
extended hours or resequencing were considered.
breaching_items = "extension unexplained".
Empty or not applicable -> Pass.  Non-empty -> Fail.
```

### `narrative_work_hour_restrictions` — `narrative`, `sp`

```
Special Provisions 108.08 OCCUPANCY CHARGES: "The closure schedule shown in the
plans indicates the time periods for allowable closures", "Alternating Traffic
Pattern/Traffic Shift" time limits, "$10/minute". Also 108.06 night operations
and SECTION 159 - TRAFFIC CONTROL.

Work-hour restrictions are limits on HOURS OF WORK: closure time windows, night
work limits, movable bridge openings, railroad windows, special events,
municipal ordinances. A seasonal environmental window (in-water work, tree
clearing) is NOT a work-hour restriction - do not cite one as satisfying this.

Where the SP establishes a closure schedule with enforceable time limits, the
narrative must address it.

breaching_items = each SP work-hour restriction the narrative does not address.
Empty -> Pass.  Non-empty -> Fail.  SP not retrieved -> Warning.
```

### `narrative_emergency_routes` — `narrative`, `schedule`

```
Emergency access matters when the project closes or narrows a route serving
emergency response, detours traffic, or shuts a crossing overnight with no
parallel route.
If the narrative addresses emergency access -> Pass. If project conditions imply
it matters and the narrative is silent -> Fail, naming the condition. If no
condition implies a requirement -> Pass, saying why.
breaching_items = "emergency access unaddressed despite [condition]".
```

### `narrative_night_work` — `narrative`, `schedule`, `sp`

```
Special Provisions 105.07.02: "safe-time", "fiber optic cable splicing", "night
work", "12:00am to 6:00am", "advance notice for nighttime work".

Night work can be REQUIRED by contract even when the schedule shows none. Check
the SP first, then the schedule.

Apply this table exactly:
  SP mandates night work AND the narrative explains it AND the schedule shows a
    night activity                                              -> Pass
  SP mandates night work AND (the narrative does not explain it
    OR no night activity exists in the schedule)                -> Fail
  Night work is scheduled but the narrative does not explain it -> Fail
  No SP mandate and no night work scheduled                     -> Pass

A "Night Lighting Plan" submittal is not a night work activity. Finding an SP
mandate and finding no night activity is the Fail branch, not the Pass branch.

breaching_items = the SP mandate with no schedule or narrative coverage.
SP not retrieved -> Warning.
```

### `traffic_control_staging` — `sp`, `narrative`, `schedule`

```
Special Provisions SECTION 703 - HIGHWAY LIGHTING and Traffic Control Stage
notes: "TEMPORARY HIGHWAY LIGHTING SYSTEM (STAGE", "Pre-Stage 1B", "Stage 1",
"Stage 2", "Post-Stage 2", "during Traffic Control Stage", "to be reset during".

Three comparisons, all required:

1. Stage sequence and numbering across the Traffic Control Plans, the narrative
   and the schedule WBS.
2. Stage completion milestones against the narrative's dates.
3. STAGE-ASSIGNED WORK. The SP assigns specific work to specific stages -
   temporary lighting systems per stage, utility valve and manhole resets per
   stage, utility work items per stage. For EACH such assignment, find a matching
   activity in that stage in ALL SCHEDULE ACTIVITIES. A required item with no
   activity in its assigned stage is a breach, cited separately from any
   sequence finding.

Matching milestone dates alone does not satisfy this check. If your evidence
contains no SP text, say so - do not conclude from the narrative alone.

breaching_items = each stage-assigned SP requirement with no matching activity,
plus each date or sequence disagreement.
Empty -> Pass.  Non-empty -> Fail.  SP not retrieved -> Warning.
```

### `summer_shutdown` — `sp`, `keymap`

```
Special Provisions search terms: "summer", "seasonal restriction", "shutdown",
"moratorium", "shore route", "Memorial Day", "Labor Day".

If the SP was retrieved and contains no such provision, and the key map places
the project off a shore route, that is a determination: Pass, naming the terms
searched and the project location.

breaching_items = work scheduled inside a stated summer restriction.
Empty or no restriction applies -> Pass.  Non-empty -> Fail.
SP not retrieved at all -> Warning.
```

### `required_activities_present` — `schedule`, `sp`, `estimate`

```
Special Provisions "MEASUREMENT AND PAYMENT", "Item Pay Unit" - the pay-item
lists establish scope.

Check for: guide rail, rumble strips, concrete curb, sidewalk and ADA ramps,
islands, driveways, topsoil and seeding, signing, striping, SESC measures and
their removal, demolition.

For each, either find the activity or establish the item is not in scope.
Absence is a breach only where the scope includes the item.

breaching_items = in-scope items with no scheduled activity.
Empty -> Pass.  Non-empty -> Fail.
```

### `multi_year_funding` — `sp`, `schedule`

```
Special Provisions 108.10 CONTRACT TIME: "Substantial Completion on or before",
"Achieve Completion on or before", fiscal year, funding.

Multi-year funding appears as language splitting work across State fiscal years,
capping value in a period, or setting an interim completion tied to a funding
year. Most 108.10 sections contain only completion dates.

If the retrieved 108.10 holds only completion dates, that is a determination:
Pass, quoting it.

breaching_items = funding constraints with no schedule representation.
Empty or no funding language -> Pass.  Non-empty -> Fail.
108.10 not retrieved -> Warning.
```

### `nearby_projects` — `sp`, `spec`, `schedule`

```
Special Provisions 105.06 "Cooperation with Others", "work performed by Others",
adjacent project names or contract numbers.

The base specification language is a generic standing duty. What matters is
whether the SP AMENDS it to name specific adjacent projects or coordination
requirements. Generic base form with no project-specific additions is a
determination: Pass, stating you read 105.06 and it names no adjacent work.

Distinguish "105.06 was not retrieved" from "105.06 exists and adds nothing" -
different findings.

breaching_items = named adjacent work the schedule does not account for.
Empty or none named -> Pass.  Non-empty -> Fail.
105.06 not retrieved -> Warning.
```

---

## Two things to verify before rolling these out

**1. Do `traffic_control_staging` and `narrative_work_hour_restrictions` actually
receive `sp` evidence?** In report 23 the first shows
`Source: narrative and schedule milestones/WBS` — no SP at all — and in report 22
the second showed `Source: narrative` only. Both have `sp` in `source_files` and
both have had anchors added twice with no effect. Log the evidence blocks these
two checks receive before changing their instructions a third time.

**2. Is the retrieval stable run to run?** `narrative_night_work` quoted the
safe-time clause correctly in report 22 and did not retrieve it in report 23, on
identical input. If `sp_top_k` or the search function differs between code paths,
or temperature is non-zero on the embedding call, the same check will keep
flipping regardless of instruction quality.

The `no_concrete_winter` oscillation across four runs is the same symptom. The
Step 2 intersection test above is written as an arithmetic predicate specifically
so the model has nothing left to weigh — if it still flips, the cause is upstream
of the prompt.
