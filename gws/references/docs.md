# docs — 상세 레퍼런스

> 전제: 메인 `../SKILL.md`의 인증·보안 규칙을 먼저 따른다. PATH 미설치 시 `google-cli` → `./venv/bin/google-cli`.

## 명령

### read — 본문 읽기
```bash
google-cli docs read DOCUMENT_ID [-f json|text]
```
`-f text`는 본문 텍스트, `-f json`은 문서 구조(제목·body content)까지.

### info — 메타데이터
```bash
google-cli docs info DOCUMENT_ID [-f json|text]
```

### write — 끝에 텍스트 추가 ⚠
```bash
google-cli docs write DOCUMENT_ID "추가할 텍스트"
```
문서 **끝에 append** 한다(기존 내용 유지). 위치 지정 삽입·서식은 api 사용.

### create — 새 문서 ⚠
```bash
google-cli docs create TITLE [-f json]
```
반환 JSON의 `documentId`를 후속 명령에 사용.

## 요령
- `docs write`는 단순 말미 추가 전용. **특정 위치 삽입/서식(굵게·제목 스타일·표)** 은 `docs api documents.batchUpdate`로:
```bash
google-cli docs api documents.batchUpdate -p documentId=DID \
  --body '{"requests":[{"insertText":{"location":{"index":1},"text":"머리말\n"}}]}'
```
- 새 문서를 만들고 내용을 채우는 흐름: `create` → `documentId` 확보 → `write` 또는 `batchUpdate`.

```bash
# 새 문서 생성 후 내용 추가
DID=$(google-cli docs create "회의록 2026-06-10" -f json | jq -r '.data.documentId')
google-cli docs write "$DID" "참석자: ...\n안건: ..."
```

## See Also
- 메인 치트시트: `../SKILL.md`
- 다단계 레시피(시트→문서 보고서 등): `recipes.md`
