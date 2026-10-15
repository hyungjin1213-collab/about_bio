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
    assert years_of("2014.03 - 2019.08", "phd") == (2014, 2019, 5.5)
    assert years_of("2017-2019", "ms") == (2017, 2019, 2.0)
    assert years_of("graduated 2020", "ms") == (None, 2020, None)
    assert years_of("1999-2020", "ms")[2] is None      # not a plausible M.S.


def test_list_page():
    people = parse_alumni(LIST_PAGE)
    assert [p["name_ko"] for p in people] == ["김민수", "이지은", "박준형"]
    kim, lee, park = people
    assert kim["name_en"] == "Min Su Kim" and kim["degree"] == "phd" and kim["years"] == 5.5
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
    assert s["alumni"] == 3 and s["phd_graduates"] == 2 and s["phd_years"] == 5.5
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


def test_alumni_professors_at_korean_universities_are_added(tmp_path, monkeypatch):
    monkeypatch.setattr(alumni, "DATA_DIR", tmp_path)
    pd.DataFrame([{"university": "Inha University", "name_ko": "인하대학교", "org_type": "university"}]
                 ).to_csv(tmp_path / "universities_seed.csv", index=False)
    profs = pd.DataFrame([{"professor_id": "P0001", "name_ko": "정교수", "name_en": "Gyo Su Jeong",
                           "university": "Seoul National University", "source_url": "", "identity_status": ""}])
    profs.to_csv(tmp_path / "professors_seed.csv", index=False)
    people = [
        {"name_ko": "강하나", "name_en": "Hana Kang", "career": "faculty", "position": "Professor, Inha university"},
        {"name_ko": "오두리", "name_en": "", "career": "faculty", "position": "Associate Professor, KENTECH"},
        {"name_ko": "신세계", "name_en": "", "career": "faculty", "position": "Professor, University of Washington"},
        {"name_ko": "조나래", "name_en": "", "career": "industry", "position": "LG Chem"},
    ]
    rows = alumni.new_faculty_rows("P0001", people, alumni.ProfessorIndex(profs), "http://lab/alumni",
                                   alumni.korean_universities())
    assert [(r["name_ko"], r["university"]) for r in rows] == [("강하나", "Inha University"), ("오두리", "KENTECH")]
    assert alumni.add_professors(rows) == 2 and alumni.add_professors(rows) == 0       # once only
    seed = pd.read_csv(tmp_path / "professors_seed.csv", dtype=str).fillna("")
    assert list(seed["professor_id"]) == ["P0001", "P0002", "P0003"]
    assert seed.iloc[1]["identity_status"] == "alumni_page"


def test_name_years_role_layout():
    # Made-up names; layout: 'Name (years; role)' / current position.
    html = "<h2>Alumni</h2>" + "".join(f"<p>{x}</p>" for x in [
        "정하나 (2017-2024; 박사후연구원)", "가천대학교 생명과학과 조교수",
        "김두리 (2015-2024;박사과정,박사후연구원)", "Some Institute 박사후연구원",
        "손아현 (2015-2022;박사과정,박사후연구원)",
        "투안 얀 구옌 (2012-2016 – 박사후연구원)", "홍콩과학기술대학교 (홍콩) 교수",
        "이세나 (2007-2016 – administrative assistant)",
        "장세나 (2009-2018 – 박사과정, 박사후연구원)", "서울대학교 생명과학부 조교수"])
    got = [(p["name_ko"], p["degree"], p["years"], p["career"]) for p in parse_alumni(html)]
    assert got == [("정하나", "postdoc", 7.0, "faculty"), ("김두리", "phd", None, "postdoc"),
                   ("손아현", "phd", None, "unknown"), ("투안얀구옌", "postdoc", 4.0, "faculty"),
                   ("장세나", "phd", None, "faculty")]


