import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import faculty_identity_v2 as fi  # noqa: E402
from faculty_identity_v2 import _best_openalex, institution_matches, resolve_person  # noqa: E402
from orcid_client import KoreanORCIDSearch, korean_orcid_query  # noqa: E402

BIO = [{"count": 40, "domain": {"display_name": "Life Sciences"}, "subfield": {"display_name": "Immunology"}}]
ENG = [{"count": 40, "domain": {"display_name": "Physical Sciences"}, "subfield": {"display_name": "Electrical Engineering"}}]


def _author(aid, name, inst, past=(), orcid="", topics=BIO, works=50):
    return {
        "id": f"https://openalex.org/{aid}",
        "display_name": name,
        "orcid": f"https://orcid.org/{orcid}" if orcid else None,
        "works_count": works,
        "topics": topics,
        "last_known_institutions": [{"display_name": inst}],
        "affiliations": [{"institution": {"display_name": p}} for p in past],
    }


class FakeOpenAlex:
    def __init__(self, results, by_orcid=None):
        self.results = results
        self.by_orcid = by_orcid or {}
        self.queries = []

    def _get(self, path, params):
        self.queries.append(params["search"])
        return {"results": self.results}

    def authors_by_orcid(self, orcid):
        return self.by_orcid.get(orcid, [])

    def get_author(self, author_id):
        return next((a for authors in self.by_orcid.values() for a in authors
                     if a["id"].endswith(author_id)), None)


class FakeORCID(KoreanORCIDSearch):
    def __init__(self, records):
        super().__init__()
        self.records = records
        self.queries = []

    def _search(self, query, rows=200):
        self.queries.append(query)
        if query.startswith("email:"):
            email = query.split('"')[1]
            return [r for r in self.records if email in r["emails"]]
        return list(self.records)


def _orcid_rec(oid, given, family, emails=()):
    return {"orcid": oid, "name": f"{given} {family}", "other_names": [], "credit_name": "",
            "emails": list(emails), "institution": "Seoul National University", "url": ""}


PERSON = {"source_id": "SNU", "name": "김경수", "name_ko": "김경수", "name_en": "",
          "university": "Seoul National University", "email": "kskim@snu.ac.kr"}


# ---------------------------------------------------------------- OpenAlex only
def test_korean_name_matches_romanized_openalex_author():
    client = FakeOpenAlex([
        _author("A1", "Kyung-Soo Kim", "Seoul National University"),
        _author("A2", "Kyung-Soo Kim", "Yonsei University"),
        _author("A3", "Kyung Min Kim", "Seoul National University"),
    ])
    best, reason = _best_openalex(client, "김경수", "", "Seoul National University")
    assert best["id"].endswith("A1"), reason
    # Searched with romanized English (common spelling first), never Hangul.
    assert client.queries == ["Kyung-Soo Kim", "Gyeong-Su Kim"]


def test_acronym_institution_alias():
    client = FakeOpenAlex([_author("A1", "Won-Suk Chung", "Korea Advanced Institute of Science and Technology")])
    best, _ = _best_openalex(client, "정원석", "Won-Suk Chung", "KAIST")
    assert best is not None


def test_same_name_same_school_goes_to_review():
    client = FakeOpenAlex([
        _author("A1", "Jin Woo Kim", "Seoul National University"),
        _author("A2", "Jinwoo Kim", "Seoul National University"),
    ])
    best, reason = _best_openalex(client, "김진우", "", "Seoul National University")
    assert best is None and reason.startswith("ambiguous")


def test_same_name_resolved_by_bio_field():
    client = FakeOpenAlex([
        _author("A1", "Jin Woo Kim", "Seoul National University", topics=BIO),
        _author("A2", "Jinwoo Kim", "Seoul National University", topics=ENG),
    ])
    best, reason = _best_openalex(client, "김진우", "", "Seoul National University")
    assert best["id"].endswith("A1") and "only bio author" in reason


