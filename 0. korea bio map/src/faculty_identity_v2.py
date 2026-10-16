"""Resolve scraped faculty to ORCID + OpenAlex identities.

Order of evidence, strongest first:

1. ORCID record whose public email equals the faculty-page email.
2. The only ORCID record with this (romanized) name at this institution.
3. The only OpenAlex author with this name whose current affiliation is this
   institution and whose work is mainly Life / Health Sciences. A past
   affiliation alone is not enough (too many same-name mismatches).

OpenAlex IDs listed in data/identity_rejections.csv are never used.

1-2 are "verified": the OpenAlex profile is then fetched by ORCID, which
also collects split OpenAlex profiles of the same person. 3 is "probable".
Anything else (same-name collisions, no candidates) goes to manual_review.
"""
from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

from config import DATA_DIR, OUTPUT_DIR
from name_extraction import (
    normalize_english_name,
    english_matches_korean,
    name_compatible,
    english_query_variants,
    surname_from_email,
)
from openalex_client import OpenAlexClient, OpenAlexUnavailable, normalize_openalex_id
from orcid_client import KoreanORCIDSearch

BATCH_LIMIT = int(os.getenv("IDENTITY_BATCH_LIMIT", "300"))
RETRY_REVIEW = os.getenv("IDENTITY_RETRY_REVIEW", "").strip().casefold() in {"1", "yes", "true"}
IMPORT_STATUSES = {
    s.strip() for s in os.getenv("IMPORT_IDENTITY_STATUSES", "verified,probable").split(",") if s.strip()
}
BIO_DOMAINS = {"Life Sciences", "Health Sciences"}
# Pharmacy and bioengineering faculty publish in these too. Materials Science
# is deliberately excluded: engineers and physicists publish there heavily
# (run #23 accepted same-name EE / physics ORCIDs through it).
BIO_ADJACENT_FIELDS = {"Chemistry", "Chemical Engineering"}
BIO_ADJACENT_SUBFIELDS = {"Biomedical Engineering", "Bioengineering"}
MIN_BIO_SHARE = 0.5
# An ORCID found by name alone may belong to a same-name researcher in
# another field (e.g. an electrical engineer at the same university).
MIN_BIO_SHARE_ORCID = 0.3

# OpenAlex spells institutions out in full; faculty pages often use acronyms.
INSTITUTION_ALIASES = {
    "kaist": "korea advanced institute science technology",
    "postech": "pohang university science technology",
    "unist": "ulsan national institute science technology",
    "gist": "gwangju institute science technology",
    "dgist": "daegu gyeongbuk institute science technology",
    "skku": "sungkyunkwan",
    "snu": "seoul",
}

COLUMNS = [
    "source_id", "name", "name_ko", "name_en", "title", "university", "department", "email",
    "profile_url", "source_page", "orcid", "orcid_reason", "openalex_id", "openalex_ids",
    "openalex_name", "openalex_affiliation", "primary_field", "works_count",
    "identity_confidence", "identity_status", "identity_reason",
]


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str).fillna("")


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", (value or "").casefold())


def _name_similarity(a: str, b: str) -> float:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _tokens(value: str) -> set[str]:
    stop = {"university", "college", "school", "hospital", "institute", "department", "national",
            "research", "science", "technology", "and", "of", "the"}
    text = (value or "").casefold()
    for short, full in INSTITUTION_ALIASES.items():
        if re.search(rf"\b{short}\b", text):
            text += " " + full
    return {t for t in re.findall(r"[a-z0-9가-힣]+", text) if len(t) >= 3 and t not in stop}


_INST_STOP = {"university", "of", "the", "and", "at", "in", "campus"}


def _inst_tokens(value: str) -> set[str]:
    text = (value or "").casefold()
    for short, full in INSTITUTION_ALIASES.items():
        text = re.sub(rf"\b{short}\b", full, text)
    return {t for t in re.findall(r"[a-z0-9]+", text) if t not in _INST_STOP and len(t) >= 2}