def test_words_split_over_spans_are_joined():
    # Site builders that split words over spans: 'H' + 'ajun Park', '20' + '19F' + '~2025'.
    html = """<html><body><h2>Alumni</h2><h3>Graduate Students</h3>
    <div><p><span>Minsu Lee</span></p><p><span>MS/</span></p>
      <p><span>PhD Program in Department of Pharmaceutical Medicine</span></p>
      <p><span>2021S~2025F (Full time)</span></p><p><span>Post-Doctoral Fellow in YIPS</span></p></div>
    <div><p><span>H</span><span>ajun Park</span></p><p><span>MS</span></p>
      <p><span>Program in</span> <span>Department of</span> <span>Industrial Pharmaceutical Science</span></p>
      <p><span>20</span><span>19F</span><span>~2025</span> <span>(Part time)</span></p>
      <p><span>QUAD Investment Management</span></p></div>
    <div><p><span>Jiwon Choi</span></p><p><span>MS</span></p>
      <p><span>Program in</span> <span>Department of P</span><span>harmacy</span></p>
      <p><span>20</span><span>23S</span><span>~2025 (Full time)</span></p>
      <p><span>SyneosHealth Korea</span></p></div>
    <div><p><span>Sora Kang</span></p><p><span>MS</span></p>
      <p><span>Program in Department of Industrial Pharmaceutical Science</span></p>
      <p><span>202</span><span>2F</span><span>~202</span><span>4</span> <span>(Full time)</span></p>
      <p><span>Researcher of</span> <span>Hanmi Pharm</span></p></div>
    </body></html>"""
    people = parse_alumni(html)
    got = {p["name_en"]: (p["end_year"], p["career"]) for p in people}
    assert len(people) == 4
    assert got["Hajun Park"] == (2025, "industry")
    assert got["Sora Kang"][0] == 2024
    assert got["Minsu Lee"][1] == "postdoc"


def test_board_list_inside_form_is_read():
    rows = "".join(f"<tr><td>{n}</td><td>Ph.D. 2015-2020</td><td>Assistant Professor, Korea University</td></tr>"
                   for n in ["김가람", "이나래", "박다솜", "최라온", "정마루"])
    html = (f"<html><body><form name='search'><input name='q'> 검색</form>"
            f"<form name='fboardlist'><table>{rows}</table></form></body></html>")
    people = parse_alumni(html)
    assert len(people) == 5 and all(p["career"] == "faculty" for p in people)


def test_alumni_page_found_under_people_page():
    from alumni import find_alumni
    pages = {
        "http://lab.example.ac.kr/": '<a href="index.php?hCode=members_01_01">구성원 People</a>',
        "http://lab.example.ac.kr/index.php?hCode=members_01_01":
            '<a href="index.php?hCode=members_01_01">Professor</a><a href="index.php?hCode=members_01_03">Alumni</a>',
        "http://lab.example.ac.kr/index.php?hCode=members_01_03":
            "<ul><li>김가람 박사 2012-2017</li><li>현: 고려대학교 조교수</li>"
            "<li>이나래 석사 2015-2017</li><li>현: 삼성전자</li></ul>",
    }

    class Fetch:
        session = None

        def html(self, url):
            return pages.get(url, "")

    lab, used, people = find_alumni(Fetch(), "", {"lab_url": "http://lab.example.ac.kr/"})
    assert used == ["http://lab.example.ac.kr/index.php?hCode=members_01_03"]
    assert [p["career"] for p in people] == ["faculty", "industry"]


def test_alumni_cards_with_work_and_previously():
    cards = [
        ("박가온 (Gaon Park)", "Samsung Electronics",
         ["Ph.D. at Our Group (2017-2025)", "M.S. at Our Group (2015-2017)", "B.S. in Chemistry (2011-2015)"]),
        ("이다인 (Dain Lee)", "Professor, Department of Chemistry, Sungkyunkwan University", ["Post Doc. (2003~2004)"]),
        ("최하늘 (Haneul Choi)", "(주)효성 전략본부", ["Master Thesis (Feb. 2000.)", '"Synthesis of things"']),
        ("정바다 (Bada Jung)", "LG화학", []),
        ("한누리 (Nuri Han)", "Korea Institute of Science and Technology", ["PhD at Our Group [2020-2025]"]),
    ]
    body = "<p>Total 5건</p>"
    for name, work, prev in cards:
        body += f"<div><p>{name}</p><p>E-Mail</p><p>someone@example.com</p><p>Work</p><p>{work}</p>"
        if prev:
            body += "<p>Previously</p>" + "".join(f"<p>{x}</p>" for x in prev)
        body += "</div>"
    people = {p["name_ko"]: p for p in parse_alumni(f"<html><body>{body}</body></html>")}
    assert len(people) == 5
    assert (people["박가온"]["degree"], people["박가온"]["years"], people["박가온"]["career"]) == ("phd", 8.0, "industry")
    assert (people["이다인"]["degree"], people["이다인"]["career"]) == ("postdoc", "faculty")
    assert (people["최하늘"]["degree"], people["최하늘"]["end_year"], people["최하늘"]["career"]) == ("ms", 2000, "industry")
    assert people["정바다"]["career"] == "industry"
    assert people["한누리"]["career"] == "institute"


