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


def test_heading_gives_degree_and_no_fake_other():
    html = """<h2>Ph.D. Alumni</h2><p>김민수</p><p>2014 - 2019</p><p>홍길동</p><p>2012 - 2017</p>
    <h2>M.S. Alumni</h2><p>이지은</p><p>2017 - 2019</p>"""
    people = parse_alumni(html)
    assert [(p["name_ko"], p["degree"], p["career"]) for p in people] == [
        ("김민수", "phd", "unknown"), ("홍길동", "phd", "unknown"), ("이지은", "ms", "unknown")]


def test_department_pages_are_not_alumni_links():
    lab = """<a href="/about/former-chairperson">Former Chairperson</a><a href="/news.do?articleNo=1">졸업생 소식</a>
    <a href="/people/alumni">Alumni</a>"""
    assert alumni_links(lab, "http://dept.ac.kr/")[0] == ["http://dept.ac.kr/people/alumni"]


def test_v3_fixes():
    assert years_of("2019.03 - 2020 - 2023.02", "phd")[0] == 2019          # mixed month/no month: no crash
    assert career_of("Post doctoral Researcher at Harvard") == "postdoc"
    assert career_of("Res. Prof. Seoul") == "faculty"
    assert career_of("Program in Department of Pharmaceutical Sciences") == "other"
    assert career_of("Senior Scientist, Genentech") == "industry"
    assert career_of("한국콜마 연구원") == "industry"
    prose = "<h2>Alumni</h2><p>최근에 해외 유학 학생 수는 점차 줄어들고 있는 실정이다.</p><p>해외에 유학 을 하게 된다.</p>"
    assert parse_alumni(prose) == []
    table = "<table><tr><td>김민수</td><td>박사후연구원</td><td>2015-2018</td></tr></table>"
    p = parse_alumni(table)[0]
    assert p["degree"] == "postdoc" and p["career"] == "unknown"
    footer = "<p>이지은 석사 2017-2019 현재 KIST</p><div>Copyright Lab</div><p>시신기증 총동문회 2020</p>"
    assert [x["name_ko"] for x in parse_alumni(footer)] == ["이지은"]


def test_duration_from_years_only_is_an_estimate():
    people = parse_alumni(LIST_PAGE)          # 김민수 2014.03-2019.08 (exact), 이지은 2017-2019 (years only)
    assert people[0]["years_exact"] and not people[1]["years_exact"]
    s = summarize("P1", "", [], people, today=date(2024, 6, 1))
    assert s["phd_years_est"] == 0 and s["ms_years_est"] == 1 and s["ms_years"] == 2.0