def _known_universities() -> list[set[str]]:
    path = DATA_DIR / "universities_seed.csv"
    if not path.exists():
        return []
    names = pd.read_csv(path, dtype=str).fillna("")["university"]
    return [t for t in (_inst_tokens(n) for n in names) if t]


KNOWN_UNIVERSITIES = _known_universities()


def institution_matches(university: str, institution: str) -> bool:
    """True if an OpenAlex institution name is this university (or its hospital / college).

    Every word of the university must appear ("Seoul National University"
    needs both seoul and national, so "University of Seoul" fails), and the
    institution must not be a different, longer-named university that
    contains those words (Seoul National University of Science and
    Technology is not Seoul National University; KAIST is not Korea
    University). Sharing one word such as "seoul" is not enough: run #26
    accepted a Korea University endocrinologist for an SNU pharmacist that way.
    """
    target, inst = _inst_tokens(university), _inst_tokens(institution)
    if not target or not target <= inst:
        return False
    return not any(target < other <= inst for other in KNOWN_UNIVERSITIES)


def _matches_any(university: str, institutions: str) -> bool:
    return any(institution_matches(university, x) for x in institutions.split(";") if x.strip())


def load_rejections() -> set[str]:
    """OpenAlex IDs a person checked and found to be someone else (data/identity_rejections.csv)."""
    path = DATA_DIR / "identity_rejections.csv"
    if not path.exists():
        return set()
    df = pd.read_csv(path, dtype=str).fillna("")
    return {normalize_openalex_id(x) for x in df.get("openalex_id", []) if x.strip()}


def _affiliation(author: dict) -> str:
    institutions = author.get("last_known_institutions") or []
    return "; ".join(
        x.get("display_name", "") for x in institutions
        if isinstance(x, dict) and x.get("display_name")
    )


def _all_affiliations(author: dict) -> str:
    names = [_affiliation(author)]
    for aff in author.get("affiliations") or []:
        inst = aff.get("institution") if isinstance(aff, dict) else None
        if isinstance(inst, dict) and inst.get("display_name"):
            names.append(inst["display_name"])
    return "; ".join(n for n in names if n)


def _orcid(author: dict) -> str:
    value = str(author.get("orcid", "") or "").strip()
    return value.rstrip("/").split("/")[-1] if value else ""


def _is_bio_topic(topic: dict) -> bool:
    domain = (topic.get("domain") or {}).get("display_name", "")
    field = (topic.get("field") or {}).get("display_name", "")
    subfield = (topic.get("subfield") or {}).get("display_name", "")
    return domain in BIO_DOMAINS or field in BIO_ADJACENT_FIELDS or subfield in BIO_ADJACENT_SUBFIELDS


def _bio_share(*authors: dict) -> float:
    """Share of topic counts in bio / bio-adjacent fields (1.0 if unknown)."""
    total = bio = 0
    for author in authors:
        for topic in author.get("topics") or []:
            count = int(topic.get("count", 1) or 1)
            total += count
            if _is_bio_topic(topic):
                bio += count
    return bio / total if total else 1.0


def _primary_field(author: dict) -> str:
    topics = author.get("topics") or []
    if not topics:
        return ""
    return str(((topics[0].get("subfield") or {}).get("display_name", "")))


def _key(row: pd.Series | dict) -> str:
    return "|".join([
        str(row.get("source_id", "")).strip().casefold(),
        str(row.get("name", "")).strip().casefold(),
        str(row.get("university", "")).strip().casefold(),
    ])


def _person_key(name_ko: str, name_en: str, university: str, email: str) -> str:
    """Same person listed by two departments of one university."""
    return "|".join([email.casefold() or _norm_name(name_ko or name_en), _norm_name(university)])


def _name_matches(author: dict, name_ko: str, name_en: str) -> tuple[bool, float]:
    names = [str(author.get("display_name", ""))] + [
        str(x) for x in author.get("display_name_alternatives") or []
    ]
    if name_ko:
        if name_ko in names:
            return True, 1.0
        if any(english_matches_korean(n, name_ko) for n in names):
            return True, 0.95
    if name_en:
        sim = max(_name_similarity(name_en, n) for n in names)
        return sim >= 0.85, sim
    return False, 0.0


