"""Debug script: deterministic CPM engine vs P6-stored values.

Runs the full scheduling pipeline on an XER file — extraction, calendar
build, network build, forward/backward pass, cross-check — and prints the
computed results next to P6's stored values so engine fidelity can be judged
before any LLM ever sees the numbers.

    python -m debug.cpm [path/to/Schedule.xer] [--all] [--activity ID]

Defaults to debug/Schedule.xer.  --all prints every activity row (default:
first 25 + all mismatching); --activity prints one activity in detail.
"""

from __future__ import annotations

import argparse
from pathlib import Path

# ── Bootstrap sys.path and env ────────────────────────────────────────────────
import debug  # noqa: F401 — side-effect: adds backend/ to sys.path

from debug import bold, cyan, dim, green, hr, red, section, subsection, yellow

from app.scheduling import (
    build_calendars, build_network, cross_check, parse_xer_all, run_cpm,
)

_DEFAULT_XER = Path(__file__).parent / "Schedule.xer"


def _fmt(v) -> str:
    return "—" if v is None else str(v)


def _tf_color(delta) -> str:
    if delta is None:
        return dim("—")
    if abs(delta) <= 1:
        return green(f"{delta:+d}")
    return red(f"{delta:+d}")


def main() -> None:
    ap = argparse.ArgumentParser(description="CPM engine debug run")
    ap.add_argument("xer", nargs="?", default=str(_DEFAULT_XER))
    ap.add_argument("--all", action="store_true", help="print every activity row")
    ap.add_argument("--activity", help="print one activity in detail")
    args = ap.parse_args()

    xer_text = Path(args.xer).read_text(encoding="utf-8", errors="ignore")

    # ── Extraction ────────────────────────────────────────────────────────────
    section("XER EXTRACTION")
    parsed = parse_xer_all(xer_text)
    activities, calendars, project = (
        parsed["activities"], parsed["calendars"], parsed["project"])

    print(f"Activities: {bold(str(len(activities)))}")
    rel_count = sum(len(a.get('pred_links', [])) for a in activities)
    print(f"Relationships: {bold(str(rel_count))}")
    typed = {}
    for a in activities:
        for l in a.get("pred_links", []):
            typed[l["type"]] = typed.get(l["type"], 0) + 1
    print(f"Relationship types: {typed}")
    lagged = sum(1 for a in activities for l in a.get("pred_links", []) if l.get("lag_days"))
    print(f"Relationships with lag: {lagged}")
    print(f"Calendars: {[(c['id'], c['name'], c['day_hr_cnt']) for c in calendars]}")

    if project:
        subsection("PROJECT")
        for k, v in project.items():
            print(f"  {k:20s} {v}")

    # ── CPM run ───────────────────────────────────────────────────────────────
    section("CPM FORWARD/BACKWARD PASS")
    cals = build_calendars(calendars)
    net = build_network(activities)
    cpm = run_cpm(net, cals, project)

    print(f"Data date:        {bold(_fmt(cpm.data_date))}")
    print(f"Computed finish:  {bold(_fmt(cpm.project_finish))}")
    if project and project.get("scd_finish_date"):
        stored = project["scd_finish_date"]
        match = str(cpm.project_finish) == stored
        badge = green("MATCH") if match else yellow(f"stored {stored}")
        print(f"P6 stored finish: {stored}  [{badge}]")
    print(f"Computed critical activities: "
          f"{bold(str(sum(1 for r in cpm.activities.values() if r.is_critical)))}")
    print(f"P6 longest-path activities:   "
          f"{bold(str(sum(1 for a in activities if a.get('is_longest_path'))))}")

    if cpm.cycles:
        subsection("CYCLES (broken to proceed)")
        for c in cpm.cycles:
            print(red("  " + " → ".join(c)))
    if cpm.open_starts or cpm.open_ends:
        subsection("OPEN ENDS")
        print(f"  Open starts ({len(cpm.open_starts)}): {', '.join(cpm.open_starts[:10])}")
        print(f"  Open ends   ({len(cpm.open_ends)}): {', '.join(cpm.open_ends[:10])}")
    if cpm.warnings:
        subsection(f"WARNINGS ({len(cpm.warnings)})")
        for w in cpm.warnings[:15]:
            print(yellow(f"  ⚠ {w}"))
        if len(cpm.warnings) > 15:
            print(dim(f"  … +{len(cpm.warnings) - 15} more"))

    subsection("CRITICAL PATH CHAINS")
    if not cpm.critical_paths:
        print(dim("  none found"))
    for i, chain in enumerate(cpm.critical_paths, 1):
        print(f"\n  {bold(f'Chain {i}')} ({len(chain)} activities):")
        by_id = {a["activity_id"]: a for a in activities}
        for aid in chain:
            r = cpm.activities.get(aid)
            name = by_id.get(aid, {}).get("activity_name", "")[:45]
            print(f"    {cyan(aid):24s} {name:47s} "
                  f"ES {_fmt(r.es)}  EF {_fmt(r.ef)}  TF {_fmt(r.total_float)}")

    # ── Cross-check ───────────────────────────────────────────────────────────
    section("CROSS-CHECK: COMPUTED vs P6-STORED")
    xc = cross_check(activities, cpm)
    s = xc["summary"]
    print(f"Checked: {s['checked']}   Clean: {green(str(s['clean']))}   "
          f"Float mismatches: {red(str(s['float_mismatches'])) if s['float_mismatches'] else green('0')}   "
          f"Date mismatches: {red(str(s['date_mismatches'])) if s['date_mismatches'] else green('0')}   "
          f"Critical-flag diffs: {s['critical_flag_diffs']}")
    if s["systematic_offset_days"] is not None:
        print(yellow(f"Systematic offset: {s['systematic_offset_days']:+d} day(s) "
                     f"(convention difference, not per-activity noise)"))

    subsection("ASSESSMENT")
    for line in xc["assessment"]:
        print(f"  • {line}")

    if xc["mismatches"]:
        subsection(f"MISMATCH ROWS (first {len(xc['mismatches'])})")
        print(f"  {'Activity':22s} {'Field':14s} {'Computed':>12s} {'Stored':>12s} {'Δ':>6s}")
        hr()
        for m in xc["mismatches"]:
            print(f"  {m['activity_id']:22s} {m['field']:14s} "
                  f"{_fmt(m['computed']):>12s} {_fmt(m['stored']):>12s} "
                  f"{_tf_color(m['delta_days']):>6s}")

    # ── Per-activity table ────────────────────────────────────────────────────
    if args.activity:
        by_id = {a["activity_id"]: a for a in activities}
        act = by_id.get(args.activity)
        r = cpm.activities.get(args.activity)
        section(f"ACTIVITY {args.activity}")
        if not act or not r:
            print(red("  not found"))
        else:
            for k in ("activity_name", "task_type", "status", "wbs_path",
                      "duration_days", "remaining_duration_days", "calendar_id",
                      "constraints", "stored_total_float_days", "is_longest_path",
                      "early_start", "early_finish", "late_start", "late_finish",
                      "actual_start", "actual_finish"):
                print(f"  {k:26s} {act.get(k)}")
            hr()
            for k, v in r.to_dict().items():
                print(f"  computed.{k:17s} {v}")
            print(f"  pred_links: {act.get('pred_links')}")
            print(f"  succ_links: {act.get('succ_links')}")
    else:
        subsection("ACTIVITY TABLE (computed vs stored TF)")
        mismatch_ids = {m["activity_id"] for m in xc["mismatches"]}
        rows = [a for a in activities
                if args.all or a["activity_id"] in mismatch_ids
                or activities.index(a) < 25]
        print(f"  {'Activity':22s} {'St':2s} {'ES comp':>10s} {'ES P6':>10s} "
              f"{'TF comp':>8s} {'TF P6':>6s} {'Crit':>5s} {'LP':>3s}")
        hr()
        shown = 0
        for a in rows:
            r = cpm.activities.get(a["activity_id"])
            if r is None or r.excluded:
                continue
            crit = red("YES") if r.is_critical else dim("no")
            lp = "Y" if a.get("is_longest_path") else "·"
            st = {"Not Started": "NS", "In Progress": "IP", "Complete": "C"}.get(
                a.get("status", ""), "?")
            print(f"  {a['activity_id']:22s} {st:2s} {_fmt(r.es):>10s} "
                  f"{_fmt(a.get('early_start')):>10s} {_fmt(r.total_float):>8s} "
                  f"{_fmt(a.get('stored_total_float_days')):>6s} {crit:>5s} {lp:>3s}")
            shown += 1
            if not args.all and shown >= 60:
                print(dim(f"  … use --all for all {len(activities)} activities"))
                break

    hr()
    print(bold("Done."))


if __name__ == "__main__":
    main()
