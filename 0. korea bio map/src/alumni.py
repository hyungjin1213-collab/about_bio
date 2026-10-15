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
from urllib.parse import unquote, urljoin, urlparse

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
# PI -> alumnus links for alumni who are professors in the map (both public faculty).
ALUMNI_REL_PATH = OUTPUT_DIR / "relationships_alumni.csv"
REL_COLUMNS = ["relationship_id", "professor_a_id", "professor_b_id", "relationship_type",
               "collaboration_paper_count", "evidence_url", "verified", "notes"]
UNI_NOISE = {"university", "national", "college", "of", "the", "and", "institute", "science", "technology",
             "korea", "korean", "school", "medicine", "medical", "center", "대학교", "대학"}
PEOPLE_DIR = OUTPUT_DIR / "alumni"
SITE_COLUMNS = ["professor_id", "lab_url", "alumni_urls", "status", "alumni_found", "checked_at", "parser"]
# Bump when the parser changes: labs read by an older parser are read again.
PARSER_VERSION = "14"
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
    ("integrated", r"석\s*박\s*사?\s*통합|통합\s*과정|integrated|(?:combined\s+)?m\.?s\.?\s*[-/&]\s*ph\.?\s?d"
                   r"|master'?s?\s+(?:and|&)\s+(?:ph\.?\s?d|doctor)|\bma\s*/\s*ph\.?\s?d"),
    ("postdoc", r"post[-\s.]?\s?doc|박사\s*후|research\s+fellow|박사후연구원"),
    ("phd", r"ph\.?\s?d\.?|박사|doctor(?:al|ate)?"),
    ("ms", r"\bm\.?\s?s\.?\b|\bm\.?\s?sc\.?\b|\bm\.?a\.?\s+degree|석사|master"),
    ("undergrad", r"\bb\.?\s?sc?\.?\b|(?<![가-힣])학부|undergrad|intern|인턴"),
]
TYPICAL_YEARS = {"phd": (2.0, 9.0), "integrated": (3.0, 10.0), "ms": (1.0, 4.0), "postdoc": (0.5, 8.0)}
CAREERS = [  # (kind, pattern); first match wins.
    ("study", r"ph\.?\s?d\.?\s*(?:program|student|course|candidate)|박사\s*과정|대학원\s*진학|graduate\s+student"),
    ("postdoc", r"post[-\s.]?\s?doc|박사\s*후|research\s+fellow|박사후"),
    ("faculty", r"pro\w{0,3}ss?or|\bprof\b\.?|교수|lecturer|faculty|강사"),
    ("hospital", r"hospital|병원|medical\s+center|의료원|resident|레지던트|전공의|clinic|의원"),
    # Public sector before companies ('Examiner ... Bio-Healthcare ... Ministry').
    ("institute", r"research\s+foundation|연구재단|\bnrf\b|ministry|examiner|심사관|식약처|mfds|\bfda\b|질병관리|kdca|government|정부|공무원|특허청"),
    ("industry", r"\binc\b|\bco\.|\bltd\b|\bcorp|주식회사|\(주\)|제약|약품|samsung|삼성|\blg\b|"
                 r"\bsk\b|celltrion|셀트리온|유한|한미|녹십자|종근당|대웅|\bcj\b|amore|아모레|콜마|kolmar|genentech|novartis|"
                 r"pfizer|roche|merck|astrazeneca|startup|스타트업|회사|기업|화학|전자|헬스케어|바이오로직스|바이오사이언스|"
                 r"특허|변리사|consult|컨설팅|\bplc\b|investment|capital|holdings|medicines|healthcare|health\s+korea|"
                 r"syneos|iqvia|icon\b|novotech|parexel|\bcro\b|venture|벤처|투자|hyundai|현대|oil\s*bank|pacific|롯데|lotte|"
                 r"hanwha|한화|gs\b|posco|포스코|bioepis|에피스|cosmax|코스맥스|biopharma|daewoong|celltrion|cheiljedang"),
    # Generic words count as industry only away from schools and institutes
    # ("Program in Department of Pharmaceutics" is not a company).
    ("industry_weak", r"pharm|biologics|bioscience|biotech|company|scientist|engineer|patent"),
    ("institute", r"ministry|examiner|심사관|kist|kribb|생명공학연구원|ibs|기초과학연구원|institute|연구원|연구소|nih|nci|max\s+planck|"
                  r"식약처|mfds|질병관리|kdca|government|정부|공무원"),
]
# Table headers and degree words that look like Korean names (박사 = 박 + 사).
NOT_NAMES = {"이름", "성명", "학위", "기간", "현재", "현직", "박사", "석사", "학사", "소속", "졸업", "입학", "비고", "과정",
             "연구원", "교수", "진로", "근무처", "연도", "년도", "분야", "직위", "직책", "구분", "졸업생", "동문", "학위과정",
             "사이트맵", "홈페이지", "로그인", "연구실", "교수소개", "연구분야", "오시는길", "공지사항", "갤러리", "바로가기",
             "상세보기", "메일", "주소", "전화", "팩스", "소개", "자료실", "후기", "현황"}
POSITION_MARKERS = r"(?:position\s+at\s+leave|current(?:ly)?(?:\s+position)?|present(?:\s+position)?|now|현재|현직|(?<![가-힣])현\s|→|->|=>|▶)\s*[:：]?\s*"
YEAR_RE = re.compile(r"((?:19[89]|20[0-4])\d)(?:\s*[./]\s*(\d{1,2}))?")
EN_NAME_RE = re.compile(r"^([A-Z][a-z]+(?:[A-Z][a-z]+)?(?:[-\s][A-Z]?[a-z]+)?(?:[\s,]+[A-Z][a-z]+(?:[-\s][A-Z]?[a-z]+)?){1,2})\b")
EN_NAME_STOP = {"staff", "engineer", "software", "manager", "director", "technical", "leader", "lead", "team",
                "head", "chief", "vice", "president", "analyst", "consultant", "specialist", "officer", "partner",
                "current", "alumni", "former", "present", "member", "members", "postdoc", "professor", "research",
                "samsung", "korea", "seoul", "university", "institute", "department", "school", "college", "lab",
                "laboratory", "phd", "master", "graduate", "student", "students", "now", "position", "thesis",
                "senior", "principal", "assistant", "associate", "scientist", "researcher", "the", "and", "at", "of",
                # menu items and employers that look like 'First Last'
                "post", "doctoral", "fellow", "fellows", "home", "people", "contact", "gallery", "publications",
                "inc", "ltd", "corp", "corporation", "llc", "plc", "co", "fda", "nih", "usa", "pharmaceutical",
                "pharmaceuticals", "biologics", "bioepis", "electronics", "chemical", "chemicals", "bank", "oil",
                "hospital", "center", "centre", "clinic", "medical", "medicine", "investment", "holdings", "group"}


