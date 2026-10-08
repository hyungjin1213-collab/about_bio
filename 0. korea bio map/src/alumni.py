"""Lab alumni: where graduates went, how long their degree took, and how good their papers were.

For each professor:

1. find the lab homepage: data/lab_sites_manual.csv first, then links on the
   professor's official profile page ("연구실 홈페이지", "Lab", ...), then the
   researcher URLs on their ORCID record;
2. find the alumni page on the lab site ("Alumni", "졸업생", or an alumni
   section of the members page);
3. read each alumnus: degree (박사 / 석박통합 / 석사 / 포닥), years, and the
   current position, classified as 교수 / 포닥 / 기업 / 병원 / 연구소 / 기타;
4. find their papers with the professor in OpenAlex during the degree years:
   papers, first-author papers, field-weighted citation impact (FWCI) and
   top-10% papers.

Only per-lab totals are committed and shown on the map
(output/lab_alumni_summary.csv). Alumni are mostly private people, so the
per-person rows go to output/alumni/ (git-ignored; kept as a short-lived run
artifact for checking the parser).

Labs are checked in batches (ALUMNI_PROFESSOR_LIMIT per run), oldest check
first; data/lab_sites.csv remembers what was found.
"""
from __future__ import annotations

import os
import re
import statistics
import time
from collections import Counter
from datetime import date, datetime, timezone
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

from config import DATA_DIR, OUTPUT_DIR
from name_extraction import english_matches_korean, is_korean_person_name, normalize_english_name
from openalex_client import OpenAlexClient, OpenAlexUnavailable, normalize_openalex_id

SAMPLE_LABS = int(os.getenv("ALUMNI_SAMPLE_LABS", "40"))
PROFESSOR_LIMIT = int(os.getenv("ALUMNI_PROFESSOR_LIMIT", "700"))
RECHECK_DAYS = int(os.getenv("ALUMNI_RECHECK_DAYS", "60"))
DELAY = float(os.getenv("ALUMNI_DELAY_SECONDS", "0.5"))
TIMEOUT = 12
USER_AGENT = "Mozilla/5.0 (compatible; AboutBioFacultyCollector/2.1)"

SITES_PATH = DATA_DIR / "lab_sites.csv"
MANUAL_PATH = DATA_DIR / "lab_sites_manual.csv"
SUMMARY_PATH = OUTPUT_DIR / "lab_alumni_summary.csv"
PEOPLE_DIR = OUTPUT_DIR / "alumni"
SITE_COLUMNS = ["professor_id", "lab_url", "alumni_urls", "status", "alumni_found", "checked_at", "parser"]
# Bump when the parser changes: labs read by an older parser are read again.
PARSER_VERSION = "3"
MANUAL_COLUMNS = ["professor_id", "name", "lab_url", "alumni_url", "memo"]

LAB_LINK_WORDS = ["연구실 홈페이지", "연구실홈페이지", "홈페이지", "연구실", "lab homepage", "lab website", "homepage",
                  "home page", "website", "laboratory", " lab", "lab ", "group"]
NOT_LAB_HOSTS = ["facebook.", "youtube.", "instagram.", "twitter.", "x.com", "linkedin.", "scholar.google",
                 "orcid.org", "pubmed", "ncbi.nlm", "researchgate", "scopus.", "webofscience", "naver.",
                 "kakao", "blog.", "github.com", "openalex.org", "doi.org", "google.com/maps"]
ALUMNI_WORDS = ["alumni", "alumnus", "alumnae", "former member", "past member", "graduates", "graduated",
                "졸업생", "졸업 연구원", "졸업연구원", "former student", "former researcher", "former lab"]
# Department news / boards / chair lists are not a lab's alumni.
NOT_ALUMNI_URL = re.compile(r"news|notice|board|bbs|articleNo|chair|dean|학과장|공지|게시판", re.I)
# A short line naming a degree group: names under it share that degree.
GROUP_WORDS = ["alumni", "alumnae", "graduates", "students", "former", "members", "졸업생", "동문", "졸업자"]
MEMBER_WORDS = ["members", "member", "people", "구성원", "멤버", "연구원 소개", "lab members"]
STOP_WORDS = ["publication", "논문", "research", "연구분야", "연구 분야", "contact", "오시는", "news", "소식",
              "gallery", "갤러리", "copyright", "©"]

