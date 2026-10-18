# Korea Bio Map Web Prototype

현재 seed 교수 5명의 실제 자동수집 결과를 사용한 3D-force-graph 예시입니다.

## 로컬 실행

브라우저에서 파일을 직접 여는 것보다 간단한 로컬 서버 사용을 권장합니다.

```bash
cd "0. korea bio map/web"
python -m http.server 8000
```

그다음 http://localhost:8000 접속.

3D 라이브러리(three.js, 3d-force-graph)는 `vendor/`에 들어 있어서 인터넷 없이도 뜹니다.
`network-data.js`는 `0. korea bio map` 폴더에서 `python src/main.py rebuild`를 실행하면 다시 만들어집니다.

### 비교 탭 확인 순서

1. 별을 누르고 카드의 "비교에 담기"를 누릅니다 (최대 3곳).
2. 상단 "비교" 탭을 누르면 나란히 보는 표가 나옵니다.
3. 주소에 `#compare=P0001,P0002`가 붙으므로, 같은 주소를 열면 같은 조합이 복원됩니다.

### 라이선스

- `vendor/THREE-LICENSE.txt` (three.js, MIT)
- `vendor/3d-force-graph-LICENSE.txt` (3d-force-graph, MIT)

## 다음 단계

- network_nodes.csv / network_links.csv를 자동으로 network-data.js로 변환
- 교수 검색
- 클릭 시 이웃만 강조
- advisor_student / postdoc_mentor 색상 분리
- score에 따른 노드 크기 조절