# ------------------------------------------------------------------ parsing
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\u200b-\u200d\u2060\ufeff]", "", text or "")).strip()


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


# Semester notation: '2021S~2025F'. Entry S = spring (March), F = fall (September);
# graduation F = fall graduation (August), no letter = the regular February graduation.
SEMESTER_RE = re.compile(r"((?:19[89]|20[0-4])\d)\s*([SF])?\s*[~\-–]\s*((?:19[89]|20[0-4])\d)\s*([SF])?(?![A-Za-z])")


def _semesters(text: str) -> tuple[int, int, int, int] | None:
    """(start year, start month, end year, end month) from semester notation, if used."""
    for m in SEMESTER_RE.finditer(text):
        if m.group(2) or m.group(4):
            return (int(m.group(1)), 9 if m.group(2) == "F" else 3,
                    int(m.group(3)), 8 if m.group(4) == "F" else 2)
    return None


def exact_months(text: str) -> bool:
    """True when entry and graduation months are known ('2015.03 - 2020.08', '2021S~2025F')."""
    return _semesters(text) is not None or sum(1 for _, m in YEAR_RE.findall(text) if m and 1 <= int(m) <= 12) >= 2


def years_of(text: str, degree: str = "") -> tuple[int | None, int | None, float | None]:
    """(start, end, duration in years) from '2015.03 - 2020.08', '2015~2020', '2020' (graduation only).
    Without both months the duration is year minus year: an estimate (see exact_months)."""
    sem = _semesters(text)
    if sem:
        y1, m1, y2, m2 = sem
        months = (y2 * 12 + m2) - (y1 * 12 + m1) + 1      # Mar 2023 - Feb 2025 = 24 months
        low, high = TYPICAL_YEARS.get(degree, (0.5, 10.0))
        return y1, y2, round(months / 12, 1) if low <= months / 12 <= high else None
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
    if m1 and m2:
        duration = ((y2 * 12 + m2) - (y1 * 12 + m1) + 1) / 12    # 2023.03 - 2025.02: both months count
    else:
        duration = (y2 + ((m2 or 8) - 1) / 12) - (y1 + ((m1 or 3) - 1) / 12) if (m1 or m2) else float(y2 - y1)
    low, high = TYPICAL_YEARS.get(degree, (0.5, 10.0))
    return y1, y2, round(duration, 1) if low <= duration <= high else None


# '투안 얀 구옌 (2012-2016 – 박사후연구원)', 'Carolien Bastiaanssen (2018-2019 – 연구원)'.
NAME_YEARS_RE = re.compile(r"^([가-힣A-Za-z][가-힣A-Za-z .'\-]{0,30}?)\s*\(\s*-?\s*(?:19[89]|20[0-4])\d")
STAFF_RE = re.compile(r"administrative|행정|실험\s*기술\s*지원|technician|lab\s*manager|secretary|비서", re.I)
BILINGUAL_RE = re.compile(r"^([A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){1,3})\s*\(([가-힣]{2,4})\)")


def _name_in(line: str) -> tuple[str, str]:
    """(korean, english) name at the start of a line ('', '' if none)."""
    line = line.strip(" -•·*|:")
    line = re.sub(r"^(?:dr|prof)\.?\s+(?=[A-Z가-힣])", "", line, flags=re.I)
    if re.fullmatch(r"[가-힣](?: [가-힣]){1,3}", line):          # '천 시 민'
        line = line.replace(" ", "")
    # 'JU GYEONG KIM': capitals only.
    if (re.fullmatch(r"[A-Z][A-Z'\-]{0,8}(?: [A-Z][A-Z'\-]{0,8}){1,3}", line) and not degree_of(line)
            and not any(w.casefold() in EN_NAME_STOP for w in line.split())):
        line = line.title()
    ny = NAME_YEARS_RE.match(line)
    if ny and not degree_of(ny.group(1)) and not any(w.casefold() in EN_NAME_STOP for w in ny.group(1).split()):
        name = ny.group(1).strip()
        if re.fullmatch(r"[가-힣]{2,4}", name):
            return name, ""
        if re.fullmatch(r"[가-힣 ]{2,12}", name):        # foreign name in Hangul: '투안 얀 구옌'
            return name.replace(" ", ""), ""
        if re.fullmatch(r"[A-Za-z .'\-]{3,40}", name) and len(name.split()) >= 2:
            return "", normalize_english_name(name)
    # Sentences and menus are not name lines ("최근에 해외 유학 ...", "사이트맵").
    sentence = len(line) > 90 or re.search(r"(다|요|니다)[.!]?$", line)
    m = None if sentence else re.match(r"^([가-힣]{2,4})(?=$|[\s(,/·|:\-])", line)
    if m and re.search(r"[에는을를의로도서께]$", m.group(1)):
        m = None
    if m and m.group(1) not in NOT_NAMES and not degree_of(m.group(1)) and is_korean_person_name(m.group(1)):
        rest = line[m.end():]
        en = re.match(r"^\s*\(?\s*([A-Z][A-Za-z\-]+(?:[\s,]+[A-Z][A-Za-z\-]+){1,2})", rest)
        return m.group(1), normalize_english_name(en.group(1)) if en else ""
    # 'SeaHyoung Lee (이세형)': an English name with the Hangul name in brackets.
    both = BILINGUAL_RE.match(line)
    if both and is_korean_person_name(both.group(2)) and not any(
            w.casefold() in EN_NAME_STOP for w in re.split(r"[\s\-]+", both.group(1))):
        return both.group(2), normalize_english_name(both.group(1))
    m = EN_NAME_RE.match(line)
    if m:
        words = [w for w in re.split(r"[\s,\-]+", m.group(1)) if w]
        if len(words) >= 2 and not any(w.casefold() in EN_NAME_STOP for w in words):
            ko = re.search(r"\(([가-힣]{2,4})\)", line)
            return (ko.group(1) if ko and is_korean_person_name(ko.group(1)) else ""), normalize_english_name(m.group(1))
    return "", ""


BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "dl", "dt", "dd", "tr", "td", "th", "table", "tbody", "thead",
              "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "aside", "main", "blockquote", "pre",
              "figure", "figcaption", "hr"]


