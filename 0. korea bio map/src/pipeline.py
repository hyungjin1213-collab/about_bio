from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import pandas as pd

from config import (
    DATA_DIR,
    KOREA_COUNTRY_CODE,
    MAX_AUTHORS_PER_WORK,
    MAX_REVIEW_CANDIDATES,
    MIN_SHARED_PAPERS,
    OUTPUT_DIR,
    WEB_DIR,
)
from classify import Classifier, position
from keywords import KeywordCollector
from lineage import Authorship, infer_lineage, lineage_relationships
from name_extraction import name_compatible, normalize_english_name
from alumni import alumni_lab_info
from master_sheet import lab_info, master_relationships
from sectors import ORG_TYPE_SECTOR, SectorCollector, SectorTable
from openalex_client import OpenAlexClient, OpenAlexUnavailable, normalize_openalex_id
from scopus_client import ScopusClient, ScopusUnavailable, normalize_doi


PROFESSOR_COLUMNS = [
    "professor_id",
    "name_ko",
    "name_en",
    "university",
    "department",
    "primary_field",
    "openalex_id",
    "source_url",
    "orcid",
    "identity_status",
    "scopus_id",
]

SCOPUS_CACHE_COLUMNS = ["professor_id", "scopus_id", "reason"]
# OpenAlex accepts up to 100 OR-ed values per filter; 50 keeps URLs short.
AUTHOR_BATCH = 50

# Collaboration strength (see collaboration_weight): a pair is drawn only at or
# above this. 0.5 = one recent paper with <= 3 authors, two with 5, ~5 with 10.
MIN_COLLAB_STRENGTH = float(os.getenv("MIN_COLLAB_STRENGTH", "0.5"))
# ...or this many shared papers, whatever their size: two professors who keep
# publishing together collaborate even when each paper has 15 authors (run #26
# kept only 248 of 1,422 pairs on strength alone). Weak pairs are drawn faint.
MIN_COLLAB_PAPERS = int(os.getenv("MIN_COLLAB_PAPERS", "2"))
RECENCY_HALF_LIFE_YEARS = 8.0
RECENT_YEARS = 2
SENIOR_BONUS = 1.5
CURRENT_YEAR = pd.Timestamp.now().year


def collaboration_weight(n_authors: int, year: int, both_senior: bool, now: int = CURRENT_YEAR) -> float:
    """How much one co-authored paper says about two professors collaborating.

    - 1 / (authors - 1): a 3-author paper counts 0.5, a 90-author consortium
      paper 0.01 (Newman's co-authorship weight).
    - x1.5 when both are first/last (senior) authors: lab-to-lab work.
    - Papers from the last 2 years count fully; older ones halve every 8
      years, so current collaborations dominate.
    """
    base = 1.0 / max(n_authors - 1, 1)
    if both_senior:
        base *= SENIOR_BONUS
    age = max(now - year - RECENT_YEARS, 0) if year else RECENCY_HALF_LIFE_YEARS
    return base * 0.5 ** (age / RECENCY_HALF_LIFE_YEARS)

REL_COLUMNS = [
    "relationship_id",
    "professor_a_id",
    "professor_b_id",
    "relationship_type",
    "collaboration_paper_count",
    "evidence_url",
    "verified",
    "notes",
    "collaboration_strength",
    "first_year",
    "last_year",
]


def _read_csv(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns or [])
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=columns or [])
    if columns:
        for column in columns:
            if column not in df.columns:
                df[column] = ""
        df = df[columns]
    return df


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _openalex_ids(value: object) -> list[str]:
    return [x for x in (normalize_openalex_id(v) for v in str(value or "").split(";")) if x]


def _next_professor_id(existing_ids: set[str]) -> str:
    nums = []
    for pid in existing_ids:
        match = re.fullmatch(r"P(\d+)", str(pid).strip(), flags=re.I)
        if match:
            nums.append(int(match.group(1)))
    return f"P{(max(nums) if nums else 0) + 1:04d}"


