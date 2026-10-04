# 0. Korea Bio Map

한국 바이오 교수/PI를 3D 네트워크로 보여주는 About Bio의 데이터 수집 모듈입니다.

## 핵심 원리

- 교수/PI = node
- 공동연구/학문 계보 = link
- score = relationship에서 계산되는 파생값
- 공동연구는 OpenAlex로 자동 수집
- 교수 여부와 advisor/student 관계는 사람 검증을 거침

## 평소에 하는 일

### 1) 교수 seed 추가
`data/professors_seed.csv`에 직접 확인한 교수만 추가합니다.

### 2) GitHub Actions 실행
Actions → **Korea Bio Map Collector** → **Run workflow**

일반 갱신은 mode를 `all`로 둡니다.

이 실행이 자동으로:
1. OpenAlex ID 매칭
2. 논문/공동저자 수집
3. seed 교수끼리 공동연구 관계 생성
4. 한국 소속 공동저자 후보 정리
5. score 계산
6. 3D 웹용 `network-data.js` 생성
7. gh-pages에 최신 지도 배포

까지 수행합니다.

## 후보 교수 검토

자동 수집 결과 중 사람이 볼 파일은:

`output/collaborator_candidates.csv`

입니다.

여기에는 기본적으로:
- 한국 소속 기록이 있고
- 현재 seed 교수들과 공동논문이 2편 이상인
- 우선 검토 후보

만 들어갑니다.

`output/collaborator_candidates_all.csv`에는 전체 공동저자 후보가 남습니다.

### 후보를 실제 교수 DB에 넣는 방법

`data/candidate_approvals.csv`에 승인할 사람을 추가합니다.

예:

```csv
openalex_id,approved,name_ko,name_en,university,department,primary_field,source_url
A1234567890,yes,홍길동,Gildong Hong,Seoul National University,의과대학,면역항암,https://...
```

그 다음 Actions에서 mode를 **promote-and-all**로 실행합니다.

그러면:
1. 승인된 후보에 새 P번호 자동 부여
2. `professors_seed.csv`에 추가
3. 전체 네트워크 재수집
4. 3D 지도 자동 갱신

이 됩니다.

## Academic lineage

### 자동 추정 (`src/lineage.py`)

공동연구 수집 때 받은 논문으로 계보를 추정한다 (OpenAlex 추가 요청 없음).

- **지도교수 → 제자**: 제자 B의 첫 1저자 논문 후 약 6년 동안 B가 1저자, 교수 A가 마지막 저자인 논문이 여러 편
- **포닥 멘토 → 포닥**: 그 이후(첫 논문 후 6~11년) **박사 때와 다른 기관**에서 같은 패턴
- A가 B보다 5년 이상 먼저 논문을 냈어야 함 (동년배 공동연구 제외)
- 근거 논문 **3편 이상**: 지도에 바로 표시하고 점수 반영 (`자동 추정`)
- **2편**: `output/lineage_candidates.csv`에 `review`로만 남음

검토 방법: `data/lineage_decisions.csv`에 한 줄 추가

```csv
professor_a_id,professor_b_id,relationship_type,decision,notes
P0012,P0145,advisor_student,yes,박사 지도교수 확인
P0003,P0201,advisor_student,no,공동연구일 뿐
```

`yes`는 검토 후보를 확정하고, `no`는 자동 추정도 지운다.

### 직접 입력

지도교수/제자 교수 관계는 `data/relationships_manual.csv`에서 검증 후 넣습니다.

- `advisor_student`: A = 지도교수, B = 현재 교수/PI가 된 제자
- `postdoc_mentor`: A = mentor, B = 현재 교수/PI가 된 postdoc trainee

## 현재 score 규칙

`data/score_rules.csv` (관계마다 PI 쪽 / 제자 쪽 점수를 따로 준다)

| 관계 | PI (A) | 제자·포닥 (B) |
|---|---|---|
| 공동연구 (교수 1명당) | +1 | +1 |
| 지도교수 → 교수/PI가 된 제자 (`advisor_student`) | +10 | +1 |
| 포닥 멘토 → PI가 된 포닥 (`postdoc_mentor`) | +3 | +1 |

- 계보 관계는 `data/relationships_manual.csv`에서 `verified=yes`인 것만 점수에 반영된다.
- 가중치는 `score_rules.csv`에서 바꿀 수 있으며 원본 관계 데이터는 그대로 유지된다.

## 지도 표시