def _page(lines):
    return "<html><body>" + "".join(f"<p>{x}</p>" for x in lines) + "</body></html>"


def test_cv_timeline_profiles():
    html = _page(["Alumni", "Postdoctoral Researcher", "Doctor of Philosophy", "Master of Science",
        "Postdoctoral Researcher", "김가람 Ph.D.", "Garam Kim",
        "2012.03 ~ 2016.02 : College of Pharmacy, Korea University, Pharm.D.",
        "2016.03 ~ 2022.08 : College of Pharmacy, Seoul National University, Ph.D. (Pharmaceutics)",
        "2022.09 ~ 2023.01 : College of Pharmacy, Seoul National University, Post-doctoral fellow",
        "2023.02 ~ 2025.08 : School of Pharmacy, University of Waterloo, Postdoctoral fellow",
        "2025.07 ~ present : Alexion, AstraZeneca, Senior Manager",
        "Doctor of Philosophy", "이나래 Ph.D.", "Narae Lee",
        "1992.03 ~ 1996.02 : College of Pharmacy, Seoul National University, B.S.",
        "1996.03 ~ 1998.02 : College of Pharmacy, Seoul National University, M.S. (Pharmaceutics)",
        "1998.03 ~ 2004.02 : College of Pharmacy, Seoul National University, Ph.D. (Pharmaceutics)",
        "2005.10 ~ 2009.04 : NCI/NIH (박사후연구원)",
        "2015.03 ~ present : 강원대학교 약학대학 교수",
        "Doctor of Philosophy", "박다솜 Ph.D.",
        "1993.03 ~ 1997.02 : College of Pharmacy, Seoul National University, B.S.",
        "1999.03 ~ 2003.02 : College of Pharmacy, Seoul National University, Ph.D. (Pharmaceutics)",
        "Master of Science", "최라온 M.S.",
        "2010.03 ~ 2012.02 : College of Pharmacy, Seoul National University, M.S. (Pharmaceutics)",
        "2012.03 ~ present : 한미약품 연구원"])
    got = {p["name_ko"]: (p["degree"], p["years"], p["career"]) for p in parse_alumni(html)}
    assert got == {"김가람": ("postdoc", None, "industry"), "이나래": ("phd", 6.0, "faculty"),
                   "박다솜": ("phd", 4.0, "unknown"), "최라온": ("ms", 2.0, "industry")}


def test_profile_cards_with_education_lines():
    html = _page(["Alumni",
        "Abebe Bekele Tadesse", "Research Interest", "In vitro study", "Education",
        "Animal Science, Haramaya University | B.S. (2001 ~ 2005)",
        "Chinese Academy of Agricultural Sciences | Ph.D. (2018. 09 ~ 2021. 12)",
        "Postdoctoral researcher (2024. 03 ~ 2025. 08)",
        "Gaon Park", "E.mail: someone@example.com", "Research Interest", "Methane mitigation", "Education",
        "Chung-Ang University | B.S. (2019. 03 ~ 2023. 02)", "Chung-Ang University | M.S. (2023. 03 ~ 2025. 02)",
        "Researcher at Samsung Biologics (2025. 03 ~ )",
        "Dain Lee", "E.mail: other@example.com", "Education",
        "Chung-Ang University | B.S. (2018. 03 ~ 2024. 02)", "Chung-Ang University | M.S. (2024. 03 ~ 2026. 02)"])
    got = {p["name_en"]: (p["degree"], p["years"], p["career"]) for p in parse_alumni(html)}
    assert got["Abebe Bekele Tadesse"][0] == "postdoc"
    assert got["Gaon Park"] == ("ms", 2.0, "industry")
    assert got["Dain Lee"][:2] == ("ms", 2.0)


def test_capital_letter_names_with_dated_degrees():
    html = _page(["ALUMNI", "GARAM KIM", "garam@example.com", "Thesis : Functional food materials",
        "2018-2020 MS, Food Toxicology, Kyungpook National University",
        "2014-2018 BS, Applied Biology, Chonnam National University", "Publication",
        "Kim G, Lee N. (2022) A paper title. Journal.",
        "NARAE LEE", "narae@example.com", "2020-2022 MS, Food Toxicology, Kyungpook National University",
        "2016-2020 BS, Food Science, Kyungpook National University", "Publication",
        "DASOM PARK", "dasom@example.com", "2019-2024 PhD, Food Toxicology, Kyungpook National University",
        "2017-2019 MS, Food Toxicology, Kyungpook National University",
        "2024-present Researcher, Korea Food Research Institute"])
    got = {p["name_en"]: (p["degree"], p["years"], p["career"]) for p in parse_alumni(html)}
    assert got == {"Garam Kim": ("ms", 2.0, "unknown"), "Narae Lee": ("ms", 2.0, "unknown"),
                   "Dasom Park": ("phd", 5.0, "institute")}