DEGREES = [  # (kind, pattern); order matters: integrated before PhD/MS.
    ("integrated", r"석\s*박\s*사?\s*통합|통합\s*과정|integrated|combined\s+m\.?s\.?\s*[-/&]\s*ph\.?d"),
    ("postdoc", r"post[-\s]?doc|박사\s*후|research\s+fellow|박사후연구원"),
    ("phd", r"ph\.?\s?d\.?|박사|doctor(?:al|ate)?"),
    ("ms", r"\bm\.?\s?s\.?\b|\bm\.?\s?sc\.?\b|석사|master"),
    ("undergrad", r"\bb\.?\s?s\.?\b|학부|undergrad|intern|인턴"),
]
TYPICAL_YEARS = {"phd": (2.0, 9.0), "integrated": (3.0, 10.0), "ms": (1.0, 4.0), "postdoc": (0.5, 8.0)}
CAREERS = [  # (kind, pattern); first match wins.
    ("postdoc", r"post[-\s]?doc|박사\s*후|research\s+fellow|박사후"),
    ("faculty", r"professor|\bprof\b\.?|교수|lecturer|faculty|강사"),
    ("hospital", r"hospital|병원|medical\s+center|의료원|resident|레지던트|전공의|clinic|의원"),
    ("industry", r"\binc\b|\bco\.|\bltd\b|\bcorp|주식회사|\(주\)|제약|약품|samsung|삼성|\blg\b|"
                 r"\bsk\b|celltrion|셀트리온|유한|한미|녹십자|종근당|대웅|\bcj\b|amore|아모레|콜마|kolmar|genentech|novartis|"
                 r"pfizer|roche|merck|astrazeneca|startup|스타트업|회사|기업|화학|전자|헬스케어|바이오로직스|바이오사이언스|"
                 r"특허|변리사|consult|컨설팅"),
    # Generic words count as industry only away from schools and institutes
    # ("Program in Department of Pharmaceutics" is not a company).
    ("industry_weak", r"pharm|biologics|bioscience|biotech|company|scientist|engineer|patent"),
    ("institute", r"kist|kribb|생명공학연구원|ibs|기초과학연구원|institute|연구원|연구소|nih|nci|max\s+planck|"
                  r"식약처|mfds|질병관리|kdca|government|정부|공무원"),
]
# Table headers and degree words that look like Korean names (박사 = 박 + 사).
NOT_NAMES = {"이름", "성명", "학위", "기간", "현재", "현직", "박사", "석사", "학사", "소속", "졸업", "입학", "비고", "과정",
             "연구원", "교수", "진로", "근무처", "연도", "년도", "분야", "직위", "직책", "구분", "졸업생", "동문", "학위과정",
             "사이트맵", "홈페이지", "로그인", "연구실", "교수소개", "연구분야", "오시는길", "공지사항", "갤러리", "바로가기",
             "상세보기", "메일", "주소", "전화", "팩스", "소개", "자료실", "후기", "현황"}
POSITION_MARKERS = r"(?:current(?:ly)?(?:\s+position)?|present(?:\s+position)?|now|현재|현직|현\s|→|->|=>|▶)\s*[:：]?\s*"
YEAR_RE = re.compile(r"((?:19[89]|20[0-4])\d)(?:\s*[./]\s*(\d{1,2}))?")
EN_NAME_RE = re.compile(r"^([A-Z][a-z]+(?:[-\s][A-Z]?[a-z]+)?(?:[\s,]+[A-Z][a-z]+(?:[-\s][A-Z]?[a-z]+)?){1,2})\b")
EN_NAME_STOP = {"current", "alumni", "former", "present", "member", "members", "postdoc", "professor", "research",
                "samsung", "korea", "seoul", "university", "institute", "department", "school", "college", "lab",
                "laboratory", "phd", "master", "graduate", "student", "students", "now", "position", "thesis",
                "senior", "principal", "assistant", "associate", "scientist", "researcher", "the", "and", "at", "of"}


