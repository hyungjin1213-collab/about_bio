"""Fill missing Korean names (name_ko) in professors_seed.csv from reliable sources only.

Researchers added from OpenAlex (institutes, companies, uncrawlable
universities) arrive with an English name only. A romanized name cannot be
turned back into Hangul reliably (Kim Ji-won: 김지원 / 김지원 / 김지운 ...), so
nothing is guessed. A Korean name is taken only when it is written down
somewhere for this person, and its romanization fits the English name:

1. the same person on a crawled faculty list (same OpenAlex ID, or same
   organisation and a unique romanization match);
2. OpenAlex's alternative spellings of the author ("김빛내리");
3. the person's ORCID record (other names / credit name).

Every fill is logged in output/korean_name_fills.csv.
"""
from __future__ import annotations

import os
import re

import pandas as pd
import requests

from config import DATA_DIR, OUTPUT_DIR
from name_extraction import english_matches_korean, is_korean_person_name, normalize_english_name
from openalex_client import OpenAlexClient, OpenAlexUnavailable, normalize_openalex_id

ORCID_LOOKUP_LIMIT = int(os.getenv("ORCID_NAME_LOOKUP_LIMIT", "300"))
BATCH = 50
HANGUL_NAME = re.compile(r"^[가-힣]{2,4}$")


def _read(path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str).fillna("")


def pick_korean(candidates: list[str], name_en: str) -> str:
    """The one Hangul name among the candidates whose romanization fits name_en ("" if none or ambiguous)."""
    fits = {c.replace(" ", "") for c in candidates
            if HANGUL_NAME.match(c.replace(" ", "")) and is_korean_person_name(c.replace(" ", ""))
            and english_matches_korean(normalize_english_name(name_en), c.replace(" ", ""))}
    return fits.pop() if len(fits) == 1 else ""


def from_faculty_lists(seed: pd.DataFrame) -> dict[str, tuple[str, str]]:
    """{professor_id: (name_ko, source)} from crawled faculty pages and their identity rows."""
    out: dict[str, tuple[str, str]] = {}
    ident = _read(OUTPUT_DIR / "faculty_identity_v2.csv")
    by_id: dict[str, set[str]] = {}
    if not ident.empty:
        for _, r in ident.iterrows():
            ko = r.get("name_ko", "") or (r.get("name", "") if re.search(r"[가-힣]", r.get("name", "")) else "")
            for oid in str(r.get("openalex_ids", "") or r.get("openalex_id", "")).split(";"):
                if ko and normalize_openalex_id(oid):
                    by_id.setdefault(normalize_openalex_id(oid), set()).add(ko)
    directory = _read(OUTPUT_DIR / "faculty_directory_v2.csv")
    by_org: dict[str, list[str]] = {}
    if not directory.empty:
        for _, r in directory.iterrows():
            ko = r.get("name_ko", "") or (r.get("name", "") if re.search(r"[가-힣]", r.get("name", "")) else "")
            if ko:
                by_org.setdefault(str(r.get("university", "")).casefold(), []).append(ko)
    for _, r in seed.iterrows():
        if r["name_ko"] or not r["name_en"]:
            continue
        ids = [normalize_openalex_id(x) for x in str(r["openalex_id"]).split(";") if x]
        same_person = sorted(set().union(*(by_id.get(i, set()) for i in ids))) if ids else []
        name = pick_korean(same_person, r["name_en"])
        if name:
            out[r["professor_id"]] = (name, "faculty list (same OpenAlex ID)")
            continue
        org = str(r["university"]).split(";")[0].strip().casefold()
        name = pick_korean(by_org.get(org, []), r["name_en"])
        if name:
            out[r["professor_id"]] = (name, "faculty list (same organisation)")
    return out