def test_role_written_above_the_name():
    html = _page(["Former Members", "Senior Researcher / 2016", "Garam Kim", "Doctoral Course / 2019", "Narae Lee",
                  "Senior Researcher", "Dasom Park", "Doctoral Course / 2023", "Raon Choi",
                  "Combined Masters and Doctoral course", "Maru Jung"])
    got = {p["name_en"]: (p["degree"], p["end_year"], p["career"]) for p in parse_alumni(html)}
    assert got["Narae Lee"] == ("phd", 2019, "unknown")
    assert got["Raon Choi"] == ("phd", 2023, "unknown")
    assert got["Maru Jung"][0] == "integrated"


def test_mail_and_homepage_buttons_are_not_careers():
    html = _page(["졸업생", "김가람", "(박사 졸업생)", "메일", "홈페이지", "상세보기",
                  "이나래", "2019년 석사 졸업생", "메일", "홈페이지", "상세보기"])
    people = parse_alumni(html)
    assert [(p["name_ko"], p["degree"], p["career"]) for p in people] == [
        ("김가람", "phd", "unknown"), ("이나래", "ms", "unknown")]


def test_current_students_on_members_page_are_not_alumni():
    html = _page(["Members", "Garam Kim", "PhD Student", "(Spring 2023)", "Narae Lee", "PhD Student", "(Fall 2023)",
                  "Dasom Park", "MS/PhD Student", "(Spring 2024)", "Raon Choi", "MS Student", "(Spring 2025)",
                  "김가람", "석박통합 대학원생", "메일", "이나래", "박사후연구원, 현 아주대학교 의과대학 교수", "메일"])
    people = parse_alumni(html)
    assert [p["name_ko"] for p in people] == ["이나래"] and people[0]["career"] == "faculty"


def test_department_name_in_heading_is_not_a_degree():
    html = _page(["Alumni | 서울대학교 첨단융합학부 연구실", "Garam Kim", "고려대학교", "교수",
                  "Narae Lee", "삼성바이오로직스", "Dasom Park", "Postdoc at Fudan University",
                  "Raon Choi", "칠곡경북대학교병원", "Maru Jung", "삼성전자", "Bada Han", "GC녹십자"])
    got = {p["name_en"]: (p["degree"], p["career"]) for p in parse_alumni(html)}
    assert got == {"Garam Kim": ("unknown", "faculty"), "Narae Lee": ("unknown", "industry"),
                   "Dasom Park": ("postdoc", "unknown"), "Raon Choi": ("unknown", "hospital"),
                   "Maru Jung": ("unknown", "industry"), "Bada Han": ("unknown", "industry")}


def test_news_after_alumni_section_is_not_read():
    html = _page(["Members", "Alumni", "김가람(석사) - Kim et al., Journal (2024)", "이나래(석사) - Lee et al., Journal (2023)",
                  "2026년 6월", "박다솜 회식", "2025년 8월", "최라온 박사학위 수여"])
    assert [p["name_ko"] for p in parse_alumni(html, whole_page=False)] == ["김가람", "이나래"]


def test_one_alumnus_page_is_not_an_alumni_list():
    html = ('<a href="/lab/alumni">Alumni</a><a href="/lab/alumni/garam-kim">Garam Kim</a>'
            '<a href="/lab/members">Members</a>')
    alumni, members = alumni_links(html, "https://lab.example.org/lab")
    assert alumni == ["https://lab.example.org/lab/alumni"]


def test_citations_split_over_spans_are_not_people():
    cite = ("<span>Kwon et al., </span><span>Cell Death Dis</span><span> (</span><span>2024</span><span>)</span>"
            "<span>; Yu et al., </span><span>Biomol Ther</span><span> (2023)</span>")
    html = (f"<html><body><p>Members</p><p>Alumni</p><p>김가람(석사) - {cite}</p><p>이나래(석사) - {cite}</p>"
            "<p>2026년 6월</p><p>학회 참석</p></body></html>")
    assert [(p["name_ko"], p["degree"]) for p in parse_alumni(html, whole_page=False)] == [("김가람", "ms"), ("이나래", "ms")]


