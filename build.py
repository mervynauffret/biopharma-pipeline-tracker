"""Turn the newest snapshot into data/latest.json (flags, KPIs, heatmap, benchmark, slips).

Each group (mid-size biotech, big pharma) gets its own KPIs, heatmap and headline.
Slips: compare with the previous snapshot and log date moves in data/slips.csv.
Run:  .venv/bin/python build.py
"""

import calendar
import csv
import json
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
SNAPSHOT_DIR = HERE / "data" / "snapshots"

# Heatmap columns, in reading order
PHASE_COLUMNS = ["Phase 1", "Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Phase 4", "Obs.", "No phase"]
PHASE_2_OR_LATER = ["Phase 2", "Phase 2/3", "Phase 3", "Phase 4"]
LATE_STAGE = ["Phase 2/3", "Phase 3"]
DEFAULT_GROUPS = {"biotech": "Mid-size biotech"}  # for old snapshots saved before groups existed

# Flags, worst first. The key is used in the data; the label is shown on the page.
FLAGS = {
    "past_target": "Past target",
    "not_verified": "Not verified 12m+",
    "due_soon": "Due ≤6 months",
    "data_in": "Data in",
    "on_track": "On track",
}


# ---------- date helpers ----------

def parse_date(text, end_of_month):
    """'2026-09-30' -> that day. '2026-09' -> last day of Sept (or the 1st if end_of_month=False)."""
    if not text:
        return None
    parts = [int(p) for p in text.split("-")]
    if len(parts) == 3:
        return date(parts[0], parts[1], parts[2])
    year, month = parts
    day = calendar.monthrange(year, month)[1] if end_of_month else 1
    return date(year, month, day)


def add_months(d, months):
    """Move a date by whole months, e.g. 19 Nov 2026 + 6 -> 19 May 2027 (negative goes back)."""
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


# ---------- per-trial rules ----------

def phase_label(trial):
    """['PHASE2', 'PHASE3'] -> 'Phase 2/3'. Early Phase 1 counts as Phase 1. No phase: 'Obs.' or 'No phase'."""
    phases = [p for p in trial["phases"] if p != "NA"]
    if not phases:
        return "Obs." if trial["study_type"] == "OBSERVATIONAL" else "No phase"
    numbers = []
    for p in phases:
        n = "1" if p == "EARLY_PHASE1" else p.replace("PHASE", "")
        if n not in numbers:
            numbers.append(n)
    return "Phase " + "/".join(numbers)


def is_past_target(trial, snapshot_date):
    """Estimated primary completion date is already behind the snapshot date."""
    completion = parse_date(trial["primary_completion_date"], end_of_month=True)
    return trial["primary_completion_type"] == "ESTIMATED" and completion is not None and completion < snapshot_date


def is_not_verified(trial, snapshot_date):
    """The sponsor hasn't confirmed the record for more than 12 months."""
    verified = parse_date(trial["status_verified_date"], end_of_month=True)
    return verified is not None and verified < add_months(snapshot_date, -12)


def flag_for(trial, snapshot_date):
    """Give one flag per trial, checking the worst problem first."""
    completion = parse_date(trial["primary_completion_date"], end_of_month=True)
    estimated = trial["primary_completion_type"] == "ESTIMATED"

    if is_past_target(trial, snapshot_date):
        return "past_target"
    if is_not_verified(trial, snapshot_date):
        return "not_verified"
    if (estimated and completion and trial["phase_label"] in PHASE_2_OR_LATER
            and completion <= add_months(snapshot_date, 6)):
        return "due_soon"
    if trial["primary_completion_type"] == "ACTUAL":
        return "data_in"
    return "on_track"


# ---------- one group's summary ----------

def summarise(trials, snapshot_date):
    """KPIs, flag counts, heatmap and headline for one group of trials."""
    companies = sorted({t["company"] for t in trials},
                       key=lambda c: -sum(t["company"] == c for t in trials))  # biggest first
    flag_counts = {key: sum(t["flag"] == key for t in trials) for key in FLAGS}
    kpis = {
        "active": len(trials),
        "companies": len(companies),
        "late_stage": sum(t["phase_label"] in LATE_STAGE for t in trials),
        "past_target": flag_counts["past_target"],
        "due_soon": flag_counts["due_soon"],
        "data_in": flag_counts["data_in"],
        "due_by": add_months(snapshot_date, 6).isoformat(),
    }
    heatmap = []
    for c in companies:
        mine = [t for t in trials if t["company"] == c]
        counts = {col: sum(t["phase_label"] == col for t in mine) for col in PHASE_COLUMNS}
        heatmap.append({"company": c, "counts": counts, "total": len(mine)})

    n = kpis["past_target"]
    headline = f"{n} active trial{'s are' if n != 1 else ' is'} past {'their' if n != 1 else 'its'} own estimated completion date"
    return {"kpis": kpis, "flag_counts": flag_counts, "heatmap": heatmap, "headline": headline}


def benchmark_row(trials, snapshot_date):
    """Like-for-like comparison: Phase 3 and 2/3 trials only. Each problem counted on its own."""
    late = [t for t in trials if t["phase_label"] in LATE_STAGE]
    n = len(late)
    past = sum(is_past_target(t, snapshot_date) for t in late)
    stale = sum(is_not_verified(t, snapshot_date) for t in late)
    pct = lambda x: round(100 * x / n, 1) if n else 0
    return {"late_stage": n, "past_target": past, "past_target_pct": pct(past),
            "not_verified": stale, "not_verified_pct": pct(stale)}