def test_refresh_rereads_known_pages_only_and_keeps_old_on_failure(tmp_path, monkeypatch):
    for name, path in (("SITES_PATH", "s.csv"), ("SUMMARY_PATH", "sum.csv"), ("MANUAL_PATH", "m.csv")):
        monkeypatch.setattr(alumni, name, tmp_path / path)
    monkeypatch.setattr(alumni, "PEOPLE_DIR", tmp_path / "people")
    monkeypatch.setattr(alumni, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(alumni, "DATA_DIR", tmp_path)
    pd.DataFrame([{"professor_id": p, "source_url": "", "openalex_id": "", "orcid": ""} for p in ("P1", "P2", "P3")]
                 ).to_csv(tmp_path / "professors_enriched.csv", index=False)
    pd.DataFrame([
        {"professor_id": "P1", "lab_url": "http://a.kr/", "alumni_urls": "http://a.kr/alumni", "status": "alumni"},
        {"professor_id": "P2", "lab_url": "http://b.kr/", "alumni_urls": "http://b.kr/alumni", "status": "alumni"},
        {"professor_id": "P3", "lab_url": "http://c.kr/", "alumni_urls": "", "status": "lab_site"},
    ]).to_csv(tmp_path / "s.csv", index=False)
    pd.DataFrame([{"professor_id": "P2", "alumni": 9}]).to_csv(tmp_path / "sum.csv", index=False)
    fetched = []

    class F:
        count = 0
        session = None

        def html(self, url):
            fetched.append(url)
            return LIST_PAGE if url.startswith("http://a.kr") else ""      # b.kr is down today

    s = alumni.collect_alumni(object(), F(), known_only=True)
    assert fetched == ["http://a.kr/alumni", "http://b.kr/alumni"]          # no search, P3 untouched
    got = dict(zip(s["professor_id"], s["alumni"].astype(int)))
    assert got == {"P1": 3, "P2": 9}                                        # P2 keeps yesterday's figures


# Record-per-block layout (name / program / semesters / position), names made up.
BLOCK_PAGE = "<h1>Alumni</h1>" + "".join(f"<p>{x}</p>" for x in [
    "Minho Seo", "MS/PhD Program in Department of Regulatory Science", "2021S~2025F (Full time)",
    "Post-Doctoral Fellow in YIPS",
    "Jiwoo Baek", "MS Program in Department of Industrial Pharmaceutical Science", "2022F~2024 (Part time)",
    "Hoffmann-La Roche Ltd",
    "Sora Kang", "MS Program in Department of Pharmacy", "2023S~2025 (Full time)",
    "Taeho Yoon", "PhD Program in Department of Pharmacy", "2015S~2020 (Full time)",
    "Assistant Professor in College of Pharmacy, Some National University",
    "Yuna Moon", "MS/PhD Program in Department of Regulatory Science", "2013F~2020 (Full time)",
    "Examiner, Bio-Healthcare Examination Division in Ministry of Intellectual Property",
])


def test_block_layout_positions_are_not_people():
    people = parse_alumni(BLOCK_PAGE)
    assert [(p["name_en"], p["degree"], p["career"]) for p in people] == [
        ("Minho Seo", "integrated", "postdoc"), ("Jiwoo Baek", "ms", "industry"),
        ("Sora Kang", "ms", "unknown"), ("Taeho Yoon", "phd", "faculty"),
        ("Yuna Moon", "integrated", "institute")]
    assert people[1]["years"] == 1.5 and people[1]["years_exact"]   # 2022F~2024: Sep 2022 - Feb 2024


def test_semester_notation():
    assert years_of("2023S~2025 (Full time)", "ms") == (2023, 2025, 2.0)     # Mar 2023 - Feb 2025
    assert years_of("2021S~2025F (Full time)", "integrated") == (2021, 2025, 4.5)  # - Aug 2025
    assert years_of("2022F~2024 (Part time)", "ms") == (2022, 2024, 1.5)     # Sep 2022 - Feb 2024
    assert years_of("2013F~2021", "integrated") == (2013, 2021, 7.5)
    assert alumni.exact_months("2023S~2025") and not alumni.exact_months("2023~2025")


def test_photo_cards_with_bilingual_names():
    # Made-up names; layout: caption / 'English (한글)' / position / degree + year.
    html = "<h1>Alumni</h1>" + "".join(f"<p>{x}</p>" for x in [
        "Post-doctoral Fellows", "Ph.D. students", "멤버사진", "SeaRin Park (박서린)", "Professor, Dankook University",
        "Ph.D. 2011", "멤버사진", "Ha-Neul Choi (최하늘)", "Histogenetics Inc.", "M.S. 2010", "멤버사진",
        "MinJu Song (송민주)", "에이비씨파트너스(주)", "M.S./Ph.D. 2015-2020", "Tel : 02-000-0000"])
    people = parse_alumni(html)
    assert [(p["name_ko"], p["degree"], p["career"]) for p in people] == [
        ("박서린", "phd", "faculty"), ("최하늘", "ms", "industry"), ("송민주", "integrated", "industry")]
    assert people[2]["years"] == 5.0


def test_alumni_who_are_professors_become_lineage_links():
    profs = pd.DataFrame([
        {"professor_id": "P1", "name_ko": "이교수", "name_en": "Gyo Su Lee", "university": "Seoul National University"},
        {"professor_id": "P2", "name_ko": "박서린", "name_en": "Seo Rin Park", "university": "Dankook University"},
        {"professor_id": "P3", "name_ko": "박서린", "name_en": "Seo Rin Park", "university": "Busan University"},
        {"professor_id": "P4", "name_ko": "최하늘", "name_en": "Ha Neul Choi", "university": "Korea University"},
    ])
    people = [
        {"name_ko": "박서린", "name_en": "", "degree": "phd", "position": "Professor, Dankook University",
         "end_year": 2011},                                          # same name twice: university decides
        {"name_ko": "최하늘", "name_en": "", "degree": "ms", "position": "Histogenetics Inc.", "end_year": 2010},
    ]
    rows = alumni.alumni_relationships("P1", people, alumni.ProfessorIndex(profs), "http://lab/alumni")
    assert [(r["professor_b_id"], r["relationship_type"]) for r in rows] == [("P2", "advisor_student")]


def test_bilingual_pages_take_only_bilingual_lines_as_names():
    html = "<h1>Alumni</h1>" + "".join(f"<p>{x}</p>" for x in [
        "Gaon Jeong (정가온)", "Amore Pacific", "M.S. 2013", "Doyun Ha (하도윤)", "Hyundai Oil Bank", "M.S. 2012",
        "Siu Yeo (여시우)", "Professor, Some University", "Ph.D. 2014"])
    people = parse_alumni(html)
    assert [(p["name_ko"], p["career"]) for p in people] == [("정가온", "industry"), ("하도윤", "industry"),
                                                             ("여시우", "faculty")]


def test_years_in_lab_without_degree():
    # Made-up names; layout: name / email / years in the lab / Current: position.
    html = "<h2>Alumni</h2>" + "".join(f"<p>{x}</p>" for x in [
        "강하나", "Email: a@example.org", "Education and Research Experience at Lab: 2010-2016",
        "Current: Profressor, KENTECH",
        "정두리", "Email:", "Education and Research Experience at Lab: 2013-2015",
        "Current: National Research Foundation of Korea",
        "조세나", "Email: c@example.org", "Education and Research Experience at Lab: 2023-2025",
        "Current: PhD Program, Seoul National University",
        "오네모", "Email:", "Education and Research Experience at Lab: 2024-2026", "Current: COSMAX"])
    people = parse_alumni(html)
    assert [(p["name_ko"], p["degree"], p["years"], p["career"]) for p in people] == [
        ("강하나", "unknown", 6.0, "faculty"), ("정두리", "unknown", 2.0, "institute"),
        ("조세나", "unknown", 2.0, "study"), ("오네모", "unknown", 2.0, "industry")]
    s = summarize("P1", "", [], people, today=date(2026, 1, 1))
    assert s["stay_years"] == 2.0 and s["stay_years_est"] == 1 and s["career_study"] == 1