def test_google_sites_profiles_with_menu_noise():
    html = _page(["CAU Lab - Alumni", "Search this site", "Embedded Files", "Skip to main content", "Alumni",
        "Abebe Bekele Tadesse", "Research Interest", "In vitro study", "Education",
        "Animal Science, Haramaya University | B.S. (2001 ~ 2005)",
        "Animal Production, Haramaya University | M.S. (2010 ~ 2012)",
        "Chinese Academy of Agricultural Sciences | Ph.D. (2018. 09 ~ 2021. 12)",
        "Postdoctoral researcher (2024. 03 ~ 2025. 08)",
        "Gaon Park", "E.mail: someone@example.com", "Research Interest", "Education",
        "Chung-Ang University | B.S. (2019. 03 ~ 2023. 02)", "Chung-Ang University | M.S. (2023. 03 ~ 2025. 02)",
        "Researcher (2025. 03 ~ 2025. 06)",
        "Dain Lee", "E.mail: other@example.com", "Education",
        "Chung-Ang University | B.S. (2018. 03 ~ 2024. 02)", "Chung-Ang University | M.S. (2024. 03 ~ 2026. 02)",
        "Google Sites", "Report abuse", "Page details"])
    got = {p["name_en"]: p["degree"] for p in parse_alumni(html)}
    assert got == {"Abebe Bekele Tadesse": "postdoc", "Gaon Park": "ms", "Dain Lee": "ms"}


def test_camel_case_names_with_employer_only():
    html = _page(["Alumni"] + [x for n, w in [("EunHye Jeong", "서지넥스"), ("JiSu Kim", "삼성전자"),
        ("YunMi Seo", "삼성바이오에피스"), ("Garam Kim", "고려대학교 교수"), ("Narae Lee", "LG화학")] for x in (n, w)])
    assert len(parse_alumni(html)) == 5


def test_job_titles_are_not_names():
    pairs = [("김가람 (Garam Kim)", "Samsung Electronics", "Staff Engineer"),
             ("이나래 (Narae Lee)", "SK Hynix Inc.", "Technical Leader"),
             ("박다솜 (Dasom Park)", "Korea Institute of Science and Technology", "Senior Researcher"),
             ("최라온 (Raon Choi)", "Ewha Womans University", "Associate Professor"),
             ("정마루 (Maru Jung)", "Samsung Electronics", "Software Engineer")]
    people = parse_alumni(_page(["Alumni"] + [x for p in pairs for x in p]))
    assert len(people) == 5 and [p["career"] for p in people].count("industry") == 3


def test_zero_width_spaces_and_position_at_leave():
    html = _page(["Alumni​", "Garam Kim", "​", "2020.02. MA/PhD degree", "​",
                  "Position at leave:", "Assistant Professor​", "Chung-Ang University",
                  "Narae Lee", "​", "2024.02. MA degree", "​", "Position at leave:", "SK Bioscience",
                  "Dasom Park", "​", "2025.02. MA degree​", "Position at leave:", "Samsung Electronics"])
    got = {p["name_en"]: (p["degree"], p["end_year"], p["career"]) for p in parse_alumni(html)}
    assert got == {"Garam Kim": ("integrated", 2020, "faculty"), "Narae Lee": ("ms", 2024, "industry"),
                   "Dasom Park": ("ms", 2025, "industry")}


def test_doctor_titles_and_names_in_both_scripts():
    html = _page(["Ph. D. Alumni", "Dr. Garam Kim", "김 가 람", "2018 . 03 ~ 2024. 02",
                  "Dr. Narae Lee", "이 나 래", "2019 . 09 ~ 2023. 08",
                  "M. S. Alumni", "Dasom Park", "박 다 솜", "2018 . 03 ~ 2020. 02"])
    got = {p["name_ko"]: (p["name_en"], p["degree"], p["years"]) for p in parse_alumni(html)}
    assert got == {"김가람": ("Garam Kim", "phd", 6.0), "이나래": ("Narae Lee", "phd", 4.0),
                   "박다솜": ("Dasom Park", "ms", 2.0)}


def test_role_label_above_each_card():
    html = _page(["Alumni"] + [x for name, ko, years, now in [
        ("Dr. Garam Kim", "김가람", "Lab period : 2011/02 ~ 2012/06", "Current : 광운대 전자공학과 교수"),
        ("Dr. Narae Lee", "이나래", "Lab period : 2012/02 ~ 2013/02", "Current : 한국세라믹기술원 책임연구원"),
        ("Dr. Dasom Park", "박다솜", "Lab period : 2014/03 ~ 2017/03", "Current : 삼성전자")]
        for x in ("Post Doc", name, ko, years, now, "더보기")])
    got = {p["name_ko"]: (p["degree"], p["career"]) for p in parse_alumni(html)}
    assert got == {"김가람": ("postdoc", "faculty"), "이나래": ("postdoc", "institute"), "박다솜": ("postdoc", "industry")}