def _search_queries(name_ko: str, name_en: str, name: str, surname_hint: str = "") -> list[str]:
    """At most 2 OpenAlex searches per person (each search is billed)."""
    if name_en:
        return [name_en]
    queries = english_query_variants(name_ko, surname_hint) if name_ko else []
    return queries[:2] or ([name] if name else [])


# Same-name authors at one school: the largest profile wins when it is this much larger.
DOMINANT_MIN_WORKS = 20
DOMINANT_RATIO = 4


def _best_openalex(
    client: OpenAlexClient, name_ko: str, name_en: str, university: str, fallback_name: str = "",
    surname_hint: str = "",
) -> tuple[dict | None, str]:
    """Pick the OpenAlex author whose romanized name and institution both fit.

    OpenAlex display names are English, so searching with a Hangul name
    returns nothing useful: search with the page's English name, or with
    romanization variants of the Korean name when the page has none.
    """
    rejected = load_rejections()
    name_en = normalize_english_name(name_en)
    candidates: dict[str, dict] = {}
    for query in _search_queries(name_ko, name_en, fallback_name, surname_hint):
        # OpenAlexUnavailable propagates so the caller can stop and retry later.
        data = client._get("authors", {"search": query, "per-page": 25})
        for author in data.get("results", []) or []:
            if normalize_openalex_id(str(author.get("id", ""))) in rejected:
                continue
            candidates.setdefault(str(author.get("id", "")), author)

    if not candidates:
        return None, "no OpenAlex candidates"

    passing: list[tuple[int, int, float, dict]] = []
    name_hits = past_only = 0
    for author in candidates.values():
        ok, sim = _name_matches(author, name_ko, name_en)
        if not ok:
            continue
        name_hits += 1
        current = int(_matches_any(university, _affiliation(author)))
        ever = int(current or _matches_any(university, _all_affiliations(author)))
        if university and not current:
            # A past affiliation alone is too weak by name: OpenAlex lists
            # every institution a co-author list ever touched, and run #26
            # matched 44 people that way, many of them wrong.
            past_only += bool(ever)
            continue
        passing.append((current, ever, sim, author))

    if not passing:
        if past_only:
            return None, f"{past_only} name match(es) at {university} only in past affiliations"
        if name_hits:
            return None, f"{name_hits} name match(es) but none at {university}"
        return None, "no romanization-consistent name match"

    passing.sort(key=lambda x: (x[0] > 0, x[1], x[2], x[3].get("works_count", 0)), reverse=True)
    current, ever, sim, best = passing[0]
    same_level = [p for p in passing if (p[0] > 0) == (current > 0) and p[1] == ever]
    if len(same_level) > 1:
        # Same name, same school: a bio professor vs. an engineer/chemist is
        # the most common case, so keep only mainly-bio authors.
        bio = [p for p in same_level if _bio_share(p[3]) >= MIN_BIO_SHARE]
        if len(bio) != 1:
            # One profile far larger than the rest: the others are mostly split-off fragments
            # of the same person (전누리 236 / 39 / 30 papers, 신상도 589 / 12). Taken, and
            # logged as "dominant" so the pick can be reviewed.
            by_size = sorted(same_level, key=lambda p: int(p[3].get("works_count", 0) or 0), reverse=True)
            top, second = (int(p[3].get("works_count", 0) or 0) for p in by_size[:2])
            if top >= DOMINANT_MIN_WORKS and top >= DOMINANT_RATIO * max(second, 1):
                current, ever, sim, best = by_size[0]
                return best, (f"name match {sim:.2f}; dominant profile among {len(same_level)} "
                              f"same-name authors at {university} ({top} vs {second} works)")
            return None, f"ambiguous: {len(same_level)} same-name authors at {university}"
        current, ever, sim, best = bio[0]
        where = "current" if current else "past"
        return best, f"name match {sim:.2f}; {where} institution match; only bio author among {len(same_level)}"
    where = "current" if current else "past"
    return best, f"name match {sim:.2f}; {where} institution match"