def resolve_professors(client: OpenAlexClient) -> pd.DataFrame:
    professors = _read_csv(DATA_DIR / "professors_seed.csv", PROFESSOR_COLUMNS).copy()
    resolved_rows: list[dict[str, str]] = []

    for _, row in professors.iterrows():
        item = row.to_dict()
        # openalex_id may hold several ";"-separated IDs (split OpenAlex profiles).
        current_id = ";".join(_openalex_ids(item.get("openalex_id", "")))

        if not current_id and item.get("orcid", ""):
            # Imported by ORCID while OpenAlex was unavailable, or OpenAlex
            # linked the ORCID later: an exact lookup, retried every run.
            try:
                current_id = ";".join(
                    normalize_openalex_id(a.get("id", "")) for a in client.authors_by_orcid(item["orcid"])
                )
            except OpenAlexUnavailable:
                pass

        if not current_id and not item.get("orcid", ""):
            # Hand-entered rows: same matcher as imported faculty (romanization
            # variants, institution acronyms like POSTECH, past affiliations).
            from faculty_identity_v2 import MIN_BIO_SHARE, _best_openalex, _bio_share, _name_similarity

            try:
                match, _ = _best_openalex(client, item.get("name_ko", ""), item.get("name_en", ""),
                                          item.get("university", ""), item.get("name_en") or item.get("name_ko", ""))
            except OpenAlexUnavailable:
                match = None
            shown = str((match or {}).get("display_name", ""))
            name_en = normalize_english_name(item.get("name_en", ""))
            # Korean name romanizes to the profile name, or the English name
            # entered by hand is that name (foreign faculty: 마틴 슈타이네거).
            name_ok = (not item.get("name_ko") or name_compatible(shown, item["name_ko"])
                       or (name_en and _name_similarity(name_en, shown) >= 0.85))
            # Same checks as imported faculty: run #26 gave 김원종 (POSTECH,
            # polymers) a marketing researcher's profile.
            if match and name_ok and _bio_share(match) >= MIN_BIO_SHARE:
                current_id = normalize_openalex_id(match.get("id", ""))

        if current_id:
            from faculty_identity_v2 import load_rejections

            rejected = load_rejections()
            current_id = ";".join(x for x in _openalex_ids(current_id) if x not in rejected)

        item["openalex_id"] = current_id
        if current_id and not item.get("primary_field", ""):
            # Older seed rows have no field: take the main OpenAlex subfield so
            # the map can colour and filter them (one request per such row).
            author = client.get_author(_openalex_ids(current_id)[0])
            topics = (author or {}).get("topics") or []
            if topics:
                item["primary_field"] = str((topics[0].get("subfield") or {}).get("display_name", ""))
        resolved_rows.append(item)

    resolved = pd.DataFrame(resolved_rows, columns=PROFESSOR_COLUMNS)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    resolved.to_csv(OUTPUT_DIR / "professors_enriched.csv", index=False)
    return resolved


def resolve_scopus_ids(scopus: ScopusClient | None, professors: pd.DataFrame) -> pd.DataFrame:
    """Fill professors["scopus_id"] from data/scopus_author_cache.csv, looking up new ones.

    Lookups (hits and misses) are cached so each professor costs Scopus
    Author Search quota once.
    """
    cache_path = DATA_DIR / "scopus_author_cache.csv"
    cache = _read_csv(cache_path, SCOPUS_CACHE_COLUMNS)
    known = {row["professor_id"]: row for _, row in cache.iterrows()}
    new_rows = []

    if scopus is not None and scopus.enabled:
        for _, prof in professors.iterrows():
            pid = prof["professor_id"]
            if pid in known or prof.get("scopus_id", ""):
                continue
            try:
                sid, reason = scopus.find_author_id(
                    prof.get("orcid", ""), prof.get("name_ko", ""), prof.get("name_en", ""),
                    prof.get("university", ""),
                )
            except ScopusUnavailable:
                break
            row = {"professor_id": pid, "scopus_id": sid, "reason": reason}
            known[pid] = row
            new_rows.append(row)

    if new_rows:
        cache = pd.concat([cache, pd.DataFrame(new_rows, columns=SCOPUS_CACHE_COLUMNS)], ignore_index=True)
        cache.to_csv(cache_path, index=False, encoding="utf-8-sig")
        print(f"[scopus] looked up {len(new_rows)} author IDs", flush=True)

    professors = professors.copy()
    professors["scopus_id"] = [
        prof.get("scopus_id", "") or known.get(prof["professor_id"], {}).get("scopus_id", "")
        for _, prof in professors.iterrows()
    ]
    return professors


