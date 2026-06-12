# sheets — 상세 레퍼런스

> 전제: 메인 `../SKILL.md`의 인증·보안 규칙을 먼저 따른다. PATH 미설치 시 `google-cli` → `./venv/bin/google-cli`.

## 명령

### read — 값 읽기
```bash
google-cli sheets read SPREADSHEET_ID RANGE [-f json|text|table|csv]
```
- `RANGE`는 A1 표기: `'Sheet1!A1:C10'`, `'A1:B'`, `'시트명!A:A'`.
- `-f csv`로 바로 CSV 출력(백업/후처리에 유용).

### info — 메타데이터
```bash
google-cli sheets info SPREADSHEET_ID [-f json|text]
```
시트(탭) 이름·인덱스 등 확인.

### write — 범위에 값 쓰기 ⚠
```bash
google-cli sheets write SPREADSHEET_ID RANGE -v '[["a","b"],[1,2]]'
```
`-v, --values`는 **JSON 2차원 배열**(행의 배열). 지정 범위에 덮어쓴다.

### append — 한 행 추가 ⚠
```bash
google-cli sheets append SPREADSHEET_ID RANGE -v '값1,값2,값3'
```
`-v`는 **콤마 구분 한 행**. RANGE는 보통 시작 셀/시트명(`'Sheet1!A1'`)을 준다.

### create — 새 스프레드시트 ⚠
```bash
google-cli sheets create TITLE [-f json]
```
반환 JSON의 `spreadsheetId`를 후속 명령에 사용.

## A1 표기 / values 포맷 요령
- 셀/범위: `A1`(단일), `A1:C10`(블록), `A:A`(열 전체), `1:1`(행 전체).
- 다른 탭: `'탭이름!A1:B2'` — 탭 이름에 공백/한글이 있으면 작은따옴표로 감싸기.
- `write`의 값은 JSON 배열(`'[[...],[...]]'`), `append`의 값은 콤마 문자열(`'a,b,c'`) — **둘이 다르다.**

```bash
# 헤더 + 1행 쓰기
google-cli sheets write SID 'Sheet1!A1:B2' -v '[["이름","점수"],["김",90]]'
# 한 행 누적
google-cli sheets append SID 'Sheet1!A1' -v '이,85'
# CSV로 백업
google-cli sheets read SID 'Sheet1!A1:Z1000' -f csv > backup.csv
```

## api 패스스루 (서식·차트·배치 등)
```bash
google-cli sheets api --list
google-cli sheets api spreadsheets.batchUpdate -p spreadsheetId=SID --body '{"requests":[...]}'
```

## See Also
- 메인 치트시트: `../SKILL.md`
- 다단계 레시피: `recipes.md`
