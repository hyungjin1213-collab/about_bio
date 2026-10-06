"""Add bio researchers by OpenAlex institution, for organisations the crawler cannot read.

Some university and institute sites block crawlers (robots.txt), time out,
or build their staff lists with JavaScript; the site crawler then finds no
faculty page. For those, ask OpenAlex directly: authors whose current
affiliation is that institution (or its hospital), who publish mainly in
life / health sciences, and who look like PIs (enough papers, h-index,
still active). They are added to professors_seed.csv with their OpenAlex
ID, so identity is exact; the Korean name stays empty until someone adds it.

Organisations handled: every research institute in universities_seed.csv,
and universities whose crawl found no faculty page. A few per run
(INSTITUTION_DISCOVERY_LIMIT), logged in data/institution_discovery_log.csv.
"""
from __future__ import annotations

import os
import re
from datetime import date

import pandas as pd

from config import DATA_DIR
from faculty_identity_v2 import _inst_tokens, _primary_field, load_rejections
from name_extraction import ALL_ROMANIZATIONS, english_matches_korean, normalize_english_name
from openalex_client import OpenAlexClient, OpenAlexUnavailable, normalize_openalex_id

INSTITUTION_DISCOVERY_LIMIT = int(os.getenv("INSTITUTION_DISCOVERY_LIMIT", "8"))
MAX_PER_INSTITUTION = int(os.getenv("INSTITUTION_MAX_RESEARCHERS", "60"))
MIN_WORKS = 30
MIN_H_INDEX = 12
MIN_RECENT_WORKS = 3          # in the last 3 years: still active here
MIN_BIO_SHARE = 0.5
# Company scientists publish far less than academics: lower bars, fewer people.
THRESHOLDS = {
    "default": {"works": MIN_WORKS, "h": MIN_H_INDEX, "recent": MIN_RECENT_WORKS, "max": None},
    "company": {"works": 10, "h": 5, "recent": 1, "max": 30},
}
AUTHOR_PAGES = 2              # 200 authors per page, most prolific first
LOG_COLUMNS = ["university", "institution_ids", "institution_names", "authors_seen", "added", "run_at", "note"]
AUTHOR_FIELDS = ("id,display_name,orcid,works_count,summary_stats,counts_by_year,"
                 "last_known_institutions,affiliations,topics")
# Words an institution's name may add to the organisation's own name: its
# hospital / medical school. Anything else is another organisation (Korea
# Institute of *Ocean* Science and Technology is not KIST).
SAME_ORG_EXTRA_WORDS = {"hospital", "hospitals", "medical", "center", "centre", "medicine", "college", "school",
                        "graduate", "health", "system", "campus", "clinic", "south", "korea", "republic"}
LIFE_DOMAINS = {"Life Sciences", "Health Sciences"}
# Clinical specialties: researchers at a research institute that is not a
# hospital rarely have one as their main subfield; such profiles were
# merged same-name clinicians in run #34.
CLINICAL_SUBFIELDS = {
    "Surgery", "Pulmonary and Respiratory Medicine", "Dermatology", "Otorhinolaryngology", "Hematology",
    "Rheumatology", "Radiology, Nuclear Medicine and Imaging", "Cardiology and Cardiovascular Medicine",
    "Pediatrics, Perinatology and Child Health", "Oral Surgery", "Reproductive Medicine", "Hepatology",
    "Obstetrics and Gynecology", "Orthopedics and Sports Medicine", "Ophthalmology", "Urology", "Nephrology",
    "Gastroenterology", "Anesthesiology and Pain Medicine", "Emergency Medicine", "Critical Care and Intensive Care Medicine",
    "Neurology", "Psychiatry and Mental health", "Geriatrics and Gerontology", "Endocrinology, Diabetes and Metabolism",
    "Transplantation", "Otolaryngology", "Dentistry", "Periodontics", "Orthodontics",
}
LIFE_SUBFIELDS = {"Biomedical Engineering", "Bioengineering"}
KOREAN_SURNAMES = {r.casefold() for rs in ALL_ROMANIZATIONS.values() for r in rs}
AFFILIATION_YEARS = 2          # years at the organisation within the last 4


def _read(path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str).fillna("")


def targets(universities: pd.DataFrame, discovery_log: pd.DataFrame, done: set[str]) -> list[dict]:
    """Institutes, then universities the crawler found nothing at; skipping ones already handled."""
    crawled_pages: dict[str, int] = {}
    for _, r in discovery_log.iterrows():
        try:
            n = int(r.get("pages_found", "0") or 0)
        except ValueError:
            n = 0
        crawled_pages[r["university"]] = max(crawled_pages.get(r["university"], 0), n)
    out = []
    for _, u in universities.iterrows():
        name, kind = u["university"], (u.get("org_type", "") or "university")
        if name in done or kind == "hospital":
            continue
        if kind in ("institute", "company") or (name in crawled_pages and crawled_pages[name] == 0):
            out.append({"university": name, "org_type": kind})
    out.sort(key=lambda t: {"institute": 0, "company": 1}.get(t["org_type"], 2))   # institutes, companies, then universities
    return out