- 공동연구 선: 거의 보이지 않는 흐린 선. 별을 누르면(또는 마우스를 올리면) 그 교수의 공동연구 선만 밝게 강조된다.
- 지도교수 → 제자: 굵은 금색 선 + 화살표 + 흐르는 입자 (PI → 제자 방향).
- 포닥 멘토 → 포닥: 보라색 선 + 화살표 + 흐르는 입자.
- 계보로 이어진 교수들은 서로 가깝게 모이도록 배치된다.
- 별 색 = 분야 대분류 12개 (아래 "분야 분류")
- 오른쪽 위 **필터** 탭: 분야 / 지역 / 직위(교수·부교수·조교수) / 계보 있음·최근 5년 공동연구·신원 확인 체크. 지도·검색·순위에 모두 적용된다.
- 오른쪽 위 **검색 · 순위** 탭: 이름(한글/영문)·대학·학과·분야 검색, 종합 점수 / 공동연구 수 / 교수 된 제자 수 순위와 대학 필터. 항목을 누르면 그 별로 이동한다. 모바일에서는 버튼으로 연다.

## 마스터 엑셀 (직접 입력)

`data/about_bio_master.xlsx` 하나에 직접 입력한다. 매일 자동 실행 때 반영된다. 사람은 ID 대신 **이름 + 소속**
(한글 "서울대학교"/"서울대" 또는 영문)으로 적는다.

| 탭 | 내용 |
|---|---|
| 교수추가 | 크롤링에서 빠진 교수·연구원 (ORCID / OpenAlex ID를 알면 적기) → `professors_seed.csv`에 추가 |
| 관계추가 | 직접 긋는 선: 공동연구 / 지도교수-제자 / 포닥멘토-포닥 (A가 지도교수·멘토), 확인된 선으로 점수 반영 |
| 연구실정보 | 박사·석박통합·석사 평균 졸업기간, 박사 졸업생 수, 학생당 (1저자) 논문 수, 현재 인원, 모집 중, 기준연도, 출처 → 교수 툴팁(카드)에 표시 |

- 학생 개인 정보는 적지 않고 평균·인원수만 적는다.
- 못 찾은 행은 `output/master_unmatched.csv`에 이유와 함께 남는다.
- 빈 양식 다시 만들기: `python src/master_sheet.py`
- 예전 방식(`data/relationships_manual.csv`, ID로 입력)도 계속 동작한다.

## 산학연병 보기

- 처음 화면은 원래 은하. 왼쪽 아래 **산학연병으로 보기**를 체크하면 대학(학)·병원(병)·연구소(연)·기업(산)
  4개 원(은하)이 나란히 놓이고, 각자 **소속된 곳(명단이 있던 페이지)** 원에 들어간다.
  원 크기는 인원수에 비례, 휴대폰 세로 화면에서는 2×2로 배치.
- 위쪽 바에서 **학 · 대학** 같은 버튼을 누르면 그 원으로 줌인하고 다른 원 사람은 숨긴다. **전체**로 돌아온다.
- **교집합 보기**를 체크해야 원이 겹친 집합(벤 다이어그램)으로 바뀌고, 여러 곳에 걸친 사람이
  겹친 영역(예: 대학 + 대학병원 임상교수 → 학∩병)으로 이동한다. 이때 버튼은 그 집합 전체(교집합 포함)를 보여준다.
- 소속: 명단 페이지 종류(`data/universities_seed.csv`의 `org_type`: university / institute / hospital / company)
  + 최근 10년 논문의 본인 소속 기관 유형(OpenAlex). 논문의 20% 이상, 2편 이상에서 나온 유형만 인정
  (`src/sectors.py`, `output/professor_sectors.csv`). 틀리면 `data/sector_overrides.csv`: `P0001,학;산,바이오 스타트업 겸직`
- 필터 탭의 **산학연병** 칩으로도 거를 수 있다.

## OpenAlex 소속 검색 (크롤링이 안 되는 곳)

- 크롤러가 교수진 페이지를 못 찾은 대학과 모든 연구소는 OpenAlex에 직접 묻는다:
  **현재 소속이 그 기관(또는 그 기관 병원)** 이고, **바이오 논문 비중 50% 이상**, **논문 30편·h-index 12 이상**,
  **최근 3년 논문 3편 이상**인 연구자 (`src/institution_discovery.py`).
- OpenAlex ID로 바로 들어가서 신원이 정확하다. 한글 이름은 비어 있으니 필요하면 직접 채운다
  (`identity_status = openalex_institution`, `source_url` = OpenAlex 프로필).