# ------------------------------------------------------------------ parsing
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def degree_of(text: str) -> str:
    """The degree earned in the lab; a postdoc only when no student degree is named
    ('Ph.D. 2019, postdoc at Harvard' is a Ph.D.; '박사후연구원' is not a 박사)."""
    low = text.casefold()
    postdoc = dict(DEGREES)["postdoc"]
    student = re.sub(postdoc, " ", low)
    for kind, pattern in DEGREES:
        if kind != "postdoc" and kind != "undergrad" and re.search(pattern, student):
            return kind
    if re.search(postdoc, low):
        return "postdoc"
    return "undergrad" if re.search(dict(DEGREES)["undergrad"], low) else ""


def career_of(position: str) -> str:
    low = position.casefold()
    if not low.strip():
        return ""
    academic = re.search(r"department|dept\.?|program|school|college|universit|institute|대학|학과|과정|연구원|연구소", low)
    for kind, pattern in CAREERS:
        if re.search(pattern, low):
            if kind == "industry_weak":
                if academic:
                    continue
                kind = "industry"
            return kind
    return "other"


def years_of(text: str, degree: str = "") -> tuple[int | None, int | None, float | None]:
    """(start, end, duration in years) from '2015.03 - 2020.08', '2015~2020', '2020' (graduation only)."""
    found = [(int(y), int(m) if m and 1 <= int(m) <= 12 else None) for y, m in YEAR_RE.findall(text)]
    found = [(y, m) for y, m in found if 1980 <= y <= date.today().year + 1]
    if not found:
        return None, None, None
    if len(found) == 1:
        return None, found[0][0], None
    key = lambda ym: (ym[0], ym[1] or 0)
    (y1, m1), (y2, m2) = min(found, key=key), max(found, key=key)
    if y1 == y2:
        return None, y2, None
    duration = (y2 + ((m2 or 8) - 1) / 12) - (y1 + ((m1 or 3) - 1) / 12) if (m1 or m2) else float(y2 - y1)
    low, high = TYPICAL_YEARS.get(degree, (0.5, 10.0))
    return y1, y2, round(duration, 1) if low <= duration <= high else None


def _name_in(line: str) -> tuple[str, str]:
    """(korean, english) name at the start of a line ('', '' if none)."""
    line = line.strip(" -•·*|:")
    # Sentences and menus are not name lines ("최근에 해외 유학 ...", "사이트맵").
    sentence = len(line) > 90 or re.search(r"(다|요|니다)[.!]?$", line)
    m = None if sentence else re.match(r"^([가-힣]{2,4})(?=$|[\s(,/·|:\-])", line)
    if m and re.search(r"[에는을를의로도서께]$", m.group(1)):
        m = None
    if m and m.group(1) not in NOT_NAMES and not degree_of(m.group(1)) and is_korean_person_name(m.group(1)):
        rest = line[m.end():]
        en = re.match(r"^\s*\(?\s*([A-Z][A-Za-z\-]+(?:[\s,]+[A-Z][A-Za-z\-]+){1,2})", rest)
        return m.group(1), normalize_english_name(en.group(1)) if en else ""
    m = EN_NAME_RE.match(line)
    if m:
        words = [w for w in re.split(r"[\s,\-]+", m.group(1)) if w]
        if len(words) >= 2 and not any(w.casefold() in EN_NAME_STOP for w in words):
            ko = re.search(r"\(([가-힣]{2,4})\)", line)
            return (ko.group(1) if ko and is_korean_person_name(ko.group(1)) else ""), normalize_english_name(m.group(1))
    return "", ""


def _section_lines(soup: BeautifulSoup, whole_page: bool) -> list[str]:
    """Lines of the alumni part of a page (the whole page for a dedicated alumni page)."""
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form"]):
        tag.decompose()
    lines = [_clean(x) for x in soup.get_text("\n").split("\n")]
    lines = [x for x in lines if x]
    for i, line in enumerate(lines):
        if re.search(r"copyright|all rights reserved|©", line, flags=re.I):
            lines = lines[:i]
            break
    if whole_page:
        return lines
    for i, line in enumerate(lines):
        if len(line) <= 40 and any(w in line.casefold() for w in ALUMNI_WORDS):
            out = []
            for nxt in lines[i + 1:]:
                if len(nxt) <= 30 and any(w in nxt.casefold() for w in STOP_WORDS):
                    break
                out.append(nxt)
            return out
    return []


