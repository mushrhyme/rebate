# Phase 3 — 판매처(dist) 1:N 결정 프롬프트

소매처는 확정됐으나 판매처(販売先) 후보가 2건 이상(1:N)일 때, Claude가 후보 중
하나를 고르거나 pending으로 둘지 결정하는 프롬프트다.

운영(Tool Use) 경로 `backend/pipeline/phase3_fallback.py:_run_single_dist_mapping`가
이 파일의 `## 프롬프트` 코드펜스를 읽어 사용한다. mtime 기반 캐시 — 이 파일을 고치면
백엔드 재시작 없이 다음 호출부터 반영된다. (legacy run_phase3 경로는
`docs/phase3-prompt.md`의 "판매처코드 결정" 섹션을 사용한다.)

## 치환 토큰

코드가 아래 토큰을 실제 값으로 치환한다(단순 문자열 치환). 토큰 외 본문은 그대로 전달된다.

| 토큰 | 의미 |
|------|------|
| `{{FORM_RULE_BLOCK}}` | 양식 정의(form_XX.md) 전문 블록. "판매처 결정 규칙" 포함. 없으면 빈 문자열 |
| `{{OCR_NAME}}` | OCR 소매처명 |
| `{{RETAILER_CODE}}` | 확정된 소매처코드 |
| `{{FORM_ID}}` | 양식 ID |
| `{{ISSUER}}` | 발행처 지문(issuer_fingerprint) |
| `{{JISHO_BLOCK}}` | `入出荷支店(jisho): <값>` 한 줄. jisho 없으면 빈 문자열 |
| `{{N_CANDIDATES}}` | 판매처 후보 개수 |
| `{{CANDIDATES}}` | 판매처 후보 목록(들여쓰기된 여러 줄) |

## 프롬프트

```
다음 소매처의 판매처(販売先)를 후보 목록에서 선택해라.

{{FORM_RULE_BLOCK}}소매처명: {{OCR_NAME}}
소매처코드: {{RETAILER_CODE}}
양식 ID: {{FORM_ID}}
발행처: {{ISSUER}}
{{JISHO_BLOCK}}
판매처 후보 ({{N_CANDIDATES}}건):
{{CANDIDATES}}

처리 기준:
1. 양식 정의에 "판매처 결정 규칙"이 있으면 그것을 최우선으로 따른다.
   특히 jisho(入出荷支店) 값이 주어지면 규칙의 jisho↔판매처 대응을 먼저 적용한다.
   같은 소매처라도 jisho가 다르면 다른 판매처가 될 수 있다.
2. 규칙으로 특정 불가 시 소매처명·코드·발행처 정보로 가장 적합한 판매처를 선택한다.
3. 확신이 없거나 구분이 불가능하면 "pending"을 선택한다.
4. 최종 응답은 아래 JSON만 출력한다. 설명·markdown·code fence 금지.

선택 케이스:
{"decision": "confirmed", "dist_code": "<후보_코드>", "reason": "<한 줄 이유>"}

미확정 케이스:
{"decision": "pending", "reason": "<판단 불가 이유>"}

주의: dist_code는 위 후보 목록에 있는 코드만 선택 가능. 회계 계산 금지.
```
