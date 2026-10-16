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
    collect_author_stats,
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
    collect_author_stats(client, professors)
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
        os.environ.setdefault("ALUMNI_PROBE_FACULTY", "1")

        probe([x for x in os.getenv("ALUMNI_PROBE_IDS", "P2795").split(",") if x])
        # OpenAlex profiles that may belong to one professor (split profiles).
        for name in filter(None, os.getenv("OPENALEX_PROBE_NAMES", "").split(",")):
            data = client._get("authors", {"search": name, "per-page": 15,
                                           "select": "id,display_name,works_count,cited_by_count,last_known_institutions,orcid"})
            for a in data.get("results") or []:
                inst = "; ".join(i.get("display_name", "") for i in a.get("last_known_institutions") or [])
                print(f"[openalex-probe] {name}: {a['id'].split('/')[-1]} {a['display_name']} works={a['works_count']} "
                      f"cited={a['cited_by_count']} orcid={a.get('orcid') or ''} inst={inst}", flush=True)
        # Why hand-entered professors found no OpenAlex profile: the matcher's pick and the checks.
        resolve_ids = [x for x in os.getenv("OPENALEX_RESOLVE_PROBE", "").split(",") if x]
        if resolve_ids:
            # The matcher's candidates for professors it could not settle: name-compatible
            # OpenAlex authors with institution fit, size, bio share and main topics.
            from faculty_identity_v2 import (_search_queries, _name_matches, _matches_any, _affiliation,
                                             _all_affiliations, _bio_share)
            from pipeline import _read_csv, DATA_DIR as PDATA
            seed = _read_csv(PDATA / "professors_seed.csv")
            for _, row in seed[seed["professor_id"].isin(resolve_ids)].iterrows():
                print(f"[resolve-probe] {row['professor_id']} {row['name_ko']} / {row['name_en']} / "
                      f"{row['university']} / {row['department']}", flush=True)
                seen = {}
                for query in _search_queries(row["name_ko"], row["name_en"], row["name_en"]):
                    for a in client._get("authors", {"search": query, "per-page": 25}).get("results") or []:
                        seen.setdefault(a["id"], a)
                rows = []
                for a in seen.values():
                    ok, sim = _name_matches(a, row["name_ko"], row["name_en"])
                    if not ok:
                        continue
                    cur = _matches_any(row["university"], _affiliation(a))
                    ever = cur or _matches_any(row["university"], _all_affiliations(a))
                    rows.append((cur, ever, a.get("works_count", 0), a))
                rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
                for cur, ever, works, a in rows[:6]:
                    inst = "; ".join(i.get("display_name", "") for i in a.get("last_known_institutions") or [])[:70]
                    top = ", ".join(t.get("display_name", "") for t in (a.get("topics") or [])[:3])
                    print(f"[resolve-probe]   {a['id'].split('/')[-1]} {a['display_name']} works={works} "
                          f"{'CUR' if cur else ('PAST' if ever else '-')} bio={_bio_share(a):.2f} inst={inst} | {top}",
                          flush=True)
        # What an OpenAlex profile is about: topics, institutions, a few recent titles.
        for aid in filter(None, os.getenv("OPENALEX_PROBE_AUTHORS", "").split(",")):
            a = client._get(f"authors/{aid}", {"select": "id,display_name,works_count,topics,affiliations"})
            topics = ", ".join(t.get("display_name", "") for t in (a.get("topics") or [])[:5])
            insts = "; ".join(f"{x['institution']['display_name']} {min(x.get('years') or [0])}-{max(x.get('years') or [0])}"
                              for x in (a.get("affiliations") or [])[:6])
            print(f"[openalex-author] {aid} {a.get('display_name')} topics={topics} | {insts}", flush=True)
            works = client._get("works", {"filter": f"author.id:{aid}", "sort": "publication_year:desc", "per-page": 4,
                                          "select": "publication_year,title,primary_location"})
            for w in works.get("results") or []:
                src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name", "")
                print(f"[openalex-author]   {w.get('publication_year')} {str(w.get('title'))[:90]} ({src})", flush=True)
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
