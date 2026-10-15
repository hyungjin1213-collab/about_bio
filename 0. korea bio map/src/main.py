from __future__ import annotations

import argparse
import atexit
import os
import time

import pandas as pd

from alumni import collect_alumni
from candidate_review import classify_candidates
from department_discovery import discover_departments
from faculty_identity import import_verified_faculty, match_faculty_identities
from faculty_identity_v2 import import_verified_faculty_v2, resolve_faculty_identities_v2
from institution_discovery import discover_by_institution
from korean_names import fill_korean_names
from faculty_agent import collect_faculty_with_agent
from faculty_scraper import scrape_faculty
from faculty_scraper_v2 import scrape_faculty_v2
from config import DATA_DIR, OUTPUT_DIR
from master_sheet import add_master_professors
from openalex_client import OpenAlexClient, OpenAlexUnavailable
from pipeline import (
    PROFESSOR_COLUMNS,
    build_network,
    collect_collaborations,
    promote_approved_candidates,
    resolve_professors,
    resolve_scopus_ids,
)
from scholar_client import ScholarClient
from scopus_client import ScopusClient


def run_all(client: OpenAlexClient) -> None:
    added = add_master_professors(DATA_DIR / "professors_seed.csv", PROFESSOR_COLUMNS)
    if added:
        print(f"Added {added} professors from the master Excel file", flush=True)
    professors = resolve_professors(client)
    scopus = ScopusClient()
    if scopus.enabled:
        professors = resolve_scopus_ids(scopus, professors)
    auto, candidates = collect_collaborations(client, professors, scopus)
    reviewed = classify_candidates(client, ScholarClient())
    nodes, links = build_network(professors)
    print(f"Professors: {len(professors)}")
    print(f"Auto relationships: {len(auto)}")
    print(f"Review candidates: {len(candidates)}")
    print(f"Triaged candidates: {len(reviewed)}")
    print(f"Network nodes: {len(nodes)}")
    print(f"Network links: {len(links)}")


# The daily run stops starting new expansion steps after this many minutes, so the
# job (timeout 350 min) always reaches the commit and publish steps.
EXPANSION_DEADLINE_MIN = float(os.getenv("EXPANSION_DEADLINE_MIN", "270"))


def daily(client: OpenAlexClient) -> None:
    """Every day, spending as much of the OpenAlex budget as the day allows:
    1. lab alumni pages read again (cheap);
    2. the whole map refreshed: professors, collaborations, Korean names in use (core);
    3. with what is left of the budget and time: new departments, faculty, identities,
       institutes and companies, Korean names. Newly added people join the map on the
       next day's step 2. Each step stops by itself when the OpenAlex budget runs out.
    Step 2 comes before 3 so a spent budget never costs the day's map update."""
    started = time.monotonic()
    try:
        collect_alumni(client, known_only=True)
    except Exception as exc:  # alumni pages must not stop the map update
        print(f"[daily] alumni refresh skipped: {exc}", flush=True)
    run_all(client)
    steps = [
        ("departments", discover_departments),
        ("faculty pages", scrape_faculty_v2),
        ("faculty identities", lambda: resolve_faculty_identities_v2(client)),
        ("import faculty", import_verified_faculty_v2),
        ("institutes and companies", lambda: discover_by_institution(client)),
        ("Korean names", lambda: fill_korean_names(client)),
    ]
    for name, step in steps:
        minutes = (time.monotonic() - started) / 60
        if minutes > EXPANSION_DEADLINE_MIN:
            print(f"[daily] {minutes:.0f} min used: stopping before '{name}' (continues tomorrow)", flush=True)
            break
        try:
            result = step()
            print(f"[daily] {name}: {len(result) if hasattr(result, '__len__') else result}", flush=True)
        except OpenAlexUnavailable as exc:
            print(f"[daily] OpenAlex budget spent at '{name}': {exc}; the rest continues tomorrow", flush=True)
            break
    # Today's additions to professors_seed are collected tomorrow; the map is already current.


def rebuild() -> None:
    """Redraw the map from data already collected: no OpenAlex, no crawling."""
    professors = pd.read_csv(OUTPUT_DIR / "professors_enriched.csv", dtype=str).fillna("")
    nodes, links = build_network(professors)
    print(f"Rebuilt the map: {len(nodes)} nodes, {len(links)} links")