def find_institutions(client: OpenAlexClient, name: str, org_type: str = "") -> list[dict]:
    """OpenAlex institutions that are this organisation or its hospital / medical centre (max 3)."""
    data = client._get("institutions", {"search": name, "filter": "country_code:KR", "per-page": 15})
    hits = [i for i in data.get("results", []) or []
            if str(i.get("country_code", "KR")).upper() == "KR" and same_organisation(name, i.get("display_name", ""))]
    if org_type == "company":
        # "university" is not a distinguishing word for same_organisation, so a
        # company would also match a university of the same name (Yuhan ->
        # Yuhan University).
        hits = [i for i in hits if str(i.get("type", "company")) == "company"
                and "university" not in str(i.get("display_name", "")).casefold()]
    return hits[:3]


def same_organisation(name: str, institution: str) -> bool:
    """Every word of the name, and nothing else but hospital / medical-school words."""
    target, inst = _inst_tokens(name), _inst_tokens(institution)
    return bool(target) and target <= inst and (inst - target) <= SAME_ORG_EXTRA_WORDS


def life_science_share(author: dict) -> float:
    """Share of topics in life / health sciences only (chemistry does not count here)."""
    total = bio = 0
    for t in author.get("topics") or []:
        n = int(t.get("count", 1) or 1)
        total += n
        if (t.get("domain") or {}).get("display_name", "") in LIFE_DOMAINS or \
                (t.get("subfield") or {}).get("display_name", "") in LIFE_SUBFIELDS:
            bio += n
    return bio / total if total else 0.0


def korean_name(display: str) -> bool:
    """Romanized Korean name, given names first (OpenAlex order): a Korean surname as the last word.

    The first word is not checked: Japanese names such as "Kan Yonemori"
    start with a syllable that is also a Korean surname.
    """
    words = [w for w in re.split(r"[\s.,]+", normalize_english_name(display)) if w]
    return len(words) >= 2 and words[-1].casefold() in KOREAN_SURNAMES


def settled_at(author: dict, inst_ids: set[str], now_year: int) -> bool:
    """Affiliated with the organisation in at least AFFILIATION_YEARS of the last 4 years.

    last_known_institutions alone is noisy: one co-affiliated paper can put a
    hospital surgeon or a foreign professor "at" a research institute.
    """
    years: set[int] = set()
    elsewhere: dict[str, set[int]] = {}
    for aff in author.get("affiliations") or []:
        inst = normalize_openalex_id(str((aff.get("institution") or {}).get("id", "")))
        recent = {int(y) for y in aff.get("years") or [] if int(y) >= now_year - 3}
        if inst in inst_ids:
            years |= recent
        elif inst:
            elsewhere[inst] = recent
    # The organisation must also be the main affiliation: OpenAlex merges
    # same-name people, so a hospital surgeon's profile can carry a few
    # institute years next to four hospital years (run #34).
    busiest_elsewhere = max((len(y) for y in elsewhere.values()), default=0)
    return len(years) >= AFFILIATION_YEARS and len(years) >= busiest_elsewhere


def main_topic_ok(author: dict, clinical_ok: bool) -> bool:
    """The author's top topic is life / health science (and not clinical, at a non-hospital institute)."""
    topics = author.get("topics") or []
    if not topics:
        return False
    top = topics[0]
    domain = (top.get("domain") or {}).get("display_name", "")
    subfield = (top.get("subfield") or {}).get("display_name", "")
    if domain not in LIFE_DOMAINS and subfield not in LIFE_SUBFIELDS:
        return False
    return clinical_ok or subfield not in CLINICAL_SUBFIELDS


def is_pi_like(author: dict, now_year: int, org_type: str = "") -> tuple[bool, str]:
    bar = THRESHOLDS.get(org_type, THRESHOLDS["default"])
    works = int(author.get("works_count", 0) or 0)
    h = int((author.get("summary_stats") or {}).get("h_index", 0) or 0)
    recent = sum(int(c.get("works_count", 0) or 0) for c in author.get("counts_by_year") or []
                 if int(c.get("year", 0) or 0) >= now_year - 2)
    if not author.get("topics"):
        return False, "no topics"
    share = life_science_share(author)
    if works < bar["works"] or h < bar["h"]:
        return False, f"{works} works, h {h}"
    if recent < bar["recent"]:
        return False, f"{recent} recent works"
    if share < MIN_BIO_SHARE:
        return False, f"{share:.0%} bio"
    return True, ""


def _authors_at(client: OpenAlexClient, insts: list[dict], min_works: int = MIN_WORKS) -> tuple[list[dict], int]:
    """Authors whose current affiliation is one of these institutions, most prolific first."""
    inst_filter = "|".join(normalize_openalex_id(i["id"]) for i in insts)
    candidates: list[dict] = []
    for page in range(1, AUTHOR_PAGES + 1):
        data = client._get("authors", {
            "filter": f"last_known_institutions.id:{inst_filter},works_count:>{min_works - 1}",
            "sort": "works_count:desc", "per-page": 200, "page": page, "select": AUTHOR_FIELDS,
        })
        results = data.get("results", []) or []
        candidates += results
        if len(results) < 200:
            break
    return candidates, len(candidates)