def test_wrong_surname_rejected():
    client = FakeOpenAlex([_author("A1", "Kyungsoo Park", "Seoul National University")])
    assert _best_openalex(client, "김경수", "", "Seoul National University")[0] is None


def test_past_affiliation_alone_is_not_enough():
    # Run #26: OpenAlex past affiliations matched many same-name people.
    client = FakeOpenAlex([_author("A1", "Jaewon Lee", "Harvard University", past=["Pusan National University"])])
    best, reason = _best_openalex(client, "이재원", "Jaewon Lee", "Pusan National University")
    assert best is None and "past" in reason


def test_institution_match_needs_every_word_and_not_a_longer_university():
    assert institution_matches("Seoul National University", "Seoul National University Hospital")
    assert institution_matches("postech", "Pohang University of Science and Technology")
    assert institution_matches("Korea University", "Korea University Anam Hospital")
    assert not institution_matches("Seoul National University", "Korea University")
    assert not institution_matches("Seoul National University", "University of Seoul")
    assert not institution_matches("Seoul National University",
                                   "Seoul National University of Science and Technology")
    assert not institution_matches("Korea University", "Korea Advanced Institute of Science and Technology")


def test_rejected_openalex_ids_are_never_chosen(monkeypatch):
    import faculty_identity_v2 as fi

    monkeypatch.setattr(fi, "load_rejections", lambda: {"A1"})
    client = FakeOpenAlex([_author("A1", "Jaewon Lee", "Pusan National University")])
    best, _ = _best_openalex(client, "이재원", "Jaewon Lee", "Pusan National University")
    assert best is None


# ------------------------------------------------------------- ORCID first
def test_orcid_query_uses_surname_variants_and_initials():
    q = korean_orcid_query("김경수", "Seoul National University", surname_hint="Kim")
    assert q.startswith("family-name:(Kim OR Gim) AND given-names:(")
    assert "K*" in q and "G*" in q and 'affiliation-org-name:"Seoul National University"' in q


def test_verified_by_orcid_email_collects_split_profiles():
    orcid = FakeORCID([
        _orcid_rec("0000-0001-0000-0001", "Kyung-Soo", "Kim", emails=["kskim@snu.ac.kr"]),
        _orcid_rec("0000-0001-0000-0002", "Kyungsoo", "Kim"),
    ])
    oa = FakeOpenAlex([], by_orcid={"0000-0001-0000-0001": [
        _author("A1", "Kyung-Soo Kim", "Seoul National University", works=80),
        _author("A9", "K. S. Kim", "Seoul National University", works=5),
    ]})
    row = resolve_person(oa, orcid, PERSON)
    assert row["identity_status"] == "verified"
    assert row["orcid"] == "0000-0001-0000-0001"
    assert row["openalex_ids"] == "A1;A9" and row["primary_field"] == "Immunology"


def test_ambiguous_orcid_resolved_by_openalex_choice():
    orcid = FakeORCID([
        _orcid_rec("0000-0001-0000-0001", "Kyung-Soo", "Kim"),
        _orcid_rec("0000-0001-0000-0002", "Kyungsoo", "Kim"),
    ])
    oa = FakeOpenAlex([
        _author("A1", "Kyung-Soo Kim", "Seoul National University", orcid="0000-0001-0000-0002", topics=BIO),
        _author("A2", "Kyungsoo Kim", "Seoul National University", orcid="0000-0001-0000-0001", topics=ENG),
    ])
    row = resolve_person(oa, orcid, {**PERSON, "email": ""})
    assert (row["identity_status"], row["orcid"], row["openalex_id"]) == ("verified", "0000-0001-0000-0002", "A1")


def test_no_orcid_unique_openalex_is_probable():
    oa = FakeOpenAlex([_author("A1", "Kyung-Soo Kim", "Seoul National University")])
    row = resolve_person(oa, FakeORCID([]), PERSON)
    assert row["identity_status"] == "probable" and row["openalex_id"] == "A1"