def test_probe_lines_hide_names_and_mail():
    from alumni import _mask_line
    assert "Gyeong" not in _mask_line("JU GYEONG KIM")
    assert "naver" not in _mask_line("e-mail : someone1013@naver.com")
    assert _mask_line("Kim G, Lee N. (2022) A paper title. Journal.") == "[citation]"
    assert "가람" not in _mask_line("김가람 (Garam Kim)") and "Garam" not in _mask_line("김가람 (Garam Kim)")
    assert "Roh" not in _mask_line("Gildong, Roh") and _mask_line("Ph.D") == "Ph.D"
    assert "린다" not in _mask_line("벨린다 Ong National University of Singapore 박사과정")
    assert "Hye" not in _mask_line("EunHye Jeong")


def test_dates_in_a_grid_are_not_cv_profiles():
    html = _page(["Alumni | Hong research group", "Ph. D. Alumni",
                  "Dr. Garam Kim", "김 가 람​", "2018 . 03 ~ 2024. 02",
                  "Dr. Narae Lee", "​이 나 래", "2020 . 03 ~ 2026. 02",
                  "M. S. Alumni", "Dasom Park", "박 다 솜", "2018 . 03 ~ 2020. 02",
                  "Raon Choi", "Maru Jung", "최 라 온", "정 마 루", "2018 . 09 ~ 2020. 08", "2019 . 03 ~ 2021. 02",
                  "Bada Han", "Sora Kang", "Hana Yoon", "한 바 다", "강 소 라", "윤 하 나",
                  "2019 . 03 ~ 2021. 02", "2019 . 09 ~ 2021. 08", "2019 . 09 ~ 2021. 08",
                  "Post. Doc. Alumni", "Dr. Nuri Song", "2018 . 02 ~ 2024. 02"])
    people = parse_alumni(html)
    assert len(people) >= 8
    assert sum(p["degree"] == "phd" for p in people) == 2


def test_research_professor_line_does_not_end_the_section():
    html = _page(["Members", "Alumni", "Garam Kim", "2020.02. MA/PhD degree", "2021.12. Post-Doc", "Research Professor",
                  "Position at leave:", "Assistant Professor", "Narae Lee", "2022.02. MA/PhD degree",
                  "Position at leave:", "Voronoi Inc.", "Research", "Dasom Park", "2023.02. MA degree"])
    assert [p["name_en"] for p in parse_alumni(html, whole_page=False)] == ["Garam Kim", "Narae Lee"]


def test_undergraduate_interns_are_not_alumni():
    html = _page(["Former Members", "2025", "GARAM KIM (Undergraduate student, Kyung Hee University)",
                  "NARAE LEE (Undergraduate student, Kyung Hee University)", "DASOM PARK (Research internship, Some University)",
                  "Raon Choi", "MS", "MAR 2020 ~ FEB 2022", "BASGENBIO"])
    assert [p["name_en"] for p in parse_alumni(html)] == ["Raon Choi"]


def test_probe_hides_names_inside_lines():
    from alumni import _mask_line
    assert "CHOI" not in _mask_line("AREUM CHOI (Undergraduate student, Kyung Hee University)")
    assert "Seok" not in _mask_line("Celebrating Seok June and Dae Yeol's publication")
    assert "KIM" not in _mask_line("JiMin KIM, PhD (August, 2026)")


def test_cv_current_university_department_is_faculty():
    html = _page(["Alumni", "Doctor of Philosophy", "김가람 Ph.D.",
        "1998.03 ~ 2004.02 : College of Pharmacy, Seoul National University, Ph.D. (Pharmaceutics)",
        "2015.03 ~ present : 강원대학교 약학대학",
        "Doctor of Philosophy", "이나래 Ph.D.",
        "2007.03 ~ 2012.02 : College of Pharmacy, Seoul National University, Ph.D.",
        "2013.09 ~ 2017.10 : 종근당", "present : 우석대학교 약학과",
        "Doctor of Philosophy", "박다솜 Ph.D.",
        "2004.03 ~ 2009.02 : College of Pharmacy, Seoul National University, Ph.D.",
        "2009.03 ~ present : 연세대학교 생명시스템대학 행정팀 과장",
        "Master of Science", "최라온 M.S.",
        "2016.03 ~ 2018.02 : College of Pharmacy, Seoul National University, M.S.",
        "2018.03 ~ present : College of Pharmacy, Korea University, Pharm.D."])
    got = {p["name_ko"]: p["career"] for p in parse_alumni(html)}
    assert got == {"김가람": "faculty", "이나래": "faculty", "박다솜": "other", "최라온": "unknown"}