def from_openalex(client: OpenAlexClient, seed: pd.DataFrame, skip: set[str]) -> dict[str, tuple[str, str]]:
    """Hangul spellings in OpenAlex display_name_alternatives, 50 authors per request."""
    todo = {}
    for _, r in seed.iterrows():
        if r["name_ko"] or not r["name_en"] or r["professor_id"] in skip:
            continue
        for oid in str(r["openalex_id"]).split(";"):
            if normalize_openalex_id(oid):
                todo[normalize_openalex_id(oid)] = r
    out: dict[str, tuple[str, str]] = {}
    ids = sorted(todo)
    for start in range(0, len(ids), BATCH):
        batch = ids[start:start + BATCH]
        try:
            data = client._get("authors", {"filter": "openalex:" + "|".join(batch), "per-page": BATCH,
                                           "select": "id,display_name,display_name_alternatives"})
        except OpenAlexUnavailable as exc:
            print(f"[korean names] stopping OpenAlex lookups: {exc}", flush=True)
            break
        for a in data.get("results", []) or []:
            r = todo.get(normalize_openalex_id(str(a.get("id", ""))))
            if r is None or r["professor_id"] in out:
                continue
            name = pick_korean([str(x) for x in a.get("display_name_alternatives") or []], r["name_en"])
            if name:
                out[r["professor_id"]] = (name, "OpenAlex alternative name")
    return out


def _orcid_names(session: requests.Session, orcid: str) -> list[str]:
    r = session.get(f"https://pub.orcid.org/v3.0/{orcid}/personal-details",
                    headers={"Accept": "application/json"}, timeout=15)
    if r.status_code != 200:
        return []
    data = r.json() or {}
    names = []
    name = data.get("name") or {}
    for key in ("credit-name",):
        v = (name.get(key) or {}).get("value")
        if v:
            names.append(v)
    given = ((name.get("given-names") or {}).get("value") or "")
    family = ((name.get("family-name") or {}).get("value") or "")
    if re.search(r"[가-힣]", family + given):
        names.append(family + given)
    for other in ((data.get("other-names") or {}).get("other-name") or []):
        if other.get("content"):
            names.append(other["content"])
    return names


def from_orcid(seed: pd.DataFrame, skip: set[str], session: requests.Session | None = None,
               limit: int = ORCID_LOOKUP_LIMIT) -> dict[str, tuple[str, str]]:
    session = session or requests.Session()
    out: dict[str, tuple[str, str]] = {}
    done = 0
    for _, r in seed.iterrows():
        if done >= limit:
            break
        if r["name_ko"] or not r["name_en"] or not r["orcid"] or r["professor_id"] in skip:
            continue
        done += 1
        try:
            names = _orcid_names(session, r["orcid"])
        except requests.RequestException:
            continue
        name = pick_korean(names, r["name_en"])
        if name:
            out[r["professor_id"]] = (name, "ORCID record")
    return out


def fill_korean_names(client: OpenAlexClient | None = None, orcid_session: requests.Session | None = None) -> int:
    path = DATA_DIR / "professors_seed.csv"
    seed = _read(path)
    if seed.empty:
        return 0
    fills = from_faculty_lists(seed)
    fills.update(from_openalex(client or OpenAlexClient(), seed, set(fills)))
    fills.update(from_orcid(seed, set(fills), orcid_session))
    if not fills:
        return 0
    for pid, (name, _) in fills.items():
        seed.loc[seed["professor_id"] == pid, "name_ko"] = name
    seed.to_csv(path, index=False, encoding="utf-8-sig")
    log_path = OUTPUT_DIR / "korean_name_fills.csv"
    old = _read(log_path)
    new = pd.DataFrame([{"professor_id": p, "name_ko": n, "source": s} for p, (n, s) in fills.items()])
    pd.concat([old, new], ignore_index=True).to_csv(log_path, index=False, encoding="utf-8-sig")
    print(f"[korean names] filled {len(fills)}: " +
          ", ".join(f"{s} {sum(1 for v in fills.values() if v[1] == s)}" for s in sorted({v[1] for v in fills.values()})),
          flush=True)
    return len(fills)