def _scopus_pairs(scopus: ScopusClient, professors: pd.DataFrame) -> dict[tuple[str, str], set[str]]:
    by_scopus = {row["scopus_id"]: row["professor_id"] for _, row in professors.iterrows() if row.get("scopus_id", "")}
    pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for sid, pid in by_scopus.items():
        try:
            for doc in scopus.iter_documents(sid):
                if len(doc["author_ids"]) > MAX_AUTHORS_PER_WORK:
                    continue
                key = doc["doi"] or f"scopus:{doc['id']}"
                for other in doc["author_ids"]:
                    other_pid = by_scopus.get(other)
                    if other_pid and other_pid != pid:
                        pairs[tuple(sorted((pid, other_pid)))].add(key)
        except ScopusUnavailable as exc:
            print(f"[scopus] stopped: {exc}", flush=True)
            break
    return pairs


def collect_collaborations(
    client: OpenAlexClient,
    professors: pd.DataFrame,
    scopus: ScopusClient | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    confirmed_by_openalex = {
        oid: row["professor_id"]
        for _, row in professors.iterrows()
        for oid in _openalex_ids(row["openalex_id"])
    }

    pair_to_works: dict[tuple[str, str], set[str]] = defaultdict(set)
    openalex_pairs: set[tuple[str, str]] = set()
    pair_strength: dict[tuple[str, str], float] = defaultdict(float)
    pair_years: dict[tuple[str, str], list[int]] = defaultdict(list)
    candidate_stats: dict[str, dict[str, object]] = {}

    # One request covers up to AUTHOR_BATCH professors' works (OpenAlex OR
    # filter), instead of one request per professor: far fewer billed calls.
    all_ids = sorted(confirmed_by_openalex)
    seen_works: set[str] = set()
    lineage_records: dict[str, list[Authorship]] = defaultdict(list)
    keyword_collector = KeywordCollector(CURRENT_YEAR)
    sector_collector = SectorCollector(CURRENT_YEAR)
    for start in range(0, len(all_ids), AUTHOR_BATCH):
        batch = all_ids[start:start + AUTHOR_BATCH]
        print(f"[collect] authors {start + 1}-{start + len(batch)} of {len(all_ids)}", flush=True)
        for work in client.iter_works_by_authors(batch):
            work_id = normalize_openalex_id(work.get("id", ""))
            work_title = work.get("display_name", "")
            if work_id and work_id in seen_works:
                continue  # already seen via another batch
            seen_works.add(work_id)
            # DOI as the paper key so OpenAlex and Scopus copies count once.
            work_key = normalize_doi(work.get("doi", "") or "") or work_id or work_title
            authorships = work.get("authorships") or []
            # Mega-consortium papers (hundreds of authors) are not collaboration.
            if len(authorships) > MAX_AUTHORS_PER_WORK:
                continue

            profs_on_work = {
                confirmed_by_openalex[aid]
                for aid in (normalize_openalex_id((a.get("author") or {}).get("id", "")) for a in authorships)
                if aid in confirmed_by_openalex
            }
            year = _safe_int(work.get("publication_year"), 0)
            senior = {
                confirmed_by_openalex[aid]
                for a in authorships
                if a.get("author_position") in ("first", "last")
                for aid in [normalize_openalex_id((a.get("author") or {}).get("id", ""))]
                if aid in confirmed_by_openalex
            }
            for pair in combinations(sorted(profs_on_work), 2):
                pair_to_works[pair].add(work_key)
                openalex_pairs.add(pair)
                pair_strength[pair] += collaboration_weight(
                    len(authorships), year, pair[0] in senior and pair[1] in senior)
                if year:
                    pair_years[pair].append(year)

            # Per-professor authorship records for lineage inference (same data, no extra calls).
            last_pid = ""
            for a in authorships:
                if a.get("author_position") == "last":
                    last_pid = confirmed_by_openalex.get(normalize_openalex_id((a.get("author") or {}).get("id", "")), "")
            for a in authorships:
                pid_on = confirmed_by_openalex.get(normalize_openalex_id((a.get("author") or {}).get("id", "")))
                if pid_on:
                    keyword_collector.add_work(pid_on, str(a.get("author_position", "")), work)
                    sector_collector.add(pid_on, year, a.get("institutions") or [])
                    lineage_records[pid_on].append(Authorship(
                        year=_safe_int(work.get("publication_year"), 0),
                        position=str(a.get("author_position", "")),
                        last_author_pid=last_pid,
                        institutions=tuple(i.get("display_name", "") for i in a.get("institutions") or [] if i.get("display_name")),
                        title=work_title,
                        doi=normalize_doi(work.get("doi", "") or ""),
                    ))

            for authorship in authorships:
                author = authorship.get("author") or {}
                coauthor_openalex_id = normalize_openalex_id(author.get("id", ""))
                if not coauthor_openalex_id or coauthor_openalex_id in confirmed_by_openalex:
                    continue

                stats = candidate_stats.setdefault(
                    coauthor_openalex_id,
                    {
                        "openalex_id": coauthor_openalex_id,
                        "display_name": author.get("display_name", ""),
                        "shared_work_ids": set(),
                        "seed_professor_ids": set(),
                        "institutions": set(),
                        "country_codes": set(),
                        "example_work": "",
                    },
                )
                stats["shared_work_ids"].add(work_id or work_title)
                stats["seed_professor_ids"].update(profs_on_work)

                if not stats["example_work"] and work_title:
                    stats["example_work"] = work_title

                for institution in authorship.get("institutions") or []:
                    name = institution.get("display_name", "")
                    country = institution.get("country_code", "")
                    if name:
                        stats["institutions"].add(name)
                    if country:
                        stats["country_codes"].add(country)

    scopus_pairs: dict[tuple[str, str], set[str]] = {}
    if scopus is not None and scopus.enabled and "scopus_id" in professors.columns:
        scopus_pairs = _scopus_pairs(scopus, professors)
        for pair, keys in scopus_pairs.items():
            new_keys = keys - pair_to_works[pair]
            pair_to_works[pair] |= keys
            # Scopus gives no author counts here: a modest, fixed weight per extra paper.
            pair_strength[pair] += 0.25 * len(new_keys)
        print(f"[scopus] {len(scopus_pairs)} professor pairs from Scopus", flush=True)

    auto_rows = []
    kept = sorted(p for p in pair_to_works
                  if pair_strength[p] >= MIN_COLLAB_STRENGTH or len(pair_to_works[p]) >= MIN_COLLAB_PAPERS)
    print(f"[collect] {len(kept)} of {len(pair_to_works)} co-author pairs pass "
          f"{MIN_COLLAB_PAPERS}+ shared papers or "
          f"collaboration strength >= {MIN_COLLAB_STRENGTH}", flush=True)
    for index, pair in enumerate(kept, start=1):
        professor_a_id, professor_b_id = pair
        work_ids = pair_to_works[pair]
        years = pair_years.get(pair) or []
        sources = [name for name, found in (("OpenAlex", pair in openalex_pairs), ("Scopus", pair in scopus_pairs)) if found]
        auto_rows.append(
            {
                "relationship_id": f"AUTO{index:06d}",
                "professor_a_id": professor_a_id,
                "professor_b_id": professor_b_id,
                "relationship_type": "collaboration",
                "collaboration_paper_count": len(work_ids),
                "evidence_url": "",
                "verified": "auto",
                "notes": f"{' + '.join(sources)} coauthorship between confirmed professors",
                "collaboration_strength": round(pair_strength[pair], 3),
                "first_year": min(years) if years else "",
                "last_year": max(years) if years else "",
            }
        )

    candidate_rows = []
    for stats in candidate_stats.values():
        shared = len(stats["shared_work_ids"])
        seed_count = len(stats["seed_professor_ids"])
        country_codes = sorted(stats["country_codes"])
        is_korea = KOREA_COUNTRY_CODE in country_codes
        candidate_rows.append(
            {
                "openalex_id": stats["openalex_id"],
                "display_name": stats["display_name"],
                "shared_paper_count": shared,
                "seed_connection_count": seed_count,
                "connected_seed_professor_ids": "; ".join(sorted(stats["seed_professor_ids"])),
                "institutions": "; ".join(sorted(stats["institutions"])),
                "country_codes": "; ".join(country_codes),
                "korea_affiliated": "yes" if is_korea else "no",
                "review_priority": shared + max(seed_count - 1, 0) * 3,
                "example_work": stats["example_work"],
                "review_status": "needs_review" if is_korea else "out_of_scope",
            }
        )

    candidate_columns = [
        "openalex_id",
        "display_name",
        "shared_paper_count",
        "seed_connection_count",
        "connected_seed_professor_ids",
        "institutions",
        "country_codes",
        "korea_affiliated",
        "review_priority",
        "example_work",
        "review_status",
    ]

    auto_df = pd.DataFrame(auto_rows, columns=REL_COLUMNS)
    all_candidates_df = pd.DataFrame(candidate_rows, columns=candidate_columns)

    if not all_candidates_df.empty:
        all_candidates_df = all_candidates_df.sort_values(
            by=["korea_affiliated", "review_priority", "shared_paper_count"],
            ascending=[False, False, False],
        )

    review_df = all_candidates_df[
        (all_candidates_df["korea_affiliated"] == "yes")
        & (all_candidates_df["shared_paper_count"].map(_safe_int) >= MIN_SHARED_PAPERS)
    ].head(MAX_REVIEW_CANDIDATES).copy()

    names = {row["professor_id"]: row.get("name_ko", "") or row.get("name_en", "") for _, row in professors.iterrows()}
    lineage = infer_lineage(lineage_records, names, _read_csv(DATA_DIR / "lineage_decisions.csv"))
    print(f"[lineage] {len(lineage)} candidates: {lineage['status'].value_counts().to_dict() if not lineage.empty else {}}", flush=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    lineage.to_csv(OUTPUT_DIR / "lineage_candidates.csv", index=False, encoding="utf-8-sig")
    keyword_collector.to_frame().to_csv(OUTPUT_DIR / "professor_keywords.csv", index=False, encoding="utf-8-sig")
    sector_collector.to_frame().to_csv(OUTPUT_DIR / "professor_sectors.csv", index=False, encoding="utf-8-sig")
    lineage_relationships(lineage).to_csv(OUTPUT_DIR / "relationships_lineage.csv", index=False)
    auto_df.to_csv(OUTPUT_DIR / "relationships_auto.csv", index=False)
    all_candidates_df.to_csv(OUTPUT_DIR / "collaborator_candidates_all.csv", index=False)
    review_df.to_csv(OUTPUT_DIR / "collaborator_candidates.csv", index=False)
    return auto_df, review_df


def promote_approved_candidates() -> int:
    professors_path = DATA_DIR / "professors_seed.csv"
    decisions_path = DATA_DIR / "candidate_decisions.csv"

    professors = _read_csv(professors_path, PROFESSOR_COLUMNS).copy()
    decisions = _read_csv(decisions_path, ["openalex_id", "decision"])

    if decisions.empty:
        return 0

    candidate_path = OUTPUT_DIR / "candidate_review.csv"
    if not candidate_path.exists():
        candidate_path = OUTPUT_DIR / "collaborator_candidates_all.csv"

    candidate_lookup = {}
    if candidate_path.exists():
        candidate_df = _read_csv(candidate_path)
        if not candidate_df.empty and "openalex_id" in candidate_df.columns:
            candidate_lookup = {
                normalize_openalex_id(row["openalex_id"]): row.to_dict()
                for _, row in candidate_df.iterrows()
            }

    existing_openalex = {
        oid for value in professors["openalex_id"] for oid in _openalex_ids(value)
    }
    existing_ids = set(professors["professor_id"])
    new_rows = []

    for _, decision in decisions.iterrows():
        if str(decision.get("decision", "")).strip().casefold() != "yes":
            continue

        oid = normalize_openalex_id(decision.get("openalex_id", ""))
        if not oid or oid in existing_openalex:
            continue

        candidate = candidate_lookup.get(oid, {})
        name_en = candidate.get("display_name", "")
        university = (
            candidate.get("current_affiliation", "")
            or candidate.get("institutions", "")
        )

        # OpenAlex ID is the durable identity key. Missing/uncertain metadata
        # must not block a user-approved professor from entering the graph.
        if not name_en:
            name_en = oid

        pid = _next_professor_id(existing_ids)
        existing_ids.add(pid)
        existing_openalex.add(oid)

        new_rows.append(
            {
                "professor_id": pid,
                "name_ko": "",
                "name_en": name_en,
                "university": university,
                "department": "",
                "primary_field": "",
                "openalex_id": oid,
                "source_url": candidate.get("web_url", "")
                or candidate.get("scholar_profile_url", ""),
            }
        )

    if new_rows:
        professors = pd.concat(
            [professors, pd.DataFrame(new_rows, columns=PROFESSOR_COLUMNS)],
            ignore_index=True,
        )
        professors.to_csv(professors_path, index=False, encoding="utf-8-sig")

    return len(new_rows)

def _titles_by_professor(nodes: pd.DataFrame) -> dict[str, str]:
    """Faculty-page title (교수/부교수/조교수) per professor, via the identity file."""
    identity = _read_csv(OUTPUT_DIR / "faculty_identity_v2.csv")
    if identity.empty or "title" not in identity.columns:
        return {}
    by_key: dict[str, str] = {}
    for _, r in identity.iterrows():
        t = position(r.get("title", ""))
        if not t:
            continue
        for oid in _openalex_ids(r.get("openalex_ids", "") or r.get("openalex_id", "")):
            by_key.setdefault(f"oa:{oid}", t)
        if r.get("orcid", ""):
            by_key.setdefault(f"orcid:{r['orcid']}", t)
        if r.get("name_ko", ""):
            by_key.setdefault(f"name:{r['name_ko']}|{r.get('university', '').casefold()}", t)
    out = {}
    for _, row in nodes.iterrows():
        keys = [f"oa:{o}" for o in _openalex_ids(row.get("openalex_id", ""))]
        keys += [f"orcid:{row.get('orcid', '')}", f"name:{row.get('name_ko', '')}|{str(row.get('university', '')).casefold()}"]
        out[row["professor_id"]] = next((by_key[k] for k in keys if k in by_key), "")
    return out


def export_web_data(nodes: pd.DataFrame, links: pd.DataFrame) -> None:
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    classifier = Classifier(DATA_DIR)
    sector_table = SectorTable(DATA_DIR, OUTPUT_DIR)
    # Lab facts: totals from lab alumni pages, overridden field by field by
    # what was typed into the master Excel file (연구실정보).
    labs = alumni_lab_info()
    for pid, info in lab_info(nodes).items():
        merged = {**labs.get(pid, {}), **info}
        if pid in labs and "source" in info:
            merged["source"] = f"{info['source']} · {labs[pid]['source']}"
        labs[pid] = merged
    titles = _titles_by_professor(nodes)
    terms: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"keyword": [], "technique": []})
    kw = _read_csv(OUTPUT_DIR / "professor_keywords.csv")
    if not kw.empty:
        kw["rank_n"] = kw["rank"].map(_safe_int)
        for _, r in kw.sort_values(["professor_id", "kind", "rank_n"]).iterrows():
            terms[r["professor_id"]][r["kind"]].append(r["term"])

    node_records = []
    for _, row in nodes.iterrows():
        node_records.append(
            {
                "id": row.get("professor_id", ""),
                "name": row.get("name_ko", "") or row.get("name_en", ""),
                "name_en": row.get("name_en", ""),
                "university": row.get("university", ""),
                "field": row.get("primary_field", ""),
                "category": classifier.category(row.get("professor_id", ""), row.get("primary_field", ""),
                                                row.get("department", "")),
                "region": classifier.region(row.get("university", "")),
                # 산학연병 (industry / academia / institute / hospital) set membership
                "home_sector": ORG_TYPE_SECTOR.get(classifier.org_type(row.get("university", "")), "학"),
                "sectors": sector_table.sectors(
                    row.get("professor_id", ""),
                    ORG_TYPE_SECTOR.get(classifier.org_type(row.get("university", "")), "학"),
                    row.get("department", "")),
                "university_ko": classifier.university_ko(row.get("university", "")),
                "position": titles.get(row.get("professor_id", ""), ""),
                "keywords": terms[row.get("professor_id", "")]["keyword"][:8],
                "techniques": terms[row.get("professor_id", "")]["technique"][:5],
                "department": row.get("department", ""),
                # 연구실정보 tab of the master Excel file (graduation time, papers per student ...)
                "lab": labs.get(row.get("professor_id", ""), {}),
                "orcid": row.get("orcid", ""),
                # lab / faculty page (or OpenAlex profile) for the professor card
                "url": row.get("source_url", "") if str(row.get("source_url", "")).startswith(("http://", "https://")) else "",
                "openalex_id": (_openalex_ids(row.get("openalex_id", "")) or [""])[0],
                "identity": row.get("identity_status", "") or "seed",
                "score": _safe_int(row.get("network_score", 0)),
                "collaborator_count": _safe_int(row.get("collaborator_count", 0)),
                "faculty_trainee_count": _safe_int(row.get("faculty_trainee_count", 0)),
                "postdoc_PI_count": _safe_int(row.get("postdoc_PI_count", 0)),
                "advisor_count": _safe_int(row.get("advisor_count", 0)),
                "postdoc_mentor_count": _safe_int(row.get("postdoc_mentor_count", 0)),
            }
        )

    link_records = []
    if not links.empty:
        for _, row in links.iterrows():
            link_records.append(
                {
                    "source": row.get("professor_a_id", ""),
                    "target": row.get("professor_b_id", ""),
                    "type": row.get("relationship_type", ""),
                    "paper_count": _safe_int(row.get("collaboration_paper_count", 0), 0),
                    "verified": row.get("verified", ""),
                    "strength": float(row.get("collaboration_strength", "") or 0),
                    "last_year": _safe_int(row.get("last_year", 0), 0),
                }
            )

    synonyms = _read_csv(DATA_DIR / "keyword_synonyms.csv")
    techniques = _read_csv(DATA_DIR / "technique_terms.csv")
    payload = {
        "nodes": node_records,
        "links": link_records,
        # Korean search words -> English keywords ("T세포" -> "T-Lymphocytes")
        "synonyms": {r["korean"]: [t.strip() for t in r["english"].split("|") if t.strip()]
                     for _, r in synonyms.iterrows()} if not synonyms.empty else {},
        # canonical technique (Korean) -> English label, so both languages search
        "techniques_en": {r["technique"]: r["label_en"] for _, r in techniques.iterrows()}
                         if not techniques.empty else {},
    }
    text = "window.KOREA_BIO_MAP = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n"
    (WEB_DIR / "network-data.js").write_text(text, encoding="utf-8")


