import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import date  # noqa: E402

import pandas as pd  # noqa: E402

import alumni  # noqa: E402
from alumni import (_same_person, alumni_links, career_of, degree_of, lab_links, parse_alumni,  # noqa: E402
                    summarize, years_of)

LIST_PAGE = """<html><body><nav><a href="/">Home</a></nav><h2>Alumni</h2><ul>
<li>김민수 (Min Su Kim) Ph.D. 2014.03 - 2019.08 / 현재: 삼성바이오로직스 선임연구원</li>
<li>이지은 석사 2017-2019 → Postdoc, Harvard Medical School</li>
<li>박준형 석박사통합과정 2012.03 ~ 2018.02, 현재 충남대학교 조교수</li>
<li>Seoul National University</li>
</ul></body></html>"""

TABLE_PAGE = """<html><body><table>
<tr><th>이름</th><th>학위</th><th>기간</th><th>현재</th></tr>
<tr><td>정하늘</td><td>박사</td><td>2013-2018</td><td>서울아산병원 연구교수</td></tr>
<tr><td>최유진</td><td>석사</td><td>2019-2021</td><td>한미약품</td></tr>
</table></body></html>"""

ENGLISH_PAGE = """<html><body><h1>Former members</h1>
<p>Jae Hyun Lee</p><p>Ph.D. (2016-2021)</p><p>Current position: Senior Scientist, Genentech</p>
<p>Soo Jin Park</p><p>M.S. 2020</p><p>Now: KRIBB researcher</p>
</body></html>"""


def test_degree_and_career():
    assert degree_of("석박사통합과정") == "integrated"
    assert degree_of("Ph.D. 2019") == "phd"
    assert degree_of("M.S. student") == "ms"
    assert career_of("Postdoc, Harvard") == "postdoc"
    assert career_of("충남대학교 조교수") == "faculty"
    assert career_of("삼성바이오로직스 선임연구원") == "industry"
    assert career_of("서울아산병원") == "hospital"
    assert career_of("KRIBB researcher") == "institute"
    assert career_of("") == ""


def test_years():
    assert years_of("2014.03 - 2019.08", "phd") == (2014, 2019, 5.4)
    assert years_of("2017-2019", "ms") == (2017, 2019, 2.0)
    assert years_of("graduated 2020", "ms") == (None, 2020, None)
    assert years_of("1999-2020", "ms")[2] is None      # not a plausible M.S.


def test_list_page():
    people = parse_alumni(LIST_PAGE)
    assert [p["name_ko"] for p in people] == ["김민수", "이지은", "박준형"]
    kim, lee, park = people
    assert kim["name_en"] == "Min Su Kim" and kim["degree"] == "phd" and kim["years"] == 5.4
    assert kim["career"] == "industry"
    assert lee["degree"] == "ms" and lee["career"] == "postdoc"
    assert park["degree"] == "integrated" and park["career"] == "faculty"


def test_table_page():
    people = parse_alumni(TABLE_PAGE)
    assert [(p["name_ko"], p["degree"], p["years"], p["career"]) for p in people] == [
        ("정하늘", "phd", 5.0, "faculty"), ("최유진", "ms", 2.0, "industry")]


def test_english_page():
    people = parse_alumni(ENGLISH_PAGE)
    assert [(p["name_en"], p["degree"], p["career"]) for p in people] == [
        ("Jae Hyun Lee", "phd", "industry"), ("Soo Jin Park", "ms", "institute")]


def test_alumni_section_only_on_members_page():
    html = """<h2>Members</h2><p>홍길동 Ph.D. student 2022-</p><h2>Alumni</h2>
    <p>김철수 박사 2010-2015 현재 KIST 선임연구원</p><h2>Publications</h2><p>이영희 박사 2001-2006</p>"""
    people = parse_alumni(html, whole_page=False)
    assert [p["name_ko"] for p in people] == ["김철수"]


def test_links():
    profile = """<a href="https://www.facebook.com/x">Facebook</a><a href="http://neuro.snu.ac.kr">연구실 홈페이지</a>
    <a href="/about">학과소개</a>"""
    assert lab_links(profile, "https://biosci.snu.ac.kr/p/1") == ["http://neuro.snu.ac.kr"]
    lab = """<a href="/members">Members</a><a href="/alumni">Alumni</a><a href="https://other.org/alumni">x</a>"""
    assert alumni_links(lab, "http://neuro.snu.ac.kr/") == (["http://neuro.snu.ac.kr/alumni"],
                                                             ["http://neuro.snu.ac.kr/members"])


def test_same_person():
    assert _same_person("Min-Su Kim", {"name_en": "Min Su Kim"})
    assert _same_person("M. S. Kim", {"name_en": "Min Su Kim"})
    assert not _same_person("Min Su Lee", {"name_en": "Min Su Kim"})
    assert _same_person("Minsu Kim", {"name_ko": "김민수", "name_en": ""})


class FakeClient:
    def _get(self, path, params):
        work = lambda wid, year, name, pos, fwci, top: {
            "id": wid, "publication_year": year, "fwci": fwci,
            "citation_normalized_percentile": {"is_in_top_10_percent": top},
            "primary_location": {"source": {"display_name": "Nature"}},
            "authorships": [{"author": {"display_name": name}, "author_position": pos}]}
        return {"results": [work("W1", 2017, "Min Su Kim", "first", 3.0, True),
                            work("W2", 2018, "Min Su Kim", "middle", 1.0, False),
                            work("W3", 2010, "Min Su Kim", "first", 9.0, True)],   # before the degree
                "meta": {"next_cursor": None}}


def test_papers_and_summary():
    people = parse_alumni(LIST_PAGE)
    alumni.add_papers(FakeClient(), ["A1"], people)
    kim = people[0]
    assert (kim["papers"], kim["first_author"], kim["top10"]) == (2, 1, 1)
    s = summarize("P1", "http://lab", ["http://lab/alumni"], people, today=date(2024, 6, 1))
    assert s["alumni"] == 3 and s["phd_graduates"] == 2 and s["phd_years"] == 5.4
    assert s["career_industry"] == 1 and s["career_postdoc"] == 1 and s["career_faculty"] == 1
    assert s["fwci_median"] == 2.0 and s["top10_share"] == 0.5 and s["top_journals"] == "Nature"
    # Left in the last 5 years (2019+): 김민수 (2019) and 이지은 (2019), not 박준형 (2018).
    assert s["recent_alumni"] == 2 and s["recent_phd_graduates"] == 1 and s["recent_career_faculty"] == 0


def test_lab_info_has_recent(tmp_path, monkeypatch):
    people = parse_alumni(LIST_PAGE)
    path = tmp_path / "summary.csv"
    pd.DataFrame([summarize("P1", "http://lab", ["http://lab/alumni"], people, today=date(2024, 6, 1))]).to_csv(path)
    monkeypatch.setattr(alumni, "SUMMARY_PATH", path)
    info = alumni.alumni_lab_info()["P1"]
    assert info["alumni"] == 3 and info["careers"]["faculty"] == 1
    assert info["recent"]["alumni"] == 2 and "faculty" not in info["recent"]["careers"] or \
        info["recent"]["careers"]["faculty"] == 0
    assert info["alumni_url"] == "http://lab/alumni"


def test_no_openalex_id_makes_no_request():
    class Boom:
        def _get(self, *a, **k):
            raise AssertionError("no request expected")
    people = parse_alumni(LIST_PAGE)
    alumni.add_papers(Boom(), [""], people)
    assert all("papers" not in p for p in people)