def resolve_person(
    client: OpenAlexClient, orcid_search: KoreanORCIDSearch | None, person: dict,
) -> dict[str, str]:
    name = str(person.get("name", "")).strip()
    name_ko = str(person.get("name_ko", "")).strip()
    name_en = str(person.get("name_en", "")).strip()
    if not name_ko and not name_en:
        # Rows from older scraper output only have "name".
        name_ko, name_en = (name, "") if re.search(r"[가-힣]", name) else ("", name)
    university = str(person.get("university", "")).strip()
    email = str(person.get("email", "")).strip()
    surname_hint = surname_from_email(name_ko, email) if name_ko else ""

    status, confidence = "manual_review", "low"
    orcid = orcid_reason = ""
    authors: list[dict] = []
    reasons: list[str] = []

    if orcid_search is not None:
        cand, orcid_reason = orcid_search.find(name_ko, name_en, university, email, surname_hint)
        if cand:
            orcid = cand["orcid"]
            status, confidence = "verified", "high"
            rejected = load_rejections()
            authors = [a for a in client.authors_by_orcid(orcid)
                       if normalize_openalex_id(str(a.get("id", ""))) not in rejected]
            reasons.append(orcid_reason)
            reasons.append(f"{len(authors)} OpenAlex profile(s) by ORCID" if authors else "no OpenAlex profile for ORCID")
            share = _bio_share(*authors)
            main_topics = max(authors, key=lambda a: int(a.get("works_count", 0) or 0)).get("topics") if authors else []
            top_is_bio = not main_topics or _is_bio_topic(main_topics[0])
            if authors and "email" not in orcid_reason and (share < MIN_BIO_SHARE_ORCID or not top_is_bio):
                field = _primary_field(max(authors, key=lambda a: int(a.get("works_count", 0) or 0)))
                reasons.append(f"ORCID holder publishes mainly in {field or 'non-bio fields'} "
                               f"({share:.0%} bio): likely a same-name researcher")
                status, confidence, orcid, authors = "manual_review", "low", "", []

    if not authors and status == "verified":
        # Many OpenAlex profiles have no ORCID attached; fall back to name +
        # institution, but never accept a profile tied to a different ORCID.
        author, reason = _best_openalex(client, name_ko, name_en, university, name, surname_hint)
        if author and _orcid(author) in {"", orcid}:
            authors = [author]
            reasons.append(f"OpenAlex by name: {reason}")
    elif not authors:
        author, reason = _best_openalex(client, name_ko, name_en, university, name, surname_hint)
        reasons.append(reason)
        if author and _bio_share(author) < MIN_BIO_SHARE:
            reasons.append(f"not mainly bio ({_bio_share(author):.0%})")
            author = None
        if author:
            authors = [author]
            oa_orcid = _orcid(author)
            ambiguous_orcids = orcid_reason.startswith("ambiguous ORCID") and oa_orcid and oa_orcid in orcid_reason
            if ambiguous_orcids:
                # ORCID lists several same-name people at this school; OpenAlex
                # picked exactly one of them by name + institution + field.
                orcid, status, confidence = oa_orcid, "verified", "high"
                reasons.append("OpenAlex choice is one of the institution's ORCID records")
            else:
                orcid = oa_orcid
                status, confidence = "probable", "medium"
        elif orcid_reason:
            reasons.insert(0, orcid_reason)

    if authors and name_ko:
        # Last guard: OpenAlex profiles sometimes merge several people, and an
        # alias or an ORCID link can point at a profile named after someone
        # else (김건 -> "Kyung-Hee Kim"). Keep only profiles whose display name
        # is this person.
        kept = [a for a in authors if name_compatible(str(a.get("display_name", "")), name_ko)]
        if not kept:
            shown = str(authors[0].get("display_name", ""))
            reasons.append(f"OpenAlex profile name '{shown}' does not match {name_ko}")
            status, confidence, authors = "manual_review", "low", []
            if not orcid_reason.startswith("ORCID public email"):
                orcid = ""
        elif len(kept) < len(authors):
            reasons.append(f"dropped {len(authors) - len(kept)} OpenAlex profile(s) with another name")
            authors = kept

    authors.sort(key=lambda a: int(a.get("works_count", 0) or 0), reverse=True)
    main = authors[0] if authors else {}
    return {
        "source_id": person.get("source_id", ""),
        "name": name,
        "name_ko": name_ko,
        "name_en": name_en or str(main.get("display_name", "")),
        "title": person.get("title", ""),
        "university": university,
        "department": person.get("department", ""),
        "email": email,
        "profile_url": person.get("profile_url", ""),
        "source_page": person.get("source_page", ""),
        "orcid": orcid,
        "orcid_reason": orcid_reason,
        "openalex_id": normalize_openalex_id(str(main.get("id", ""))),
        "openalex_ids": ";".join(normalize_openalex_id(str(a.get("id", ""))) for a in authors),
        "openalex_name": str(main.get("display_name", "")),
        "openalex_affiliation": _affiliation(main),
        "primary_field": _primary_field(main),
        "works_count": str(sum(int(a.get("works_count", 0) or 0) for a in authors)),
        "identity_confidence": confidence,
        "identity_status": status,
        "identity_reason": "; ".join(r for r in reasons if r),
    }


