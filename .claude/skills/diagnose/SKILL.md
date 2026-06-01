---
name: diagnose
description: 설계 MD와 실제 코드 사이의 갭, 하드코딩·분기 복잡도, 누락 동작을 진단한다. "진단해줘", "검토해줘", "설계-코드 갭", "괴리 있어?", "하드코딩 있어?" 등으로 트리거.
---

# 코드-설계 진단

## 목적

아래 다섯 축으로 백엔드 코드와 설계 문서를 대조한다:

1. **설계-코드 갭** — docs/의 스펙과 backend/pipeline/ 구현이 일치하는가
2. **하드코딩·분기 복잡도** — 신규 양식 추가 시 코드 수정이 필요한 곳이 있는가
3. **누락 동작** — 스펙에 명시된 행동 중 코드에 없는 것이 있는가
4. **form 정의 동기화 갭** — form_XX.md 업무규칙이 form_types.json에 올바르게 반영되었는가, sync-form-config 스킬이 커버하지 못하는 필드가 있는가
5. **form 정의 가독성** — form_XX.md가 현업(비개발자)이 읽고 이해할 수 있는 언어로 작성되었는가

---

## 절차

### Step 1 — 설계 문서 읽기

다음 파일을 순서대로 읽는다:

- `docs/phase1-prompt.md`
- `docs/phase2-prompt.md`
- `docs/phase3-mapping.md`
- `docs/phase4-design.md`
- `docs/output-format.md`
- `config/form_types.json`
- `form_definitions/_index.md` — 등록된 form_id 목록 확인
- `form_definitions/form_XX.md` — _index.md에 등록된 모든 양식 (축 4용)
- `.claude/skills/sync-form-config/SKILL.md` — 스킬이 커버하는 필드 목록 확인 (축 4용)

### Step 2 — 구현 코드 읽기

다음 파일을 읽는다:

- `backend/pipeline/orchestrator.py`
- `backend/pipeline/phase1.py`
- `backend/pipeline/phase2.py`
- `backend/pipeline/phase3.py`
- `backend/pipeline/phase4.py`
- `backend/api/routes/` (폴더 내 전체)

### Step 2-B — form 정의 파일 읽기 (축 5용)

_index.md에 등록된 모든 form_XX.md를 읽는다.

### Step 3 — 진단 실행

각 축을 순서대로 분석한다.

#### 축 1: 설계-코드 갭

docs/의 스펙과 실제 구현을 대조한다.

확인 항목:
- 스펙에 명시된 입출력 필드가 코드에 모두 존재하는가
- 스펙에 정의된 분기 조건(양식 식별, cold-start 판단 등)이 코드에 반영되어 있는가
- 프롬프트 내용과 코드에서 Claude에게 넘기는 실제 메시지가 일치하는가
- Phase 간 데이터 흐름(이전 Phase 출력 → 다음 Phase 입력)이 스펙대로인가

#### 축 2: 하드코딩·분기 복잡도

신규 양식(form_XX.md)을 추가할 때 코드 수정 없이 동작해야 한다는 원칙에서 이탈한 곳을 찾는다.

확인 항목:
- 특정 form_id(form_01, form_04 등)를 if/elif로 분기하는 코드
- CSV 경로·컬럼명·필드명이 코드에 직접 박혀 있는 곳
- `config/form_types.json`을 읽지 않고 수식·타입을 코드에 직접 정의한 곳
- 매핑 파일 경로를 form_id 없이 고정으로 쓰는 곳

#### 축 3: 누락 동작

스펙에 명시되었으나 코드에서 확인되지 않는 동작을 열거한다.

확인 항목:
- 오류 처리(OCR 실패, LLM 타임아웃, 매핑 미발견 등) 처리 경로
- cold-start 분기(양식 미등록 시 동작)
- 캐시 누적 로직(ocr_retailer.csv, ocr_product.csv, ocr_dist.csv 자동 업데이트)
- Phase 4 교차검증 실패 시 처리

#### 축 4: form 정의 동기화 갭

