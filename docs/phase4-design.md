# Phase 4 설계 — NET 계산·교차검증·SAP 포맷 생성

> Phase 3에서 코드가 확정된 뒤 실행. 완전 결정적 Python 코드.
> LLM 없음. 재현성이 생명.

---

## 입력

| 소스                        | 내용                                                                            |
| --------------------------- | ------------------------------------------------------------------------------- |
| Phase 2 JSON `items[]`    | OCR 추출값 (`columns`, `applied_conditions` 등)                             |
| Phase 3 확정 코드           | 소매처코드, 판매처코드, 제품코드                                                |
| Phase 3 확정 タイプ         | 条件·販促費8%·CF10% 등 — 기본값 条件, 업무규칙 수령 후 양식별 로직 추가 예정 |
| `mappings/unit_price.csv` | 제품코드 → 시키리, 본부장                                                      |
| [docs/output-format.md](output-format.md) | Excel 출력 컬럼 정의 (P4 채움 / Excel 수식 / 사용자 입력 구분) |

---


## 실행 순서

### Step 1 — 단가 조회

제품코드 확정 후 `unit_price.csv`에서 시키리(仕切)·본부장(本部長) 조회.

### Step 2 — 특수 전처리 (양식별)

각 `form_XX.md`의 전처리 규칙(행 병합, 소수점 변환 등)을 적용한다.

### Step 3 — NET 계산

수식 세부 내용은 각 양식 정의 파일을 단일 출처로 한다.  
수식 정의는 `config/form_types.json`의 `net` 섹션에 저장되며, `scripts/phase4_calc.py`가 읽는다.

**수식 경로 (우선순위 순)**:

| 경로 | 키 | 용도 |
|------|-----|------|
| **DSL expr** (기본) | `formula_type: "expr"` | 산술 표현식 + 변수 매핑. 신규 양식 기본 경로. |
| Plugin (예외) | `formula_type: "plugin"` | DSL로 표현 불가한 복잡한 산식. 개발자 승인 필요. |
| Legacy named formula | `formula: "subtract_conditions"` 등 | 하위 호환. 신규 추가 금지. |

> **원칙**: Claude는 form_XX.md에서 수식 구조를 읽어 DSL expr 설정을 생성한다.  
> 실제 계산은 Python 결정적 코드(`phase4_calc.py`)만 수행한다. Claude가 직접 계산하지 않는다.  
> 자세한 내용 → [docs/phase4-dsl-readiness.md](phase4-dsl-readiness.md)

| 양식 | 현재 수식 | 세부 |
| ---- | --------- | ---- |
| **01** | `shikiri - discount` (DSL expr + computed_vars) | → [form_01.md](../form_definitions/form_01.md) |
| **02** | 미작성 | → form_02.md 수령 후 DSL 매핑 |
| **03** | 미작성 | → form_03.md 수령 후 DSL 매핑 |
| **04** | `shikiri - teiban - c1` (DSL expr + needs_teiban) | → [form_04.md](../form_definitions/form_04.md) |
| **05** | 미작성 | → form_05.md 수령 후 DSL 매핑 |

### Step 4 — 교차검증

두 단계로 실행된다:

1. **Python (`scripts/phase4_calc.py`)** — cover/summary `totals`(Phase 2 JSON)와 detail 합계를 수치 비교. 불일치 시 `ok: false` + diff 기록. **Python calc의 xv[]가 존재하면 이것이 최종 결과다.**
2. **Claude (`backend/pipeline/phase4.py`)** — Python calc가 xv[]를 생성하지 못한 경우(form_types.json에 cross_validation 미설정 등)에만 실행하는 fallback. form_XX.md 교차검증 섹션을 읽어 수치 검증 후 xv[]를 채운다.

> 회계 산수는 결정적 코드가 담당 (재현성이 생명). Python calc의 xv[]는 정확하므로 Claude 재검증은 불필요하다. Claude xv는 Python이 커버하지 못하는 신규 양식 cold-start 상황을 위한 안전망이다.

불일치 시 해당 항목에 `⚠️` flag.

**매칭 원칙**: summary totals 키는 Phase 2 OCR 원문 그대로 추출된 값이므로, items[] 집계 시 `customer_ocr`(OCR 원문)를 기준으로 groupby한다. Phase 3에서 매핑된 retail_user 소매처명 사용 금지.

#### 양식별 집계 계층

결과를 표시할 때 **계층 합계를 먼저 보여주고** 사용자가 확인한 뒤 상세로 넘어간다.
합계가 모두 일치하면 상세 확인을 건너뛸 수 있다.

교차검증 세부 규칙(검증 대상·비교 키·예외 처리)은 각 양식 정의 파일을 단일 출처로 한다.

| 양식 | 세부 교차검증 |
| ---- | ------------- |
| **01** | → [form_01.md](../form_definitions/form_01.md) |
| **02** | → form_02.md (미작성) |
| **03** | → form_03.md (미작성) |
| **04** | → [form_04.md](../form_definitions/form_04.md) |
| **05** | → form_05.md (미작성) |

### Step 5 — SAP 내보내기 필터

SAP 내보내기 화면에서 `confirmed_at IS NOT NULL`(문서 확정) 조건으로 필터링한다.
1차·2차 검토 완료 여부는 행 단위가 아닌 문서 확정 플래그 하나로 대표된다.

detail 페이지 항목만 Excel에 포함 (`page_role = 'detail'`).

**교차검증 불일치 처리**: 차단 없음. 불일치 항목에 `⚠️` flag 표시 후 사용자가 육안 확인.