def resolve_faculty_identities_v2(client: OpenAlexClient | None = None,
                                  orcid_search: KoreanORCIDSearch | None = None) -> pd.DataFrame:
    faculty_path = OUTPUT_DIR / "faculty_directory_v2.csv"
    out_path = OUTPUT_DIR / "faculty_identity_v2.csv"
    faculty = _read_csv(faculty_path)
    existing = _read_csv(out_path)

    if faculty.empty:
        out = pd.DataFrame(columns=COLUMNS)
        out.to_csv(out_path, index=False, encoding="utf-8-sig")
        return out

    done: set[str] = set()
    if not existing.empty:
        for _, row in existing.iterrows():
            transient = "unavailable" in str(row.get("identity_reason", ""))
            retry = RETRY_REVIEW and row.get("identity_status", "") == "manual_review"
            if not transient and not retry:
                done.add(_key(row))
    pending = faculty[faculty.apply(lambda r: _key(r) not in done, axis=1)].head(BATCH_LIMIT)
    print(f"Identity v2: {len(done)} done / {len(faculty)} total; processing {len(pending)}", flush=True)

    if pending.empty:
        return existing if not existing.empty else pd.DataFrame(columns=COLUMNS)

    client = client or OpenAlexClient()
    orcid_search = orcid_search or KoreanORCIDSearch()
    rows: list[dict[str, str]] = []
    resolved_people: dict[str, dict[str, str]] = {}

    for i, (_, person) in enumerate(pending.iterrows(), start=1):
        p = person.to_dict()
        pkey = _person_key(p.get("name_ko", "") or p.get("name", ""), p.get("name_en", ""),
                           p.get("university", ""), p.get("email", ""))
        print(f"[{i}/{len(pending)}] {p.get('name', '')} | {p.get('university', '')}", flush=True)
        if pkey in resolved_people:
            # Listed by another department too; reuse the lookup.
            row = dict(resolved_people[pkey])
            row.update({k: p.get(k, "") for k in ("source_id", "department", "profile_url", "source_page", "title")})
        else:
            try:
                row = resolve_person(client, orcid_search, p)
            except OpenAlexUnavailable as exc:
                # Keep what is done; the remaining rows are retried next run.
                print(f"Stopping identity batch early: {exc}", flush=True)
                break
            resolved_people[pkey] = row
        rows.append(row)

    batch = pd.DataFrame(rows, columns=COLUMNS)
    out = pd.concat([existing, batch], ignore_index=True) if not existing.empty else batch
    out = out.drop_duplicates(subset=["source_id", "name", "university"], keep="last")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"Saved {len(out)} identity v2 rows", flush=True)
    for status, count in out["identity_status"].value_counts().items():
        print(f"  {status}: {count}", flush=True)
    return out