def _section_lines(soup: BeautifulSoup, whole_page: bool, inline: bool = False) -> list[str]:
    """Lines of the alumni part of a page (the whole page for a dedicated alumni page).
    inline: break lines only at block elements, as a browser does. Some site builders
    split words over several spans ('H' + 'ojong Kim', '20' + '19F' + '~2025')."""
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        tag.decompose()
    # Search and login forms are noise, but some boards wrap the whole list in a form.
    for tag in soup("form"):
        if len(_clean(tag.get_text(" "))) < 120:
            tag.decompose()
    if inline:
        for br in soup("br"):
            br.replace_with("\n")
        for tag in soup(BLOCK_TAGS):
            tag.insert_before("\n")
            tag.insert_after("\n")
        text = soup.get_text("")
    else:
        text = soup.get_text("\n")
    lines = [_clean(x) for x in text.split("\n")]
    lines = [x for x in lines if x]
    # Photo captions, phone and address lines carry no alumni information.
    lines = [x for x in lines if not re.fullmatch(r"(멤버\s*)?사진|member\s*photo|photo|image|프로필\s*사진", x, re.I)
             and not re.match(r"(tel|fax|phone|전화|팩스|주소|address)\b", x, re.I)]
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
                if (len(nxt) <= 16 and any(nxt.casefold().startswith(w) for w in STOP_WORDS)) \
                        or re.fullmatch(r"\d{4}년\s*\d{1,2}월", nxt):
                    break
                out.append(nxt)
            return out
    return []


def parse_alumni(html: str, whole_page: bool = True) -> list[dict]:
    """One dict per alumnus found on an alumni page (or the alumni section of a members page).
    The page is read with line breaks at every tag and at block elements only; the
    reading that finds more complete records wins."""
    best: list[dict] = []
    for inline in (False, True):
        people = _parse_lines(_section_lines(BeautifulSoup(html, "html.parser"), whole_page, inline))
        if _quality(people) > _quality(best):
            best = people
    return best


def _quality(people: list[dict]) -> tuple[int, int]:
    """Records with a degree or a known career, less those with neither (citation or menu
    fragments read as names)."""
    filled = [(p["degree"] != "unknown") + (p["career"] not in ("other", "unknown")) + bool(p["end_year"])
              for p in people]
    loose = sum(1 for p in people if p["degree"] == "unknown" and p["career"] in ("other", "unknown"))
    return sum(filled) - 2 * loose, len(people)


CARD_LABELS = {"e-mail", "email", "work", "previously", "이메일", "현재", "이전"}
CARD_ORDER = ("integrated", "phd", "ms", "postdoc")


