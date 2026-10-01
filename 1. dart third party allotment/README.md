# 유상증자 제3자배정 회사 수집기 (DART)

DART 오픈API에서 **유상증자결정 공시 중 증자방식이 제3자배정인 건**을 모아 CSV로 저장합니다.

## 결과 파일 (`output/`)

| 파일 | 내용 |
|---|---|
| `third_party_allotments.csv` | 공시 1건당 1행: 회사명, 종목코드, 시장, 신주 수, 조달금액·자금용도, 배정대상자, 대표자, 업종코드, 주소, 공시링크 |
| `allottees.csv` | 배정대상자 1명당 1행: 대상자명, 회사와의 관계, 배정주식수, 비고 |

엑셀에서 바로 열리도록 UTF-8 BOM으로 저장합니다.

## 실행 방법

1. https://opendart.fss.or.kr 에서 인증키를 발급받습니다.
2. GitHub 저장소 **Settings → Secrets and variables → Actions** 에 `DART_API_KEY` 를 추가합니다.
3. **Actions → DART Third-Party Allotment → Run workflow** 를 누릅니다. 조회 기간(일)을 바꿀 수 있습니다.
   결과는 실행 화면의 Artifact로 받을 수 있고, 같은 브랜치의 `output/` 폴더에도 커밋됩니다.

로컬 실행:

```bash
DART_API_KEY=발급받은키 python src/collect.py --days 365
DART_API_KEY=발급받은키 python src/collect.py --start 20250101 --end 20250930
python src/collect.py --help
```

외부 패키지는 필요 없습니다 (Python 3.9+ 표준 라이브러리만 사용).

## 참고

- 회사코드 없이 공시 목록을 조회할 때 DART는 기간을 3개월로 제한하므로 89일 단위로 나눠 조회합니다.
- 정정공시(`[기재정정]`)도 별도 행으로 포함됩니다. 최종본만 보려면 같은 회사·날짜의 최신 접수번호를 쓰세요.
- 배정대상자는 공시 원문 표에서 추출하므로 표 형식이 특이한 공시는 비어 있을 수 있습니다. 이때는 `공시링크`로 원문을 확인하세요.
- 업종코드(KSIC)로 바이오 기업만 거를 수 있습니다. 예: `21`(의약품 제조), `271`(의료용 기기 제조), `70113`(의학·약학 연구개발).