def import_verified_faculty_v2() -> int:
    """Add verified / probable faculty to professors_seed.csv, one row per person.

    A person is keyed by ORCID, else by OpenAlex ID; a professor listed by
    two departments is added once with both departments.
    """
    from pipeline import PROFESSOR_COLUMNS

    identity = _read_csv(OUTPUT_DIR / "faculty_identity_v2.csv")
    professors_path = DATA_DIR / "professors_seed.csv"
    professors = _read_csv(professors_path)
    if identity.empty:
        return 0
    for column in PROFESSOR_COLUMNS:
        if column not in professors.columns:
            professors[column] = ""

    existing_openalex = {
        normalize_openalex_id(x)
        for ids in professors["openalex_id"] for x in str(ids).split(";") if normalize_openalex_id(x)
    }
    existing_orcid = {str(x).strip() for x in professors["orcid"] if str(x).strip()}
    # Hand-entered seed rows often have no OpenAlex ID / ORCID yet, so also
    # treat the same Korean name at the same university as the same person.
    def _name_key(name_ko: str, university: str) -> str:
        return f"{str(name_ko).strip()}|{str(university).split(';')[0].strip().casefold()}"
    existing_names = {
        _name_key(n, u) for n, u in zip(professors["name_ko"], professors["university"]) if str(n).strip()
    }
    nums = []
    for pid in professors.get("professor_id", []):
        m = re.fullmatch(r"P(\d+)", str(pid).strip(), flags=re.I)
        if m:
            nums.append(int(m.group(1)))
    next_num = max(nums) + 1 if nums else 1

    rejected = load_rejections()
    new_rows: dict[str, dict[str, str]] = {}
    for _, row in identity.iterrows():
        if row.get("identity_status", "") not in IMPORT_STATUSES:
            continue
        ids = [normalize_openalex_id(x) for x in str(row.get("openalex_ids", "") or row.get("openalex_id", "")).split(";")]
        ids = [x for x in ids if x and x not in rejected]
        orcid = str(row.get("orcid", "")).strip()
        if not ids and not orcid:
            continue
        person = f"orcid:{orcid}" if orcid else (f"openalex:{ids[0]}" if ids else "")
        if not person:
            continue
        if person in new_rows:
            dept = row.get("department", "")
            if dept and dept not in new_rows[person]["department"]:
                new_rows[person]["department"] += f"; {dept}"
            continue
        if (orcid and orcid in existing_orcid) or any(x in existing_openalex for x in ids):
            continue
        name_ko = row.get("name_ko", "") or (row.get("name", "") if re.search(r"[가-힣]", row.get("name", "")) else "")
        if name_ko and _name_key(name_ko, row.get("university", "")) in existing_names:
            continue
        if name_ko:
            existing_names.add(_name_key(name_ko, row.get("university", "")))
        new_rows[person] = {
            "professor_id": f"P{next_num:04d}",
            "name_ko": name_ko,
            "name_en": row.get("name_en", "") or row.get("openalex_name", ""),
            "university": row.get("university", ""),
            "department": row.get("department", ""),
            "primary_field": row.get("primary_field", ""),
            "openalex_id": ";".join(ids),
            "source_url": row.get("profile_url", "") or row.get("source_page", ""),
            "orcid": orcid,
            "identity_status": row.get("identity_status", ""),
        }
        next_num += 1

    if new_rows:
        professors = pd.concat([professors, pd.DataFrame(list(new_rows.values()))], ignore_index=True)
        professors = professors[PROFESSOR_COLUMNS]
        professors.to_csv(professors_path, index=False, encoding="utf-8-sig")
    return len(new_rows)


if __name__ == "__main__":
    df = resolve_faculty_identities_v2()
    print(f"Faculty identity v2 rows: {len(df)}")