def _card_lines(lines: list[str]) -> list[str]:
    """Rewrite 'name / E-Mail / addr / Work / position / Previously / history' cards as
    'name / degree line / 현재: position' records the line parser reads."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        ko, en = _name_in(lines[i])
        nxt = lines[i + 1].casefold() if i + 1 < len(lines) else ""
        if not (ko or en) or nxt not in CARD_LABELS:
            i += 1
            continue
        card: dict[str, list[str]] = {"name": [lines[i]]}
        label = "name"
        i += 1
        while i < len(lines):
            low = lines[i].casefold()
            if low in CARD_LABELS:
                label = {"이메일": "e-mail", "email": "e-mail", "현재": "work", "이전": "previously"}.get(low, low)
                card.setdefault(label, [])
            elif any(_name_in(lines[i])) and i + 1 < len(lines) and lines[i + 1].casefold() in CARD_LABELS:
                break
            elif label != "e-mail":
                card.setdefault(label, []).append(lines[i])
            i += 1
        history = [x for x in card.get("previously", []) if degree_of(x) in CARD_ORDER]
        history.sort(key=lambda x: CARD_ORDER.index(degree_of(x)))
        work = " / ".join(card.get("work", []))
        out += card["name"] + history[:1] + ([f"현재: {work}"] if work else [])
    return out


ROLE_RE = re.compile(r"researcher|course|student|fellow|연구원|과정|졸업|대학원생", re.I)
TIMELINE_RE = re.compile(r"((?:19|20)\d\d)(?:\s*\.\s*\d{1,2})?\s*[~\-–]\s*"
                         r"(?:((?:19|20)\d\d)|present|현재|now|\))", re.I)
PRESENT_RE = re.compile(r"present|현재|\bnow\b|[~\-–]\s*(?:\)|$)", re.I)
EDUCATION = ("phd", "ms", "integrated", "undergrad")


def _is_role(line: str) -> bool:
    return (len(line) <= 60 and not any(_name_in(line))
            and not re.search(r"alumni|graduates|former|졸업생|동문", line, re.I)    # a group heading
            and bool(degree_of(line) in CARD_ORDER or ROLE_RE.search(line)))


def _role_first(lines: list[str]) -> list[str]:
    """'Doctoral Course / 2019' above each name: put the role after the name."""
    names = [i for i, x in enumerate(lines) if any(_name_in(x))]
    if not names or names[0] == 0 or not _is_role(lines[names[0] - 1]):
        return lines
    before = sum(1 for i in names if i > 0 and _is_role(lines[i - 1]))
    after = sum(1 for i in names if i + 1 < len(lines) and _is_role(lines[i + 1]))
    # Strict alternation: the line above the role is the previous person's name.
    paired = sum(1 for i in names if i > 1 and _is_role(lines[i - 1])
                 and (any(_name_in(lines[i - 2])) or not YEAR_RE.search(lines[i - 1])))
    if before < 3 or before <= after or paired < 0.6 * before:
        return lines
    out, i = [], 0
    while i < len(lines):
        if i + 1 < len(lines) and _is_role(lines[i]) and any(_name_in(lines[i + 1])):
            out += [lines[i + 1], lines[i]]
            i += 2
        else:
            out.append(lines[i])
            i += 1
    return out


DATE_ONLY_RE = re.compile(r"\(?\s*(?:19|20)\d\d(?:\s*[./]\s*\d{1,2})?\s*[~\-–]\s*(?:(?:19|20)\d\d(?:\s*[./]\s*\d{1,2})?|present|현재)?\s*\)?")


def _dates_first(lines: list[str]) -> list[str]:
    """'2008-2012' written above each name: move the years under the name."""
    names = [i for i, x in enumerate(lines) if any(_name_in(x))]
    above = [i for i in names if i > 0 and DATE_ONLY_RE.fullmatch(lines[i - 1])]
    if len(above) < 3 or len(above) < 0.6 * len(names):
        return lines
    out, i = [], 0
    while i < len(lines):
        if i + 1 < len(lines) and DATE_ONLY_RE.fullmatch(lines[i]) and any(_name_in(lines[i + 1])):
            out += [lines[i + 1], lines[i]]
            i += 2
        else:
            out.append(lines[i])
            i += 1
    return out


def _profile_lines(lines: list[str]) -> list[str] | None:
    """CV-style profiles ('2002.03 ~ 2010.02 : ..., Ph.D.', 'present : 강원대학교'), several
    dated lines per person: rewrite each as 'name / degree line / 현재: position'."""
    # Dated lines under the names, not on them ('김가람 (2015-2020; 박사과정)' is one line per person).
    dated, names, current = 0, 0, False
    for x in lines:
        if any(_name_in(x)) and not TIMELINE_RE.search(x):
            current = False
        elif TIMELINE_RE.search(x) and not any(_name_in(x)) \
                and len(re.sub(r"[^A-Za-z가-힣]", "", TIMELINE_RE.sub("", x))) >= 6:   # a date alone is no CV line
            dated += 1
            names += not current
            current = True
    if dated < 6 or names < 2 or dated < 1.6 * names:
        return None
    blocks: list[dict] = []
    for i, line in enumerate(lines):
        ko, en = _name_in(line)
        if (ko or en) and not TIMELINE_RE.search(line):
            if blocks and not blocks[-1]["rest"]:
                continue              # 'Kim Gildong' under '김길동': the same person
            heading = lines[i - 1] if i and _is_role(lines[i - 1]) and not TIMELINE_RE.search(lines[i - 1]) else ""
            blocks.append({"name": f"{ko} ({en})" if ko and en else (ko or en), "heading": heading, "rest": []})
        elif blocks:
            blocks[-1]["rest"].append(line)
    out: list[str] = []
    for b in blocks:
        timeline = [x for x in b["rest"] if TIMELINE_RE.search(x) or re.match(r"present\b|현재", x, re.I)]
        start = lambda x: int(TIMELINE_RE.search(x).group(1)) if TIMELINE_RE.search(x) else 9999
        kind = degree_of(b["heading"]) if degree_of(b["heading"]) in CARD_ORDER else ""
        roles = [x for x in timeline if degree_of(x) in CARD_ORDER]
        if kind:
            role = next((x for x in roles if degree_of(x) == kind), "")
        else:
            role = max(roles, key=start, default="")
        later = [x for x in timeline if degree_of(x) not in EDUCATION and x != role and not PHARMD_RE.search(x)
                 and (not role or start(x) >= start(role))]
        now = [x for x in later if PRESENT_RE.search(x)]
        position = (now or sorted(later, key=start))[-1] if (now or later) else ""
        position = re.sub(r"^[\d.\s~\-–]*(?:present)?\s*[:：]?\s*", "", position, flags=re.I)
        if PHARMD_RE.search(position):
            position = ""        # 'College of Pharmacy, ..., Pharm.D': more study, not a job
        elif (re.search(r"대학교|대학|universit|college", position, re.I)
                and re.search(r"대학(?!교)|학과|학부|대학원|college\s+of|department|school\s+of", position, re.I)
                and not re.search(r"student|학생|과정|연구원|researcher|fellow|staff|행정|팀|manager|과장|"
                                  r"post[-\s.]?\s?doc|박사\s*후|visiting|방문", position, re.I)):
            position += " 교수"   # a CV's current job at a university department is a faculty post
        out += [b["name"], role or b["heading"] or ""] + ([f"현재: {position}"] if position else [])
    return [x for x in out if x]


PHARMD_RE = re.compile(r"pharm\.?\s?d\b|약학\s*박사|학사|b\.?\s?s\.?\b", re.I)
UI_LINE_RE = re.compile(r"(메일|e-?mail|홈페이지|↗?\s*homepage|상세보기|더보기|자세히|view\s*more|more)\s*[:：]?.*",
                        re.I)


def _parse_lines(lines: list[str]) -> list[dict]:
    cards = sum(1 for x in lines if x.casefold() == "work") >= 5
    if cards:
        lines = _card_lines(lines)       # one name line per card: every name starts a record
    else:
        lines = _dates_first(lines)
        profiles = _profile_lines(lines)
        if profiles is not None:
            lines, cards = profiles, True
        else:
            swapped = _role_first(lines)
            cards = swapped is not lines
            lines = swapped
    people = _records(lines, cards)
    names = sum(1 for x in lines if any(_name_in(x)))
    if not cards and names >= 5 and len(people) < 0.3 * names:
        # 'Name / 고려대학교 / 교수' with no degree or year anywhere: every name is a person.
        listed = _records(lines, True)
        if _quality(listed) > _quality(people):
            people = listed
    return people


def _records(lines: list[str], cards: bool) -> list[dict]:
    entries: list[dict] = []
    current: dict | None = None
    group = ""      # degree of the heading the names sit under ("Ph.D. Alumni")
    # When the page writes names as 'English (한글)', only such lines are names: an
    # employer like 'Amore Pacific' on its own line is a position, not a person.
    bilingual = sum(1 for x in lines if BILINGUAL_RE.match(x.strip(" -•·*|:"))) >= 3

    def is_record(i: int) -> bool:
        """A name starts an alumnus only if a degree or year follows within two lines
        (otherwise it is a company or position line such as 'Hoffmann-La Roche Ltd')."""
        if group or cards:
            return True
        for j, x in enumerate(lines[i:i + 3]):
            if j and (BILINGUAL_RE.match(x.strip(" -•·*|:")) if bilingual else any(_name_in(x))):
                return False            # the next person starts first
            if (degree_of(x) not in ("", "postdoc") or YEAR_RE.search(x) or (j == 0 and degree_of(x))
                    or re.match(r"(?:" + dict(DEGREES)["postdoc"] + r")[^,]*,\s*(?:현|present|now)", x, re.I)):
                return True
        return False

    for i, line in enumerate(lines):
        ko, en = _name_in(line)
        if bilingual and not BILINGUAL_RE.match(line.strip(" -•·*|:")):
            ko = en = ""
        if (ko or en) and not is_record(i):
            ko = en = ""
        low = line.casefold()
        if (not ko and not en and len(line) <= 40 and not YEAR_RE.search(line)
                and not (current and len(current["lines"]) == 1)      # '(박사 졸업생)' under a name
                and degree_of(line) and any(w in low for w in GROUP_WORDS)):
            group, current = degree_of(line), None
            continue
        if (ko or en) and current is not None and len(current["lines"]) <= 2 \
                and not any(YEAR_RE.search(x) for x in current["lines"]) \
                and ((ko and not en and current["name_en"] and not current["name_ko"])
                     or (en and not ko and current["name_ko"] and not current["name_en"])):
            # 'Dr. Simin Chun' then '천시민': one person written in both scripts
            current["name_ko"] = current["name_ko"] or ko
            current["name_en"] = current["name_en"] or en
        elif ko or en:
            current = {"name_ko": ko, "name_en": en, "lines": [line], "group": group}
            entries.append(current)
        elif current is not None and len(current["lines"]) < 5 and len(line) <= 200:
            current["lines"].append(line)
    people = []
    for e in entries:
        text = " | ".join(e["lines"])
        marker = re.search(POSITION_MARKERS, text, flags=re.I)
        degree = degree_of(text[:marker.start()] if marker else text) or e["group"]
        start, end, duration = years_of(text[:marker.start()] if marker else text, degree)
        stated = bool(marker)
        if marker:
            position = text[marker.end():]
        else:
            # Lines after the name that carry no degree, years or study mode are the
            # current position ('Researcher of Hanmi Pharm'); program lines are not.
            rest = [x for x in e["lines"][1:] if not YEAR_RE.search(x)
                    and not UI_LINE_RE.fullmatch(x) and "@" not in x
                    and degree_of(x) in ("", "postdoc", "undergrad")   # '생명과학부' is not a degree line
                    and not (degree == "postdoc" and degree_of(x) == "postdoc")
                    and not re.search(r"full\s*time|part\s*time", x, re.I)]
            stated = bool(rest)
            position = " | ".join(rest) if rest else re.sub(r"\([^)]*\)", " ", e["lines"][0])
            position = YEAR_RE.sub(" ", position)
            # Keep "postdoc" whole before degree words go ("박사후연구원" is not "후연구원").
            position = re.sub(dict(DEGREES)["postdoc"], " postdoc ", position, flags=re.I)
            for _, pattern in DEGREES[:1] + DEGREES[2:]:
                position = re.sub(pattern, " ", position, flags=re.I)
        position = re.sub(r"\S+@\S+|https?://\S+", " ", position)
        career = career_of(position)          # before punctuation goes: '(주)' marks a company
        position = _clean(re.sub(r"[|()\[\]~\-–,:]+", " ", position))[:160]
        if not stated and (career == "other" or (career == "postdoc" and degree == "postdoc")):
            career = ""     # leftover words of the entry / the lab role itself, not a stated position
        # A name alone is not an alumnus: nav links, captions, the PI's name.
        head = text[:marker.start()] if marker else e["lines"][0]
        if STAFF_RE.search(head) and not re.search(r"박사|석사|ph\.?\s?d|m\.?s\.?|postdoc|박사후", head, re.I):
            continue        # administrative / technical staff, not trainees
        if degree in ("phd", "ms", "integrated") and re.search(dict(DEGREES)["postdoc"], head, re.I):
            duration = None   # '2015-2024; 박사과정, 박사후연구원': PhD and postdoc together, not degree time
        if not (degree or end or career):
            continue
        if degree == "undergrad":
            continue        # undergraduate interns: no degree from the lab
        if (not stated and re.search(r"student|대학원생|재학|candidate", text, re.I)
                and not (start and end and start != end)):
            continue        # a current student ('PhD Student (Fall 2023)'), not an alumnus
        people.append({"name_ko": e["name_ko"], "name_en": e["name_en"], "degree": degree or "unknown",
                       "start_year": start, "end_year": end, "years": duration,
                       "years_exact": duration is not None and exact_months(text),
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
            try:
                return self.session.get(url, timeout=TIMEOUT, headers=headers)
            except requests.exceptions.ConnectionError as exc:
                if isinstance(exc, requests.exceptions.SSLError):
                    raise
                time.sleep(3)       # some lab servers drop a kept-alive connection: once more
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
        parts = [x for x in unquote(urlparse(url).path).casefold().split("/") if x]
        if any(w in x for x in parts[:-1] for w in ("alumni", "former", "graduates")):
            continue        # '/alumni/some-name': one alumnus's own page

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


def reread_alumni(fetch: Fetcher, lab_url: str, alumni_urls: str) -> tuple[str, list[str], list[dict]]:
    """Read the alumni pages already found for a lab again (no search for new pages)."""
    people, used = [], []
    for url in filter(None, alumni_urls.split(";")):
        html = fetch.html(url)
        if not html:
            continue
        path = unquote(urlparse(url).path).casefold()
        # A dedicated alumni page is read whole; a lab or members page only in its alumni section.
        whole = url != lab_url and any(w in path for w in ALUMNI_WORDS)
        found = parse_alumni(html, whole_page=whole)
        if not found and url != lab_url and not any(w in path for w in MEMBER_WORDS):
            found = parse_alumni(html)
        if found:
            people += found
            used.append(url)
    return lab_url, used, people


def find_alumni(fetch: Fetcher, profile_url: str, manual: dict, orcid: str = "") -> tuple[str, list[str], list[dict]]:
    """(lab url, alumni page urls, alumni) for one professor."""
    if manual.get("alumni_url", "").strip().casefold() == "none":
        return manual.get("lab_url", ""), [], []        # checked by hand: the lab lists no alumni
    if manual.get("alumni_url"):
        # Several pages may be given, ';'-separated (postdocs on one page, graduates on another).
        # A '{page}' in the address is a page number: read pages 1, 2, ... until one adds nobody new.
        people, used, seen = [], [], set()
        for url in filter(None, (u.strip() for u in manual["alumni_url"].split(";"))):
            for n in range(1, 31 if "{page}" in url else 2):
                html = fetch.html(url.replace("{page}", str(n)))
                found = [p for p in (parse_alumni(html) if html else [])
                         if (p["name_ko"], p["name_en"], p["degree"]) not in seen]
                if not found:
                    break
                seen.update((p["name_ko"], p["name_en"], p["degree"]) for p in found)
                people += found
                if url not in used:
                    used.append(url)
        return manual.get("lab_url", ""), used, people
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
        if not alumni_pages:
            # The alumni page is often a sub-menu of the people page ('구성원' -> '졸업생').
            for url in member_pages:
                sub = fetch.html(url)
                if sub:
                    alumni_pages += [u for u in alumni_links(sub, url)[0] if u not in alumni_pages]
            alumni_pages = alumni_pages[:3]
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
CAREER_KINDS = ("faculty", "postdoc", "industry", "hospital", "institute", "study", "other")
STAT_KEYS = ("alumni", "phd_graduates", "ms_graduates", "postdoc_alumni", "phd_years", "integrated_years", "ms_years",
             "phd_years_est", "integrated_years_est", "ms_years_est", "stay_years", "stay_years_est",
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
        # 1 = some of these durations come from years only (entry/graduation month not written)
        **{f"{d}_years_est": int(any(p["years"] is not None and not p.get("years_exact") for p in people
                                     if p["degree"] == d)) for d in ("phd", "integrated", "ms")},
        # Pages that give only the years in the lab, no degree ('2010-2016'): time in the lab.
        "stay_years": med(p["years"] for p in people if p["degree"] == "unknown"),
        "stay_years_est": int(any(p["years"] is not None and not p.get("years_exact") for p in people
                                  if p["degree"] == "unknown")),
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


def add_professors(rows: list[dict]) -> int:
    """Append alumni-page professors to professors_seed.csv (once; skipped if already there)."""
    if not rows:
        return 0
    path = DATA_DIR / "professors_seed.csv"
    seed = _read(path)
    have = set(zip(seed["name_ko"], seed["university"]))
    nums = [int(x[1:]) for x in seed["professor_id"] if re.fullmatch(r"P\d+", x)]
    added = []
    for r in rows:
        if (r["name_ko"], r["university"]) in have:
            continue
        nums.append(max(nums or [0]) + 1)
        added.append({c: "" for c in seed.columns} | r | {"professor_id": f"P{nums[-1]:04d}"})
        have.add((r["name_ko"], r["university"]))
    if added:
        pd.concat([seed, pd.DataFrame(added)], ignore_index=True).to_csv(path, index=False, encoding="utf-8-sig")
        print(f"[alumni] added {len(added)} professors from alumni pages: "
              + ", ".join(f"{a['name_ko']} ({a['university']})" for a in added), flush=True)
    return len(added)


def collect_alumni(client: OpenAlexClient | None = None, fetch: Fetcher | None = None,
                   limit: int = PROFESSOR_LIMIT, known_only: bool = False) -> pd.DataFrame:
    """known_only: the daily refresh. Re-read every lab whose alumni page was already found
    (degree years, careers, papers); no new labs are searched, and a lab whose page cannot be
    read today keeps its earlier figures."""
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

    if known_only:
        known = sites[(sites["status"] == "alumni") & (sites["alumni_urls"] != "")]
        site_of = {r["professor_id"]: r for _, r in known.iterrows()}
        order = [p for p in professors["professor_id"] if p in site_of or p in manual]
    else:
        order = sorted(professors["professor_id"], key=due)
        order = [p for p in order if due(p)[0] < 9][:limit]
    rows = professors.set_index("professor_id")
    summaries, site_rows, people_rows = [], [], []
    started = datetime.now(timezone.utc)
    openalex_ok = True
    old_summary = _read(SUMMARY_PATH)
    old_rels = _read(ALUMNI_REL_PATH, REL_COLUMNS)
    index = ProfessorIndex(professors)
    rel_rows: list[dict] = []
    new_profs: list[dict] = []
    unis = korean_universities()

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
        add_professors(new_profs)
        rels = pd.concat([old_rels[~old_rels["professor_a_id"].isin(done)], pd.DataFrame(rel_rows, columns=REL_COLUMNS)],
                         ignore_index=True)
        rels = rels[~rels["professor_a_id"].isin(shared)]
        rels["relationship_id"] = [f"ALU{i:06d}" for i in range(1, len(rels) + 1)]
        rels.to_csv(ALUMNI_REL_PATH, index=False, encoding="utf-8-sig")
        PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(people_rows).to_csv(PEOPLE_DIR / f"lab_alumni_people_{today.isoformat()}.csv",
                                         index=False, encoding="utf-8-sig")
        return summary

    for n, pid in enumerate(order, 1):
        r = rows.loc[pid]
        try:
            if known_only and pid not in manual:
                lab_url, pages, people = reread_alumni(fetch, site_of[pid]["lab_url"], site_of[pid]["alumni_urls"])
            else:
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
            rel_rows.extend(alumni_relationships(pid, people, index, pages[0] if pages else lab_url))
            for row in new_faculty_rows(pid, people, index, pages[0] if pages else lab_url, unis):
                if (row["name_ko"], row["university"]) not in {(r["name_ko"], r["university"]) for r in new_profs}:
                    new_profs.append(row)
            for p in people:
                people_rows.append({"professor_id": pid, **{k: v for k, v in p.items()
                                                             if k not in ("fwci", "journals", "paper_ids")},
                                    "fwci_mean": round(statistics.mean(p["fwci"]), 2) if p.get("fwci") else ""})
        no_alumni = str(manual.get(pid, {}).get("alumni_url", "")).strip().casefold() == "none"
        if known_only and not people and not no_alumni:
            print(f"[alumni] {pid}: alumni page not readable today, earlier figures kept", flush=True)
            continue
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


def _uni_tokens(university: str) -> set[str]:
    words = re.split(r"[\s,;()]+", university.casefold())
    return {w for w in words if len(w) >= 3 and w not in UNI_NOISE}


class ProfessorIndex:
    """Find an alumnus among the map's professors: same Hangul name and a matching English
    name or the professor's university in the alumnus' stated position; exactly one match."""

    def __init__(self, professors: pd.DataFrame):
        self.by_ko: dict[str, list[dict]] = {}
        self.rows = [r.to_dict() for _, r in professors.iterrows()]
        for r in self.rows:
            if r.get("name_ko"):
                self.by_ko.setdefault(r["name_ko"], []).append(r)

    def find(self, alum: dict, exclude: str) -> str:
        pos = alum.get("position", "").casefold()

        def uni_ok(r):
            return any(t in pos for t in _uni_tokens(str(r.get("university", ""))))

        # Someone who left the lab within the last two years and lists no faculty post cannot be
        # a professor yet: a same-name match is another person ('Master (2026 후기)').
        fresh = (alum.get("career") != "faculty" and alum.get("end_year")
                 and int(alum["end_year"]) >= date.today().year - 2)
        if alum.get("name_ko"):
            cands = [r for r in self.by_ko.get(alum["name_ko"], []) if r["professor_id"] != exclude]
            ok = [r for r in cands if uni_ok(r) or (not fresh and alum.get("name_en")
                                                    and _same_person(r.get("name_en", ""), alum))]
        elif alum.get("name_en"):
            ok = [r for r in self.rows if r["professor_id"] != exclude and r.get("name_en")
                  and _same_person(r["name_en"], alum) and uni_ok(r)]
        else:
            ok = []
        return ok[0]["professor_id"] if len(ok) == 1 else ""


# Korean universities not in universities_seed.csv that alumni pages name.
EXTRA_KOREAN_UNIVERSITIES = {
    "Pukyong National University": "부경대학교",
    "KENTECH": "한국에너지공과대학교",
    "Korea National University of Transportation": "한국교통대학교",
}


def korean_universities() -> dict[str, str]:
    """{lower-case name or alias: canonical English name} for Korean universities."""
    names = dict(EXTRA_KOREAN_UNIVERSITIES)
    seed = _read(DATA_DIR / "universities_seed.csv")
    if not seed.empty:
        seed = seed[seed.get("org_type", "university") == "university"] if "org_type" in seed.columns else seed
        names.update(dict(zip(seed["university"], seed.get("name_ko", ""))))
    out = {}
    for en, ko in names.items():
        out[en.casefold()] = en
        if ko:
            out[ko] = en
        out[en.casefold().replace("national", "nat'l")] = en
    out["korea institute of energy technology"] = "KENTECH"
    return out


def korean_university_in(position: str, unis: dict[str, str]) -> str:
    low = position.casefold()
    hits = [en for key, en in unis.items() if key in low]
    return max(hits, key=len) if hits else ""     # 'Catholic Kwandong University' over 'Kwandong'


def new_faculty_rows(pid: str, people: list[dict], index: "ProfessorIndex", page: str,
                     unis: dict[str, str]) -> list[dict]:
    """Alumni who are now professors at a Korean university but not in the map yet:
    new professor rows (OpenAlex profile found by the next resolve). Overseas
    universities are left to the alumni statistics."""
    rows = []
    for p in people:
        if p["career"] != "faculty" or not p.get("name_ko") or index.find(p, pid):
            continue
        uni = korean_university_in(p.get("position", ""), unis)
        if not uni or any(r.get("name_ko") == p["name_ko"] and r.get("university") == uni for r in index.rows):
            continue
        rows.append({"name_ko": p["name_ko"], "name_en": p.get("name_en", ""), "university": uni,
                     "source_url": page, "identity_status": "alumni_page"})
    return rows


def alumni_relationships(pid: str, people: list[dict], index: ProfessorIndex, page: str) -> list[dict]:
    rows, seen = [], set()
    for p in people:
        sid = index.find(p, pid)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        kind = "postdoc_mentor" if p["degree"] == "postdoc" else "advisor_student"
        rows.append({"professor_a_id": pid, "professor_b_id": sid, "relationship_type": kind,
                     "collaboration_paper_count": p.get("papers", ""), "evidence_url": page, "verified": "alumni_page",
                     "notes": f"listed on the lab's alumni page ({p['degree']}"
                              f"{', ' + str(p['end_year']) if p.get('end_year') else ''})"})
    return rows


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


def lab_homepages() -> dict[str, str]:
    """{professor_id: lab homepage}: a hand-entered lab_url first, else a found lab site
    that is not just the professor's department profile page."""
    out = {}
    sites = _read(SITES_PATH, SITE_COLUMNS)
    for _, r in sites.iterrows():
        if r["lab_url"] and r["status"] in ("alumni", "lab_site"):   # a shared page is a department page
            out[r["professor_id"]] = r["lab_url"]
    for _, r in _read(MANUAL_PATH, MANUAL_COLUMNS).iterrows():
        if r["professor_id"] and r["lab_url"]:
            out[r["professor_id"]] = r["lab_url"]
    return {k: v for k, v in out.items() if v.startswith(("http://", "https://"))}


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