# ---------- slip tracker ----------

SLIP_LOG = HERE / "data" / "slips.csv"
SLIP_COLUMNS = ["snapshot_date", "previous_snapshot", "group", "company", "nct_id", "phase",
                "old_date", "new_date", "months_slipped", "title"]


def find_slips(previous_trials, current_trials):
    """Trials in both snapshots whose primary completion date moved later."""
    old_dates = {t["nct_id"]: t["primary_completion_date"] for t in previous_trials}
    slips = []
    for t in current_trials:
        old_text = old_dates.get(t["nct_id"])
        old = parse_date(old_text, end_of_month=True)
        new = parse_date(t["primary_completion_date"], end_of_month=True)
        if old and new and new > old:
            slips.append({
                "group": t["group"],
                "company": t["company"],
                "nct_id": t["nct_id"],
                "phase": t["phase_label"],
                "old_date": old_text,
                "new_date": t["primary_completion_date"],
                "months_slipped": round((new - old).days / 30.44, 1),  # 30.44 = average days per month
                "title": t["title"],
            })
    return sorted(slips, key=lambda s: -s["months_slipped"])  # biggest slip first


def update_slip_log(slips, snapshot_date, previous_date):
    """Add this week's slips to data/slips.csv. Re-running the same week replaces its rows."""
    rows = []
    if SLIP_LOG.exists():
        with SLIP_LOG.open(newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["snapshot_date"] != snapshot_date]
    for s in slips:
        rows.append({"snapshot_date": snapshot_date, "previous_snapshot": previous_date, **s})
    with SLIP_LOG.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SLIP_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---------- main ----------

def main():
    files = sorted(SNAPSHOT_DIR.glob("*.json"))  # file names are dates, so sorted = oldest to newest
    newest = files[-1]
    previous = files[-2] if len(files) >= 2 else None
    snapshot = json.loads(newest.read_text())
    snapshot_date = date.fromisoformat(snapshot["snapshot_date"])
    group_names = snapshot.get("groups", DEFAULT_GROUPS)
    trials = snapshot["trials"]

    for t in trials:
        t.setdefault("group", "biotech")
        t["phase_label"] = phase_label(t)
        t["flag"] = flag_for(t, snapshot_date)

    # One summary per group, and the like-for-like benchmark
    groups, benchmark = {}, {}
    for key, label in group_names.items():
        mine = [t for t in trials if t["group"] == key]
        if mine:
            groups[key] = {"label": label, **summarise(mine, snapshot_date)}
            benchmark[key] = {"label": label, **benchmark_row(mine, snapshot_date)}

    # Slips against last week's snapshot (none possible in the very first week)
    previous_date, slips = None, []
    if previous:
        previous_snapshot = json.loads(previous.read_text())
        previous_date = previous_snapshot["snapshot_date"]
        slips = find_slips(previous_snapshot["trials"], trials)
        update_slip_log(slips, snapshot["snapshot_date"], previous_date)

    latest = {
        "snapshot_date": snapshot["snapshot_date"],
        "previous_snapshot": previous_date,
        "flags": FLAGS,
        "phase_columns": PHASE_COLUMNS,
        "groups": groups,
        "benchmark": benchmark,
        "slips": slips,
        "trials": trials,
    }
    out_file = HERE / "data" / "latest.json"
    out_file.write_text(json.dumps(latest, indent=2, ensure_ascii=False))

    # ---------- summary for a human to check ----------
    print(f"Read {newest.name}, wrote {out_file.relative_to(HERE)}")
    short = ["Ph1", "1/2", "Ph2", "2/3", "Ph3", "Ph4", "Obs", "None"]
    for key, g in groups.items():
        k = g["kpis"]
        print(f"\n===== {g['label']} =====")
        print(f"Headline: {g['headline']}")
        print(f"  Active {k['active']} ({k['companies']} companies) · Phase 3 or 2/3 {k['late_stage']}"
              f" · Past target {k['past_target']} · Phase 2+ due by {k['due_by']} {k['due_soon']}")
        print("  Flags: " + " · ".join(f"{FLAGS[f]} {g['flag_counts'][f]}" for f in FLAGS))
        print(f"  {'':<16}" + "".join(f"{s:>5}" for s in short) + "  Total")
        for row in g["heatmap"]:
            cells = "".join(f"{row['counts'][col] or '·':>5}" for col in PHASE_COLUMNS)
            print(f"  {row['company']:<16}{cells}  {row['total']:>5}")

    print("\n===== Benchmark: Phase 3 and 2/3 only =====")
    for key, b in benchmark.items():
        print(f"  {b['label']:<22} {b['late_stage']:>3} trials · past target {b['past_target']} ({b['past_target_pct']}%)"
              f" · not verified 12m+ {b['not_verified']} ({b['not_verified_pct']}%)")

    print("\nPast-target trials")
    for t in trials:
        if t["flag"] == "past_target":
            print(f"  {t['company']:<16}{t['phase_label']:<11}{t['nct_id']}  est. {t['primary_completion_date']}  {t['condition']}")

    print("\nSlips")
    if not previous:
        print("  Only one snapshot so far: slips start next week.")
    elif not slips:
        print(f"  No completion dates moved later since {previous_date}.")
    for s in slips:
        print(f"  {s['company']:<16}{s['phase']:<11}{s['nct_id']}  {s['old_date']} -> {s['new_date']}  (+{s['months_slipped']} months)")


if __name__ == "__main__":
    main()