def parse_alumni(html: str, whole_page: bool = True) -> list[dict]:
    """One dict per alumnus found on an alumni page (or the alumni section of a members page)."""
    lines = _section_lines(BeautifulSoup(html, "html.parser"), whole_page)
    entries: list[dict] = []
    current: dict | None = None
    group = ""      # degree of the heading the names sit under ("Ph.D. Alumni")
    for line in lines:
        ko, en = _name_in(line)
        low = line.casefold()
        if (not ko and not en and len(line) <= 40 and not YEAR_RE.search(line)
                and degree_of(line) and any(w in low for w in GROUP_WORDS)):
            group, current = degree_of(line), None
            continue
        if ko or en:
            current = {"name_ko": ko, "name_en": en, "lines": [line], "group": group}
            entries.append(current)
        elif current is not None and len(current["lines"]) < 5 and len(line) <= 200:
            current["lines"].append(line)
    people = []
    for e in entries:
        text = " | ".join(e["lines"])
        marker = re.search(POSITION_MARKERS, text, flags=re.I)
        degree = degree_of(text[:marker.start()] if marker else text) or e["group"]
        start, end, duration = years_of(text, degree)
        if marker:
            position = text[marker.end():]
        else:
            position = " | ".join(e["lines"][1:]) if len(e["lines"]) > 1 else text
            position = YEAR_RE.sub(" ", position)
            # Keep "postdoc" whole before degree words go ("박사후연구원" is not "후연구원").
            position = re.sub(dict(DEGREES)["postdoc"], " postdoc ", position, flags=re.I)
            for _, pattern in DEGREES[:1] + DEGREES[2:]:
                position = re.sub(pattern, " ", position, flags=re.I)
        position = re.sub(r"\S+@\S+|https?://\S+", " ", position)
        position = _clean(re.sub(r"[|()\[\]~\-–,:]+", " ", position))[:160]
        career = career_of(position)
        if not marker and (career == "other" or (career == "postdoc" and degree == "postdoc")):
            career = ""     # leftover words of the entry / the lab role itself, not a stated position
        # A name alone is not an alumnus: nav links, captions, the PI's name.
        if not (degree or end or career):
            continue
        people.append({"name_ko": e["name_ko"], "name_en": e["name_en"], "degree": degree or "unknown",
                       "start_year": start, "end_year": end, "years": duration,
                       "position": position, "career": career or "unknown", "raw": text[:300]})
    return people


# ------------------------------------------------------------------ fetching
class Fetcher:
    def __init__(self, session: requests.Session | None = None, delay: float = DELAY):
        self.session = session or requests.Session()
        self.delay = delay
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._last = 0.0
        self.count = 0

    def _get(self, url: str) -> requests.Response:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.count += 1
        headers = {"User-Agent": USER_AGENT}
        try:
            return self.session.get(url, timeout=TIMEOUT, headers=headers)
        except requests.exceptions.SSLError:
            # Many Korean university sites serve an incomplete certificate chain;
            # these are public pages read without credentials.
            return self.session.get(url, timeout=TIMEOUT, headers=headers, verify=False)

    def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self._get(base + "/robots.txt")
                rp.parse(r.text.splitlines()) if r.status_code == 200 else None
                rp = rp if r.status_code == 200 else None
            except requests.RequestException:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def html(self, url: str) -> str:
        if not url.startswith(("http://", "https://")) or not self.allowed(url):
            return ""
        try:
            r = self._get(url)
        except requests.RequestException:
            return ""
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            return ""
        r.encoding = r.apparent_encoding or r.encoding
        return r.text