def _mask_line(line: str) -> str:
    """A page line for the probe log with names, e-mails and citations hidden."""
    line = re.sub(r"\S+@\S+|\S+\s+at\s+\S+\.(?:com|net|kr|edu|org)\S*", "***@***", line)
    if re.search(r"et al|\(\d{4}\)|\b[A-Z][a-z]+ [A-Z]{1,2}[,.]", line):
        return "[citation]"
    if re.search(r"[A-Z][a-z]+(?:'s|’s)\b|\b[A-Z][a-z]+ and [A-Z][a-z]+", line):
        return "[caption]"
    # Hangul person names anywhere ('박사라', '무집 라만'); a short all-Hangul line is taken as a name.
    if re.fullmatch(r"[가-힣]{1,4}(?:\s[가-힣]{1,4}){0,2}", line.strip()) and len(line.strip()) <= 10:
        return line.strip()[:1] + "**"
    line = re.sub(r"(?<![가-힣])[가-힣]{2,4}(?![가-힣])",
                  lambda m: m.group(0)[:1] + "**" if is_korean_person_name(m.group(0)) else m.group(0), line)
    # Runs of capitalised words anywhere ('AREUM CHOI (Undergraduate ...)', 'JiMin KIM, PhD')
    line = re.sub(r"\b[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){1,3}\b", lambda m: m.group(0)[:1] + "**", line)
    ko, en = _name_in(line)
    for name in filter(None, (ko, en)):
        pattern = r"\s*".join(map(re.escape, name.replace(" ", "")))
        line = re.sub(pattern, name[:1] + "**", line, count=1, flags=re.I)
    if re.fullmatch(r"[A-Za-z][A-Za-z.'\-]*(?:\s+[A-Za-z][A-Za-z.'\-]*){1,3}", line) and len(line) <= 40:
        line = line[:1] + "**"          # a short line of capitalised words is likely a name
    return line