- **기업(산)**: `universities_seed.csv`에 `org_type=company`로 바이오·제약 20곳(삼성바이오로직스, 셀트리온, 한미약품,
  유한양행, GC녹십자, 종근당, 대웅제약, SK바이오사이언스, 알테오젠, 에이비엘바이오, 마크로젠, 씨젠, CJ제일제당, LG화학,
  보령, JW중외제약, 일동제약, 한올바이오파마, 제넥신, 동아에스티). 기업 연구자는 논문이 적어 기준을 낮춘다
  (논문 10편·h-index 5·최근 3년 1편 이상, 기업당 최대 30명). 기업은 크롤링하지 않는다.
- 하루 20곳씩(`INSTITUTION_DISCOVERY_LIMIT`), 기관당 최대 60명. 처리한 곳은 `data/institution_discovery_log.csv`.
- 이미 있는 사람(같은 OpenAlex ID / ORCID / 같은 기관의 같은 이름)과 `identity_rejections.csv`는 건너뛴다.

## 연구소

- `data/universities_seed.csv`에 `org_type=institute`로 14곳 추가: KIST, 생명연, 화학연, IBS, 뇌연구원,
  한의학연, 안전성평가연구소, 기초과학지원연, 식품연, 국립암센터, 국립보건연구원, 파스퇴르연구소,
  원자력의학원, 표준과학연구원. 매일 실행에서 아직 안 돌아본 곳부터 크롤링한다.
- 연구소 사이트는 전체가 바이오로 보고 **연구진 / 연구인력 / Researchers** 페이지를 찾는다.
- 책임·선임·수석연구원, 연구위원, 단장, 그룹리더(영문 Principal/Senior Researcher, Group Leader)를 PI로 받고,
  박사후·학생·위촉·연수연구원은 뺀다. 직위 필터에 책임연구원 / 선임연구원이 추가됐다.
- 신원 확인은 대학과 같다(ORCID → OpenAlex 현재 소속 + 바이오 비중).

## 분야 분류

- OpenAlex가 논문 주제로 정한 세부 분야를 `data/field_categories.csv`로 12개 대분류에 묶는다
  (분자·세포생물, 임상의학, 암·종양, 면역·감염, 신경과학, 약학·신약, 유전체·생물정보,
  의공학·바이오소재, 생리·대사, 보건·역학, 농생명·식품·생태, 기타).
- 세부 분야가 없으면 학과명 키워드(약학대학 → 약학·신약 등)로 정한다 (`src/classify.py`).
- 틀린 교수는 `data/field_overrides.csv`에 한 줄 적으면 자동 분류보다 우선한다:

```csv
professor_id,category,notes
P0001,면역·감염,T세포 면역항암
```

- 지역은 `data/universities_seed.csv`의 `region`, 직위는 교수진 페이지의 직위(교수/부교수/조교수)를 쓴다.

## 연구 키워드 · 주요 기술

`src/keywords.py`가 공동연구 수집 때 받은 논문으로 교수별 키워드를 만든다 (OpenAlex 추가 요청 없음).

- 출처: **MeSH 용어**(PubMed 색인자가 붙인 표준 용어, 핵심 주제는 x1.5) + OpenAlex 논문 키워드
- 가중치: 교신(마지막) 저자 1.0 · 1저자 0.8 · 중간 저자 0.25, 최근 논문일수록 높게 (6년마다 절반)
- `Humans`, `Mice`, `Retrospective Studies` 같은 공통 태그는 제외, 모든 연구실에 흔한 용어는 TF-IDF로 뒤로
- **연구 분야와 기술을 따로 뽑는다.** `data/technique_terms.csv`의 기술 사전(42개)에 걸리는 용어는 기술로,
  나머지는 연구 분야 키워드로 간다. 기술은 여러 표기를 하나로 모은다 (`FACS`, `flow cytometry` → **유세포분석**).
  사전에 행을 추가하면 새 기술을 인식한다 (`technique,label_en,pattern` — pattern은 정규식, 대소문자 무시).
- 지도: 별 정보창에 "연구 분야" / "주요 기술" 따로 표시, 필터 탭에 **기술** 칩(사용 교수 수 순), 검색창에 **전체 / 연구 분야 / 기술** 범위 선택
- 결과: `output/professor_keywords.csv`, 지도 별 정보창과 검색에 표시
- 한글 검색어 → 영문 키워드: `data/keyword_synonyms.csv` (`T세포` → `T-Lymphocytes|CD8-Positive T-Lymphocytes|…`). 자유롭게 추가 가능