`form_XX.md` 업무규칙과 `config/form_types.json`의 실제 값을 대조한다.

확인 항목:
- sync-form-config 스킬이 정의한 변환 규칙(2-A~2-H)대로 form_types.json이 채워져 있는가
- form_XX.md에 명시된 필드값(cover totals 키명, detail_group_field, bara_source 컬럼명 등)이 form_types.json에 그대로 반영되어 있는가
- form_XX.md가 수정되었으나 form_types.json에 반영되지 않은 항목이 있는가 (sync 누락)
- sync-form-config 스킬이 커버하지 못하는 필드가 form_types.json에 수동으로 하드코딩된 채 방치된 항목이 있는가
- form_XX.md 내 서로 다른 섹션 간 내용 모순 (예: NET 수식 섹션의 컬럼명이 추출 컬럼 섹션과 불일치)

#### 축 5: form 정의 가독성

form_XX.md는 **현업(비개발자)이 직접 읽고 검토하는 문서**다.  
아래 원칙에서 이탈한 곳을 찾는다.

**원칙 1 — 내부 필드명은 추출 컬럼 표에만 존재한다**

내부 필드명(`jisho`, `customer`, `kanri_no` 등 Phase 2 JSON 출력 키)은  
`추출 컬럼 표`(첫 번째 열)에만 나와야 한다.

위반 패턴:
- 계층 구조 섹션에 `→ items[].jisho 로 상속` 형태
- 교차검증 표에 `「jisho」기준` 또는 `items \`jisho\` groupby` 형태
- `[Phase 2]` 이외 섹션에서 `items[].fieldname` 표기

**원칙 2 — JSON·코드 표기는 Phase 섹션 안에서만**

`[Phase 2]`, `[Phase 4]` 제목이 붙은 섹션은 기술 사양이므로 허용.  
그 외 섹션(문서 구조, 계층 구조, 교차검증 등)에서:
- JSON 예시를 업무 설명으로 오해할 수 있는 형태로 노출하지 않는다
- `items[]`, `columns["X"]` 표기를 섹션 제목 없이 나열하지 않는다

**원칙 3 — 업무 판단 섹션은 한국어 또는 일본어 원문 용어만**

문서 구조, 계층 구조, 교차검증, 타입 분류 규칙의 설명 문장은  
일본어 청구서 원문 용어 + 한국어 설명으로만 구성한다.  
영어 camelCase 식별자(`by_unit`, `subtract_conditions` 등)가 설명 문장에 노출되면 위반.

확인 항목:
- 계층 구조 섹션에 내부 필드명이 노출되어 있는가
- 교차검증 표의 좌변이 시스템 필드명이 아닌 업무 명칭으로 쓰여 있는가
- 현업이 규칙을 추가·수정할 때 시스템 지식 없이도 표의 내용을 이해할 수 있는가

### Step 4 — 결과 보고

아래 형식으로 보고한다. 문제 없는 축은 "이상 없음"으로 짧게 처리한다.

```
## 진단 결과

### 축 1: 설계-코드 갭
[발견된 갭 목록, 또는 "이상 없음"]

### 축 2: 하드코딩·분기 복잡도
[발견된 항목 목록 (파일명:줄번호 포함), 또는 "이상 없음"]

### 축 3: 누락 동작
[누락 항목 목록, 또는 "이상 없음"]

### 축 4: form 정의 동기화 갭
[form별 불일치 항목 목록, 또는 "이상 없음"]

### 축 5: form 정의 가독성
[form별 위반 항목 목록 (섹션명 + 위반 내용), 또는 "이상 없음"]

### 우선순위 요약
[즉시 수정 필요 / 신규 양식 추가 전까지 / 운영 규모 전에 / 낮음 — 4단계로 분류]
```

---

## 제약

- 코드 수정은 하지 않는다. 진단·보고만 한다.
- 스펙에 없는 내용을 "있어야 한다"고 주장하지 않는다. 스펙을 기준으로 판단한다.
- 단순 코드 스타일 문제는 보고하지 않는다. 동작·구조 문제만 보고한다.