def _links(html: str, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        text = _clean(a.get_text(" ") or a.get("title", "") or "")
        out.append((text, urljoin(base, href).split("#")[0]))
    return out


def lab_links(html: str, base: str) -> list[str]:
    """Lab homepage candidates on a professor's profile page, best first."""
    scored = []
    for text, url in _links(html, base):
        low_text, low_url = f" {text.casefold()} ", url.casefold()
        if any(h in low_url for h in NOT_LAB_HOSTS) or url.rstrip("/") == base.rstrip("/"):
            continue
        score = 0
        if any(w in low_text for w in LAB_LINK_WORDS):
            score += 2
        if re.search(r"lab|labs\.|sites\.google|wixsite|wordpress|\.ac\.kr/~|/~", low_url):
            score += 1
        if score:
            scored.append((score, url))
    seen, out = set(), []
    for _, url in sorted(scored, key=lambda x: -x[0]):
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out[:3]


def alumni_links(html: str, base: str) -> tuple[list[str], list[str]]:
    """(alumni pages, member pages) linked from a lab page."""
    alumni, members = [], []
    host = urlparse(base).netloc
    for text, url in _links(html, base):
        if urlparse(url).netloc != host:
            continue
        hay = f"{text} {urlparse(url).path}".casefold()
        if NOT_ALUMNI_URL.search(url) or NOT_ALUMNI_URL.search(text):
            continue
        if any(w in hay for w in ALUMNI_WORDS):
            alumni.append(url)
        elif any(w in hay for w in MEMBER_WORDS):
            members.append(url)
    return list(dict.fromkeys(alumni))[:3], list(dict.fromkeys(members))[:2]


def orcid_urls(orcid: str, session: requests.Session) -> list[str]:
    if not orcid:
        return []
    try:
        r = session.get(f"https://pub.orcid.org/v3.0/{orcid}/researcher-urls",
                        headers={"Accept": "application/json"}, timeout=TIMEOUT)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    urls = [((u.get("url") or {}).get("value") or "") for u in (r.json() or {}).get("researcher-url", []) or []]
    return [u for u in urls if u.startswith("http") and not any(h in u.casefold() for h in NOT_LAB_HOSTS)][:2]


def find_alumni(fetch: Fetcher, profile_url: str, manual: dict, orcid: str = "") -> tuple[str, list[str], list[dict]]:
    """(lab url, alumni page urls, alumni) for one professor."""
    if manual.get("alumni_url"):
        html = fetch.html(manual["alumni_url"])
        return manual.get("lab_url", ""), [manual["alumni_url"]], parse_alumni(html) if html else []
    candidates = [manual["lab_url"]] if manual.get("lab_url") else []
    profile_html = ""
    if not candidates and profile_url.startswith("http") and "openalex.org" not in profile_url:
        profile_html = fetch.html(profile_url)
        # A personal page on the department site can itself be the lab page.
        candidates = lab_links(profile_html, profile_url) + ([profile_url] if profile_html else [])
    if not candidates:
        candidates = orcid_urls(orcid, fetch.session)
    for lab_url in candidates:
        html = profile_html if lab_url == profile_url else fetch.html(lab_url)
        if not html:
            continue
        alumni_pages, member_pages = alumni_links(html, lab_url)
        people, used = [], []
        for url in alumni_pages:
            found = parse_alumni(fetch.html(url))
            if found:
                people += found
                used.append(url)
        if not people:
            # No alumni page: an alumni section on the lab or members page.
            for url in [lab_url] + member_pages:
                page = html if url == lab_url else fetch.html(url)
                found = parse_alumni(page, whole_page=False) if page else []
                if found:
                    people += found
                    used.append(url)
                    break
        if people or alumni_pages or member_pages:
            return lab_url, used, people
    return (candidates[0] if candidates else ""), [], []


# ------------------------------------------------------------------ papers
def _same_person(author_name: str, alum: dict) -> bool:
    if not author_name:
        return False
    if alum.get("name_en"):
        a = [t for t in re.split(r"[\s,.\-]+", normalize_english_name(author_name).casefold()) if t]
        b = [t for t in re.split(r"[\s,.\-]+", alum["name_en"].casefold()) if t]
        if len(a) >= 2 and len(b) >= 2 and a[-1] == b[-1]:
            return "".join(a[:-1]) == "".join(b[:-1]) or [x[0] for x in a[:-1]] == [x[0] for x in b[:-1]]
    if alum.get("name_ko"):
        return english_matches_korean(author_name, alum["name_ko"])
    return False


def _window(alum: dict) -> tuple[int, int] | None:
    end, start = alum.get("end_year"), alum.get("start_year")
    if not end:
        return None
    if not start:
        start = end - {"phd": 5, "integrated": 6, "ms": 2, "postdoc": 3}.get(alum.get("degree", ""), 4)
    return start, end + 1   # papers from the degree often appear the year after


def add_papers(client: OpenAlexClient, openalex_ids: list[str], people: list[dict]) -> None:
    """Papers each alumnus wrote with the professor during their time in the lab."""
    todo = [p for p in people if _window(p)]
    ids = "|".join(normalize_openalex_id(i) for i in openalex_ids if normalize_openalex_id(i))
    if not todo or not ids:      # no OpenAlex profile: no papers to match
        return
    for p in todo:
        p.update(papers=0, first_author=0, fwci=[], top10=0, journals=Counter(), paper_ids=[])
    cursor = "*"
    while cursor:
        data = client._get("works", {"filter": f"author.id:{ids}", "per-page": 200, "cursor": cursor,
                                     "select": "id,publication_year,authorships,primary_location,fwci,"
                                               "citation_normalized_percentile"})
        for w in data.get("results", []) or []:
            year = w.get("publication_year") or 0
            source = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
            for p in todo:
                lo, hi = _window(p)
                if not lo <= year <= hi:
                    continue
                for a in w.get("authorships") or []:
                    if _same_person((a.get("author") or {}).get("display_name", ""), p):
                        p["papers"] += 1
                        p["paper_ids"].append(w.get("id", ""))
                        if a.get("author_position") == "first":
                            p["first_author"] += 1
                            if source:
                                p["journals"][source] += 1
                        if w.get("fwci") is not None:
                            p["fwci"].append(float(w["fwci"]))
                        if (w.get("citation_normalized_percentile") or {}).get("is_in_top_10_percent"):
                            p["top10"] += 1
                        break
        cursor = (data.get("meta") or {}).get("next_cursor")


# ------------------------------------------------------------------ summary
RECENT_YEARS = 5
CAREER_KINDS = ("faculty", "postdoc", "industry", "hospital", "institute", "other")
STAT_KEYS = ("alumni", "phd_graduates", "ms_graduates", "postdoc_alumni", "phd_years", "integrated_years", "ms_years",
             "students_with_papers", "papers_per_student", "first_author_per_student", "fwci_median", "top10_share")


def _stats(people: list[dict]) -> dict:
    """Lab totals for one group of alumni (all of them, or the recent ones)."""
    def med(values):
        values = [v for v in values if v is not None]
        return round(statistics.median(values), 1) if values else ""

    grads = [p for p in people if p["degree"] in ("phd", "integrated", "ms")]
    careers = Counter(p["career"] for p in people if p["career"] not in ("unknown", ""))
    with_papers = [p for p in grads if "papers" in p]
    fwci = {}
    for p in with_papers:
        for wid, f in zip(p["paper_ids"], p["fwci"]):
            fwci[wid] = f
    all_ids = {w for p in with_papers for w in p["paper_ids"]}
    top10 = sum(p["top10"] for p in with_papers)
    journals = Counter()
    for p in with_papers:
        journals.update(p["journals"])
    return {
        "alumni": len(people),
        "phd_graduates": sum(p["degree"] in ("phd", "integrated") for p in people),
        "ms_graduates": sum(p["degree"] == "ms" for p in people),
        "postdoc_alumni": sum(p["degree"] == "postdoc" for p in people),
        "phd_years": med(p["years"] for p in people if p["degree"] == "phd"),
        "integrated_years": med(p["years"] for p in people if p["degree"] == "integrated"),
        "ms_years": med(p["years"] for p in people if p["degree"] == "ms"),
        **{f"career_{k}": careers.get(k, 0) for k in CAREER_KINDS},
        "students_with_papers": len(with_papers),
        "papers_per_student": round(sum(p["papers"] for p in with_papers) / len(with_papers), 1) if with_papers else "",
        "first_author_per_student": round(sum(p["first_author"] for p in with_papers) / len(with_papers), 1)
        if with_papers else "",
        "fwci_median": med(fwci.values()),
        "top10_share": round(top10 / len(all_ids), 2) if all_ids else "",
        "top_journals": "; ".join(j for j, _ in journals.most_common(3)),
    }


def summarize(pid: str, lab_url: str, pages: list[str], people: list[dict], today: date | None = None) -> dict:
    """All-time totals plus recent_* totals for alumni who left in the last RECENT_YEARS years."""
    today = today or date.today()
    recent = [p for p in people if p.get("end_year") and p["end_year"] >= today.year - RECENT_YEARS]
    return {"professor_id": pid, "lab_url": lab_url, "alumni_urls": ";".join(pages),
            **_stats(people), **{f"recent_{k}": v for k, v in _stats(recent).items()},
            "checked_at": today.isoformat()}


def _read(path, columns=None) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns or [])
    df = pd.read_csv(path, dtype=str).fillna("")
    for c in columns or []:
        if c not in df.columns:
            df[c] = ""
    return df


