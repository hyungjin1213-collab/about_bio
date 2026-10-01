import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import institution_discovery as idisc  # noqa: E402
from openalex_client import OpenAlexUnavailable  # noqa: E402

BIO = [{"count": 40, "domain": {"display_name": "Life Sciences"}, "subfield": {"display_name": "Immunology"}}]
ENG = [{"count": 40, "domain": {"display_name": "Physical Sciences"}, "subfield": {"display_name": "Electrical Engineering"}}]


def _author(aid, name, works=80, h=25, recent=10, topics=BIO, orcid="", insts=("I1", "I5"), years=(2024, 2025, 2026)):
    return {"id": f"https://openalex.org/{aid}", "display_name": name, "works_count": works,
            "summary_stats": {"h_index": h}, "orcid": orcid, "topics": topics,
            "counts_by_year": [{"year": 2025, "works_count": recent}],
            "affiliations": [{"institution": {"id": f"https://openalex.org/{i}"}, "years": list(years)} for i in insts]}


class FakeOpenAlex:
    def __init__(self, institutions, authors, fail_on=None):
        self.institutions, self.authors, self.fail_on, self.calls = institutions, authors, fail_on, []

    def _get(self, path, params):
        self.calls.append((path, params))
        if path == "institutions":
            if self.fail_on and self.fail_on in params["search"]:
                raise OpenAlexUnavailable("budget")
            return {"results": self.institutions.get(params["search"], [])}
        return {"results": self.authors}


def test_targets_are_institutes_and_uncrawlable_universities():
    unis = pd.DataFrame([
        {"university": "Seoul National University", "org_type": "university"},
        {"university": "Pusan National University", "org_type": "university"},
        {"university": "Korea Institute of Science and Technology", "org_type": "institute"},
        {"university": "Samsung Medical Center", "org_type": "hospital"},
        {"university": "Ajou University", "org_type": "university"},
    ])
    log = pd.DataFrame([
        {"university": "Seoul National University", "pages_found": "9"},
        {"university": "Pusan National University", "pages_found": "0"},
        {"university": "Ajou University", "pages_found": "0"},
    ])
    got = [t["university"] for t in idisc.targets(unis, log, done={"Ajou University"})]
    assert got == ["Korea Institute of Science and Technology", "Pusan National University"]


def test_pi_filter():
    assert idisc.is_pi_like(_author("A1", "Minsu Park"), 2026)[0]
    assert not idisc.is_pi_like(_author("A2", "Postdoc Kim", works=35, h=8), 2026)[0]
    assert not idisc.is_pi_like(_author("A3", "Retired Lee", recent=0), 2026)[0]
    assert not idisc.is_pi_like(_author("A4", "Engineer Choi", topics=ENG), 2026)[0]


def test_discovery_adds_researchers_once(tmp_path, monkeypatch):
    monkeypatch.setattr(idisc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(idisc, "load_rejections", lambda: {"A9"})
    pd.DataFrame([{"university": "Korea Institute of Science and Technology", "name_ko": "한국과학기술연구원",
                   "official_domain": "kist.re.kr", "region": "서울", "org_type": "institute"}]
                 ).to_csv(tmp_path / "universities_seed.csv", index=False)
    pd.DataFrame(columns=["university", "pages_found"]).to_csv(tmp_path / "discovery_log.csv", index=False)
    pd.DataFrame([{"professor_id": "P0001", "name_ko": "홍길동", "name_en": "",
                   "university": "Korea Institute of Science and Technology", "openalex_id": "A2"}]
                 ).to_csv(tmp_path / "professors_seed.csv", index=False)
    kist = {"id": "https://openalex.org/I1", "display_name": "Korea Institute of Science and Technology"}
    kaist = {"id": "https://openalex.org/I2", "display_name": "Korea Advanced Institute of Science and Technology"}
    client = FakeOpenAlex({"Korea Institute of Science and Technology": [kaist, kist]}, [
        _author("A1", "Minsu Park"),            # new PI -> added
        _author("A2", "Already There"),         # known OpenAlex ID
        _author("A3", "Gildong Hong"),          # hand-entered 홍길동 without ID
        _author("A4", "Postdoc Kim", h=5),      # not PI-like
        _author("A9", "Rejected Lee"),          # identity_rejections
        _author("A5", "Shoichiro Tsugane"),     # not a Korean name (Japan's NCC mixed in)
        _author("A6", "Jiwon Choi", insts=("I99",)),            # never actually at KIST
        _author("A7", "Sora Kang", years=(2019, 2020, 2026)),   # one recent year only
    ])
    assert idisc.discover_by_institution(client, now_year=2026) == 1
    seed = pd.read_csv(tmp_path / "professors_seed.csv", dtype=str).fillna("")
    assert seed.iloc[-1][["professor_id", "name_en", "openalex_id", "identity_status"]].tolist() == [
        "P0002", "Minsu Park", "A1", "openalex_institution"]
    authors_call = [p for path, p in client.calls if path == "authors"][0]
    assert "I1" in authors_call["filter"] and "I2" not in authors_call["filter"]   # KAIST is not KIST
    # Logged as done: a second run does nothing.
    assert idisc.discover_by_institution(client, now_year=2026) == 0


def test_budget_exhaustion_keeps_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(idisc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(idisc, "load_rejections", lambda: set())
    pd.DataFrame([{"university": "Institute for Basic Science", "org_type": "institute"},
                  {"university": "Korea Brain Research Institute", "org_type": "institute"}]
                 ).to_csv(tmp_path / "universities_seed.csv", index=False)
    pd.DataFrame(columns=["university", "pages_found"]).to_csv(tmp_path / "discovery_log.csv", index=False)
    pd.DataFrame(columns=["professor_id", "name_ko", "name_en", "university", "openalex_id"]
                 ).to_csv(tmp_path / "professors_seed.csv", index=False)
    ibs = {"id": "https://openalex.org/I5", "display_name": "Institute for Basic Science"}
    client = FakeOpenAlex({"Institute for Basic Science": [ibs]}, [_author("A1", "Minsu Park")], fail_on="Brain")
    assert idisc.discover_by_institution(client, now_year=2026) == 1
    log = pd.read_csv(tmp_path / "institution_discovery_log.csv", dtype=str)
    assert log["university"].tolist() == ["Institute for Basic Science"]   # Brain retried next run


def test_same_organisation_is_strict():
    assert idisc.same_organisation("National Cancer Center", "National Cancer Center Hospital")
    assert idisc.same_organisation("Korea Institute of Science and Technology", "Korea Institute of Science and Technology")
    assert not idisc.same_organisation("Korea Institute of Science and Technology",
                                       "Korea Institute of Ocean Science and Technology")
    assert not idisc.same_organisation("Korea Institute of Science and Technology",
                                       "Korea Institute of Science & Technology Information")
    assert not idisc.same_organisation("Korea Food Research Institute", "Nonghyup Food Research Institute (South Korea)")


def test_chemistry_does_not_count_as_life_science():
    chem = [{"count": 40, "domain": {"display_name": "Physical Sciences"}, "field": {"display_name": "Chemistry"},
             "subfield": {"display_name": "Organic Chemistry"}}]
    assert not idisc.is_pi_like(_author("A1", "Sukbok Chang", topics=chem), 2026)[0]
    assert idisc.korean_name("Gou Young Koh") and not idisc.korean_name("Hertzel C. Gerstein")