def main() -> None:
    parser = argparse.ArgumentParser(description="About Bio - Korea Bio Map data pipeline")
    parser.add_argument(
        "command",
        choices=[
            "resolve", "collect", "review", "build", "promote", "promote-and-all", "all",
            "discover-departments",
            "scrape-faculty", "scrape-faculty-v2", "collect-faculty-agent",
            "match-faculty-identities", "match-faculty-identities-v2",
            "faculty-full", "faculty-full-v2",
            "import-faculty", "import-faculty-and-all",
            "import-faculty-v2", "import-faculty-v2-and-all",
            "galaxy-full", "daily", "alumni", "alumni-refresh", "alumni-probe", "rebuild",
        ],
        help="Pipeline step to run",
    )
    args = parser.parse_args()
    client = OpenAlexClient()
    # OpenAlex bills per request against a daily budget: report usage even on failure.
    atexit.register(lambda: print(f"[openalex] requests this run: {client.request_counts}", flush=True))

    if args.command == "galaxy-full":
        # Official pages -> names -> ORCID/OpenAlex -> professor DB -> coauthor galaxy.
        # Identity matching is batched (IDENTITY_BATCH_LIMIT); re-run to continue.
        departments = discover_departments()
        faculty = scrape_faculty_v2()
        identities = resolve_faculty_identities_v2(client)
        count = import_verified_faculty_v2()
        # Organisations whose sites the crawler cannot read: researchers by OpenAlex affiliation.
        by_institution = discover_by_institution(client)
        print(f"Added {by_institution} researchers by OpenAlex institution")
        # Hangul names for people who arrived with an English name only.
        print(f"Filled {fill_korean_names(client)} Korean names")
        print(f"Department search results: {len(departments)}")
        print(f"Faculty v2 rows: {len(faculty)}")
        print(f"Faculty identity v2 rows: {len(identities)}")
        print(f"Imported {count} faculty into professors_seed.csv")
        run_all(client)
        return

    if args.command == "daily":
        daily(client)
        return

    if args.command == "rebuild":
        rebuild()
        return

    if args.command == "alumni-probe":
        from alumni import probe
        os.environ.setdefault("ALUMNI_PROBE_ALL_LINKS", "1")
        os.environ.setdefault("ALUMNI_PROBE_URLS", "CHEM=https://chem.snu.ac.kr/,CHEM=https://chem.snu.ac.kr/kor/,CHEM=https://chem.snu.ac.kr/people/faculty")

        probe([x for x in os.getenv("ALUMNI_PROBE_IDS", "CHEM").split(",") if x])
        return

    if args.command == "alumni-refresh":
        # Daily: re-read the alumni pages already found (degree years, careers, papers); no new labs.
        summary = collect_alumni(client, known_only=True)
        print(f"Labs with alumni data: {len(summary)}")
        rebuild()
        return

    if args.command == "alumni":
        # Lab alumni pages -> degree time, careers, paper quality; then redraw.
        summary = collect_alumni(client)
        print(f"Labs with alumni data: {len(summary)}")
        rebuild()
        return

    if args.command == "discover-departments":
        df = discover_departments()
        print(f"Department candidates: {len(df)}")
        return

    if args.command == "scrape-faculty":
        df = scrape_faculty()
        print(f"Faculty rows: {len(df)}")
        return

    if args.command == "scrape-faculty-v2":
        df = scrape_faculty_v2()
        print(f"Faculty v2 rows: {len(df)}")
        return

    if args.command == "collect-faculty-agent":
        df = collect_faculty_with_agent()
        print(f"Faculty agent rows: {len(df)}")
        return

    if args.command == "match-faculty-identities":
        identities = match_faculty_identities()
        print(f"Faculty identities: {len(identities)}")
        return

    if args.command == "match-faculty-identities-v2":
        identities = resolve_faculty_identities_v2()
        print(f"Faculty identity v2 rows: {len(identities)}")
        if not identities.empty:
            print(f"Verified: {(identities['identity_status'] == 'verified').sum()}")
            print(f"Probable: {(identities['identity_status'] == 'probable').sum()}")
            print(f"Manual review: {(identities['identity_status'] == 'manual_review').sum()}")
        return

    if args.command == "faculty-full":
        faculty = scrape_faculty()
        identities = match_faculty_identities()
        print(f"Faculty rows: {len(faculty)}")
        print(f"Faculty identities: {len(identities)}")
        return

    if args.command == "faculty-full-v2":
        faculty = scrape_faculty_v2()
        identities = resolve_faculty_identities_v2()
        print(f"Faculty v2 rows: {len(faculty)}")
        print(f"Faculty identity v2 rows: {len(identities)}")
        return

    if args.command == "import-faculty":
        count = import_verified_faculty()
        print(f"Imported {count} faculty into professors_seed.csv")
        return

    if args.command == "import-faculty-and-all":
        count = import_verified_faculty()
        print(f"Imported {count} faculty into professors_seed.csv")
        run_all(client)
        return

    if args.command == "import-faculty-v2":
        count = import_verified_faculty_v2()
        print(f"Imported {count} v2 faculty into professors_seed.csv")
        return

    if args.command == "import-faculty-v2-and-all":
        count = import_verified_faculty_v2()
        print(f"Imported {count} v2 faculty into professors_seed.csv")
        run_all(client)
        return

    if args.command == "resolve":
        professors = resolve_professors(client)
        print(f"Resolved {len(professors)} professors.")
        return

    if args.command == "collect":
        professors = resolve_professors(client)
        auto, candidates = collect_collaborations(client, professors)
        print(f"Auto relationships: {len(auto)}")
        print(f"Review candidates: {len(candidates)}")
        return

    if args.command == "review":
        reviewed = classify_candidates(client, ScholarClient())
        print(f"Triaged candidates: {len(reviewed)}")
        return

    if args.command == "build":
        professors = resolve_professors(client)
        nodes, links = build_network(professors)
        print(f"Network nodes: {len(nodes)}")
        print(f"Network links: {len(links)}")
        return

    if args.command == "promote":
        count = promote_approved_candidates()
        print(f"Promoted {count} approved candidates.")
        return

    if args.command == "promote-and-all":
        count = promote_approved_candidates()
        print(f"Promoted {count} approved candidates.")
        run_all(client)
        return

    run_all(client)


if __name__ == "__main__":
    try:
        main()
    except OpenAlexUnavailable as exc:
        # Stop before overwriting earlier outputs with empty results.
        print(f"Stopped: {exc}. Earlier network files were left unchanged; re-run later.", flush=True)
        raise SystemExit(1)