def _name_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", normalize_english_name(name).casefold())


def discover_by_institution(client: OpenAlexClient | None = None, now_year: int | None = None) -> int:
    """Add PI-like bio researchers at uncrawlable organisations to professors_seed.csv."""
    from pipeline import PROFESSOR_COLUMNS

    now_year = now_year or date.today().year
    universities = _read(DATA_DIR / "universities_seed.csv")
    log_path = DATA_DIR / "institution_discovery_log.csv"
    log = _read(log_path)
    done = set(log["university"]) if not log.empty else set()
    todo = targets(universities, _read(DATA_DIR / "discovery_log.csv"), done)[:INSTITUTION_DISCOVERY_LIMIT]
    if not todo:
        return 0

    seed_path = DATA_DIR / "professors_seed.csv"
    seed = _read(seed_path)
    for c in PROFESSOR_COLUMNS:
        if c not in seed.columns:
            seed[c] = ""
    known_ids = {normalize_openalex_id(x) for ids in seed["openalex_id"] for x in str(ids).split(";") if x}
    known_orcid = {o for o in seed["orcid"] if o}
    rejected = load_rejections()
    nums = [int(m.group(1)) for p in seed["professor_id"] if (m := re.fullmatch(r"P(\d+)", str(p)))]
    next_num = max(nums, default=0) + 1

    client = client or OpenAlexClient()
    new_rows, log_rows = [], []
    for t in todo:
        name = t["university"]
        print(f"[institution] {name}", flush=True)
        try:
            insts = find_institutions(client, name, t["org_type"])
            bar = THRESHOLDS.get(t["org_type"], THRESHOLDS["default"])
            candidates, seen = _authors_at(client, insts, bar["works"]) if insts else ([], 0)
        except OpenAlexUnavailable as exc:
            # Keep what is done; this organisation is retried next run.
            print(f"  stopping institution discovery: {exc}", flush=True)
            break
        if not insts:
            log_rows.append({"university": name, "authors_seen": "0", "added": "0", "note": "no OpenAlex institution"})
            continue
        inst_filter = "|".join(normalize_openalex_id(i["id"]) for i in insts)
        inst_ids = {normalize_openalex_id(i["id"]) for i in insts}
        # Clinicians belong at universities and hospitals (National Cancer Center Hospital), not at
        # a research institute without one.
        clinical_ok = t["org_type"] not in ("institute", "company") or any(
            "hospital" in i.get("display_name", "").casefold() for i in insts)
        same_org = seed[seed["university"].str.split(";").str[0].str.strip().str.casefold() == name.casefold()]
        added = 0
        for a in candidates:
            if added >= (bar["max"] or MAX_PER_INSTITUTION):
                break
            aid = normalize_openalex_id(a.get("id", ""))
            orcid = str(a.get("orcid", "") or "").rstrip("/").split("/")[-1]
            if not aid or aid in known_ids or aid in rejected or (orcid and orcid in known_orcid):
                continue
            ok, _ = is_pi_like(a, now_year, t["org_type"])
            display = str(a.get("display_name", ""))
            # Korean organisations: romanized Korean names only (OpenAlex mixes in
            # same-name institutions abroad, e.g. Japan's National Cancer Center).
            # Foreign PIs can be added by hand.
            if not ok or not korean_name(display) or not settled_at(a, inst_ids, now_year) \
                    or not main_topic_ok(a, clinical_ok):
                continue
            # Already in the DB under a Korean name without an ID (hand-entered rows).
            if any(english_matches_korean(display, k) for k in same_org["name_ko"] if k) or \
                    any(_name_key(display) == _name_key(e) for e in same_org["name_en"] if e):
                continue
            new_rows.append({
                "professor_id": f"P{next_num:04d}", "name_ko": "", "name_en": display,
                "university": name, "department": "", "primary_field": _primary_field(a),
                "openalex_id": aid, "source_url": f"https://openalex.org/{aid}", "orcid": orcid,
                "identity_status": "openalex_institution", "scopus_id": "",
            })
            known_ids.add(aid)
            if orcid:
                known_orcid.add(orcid)
            next_num += 1
            added += 1
        log_rows.append({"university": name, "institution_ids": inst_filter,
                         "institution_names": "; ".join(i.get("display_name", "") for i in insts),
                         "authors_seen": str(seen), "added": str(added), "note": ""})
        print(f"  -> {added} researchers added ({seen} authors seen)", flush=True)

    if new_rows:
        seed = pd.concat([seed, pd.DataFrame(new_rows)], ignore_index=True)[PROFESSOR_COLUMNS]
        seed.to_csv(seed_path, index=False, encoding="utf-8-sig")
    today = date.today().isoformat()
    new_log = pd.DataFrame([{**r, "run_at": today} for r in log_rows]).reindex(columns=LOG_COLUMNS).fillna("")
    pd.concat([log, new_log], ignore_index=True).reindex(columns=LOG_COLUMNS).to_csv(log_path, index=False)
    return len(new_rows)
