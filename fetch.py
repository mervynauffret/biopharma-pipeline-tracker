"""Download active trials for each company in config.json and save one dated snapshot.

Run:  .venv/bin/python fetch.py
Output: data/snapshots/YYYY-MM-DD.json, plus a summary printed on screen.
"""

import json
from datetime import date
from pathlib import Path

import requests

HERE = Path(__file__).parent


def fetch_company(config, company):
    """Return every active study whose lead sponsor matches the company's query (all pages)."""
    studies = []
    params = {
        "query.lead": company["query"],
        "filter.overallStatus": ",".join(config["statuses"]),
        "pageSize": config["page_size"],
    }
    # Optional phase filter, e.g. "phases": ["PHASE3"] for big pharma (also returns Phase 2/3)
    if company.get("phases"):
        params["filter.advanced"] = " OR ".join(f"AREA[Phase]{p}" for p in company["phases"])
    while True:
        response = requests.get(config["api_url"], params=params, timeout=60)
        response.raise_for_status()  # stop with an error if the API says no
        page = response.json()
        studies.extend(page.get("studies", []))
        token = page.get("nextPageToken")
        if not token:  # no more pages
            break
        params["pageToken"] = token
    return studies


def simplify(study, company_name, group):
    """Keep only the fields we need, in a flat, easy-to-read shape."""
    p = study["protocolSection"]
    ident = p.get("identificationModule", {})
    status = p.get("statusModule", {})
    design = p.get("designModule", {})
    sponsor = p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
    conditions = p.get("conditionsModule", {}).get("conditions", [])
    primary = status.get("primaryCompletionDateStruct", {})

    return {
        "company": company_name,
        "group": group,
        "nct_id": ident.get("nctId"),
        "title": ident.get("briefTitle"),
        "sponsor": sponsor.get("name"),
        "phases": design.get("phases", []),
        "study_type": design.get("studyType"),
        "status": status.get("overallStatus"),
        "condition": conditions[0] if conditions else None,
        "start_date": status.get("startDateStruct", {}).get("date"),
        "primary_completion_date": primary.get("date"),
        "primary_completion_type": primary.get("type"),
        "status_verified_date": status.get("statusVerifiedDate"),
        "last_update_posted": status.get("lastUpdatePostDateStruct", {}).get("date"),
        "enrollment": design.get("enrollmentInfo", {}).get("count"),
    }


def phase_label(phases):
    """Turn ['PHASE2', 'PHASE3'] into 'Phase 2/3'; no phase -> 'N/A'."""
    if not phases or phases == ["NA"]:
        return "N/A"
    numbers = [ph.replace("EARLY_PHASE1", "0").replace("PHASE", "") for ph in phases]
    return "Phase " + "/".join(numbers)


def main():
    config = json.loads((HERE / "config.json").read_text())
    today = date.today().isoformat()

    trials = []
    for company in config["companies"]:
        studies = fetch_company(config, company)
        trials.extend(simplify(s, company["name"], company.get("group", "biotech")) for s in studies)

    # Save the snapshot
    out_dir = HERE / "data" / "snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{today}.json"
    snapshot = {"snapshot_date": today, "groups": config["groups"], "trials": trials}
    out_file.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))

    # Print a summary so a human can sanity-check it
    print(f"Saved {len(trials)} trials to {out_file.relative_to(HERE)}\n")
    for company in config["companies"]:
        mine = [t for t in trials if t["company"] == company["name"]]
        phase_counts = {}
        for t in mine:
            label = phase_label(t["phases"])
            phase_counts[label] = phase_counts.get(label, 0) + 1
        sponsors = sorted({t["sponsor"] for t in mine})

        print(f"== {company['name']} ({company.get('group', 'biotech')}): {len(mine)} trials")
        for label in sorted(phase_counts):
            print(f"     {label:<12} {phase_counts[label]}")
        print("   Sponsor names found:")
        for name in sponsors:
            print(f"     - {name}")
        print()


if __name__ == "__main__":
    main()