def probe(ids: list[str], fetch: Fetcher | None = None) -> None:
    """Diagnose alumni pages for some labs: what each page returns and how it parses.
    Names at the start of lines are masked (김**) in the printout."""
    fetch = fetch or Fetcher()
    sites = _read(SITES_PATH, SITE_COLUMNS).set_index("professor_id")
    manual = {r["professor_id"]: r.to_dict() for _, r in _read(MANUAL_PATH, MANUAL_COLUMNS).iterrows()}


    extra: dict[str, list[str]] = {}
    for item in filter(None, os.getenv("ALUMNI_PROBE_URLS", "").split(",")):
        pid, _, url = item.partition("=")
        extra.setdefault(pid, []).append(url)
    professors = _read(OUTPUT_DIR / "professors_enriched.csv")
    index = ProfessorIndex(professors) if not professors.empty else None
    def wp_terms(base: str) -> None:
        """WordPress sites: member taxonomies with counts and how many member posts there are."""
        for path in ("/wp-json/wp/v2/member_tax?per_page=100", "/wp-json/wp/v2/member?per_page=1"):
            try:
                r = fetch._get(base + path)
            except requests.RequestException as exc:
                print(f"[probe]   wp {path}: {type(exc).__name__}", flush=True)
                continue
            total = r.headers.get("X-WP-Total", "")
            try:
                data = r.json()
            except ValueError:
                data = None
            if isinstance(data, list) and data and "count" in data[0]:
                terms = [(t.get("id"), t.get("slug"), t.get("count"), t.get("parent")) for t in data]
                print(f"[probe]   wp {path}: status={r.status_code} terms={terms}", flush=True)
            else:
                keys = sorted(data[0].keys()) if isinstance(data, list) and data else type(data).__name__
                print(f"[probe]   wp {path}: status={r.status_code} total={total} keys={keys}", flush=True)

    for pid in ids:
        m = manual.get(pid, {})
        urls = [u.replace("{page}", str(n)) for u in extra.get(pid, []) + m.get("alumni_url", "").split(";")
                + [m.get("lab_url", "")] if u and u != "none" for n in ((1, 2, 3) if "{page}" in u else (1,))]
        if pid in sites.index:
            urls += [u for u in [sites.at[pid, "lab_url"], *str(sites.at[pid, "alumni_urls"]).split(";")] if u]
        print(f"\n[probe] ===== {pid} =====", flush=True)
        seen: set[str] = set()
        while urls:
            url = urls.pop(0)
            if url in seen or len(seen) >= 10:
                continue
            seen.add(url)
            allowed = fetch.allowed(url)
            try:
                r = fetch._get(url)
                status, size = r.status_code, len(r.text)
                r.encoding = r.apparent_encoding or r.encoding
                html = r.text if r.status_code == 200 else ""
            except requests.RequestException as exc:
                status, size, html = type(exc).__name__, 0, ""
            print(f"[probe] {url} robots_ok={allowed} status={status} bytes={size}", flush=True)
            if not html:
                continue
            if "wp-json" in html and os.getenv("ALUMNI_PROBE_ALL_LINKS"):
                wp_terms(f"{urlparse(url).scheme}://{urlparse(url).netloc}")
            al, mem = alumni_links(html, url)
            print(f"[probe]   alumni links={al} member links={mem}", flush=True)
            urls += [u for u in (al or mem) if u not in seen]
            other = [(t[:30], u) for t, u in _links(html, url) if any(w in f"{t} {u}".casefold()
                     for w in ("alumni", "졸업", "graduate", "member", "people", "구성원", "former"))][:12]
            print(f"[probe]   candidate links={other}", flush=True)
            hints = sorted(set(re.findall(r"(?:admin-ajax|ajaxurl|wp-json|pageid|paged|[?&]page=|data-page|"
                                          r"mod=list|kboard|tab=|category=|cat=)[^\"'<>\s]{0,60}", html)))
            print(f"[probe]   paging hints: {[h for h in hints if not re.search(r'[가-힣]', h)][:25]}", flush=True)
            soup = BeautifulSoup(html, "html.parser")
            for inp in soup.select("input[name], [data-filter-id] input, [data-filter-id] option")[:40]:
                label = inp.find_parent("label")
                print(f"[probe]   input: name={inp.get('name')} value={inp.get('value')} "
                      f"label={_mask_line(_clean(label.get_text(' ')) if label else '')[:30]}", flush=True)
            for mt in re.finditer(r"alumni|졸업생", html, re.I):
                snippet = re.sub(r"\s+", " ", html[max(0, mt.start() - 200): mt.start() + 120])
                if re.search(r"onclick|href|location|\.asp|\.php|\.html?", snippet, re.I):
                    print(f"[probe]   menu near alumni: {_mask_line(snippet)}", flush=True)
            if "javascript:" in html:
                for sc in BeautifulSoup(html, "html.parser").find_all("script", src=True)[:12]:
                    src = urljoin(url, sc["src"])
                    try:
                        js = fetch._get(src).text
                    except requests.RequestException:
                        continue
                    for mt in re.finditer(r"function\s+sub_menu\w*\s*\(", js):
                        print(f"[probe]   script {src[-40:]}: {re.sub(chr(92)+'s+', ' ', js[mt.start(): mt.start() + 900])}",
                              flush=True)
            for mt in re.finditer(r"function\s+sub_menu\w*\s*\(", html):
                print(f"[probe]   script: {re.sub(chr(92)+'s+', ' ', html[mt.start(): mt.start() + 700])}", flush=True)
            for key in ("member_tax", "var_page", "board_mode"):
                for mt in re.finditer(key, html):
                    snippet = re.sub(r"\s+", " ", html[max(0, mt.start() - 150): mt.start() + 250])
                    print(f"[probe]   around {key}: {_mask_line(snippet)}", flush=True)
                    break
            for frm in BeautifulSoup(html, "html.parser").find_all(["form", "select"]):
                print(f"[probe]   {frm.name}: action={frm.get('action', '')} name={frm.get('name', '')} "
                      f"fields={[i.get('name') for i in frm.find_all(['input', 'select', 'option'])][:15]}", flush=True)
            if os.getenv("ALUMNI_PROBE_ALL_LINKS"):
                for t, u in dict.fromkeys(_links(html, url)):
                    if urlparse(u).netloc == urlparse(url).netloc:
                        print(f"[probe]   link: {_mask_line(t[:30])} -> {unquote(u)[:120]}", flush=True)
            for whole in (True, False):
                people = parse_alumni(html, whole_page=whole)
                print(f"[probe]   parse whole_page={whole}: {len(people)} people; careers "
                      f"{dict(Counter(p['career'] for p in people))} degrees "
                      f"{dict(Counter(p['degree'] for p in people))} "
                      f"with end year {sum(1 for p in people if p['end_year'])}", flush=True)
            if index is not None:
                # Alumni who are professors on the map (public records): exact match, or same Hangul name.
                for p in parse_alumni(html):
                    sid = index.find(p, pid)
                    same = [r for r in index.by_ko.get(p.get("name_ko", ""), []) if r["professor_id"] != pid]
                    for r in ([next(x for x in index.rows if x["professor_id"] == sid)] if sid else same):
                        print(f"[probe]   professor {'match' if sid else 'same-name candidate'}: "
                              f"{r['professor_id']} {r.get('name_ko', '')} / {r.get('name_en', '')} / "
                              f"{r.get('university', '')} / {r.get('department', '')} "
                              f"(alumnus: {p['degree']} {p.get('end_year') or ''})", flush=True)
            for p in parse_alumni(html, whole_page=url != m.get("lab_url") and any(
                    w in unquote(urlparse(url).path).casefold() for w in ALUMNI_WORDS)):
                who = (p["name_ko"] or p["name_en"] or "?")[:1] + "**"
                print(f"[probe]   > {who} {p['degree']} {p['start_year']}-{p['end_year']} {p['years']} "
                      f"{p['career']} | {_mask_line(p['position'][:60])}", flush=True)
            lines = _section_lines(BeautifulSoup(html, "html.parser"), True, inline=True)
            start = next((i for i, x in enumerate(lines) if len(x) <= 40
                          and any(w in x.casefold() for w in ALUMNI_WORDS)), 0)
            print(f"[probe]   {len(lines)} text lines (block breaks); from line {start}:", flush=True)
            for line in lines[start:start + 160]:
                print(f"[probe]     | {_mask_line(line)[:110]}", flush=True)