def _score_weights() -> tuple[dict[str, int], dict[str, int]]:
    """(PI-side, trainee-side) weights per relationship type.

    Older score_rules.csv files have a single "weight" column: PI side only.
    """
    rules = _read_csv(DATA_DIR / "score_rules.csv")
    pi, trainee = {}, {}
    for _, row in rules.iterrows():
        kind = row.get("relationship_type", "")
        pi[kind] = _safe_int(row.get("pi_weight", "") or row.get("weight", ""))
        trainee[kind] = _safe_int(row.get("trainee_weight", ""))
    return pi, trainee


def build_network(professors: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    auto = _read_csv(OUTPUT_DIR / "relationships_auto.csv", REL_COLUMNS)
    manual = _read_csv(DATA_DIR / "relationships_manual.csv")
    # Lines typed into the master Excel file (관계추가 tab), by name + organisation.
    from_sheet = master_relationships(professors)
    if not from_sheet.empty:
        manual = pd.concat([manual, from_sheet], ignore_index=True, sort=False).fillna("")

    if not manual.empty:
        manual = manual.copy()
        if "collaboration_paper_count" not in manual.columns:
            manual["collaboration_paper_count"] = ""

    lineage = _read_csv(OUTPUT_DIR / "relationships_lineage.csv", REL_COLUMNS)
    # Manual rows come before inferred ones so a hand-checked link wins.
    relationships = pd.concat([auto, manual, lineage], ignore_index=True, sort=False).fillna("")
    if not relationships.empty:
        relationships = relationships.drop_duplicates(
            subset=["professor_a_id", "professor_b_id", "relationship_type"], keep="first")

    valid_professor_ids = set(professors["professor_id"])
    if not relationships.empty:
        relationships = relationships[
            relationships["professor_a_id"].isin(valid_professor_ids)
            & relationships["professor_b_id"].isin(valid_professor_ids)
        ].copy()

    pi_weight, trainee_weight = _score_weights()

    collaborator_sets: dict[str, set[str]] = defaultdict(set)
    faculty_trainees: dict[str, set[str]] = defaultdict(set)   # PI -> students now PIs
    advisors: dict[str, set[str]] = defaultdict(set)           # student -> their PhD advisor
    postdoc_pi: dict[str, set[str]] = defaultdict(set)         # mentor -> former postdocs now PIs
    postdoc_mentors: dict[str, set[str]] = defaultdict(set)    # former postdoc -> mentor

    for _, edge in relationships.iterrows():
        edge_type = edge.get("relationship_type", "")
        a = edge.get("professor_a_id", "")
        b = edge.get("professor_b_id", "")
        # "auto" = inferred lineage with enough evidence (lineage.py)
        verified = str(edge.get("verified", "")).casefold() in {"yes", "true", "1", "verified", "auto"}

        if edge_type == "collaboration":
            collaborator_sets[a].add(b)
            collaborator_sets[b].add(a)
        elif edge_type == "advisor_student" and verified:
            faculty_trainees[a].add(b)
            advisors[b].add(a)
        elif edge_type == "postdoc_mentor" and verified:
            postdoc_pi[a].add(b)
            postdoc_mentors[b].add(a)

    nodes = professors.copy()
    pid = nodes["professor_id"]
    nodes["collaborator_count"] = pid.map(lambda p: len(collaborator_sets[p]))
    nodes["faculty_trainee_count"] = pid.map(lambda p: len(faculty_trainees[p]))
    nodes["advisor_count"] = pid.map(lambda p: len(advisors[p]))
    nodes["postdoc_PI_count"] = pid.map(lambda p: len(postdoc_pi[p]))
    nodes["postdoc_mentor_count"] = pid.map(lambda p: len(postdoc_mentors[p]))
    # PI side and trainee side of a lineage link score differently
    # (data/score_rules.csv: advisor_student PI 10 / student 1, postdoc PI 3 / postdoc 1).
    nodes["network_score"] = (
        nodes["collaborator_count"] * pi_weight.get("collaboration", 0)
        + nodes["faculty_trainee_count"] * pi_weight.get("advisor_student", 0)
        + nodes["advisor_count"] * trainee_weight.get("advisor_student", 0)
        + nodes["postdoc_PI_count"] * pi_weight.get("postdoc_mentor", 0)
        + nodes["postdoc_mentor_count"] * trainee_weight.get("postdoc_mentor", 0)
    )

    links = relationships.copy()
    if not links.empty:
        links["source"] = links["professor_a_id"]
        links["target"] = links["professor_b_id"]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    relationships.to_csv(OUTPUT_DIR / "relationships.csv", index=False)
    nodes.to_csv(OUTPUT_DIR / "network_nodes.csv", index=False)
    links.to_csv(OUTPUT_DIR / "network_links.csv", index=False)
    export_web_data(nodes, links)
    return nodes, links
