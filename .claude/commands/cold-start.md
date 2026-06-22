# 신규 양식 정의 작성 (Cold-start)

`$ARGUMENTS`에 선택적으로 doc_id를 전달할 수 있다. (예: `/cold-start`, `/cold-start sample_007`)

신규 양식의 `form_definitions/form_XX.md`를 사용자와 함께 작성한다.

## 처리 순서

1. `form_definitions/_index.md`를 읽어 현재 등록된 양식과 다음 form_id를 확인한다.

2. doc_id가 전달된 경우: `extracted/{doc_id}/page_001.md` 등 기존 page MD를 읽어 양식 구조를 파악한다.

3. `form_definitions/form_template.md`를 읽어 초안 템플릿을 로드한다.

4. 사용자에게 아래를 순서대로 확인한다:
   - 발행처명 (得意先名)
   - 양식 특징 (column 구성, 합계 방식 등)
   - タイプ 분류 업무규칙 (있으면)

5. 확인된 내용으로 `form_definitions/form_XX.md` 초안을 작성하고 diff를 제시한다. 업무규칙이 확정된 항목은 form_XX.md 끝에 **`## [config]` 정본 블록**(실행 설정 JSON)도 함께 작성한다 — 이 블록이 단일 진실 소스다(형식: [docs/literate-config-migration.md](../../docs/literate-config-migration.md) §3). 미확정 항목은 블록을 비워두고 TBD로 남긴다.

6. 사용자 승인 후 저장. `form_definitions/_index.md`에 1줄 추가.

7. 저장 후 안내:
   ```
   form_XX.md 저장 완료.
   업무규칙이 확정되면 [config] 블록을 채우고 "동기화해줘"로 form_types.json을 빌드해 주세요.
   ```
   `[config]` 블록을 작성했다면 `python scripts/build_form_types.py`로 form_types.json을 빌드한다(블록이 정본, json은 생성물).

**원칙**:
- 사용자 승인 없이 form 파일을 저장하지 않는다.
- 발행처명·컬럼·업무규칙은 현업 도메인 지식이 필요하다. **개발자가 단독으로 진행하지 않는다** — 반드시 현업이 대화에 참여해야 한다.
- 확정되지 않은 타입 분류 규칙은 `TBD`로 기록한다. 현업 확인 후 update-form으로 확정한다.