def test_probe_hides_hangul_names():
    from alumni import _mask_line
    assert _mask_line("지아리나") == "지**"
    assert "라만" not in _mask_line("무집 라만")
    assert "준석" not in _mask_line("Professor. 최준석")
    assert _mask_line("Master (2026 후기)") == "M** (2026 후기)" or "2026" in _mask_line("Master (2026 후기)")


def test_manual_entry_with_two_alumni_pages():
    from alumni import find_alumni
    pages = {"https://lab.example.org/alumni/": "<ul><li>김가람 박사후연구원 2014-2017</li><li>현: 고려대학교 교수</li></ul>",
             "https://lab.example.org/grads/": "".join(f"<p>{x}</p>" for x in [
                 "Ph.D. (2026 후기)", "이나래", "Narae Lee", "Master (2026 후기)", "박다솜", "Dasom Park",
                 "Master (2026 전기)", "최라온", "Raon Choi", "Master (2026 전기)", "정마루", "Maru Jung"])}

    class Fetch:
        session = None

        def html(self, url):
            return pages.get(url, "")

    lab, used, people = find_alumni(Fetch(), "", {"lab_url": "https://lab.example.org/",
                                                  "alumni_url": "https://lab.example.org/alumni/;https://lab.example.org/grads/"})
    assert len(used) == 2
    assert sorted(p["degree"] for p in people) == ["ms", "ms", "ms", "phd", "postdoc"]


def test_fresh_graduate_is_not_a_same_name_professor():
    import pandas as pd
    from alumni import ProfessorIndex
    index = ProfessorIndex(pd.DataFrame([{"professor_id": "P9001", "name_ko": "김가람", "name_en": "Garam Kim",
                                          "university": "Korea University"}]))
    fresh = {"name_ko": "김가람", "name_en": "Garam Kim", "degree": "ms", "end_year": date.today().year,
             "career": "unknown", "position": ""}
    assert index.find(fresh, "P0001") == ""
    old = dict(fresh, end_year=2012, degree="phd")
    assert index.find(old, "P0001") == "P9001"
    now_prof = dict(fresh, career="faculty", position="고려대학교 조교수")
    assert index.find(now_prof, "P0001") == "P9001"


def test_years_written_above_each_name():
    html = _page(["ALUMNI", "2008-2012", "Garam Kim, PH.D", "(#1 Ph.D.)", "Professor, Korea University",
                  "2007-2013", "Narae Lee, PH.D.", "(#2 Ph.D.)", "Research assistant professor, Hanyang University",
                  "2012-2018", "Dasom Park, PH.D", "(#3 Ph.D.)", "Samsung Biologics",
                  "2016-2018", "Raon Choi, M.S.", "(#1 M.S.)", "LG Chem"])
    got = {p["name_en"]: (p["degree"], p["start_year"], p["end_year"], p["career"]) for p in parse_alumni(html)}
    assert got == {"Garam Kim": ("phd", 2008, 2012, "faculty"), "Narae Lee": ("phd", 2007, 2013, "faculty"),
                   "Dasom Park": ("phd", 2012, 2018, "industry"), "Raon Choi": ("ms", 2016, 2018, "industry")}


def test_manual_page_numbers_are_followed_until_nothing_new():
    from alumni import find_alumni
    def page(names):
        return "".join(f"<p>Ph.D. (20{y} 후기)</p><p>{n}</p>" for y, n in names)
    pages = {"https://lab.example.org/grads?p=1": page([(20, "김가람"), (21, "이나래"), (22, "박다솜")]),
             "https://lab.example.org/grads?p=2": page([(18, "최라온"), (19, "정마루"), (17, "한지민")]),
             "https://lab.example.org/grads?p=3": page([(18, "최라온"), (19, "정마루"), (17, "한지민")])}

    class Fetch:
        session = None

        def html(self, url):
            return pages.get(url, "")

    _, used, people = find_alumni(Fetch(), "", {"alumni_url": "https://lab.example.org/grads?p={page}"})
    assert len(people) == 6 and used == ["https://lab.example.org/grads?p={page}"]