def test_non_bio_single_match_goes_to_review():
    oa = FakeOpenAlex([_author("A1", "Kyung-Soo Kim", "Seoul National University", topics=ENG)])
    row = resolve_person(oa, FakeORCID([]), PERSON)
    assert row["identity_status"] == "manual_review"


# ------------------------------------------------------------------- import
def test_import_merges_departments_and_skips_review(tmp_path, monkeypatch):
    (tmp_path / "out").mkdir()
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(fi, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(fi, "DATA_DIR", tmp_path / "data")
    pd.DataFrame([{"professor_id": "P0001", "name_ko": "도준상", "openalex_id": "A5086462999"}]).to_csv(
        tmp_path / "data" / "professors_seed.csv", index=False)
    base = {c: "" for c in fi.COLUMNS}
    rows = [
        {**base, "name_ko": "김경수", "department": "약학대학", "orcid": "0000-0001", "openalex_ids": "A1;A9", "identity_status": "verified"},
        {**base, "name_ko": "김경수", "department": "의과학과", "orcid": "0000-0001", "openalex_ids": "A1;A9", "identity_status": "verified"},
        {**base, "name_ko": "박성준", "openalex_ids": "A2", "identity_status": "probable"},
        {**base, "name_ko": "이재원", "openalex_ids": "A3", "identity_status": "manual_review"},
        {**base, "name_ko": "도준상", "openalex_ids": "A5086462999", "identity_status": "verified"},
    ]
    pd.DataFrame(rows).to_csv(tmp_path / "out" / "faculty_identity_v2.csv", index=False)
    assert fi.import_verified_faculty_v2() == 2
    seed = pd.read_csv(tmp_path / "data" / "professors_seed.csv", dtype=str).fillna("")
    kim = seed[seed.name_ko == "김경수"].iloc[0]
    assert kim.professor_id == "P0002" and kim.department == "약학대학; 의과학과" and kim.openalex_id == "A1;A9"
    assert "이재원" not in set(seed.name_ko)


def test_import_skips_hand_entered_professor_without_ids(tmp_path, monkeypatch):
    """서형석 was hand-entered (P0003, no OpenAlex ID) and imported again as P0039."""
    (tmp_path / "out").mkdir()
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(fi, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(fi, "DATA_DIR", tmp_path / "data")
    pd.DataFrame([{"professor_id": "P0003", "name_ko": "서형석", "university": "seoul national university",
                   "openalex_id": "", "orcid": ""}]).to_csv(tmp_path / "data" / "professors_seed.csv", index=False)
    base = {c: "" for c in fi.COLUMNS}
    pd.DataFrame([{**base, "name_ko": "서형석", "university": "Seoul National University",
                   "orcid": "0000-0003-2303-7415", "openalex_ids": "A5077018885", "identity_status": "probable"}]
                 ).to_csv(tmp_path / "out" / "faculty_identity_v2.csv", index=False)
    assert fi.import_verified_faculty_v2() == 0


def test_same_name_same_school_dominant_profile_taken():
    # Split profiles of one person: the largest is taken when it is clearly larger.
    client = FakeOpenAlex([
        _author("A1", "Noo Li Jeon", "Seoul National University", topics=ENG, works=236),
        _author("A2", "Noo Li Jeon", "Seoul National University", topics=ENG, works=39),
    ])
    best, reason = _best_openalex(client, "전누리", "Noo Li Jeon", "Seoul National University")
    assert best["id"].endswith("A1") and "dominant" in reason
    # Two profiles of similar size stay for review.
    client = FakeOpenAlex([
        _author("A1", "Nam Ki Lee", "Seoul National University", topics=ENG, works=97),
        _author("A2", "Nam-Ki Lee", "Seoul National University", topics=ENG, works=71),
    ])
    best, reason = _best_openalex(client, "이남기", "Nam Ki Lee", "Seoul National University")
    assert best is None and reason.startswith("ambiguous")