def ensure_manual_file() -> None:
    if not MANUAL_PATH.exists():
        pd.DataFrame(columns=MANUAL_COLUMNS).to_csv(MANUAL_PATH, index=False, encoding="utf-8-sig")


def collect_alumni(client: OpenAlexClient | None = None, fetch: Fetcher | None = None,
                   limit: int = PROFESSOR_LIMIT) -> pd.DataFrame:
    client = client or OpenAlexClient()
    fetch = fetch or Fetcher()
    ensure_manual_file()
    professors = _read(OUTPUT_DIR / "professors_enriched.csv")
    if professors.empty:
        professors = _read(DATA_DIR / "professors_seed.csv")
    sites = _read(SITES_PATH, SITE_COLUMNS)
    manual = {r["professor_id"]: r.to_dict() for _, r in _read(MANUAL_PATH, MANUAL_COLUMNS).iterrows()
              if r["professor_id"]}
    checked = dict(zip(sites["professor_id"], sites["checked_at"]))
    parsed = dict(zip(sites["professor_id"], sites["parser"]))
    today = date.today()

    def due(pid: str) -> tuple[int, str]:
        if pid in manual:
            return (0, "")                      # hand-entered pages first
        last = checked.get(pid, "")
        if not last or parsed.get(pid, "") != PARSER_VERSION:
            return (1, "")
        return (2, last) if (today - date.fromisoformat(last)).days >= RECHECK_DAYS else (9, last)

    order = sorted(professors["professor_id"], key=due)
    order = [p for p in order if due(p)[0] < 9][:limit]
    rows = professors.set_index("professor_id")
    summaries, site_rows, people_rows = [], [], []
    started = datetime.now(timezone.utc)
    openalex_ok = True
    old_summary = _read(SUMMARY_PATH)

    def save() -> pd.DataFrame:
        """Write what this run has found so far (also mid-run: a timeout keeps the work)."""
        done = {x["professor_id"] for x in site_rows}
        all_sites = pd.concat([sites[~sites["professor_id"].isin(done)], pd.DataFrame(site_rows, columns=SITE_COLUMNS)],
                              ignore_index=True)
        # A page credited to several professors is a department page, not a lab's alumni.
        users: dict[str, set[str]] = {}
        for _, x in all_sites.iterrows():
            for u in filter(None, str(x["alumni_urls"]).split(";")):
                users.setdefault(u, set()).add(x["professor_id"])
        shared = {x["professor_id"] for _, x in all_sites.iterrows() if x["professor_id"] not in manual
                  and x["alumni_urls"] and all(len(users[u]) > 1 for u in str(x["alumni_urls"]).split(";") if u)}
        all_sites.loc[all_sites["professor_id"].isin(shared), "status"] = "shared_page"
        all_sites.to_csv(SITES_PATH, index=False, encoding="utf-8-sig")
        kept = old_summary[~old_summary["professor_id"].isin(done)] if not old_summary.empty else old_summary
        summary = pd.concat([kept, pd.DataFrame(summaries)], ignore_index=True)
        if not summary.empty:
            summary = summary[~summary["professor_id"].isin(shared)]
        summary.to_csv(SUMMARY_PATH, index=False, encoding="utf-8-sig")
        PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(people_rows).to_csv(PEOPLE_DIR / f"lab_alumni_people_{today.isoformat()}.csv",
                                         index=False, encoding="utf-8-sig")
        return summary

    for n, pid in enumerate(order, 1):
        r = rows.loc[pid]
        try:
            lab_url, pages, people = find_alumni(fetch, str(r.get("source_url", "")), manual.get(pid, {}),
                                                 str(r.get("orcid", "")))
        except Exception as exc:  # one broken site must not stop the batch
            print(f"[alumni] {pid}: {exc}", flush=True)
            lab_url, pages, people = "", [], []
        if people and openalex_ok:
            try:
                add_papers(client, str(r.get("openalex_id", "")).split(";"), people)
            except requests.RequestException as exc:
                # Half-counted papers would mislead: drop them for this lab.
                for p in people:
                    for k in ("papers", "first_author", "fwci", "top10", "journals", "paper_ids"):
                        p.pop(k, None)
                print(f"[alumni] {pid}: papers skipped: {exc}", flush=True)
                if isinstance(exc, OpenAlexUnavailable):
                    openalex_ok = False      # budget spent: keep reading alumni pages only
        if people:
            if len(summaries) < SAMPLE_LABS:
                # Parser check in the run log; names masked (김**), positions cut short.
                for p in people[:3]:
                    who = (p["name_ko"] or p["name_en"] or "?")[:1] + "**"
                    print(f"[alumni sample] {pid} {who} {p['degree']} {p['start_year']}-{p['end_year']} "
                          f"{p['career']} | {p['position'][:40]}", flush=True)
            summaries.append(summarize(pid, lab_url, pages, people))
            for p in people:
                people_rows.append({"professor_id": pid, **{k: v for k, v in p.items()
                                                             if k not in ("fwci", "journals", "paper_ids")},
                                    "fwci_mean": round(statistics.mean(p["fwci"]), 2) if p.get("fwci") else ""})
        status = "alumni" if people else ("lab_site" if lab_url else "no_lab_site")
        site_rows.append({"professor_id": pid, "lab_url": lab_url, "alumni_urls": ";".join(pages),
                          "status": status, "alumni_found": len(people), "checked_at": today.isoformat(),
                          "parser": PARSER_VERSION})
        if n % 100 == 0:
            save()
        if n % 50 == 0:
            print(f"[alumni] {n}/{len(order)} labs checked, {len(summaries)} with alumni, "
                  f"{fetch.count} pages fetched ({(datetime.now(timezone.utc) - started).seconds // 60} min)", flush=True)

    summary = save()
    print(f"[alumni] checked {len(site_rows)} labs: {len(summaries)} with alumni "
          f"({sum(len(s['alumni_urls']) > 0 for s in summaries)} alumni pages), "
          f"{sum(s['status'] != 'no_lab_site' for s in site_rows)} lab sites; {fetch.count} pages fetched", flush=True)
    return summary


def _lab_fields(r, prefix: str = "") -> dict:
    info = {}
    for key in STAT_KEYS:
        try:
            if r.get(prefix + key, "") != "":
                info[key] = float(r[prefix + key])
        except ValueError:
            pass
    careers = {k: int(float(r.get(f"{prefix}career_{k}", 0) or 0)) for k in CAREER_KINDS}
    if any(careers.values()):
        info["careers"] = careers
    if r.get(prefix + "top_journals"):
        info["top_journals"] = r[prefix + "top_journals"]
    return info


def alumni_lab_info() -> dict[str, dict]:
    """{professor_id: lab fields} from the alumni summary, in the master sheet's keys.
    "recent" holds the same fields for alumni of the last RECENT_YEARS years."""
    df = _read(SUMMARY_PATH)
    out = {}
    for _, r in df.iterrows():
        info = _lab_fields(r)
        recent = _lab_fields(r, "recent_")
        if recent.get("alumni"):
            info["recent"] = recent
        info["alumni_url"] = (r.get("alumni_urls", "") or "").split(";")[0]
        info["source"] = "연구실 홈페이지 졸업생 페이지 + OpenAlex (자동 집계)"
        info["as_of"] = r.get("checked_at", "")
        out[r["professor_id"]] = info
    return out