def test_name_email_career_cards_under_degree_headings():
    rows = ["Home > People > Alumni", "Ph.D."]
    for name, career in [("김가람 (Garam Kim)", "CJ 제일제당"), ("이나래 (Narae Lee)", "한남대학교 교수"),
                         ("박다솜 (Dasom Park)", "한국화학연구원")]:
        rows += ["Name", name, "E-mail", "someone@example.com", "Career", career]
    rows += ["M.S."]
    for name, career in [("최라온 (Raon Choi)", "LG화학"), ("정마루 (Maru Jung)", "삼성바이오로직스"), ("한지민 (Jimin Han)", "")]:
        rows += ["Name", name, "E-mail", "someone@example.com", "Career"] + ([career] if career else [])
    got = {p["name_ko"]: (p["degree"], p["career"]) for p in parse_alumni(_page(rows))}
    assert got == {"김가람": ("phd", "industry"), "이나래": ("phd", "faculty"), "박다솜": ("phd", "institute"),
                   "최라온": ("ms", "industry"), "정마루": ("ms", "industry"), "한지민": ("ms", "unknown")}


def test_stated_degree_time_and_foreign_names_in_hangul():
    html = _page(["졸업생", "김가람 (Garam Kim)", "석사 2년", "이나래 (Narae Lee)", "석박사통합 6년",
                  "카오 비엣 푸옹 (Cao Viet Phuong)", "석사2년", "박다솜 (Dasom Park)", "박사4년", "Career:", "LG화학"])
    got = {p["name_ko"]: (p["degree"], p["years"], p["years_exact"]) for p in parse_alumni(html)}
    assert got == {"김가람": ("ms", 2.0, True), "이나래": ("integrated", 6.0, True),
                   "카오비엣푸옹": ("ms", 2.0, True), "박다솜": ("phd", 4.0, True)}


def test_probe_hides_names_with_romanised_brackets():
    from alumni import _mask_line
    assert "비엣" not in _mask_line("카오 비엣 푸옹 (Cao Viet Phuong)")
    assert "solji" not in _mask_line("박** (Park solji)")
    assert "사바" not in _mask_line("사바리나탄")
    assert _mask_line("LG 생활건강") == "LG 생활건강" or "생활건강" in _mask_line("LG 생활건강")


def test_frame_urls():
    from alumni import frame_urls
    html = ('<frameset><frame src="top.html"><frame src="/web/2020/http://lab.example.org/main.html"></frameset>'
            '<meta http-equiv="refresh" content="0; url=home.php">')
    assert frame_urls(html, "https://web.archive.org/web/2020/http://lab.example.org/") == [
        "https://web.archive.org/web/2020/http://lab.example.org/top.html",
        "https://web.archive.org/web/2020/http://lab.example.org/main.html",
        "https://web.archive.org/web/2020/http://lab.example.org/home.php"]


def test_alumni_given_by_hand(tmp_path, monkeypatch):
    import alumni
    monkeypatch.setattr(alumni, "RECORDS_DIR", tmp_path)
    (tmp_path / "P0001.csv").write_text(
        "degree,start,end,months,career,position,name_ko,note\n"
        "integrated,2008.03,2013.02,59,industry,삼성SDI,,\n"
        "phd,2008.09,2012.02,41,faculty,인하대학교 생명과학과 부교수,김가람,\n"
        "postdoc,2008.08,2013.08,,unknown,,,\n"
        "integrated,2022.02,2024.02,,industry,삼성전자,,학위기간 통계 제외\n", encoding="utf-8")
    people = alumni.manual_records("P0001")
    assert [(p["degree"], p["years"], p["career"]) for p in people] == [
        ("integrated", 4.9, "industry"), ("phd", 3.4, "faculty"), ("postdoc", None, "unknown"),
        ("integrated", None, "industry")]
    assert people[1]["name_ko"] == "김가람" and people[0]["name_ko"] == ""
    assert alumni.manual_records("P0002") == []


def test_korean_university_in_position_links_alumnus(monkeypatch):
    import pandas as pd
    import alumni
    monkeypatch.setattr(alumni, "korean_universities", lambda: {"korea university": "Korea University",
                                                                  "고려대학교": "Korea University"})
    index = alumni.ProfessorIndex(pd.DataFrame([{"professor_id": "P9001", "name_ko": "김가람", "name_en": "",
                                                 "university": "Korea University"}]))
    alum = {"name_ko": "김가람", "name_en": "", "degree": "phd", "end_year": 2012, "career": "faculty",
            "position": "고려대 생명공학과 조교수"}
    assert index.find(alum, "P0001") == "P9001"
