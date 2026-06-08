# sync-form-config 스킬

## 트리거

다음 중 하나에 해당하면 이 스킬을 실행한다.

- "동기화" 포함 ("동기화해줘", "동기화시켜줘")
- "form" 또는 "md" + "수정/변경/고쳤/바꿨" 포함
- "업무규칙 변경/추가/수정" 포함
- "form_types 업데이트" 포함

**충돌 주의:**
- 이미지 첨부 + "반영해줘" → update-form 스킬 담당
- "업무규칙이 바뀌었어", "이 규칙 확정해줘", "키워드 추가해줘" 등 form_XX.md **내용 변경** 요청 → update-form 스킬 담당
- 이 스킬은 form_XX.md가 이미 수정된 상태에서 그 결과를 form_types.json에 반영할 때만 트리거한다.
  즉, "동기화해줘"처럼 명시적으로 sync를 요청하거나, update-form이 자동 연계로 호출할 때.

---

## 이 스킬이 하는 일

form_definitions/form_XX.md 를 읽어 `config/form_types.json` 을 자동 업데이트한다.

현업은 form_XX.md 만 수정하면 된다. form_types.json 을 직접 편집하지 않는다.

---

## 실행 절차

### Step 1 — 대상 form 결정

사용자가 form_id를 명시했으면 그 form만 처리한다.  
명시하지 않았으면 사용자에게 확인한다:
```
어떤 form을 동기화할까요? (예: "form_01 동기화해줘")
```

### Step 2 — form_XX.md 파싱

대상 form마다 form_definitions/form_XX.md 를 읽고 아래 섹션을 순서대로 파싱한다.

---

#### 2-A. `[Phase 4] NET 계산식` → `net` 설정

> **⚡ DSL 우선 원칙 (2026-06-05 확정)**  
> 신규 양식의 기본 경로는 반드시 `formula_type: "expr"` (DSL)이다.  
> Legacy named formula(`subtract_conditions` 등)는 기존 양식 호환용으로만 유지한다.  
> Plugin(`formula_type: "plugin"`)은 자동 적용 금지 — 반드시 개발자 승인 필요.

**DSL expr 매핑 (신규 양식 기본)**:

| MD 수식 패턴 | DSL expr 예시 | 추가 필드 |
|------------|-------------|---------|
| `仕切 - (C1 + C2)` | `"shikiri - (c1 + c2)"` | `vars: {c1: "条件", c2: null}` |
| `仕切 - (C1 + C2)`, CS÷入数 | `"shikiri - discount"` + computed_vars | `computed_vars: {discount: {expr: "c1+c2", divide_by: {...}}}` |
| `仕切 - teiban - 未収` | `"shikiri - teiban - c1"` | `vars: {c1: "未収条件"}`, `needs_teiban: true` |
| `仕切 - (C1 + C2) / 入数` (항상) | `"shikiri - (c1 + c2) / case_in"` | `vars: {c1: ..., case_in: "入数컬럼"}` |

**DSL 작성 규칙**:
- 허용 변수: `shikiri`, `teiban`, `vars`에 정의된 alias
- 허용 연산: `+`, `-`, `*`, `/`, `()`
- 금지: 함수 호출, 비교 연산, 문자열, 조건 분기
- 0 나누기 방어: `divide_by.zero_policy` 설정 (`skip_divide` 또는 `return_none`)

**`no_net_kubun` 추출:**  
NET 계산 테이블에서 "NET 계산 없음" 텍스트가 포함된 행의 条件区分 값을 배열로 수집한다.  
TBD 행은 제외한다. 해당 행이 없으면 `no_net_kubun` 키를 생략한다.

예시 — form_01.md NET 계산 테이블:
| 条件区分 = 円 | NET 계산 없음 | → `no_net_kubun: ["円"]`
| 条件区分 = % | NET 계산 TBD | → 제외 (TBD)

**위 DSL 패턴으로 표현 불가능한 경우**:
1. 먼저 개발자에게 "DSL expr으로 표현 가능한지" 검토를 요청한다.
2. 복잡한 조건 분기가 반드시 필요한 경우에만 Plugin 제안 (아래 Plugin 안전장치 참조).
3. 단순 수식이라면 반드시 DSL로 작성 — legacy named formula 신규 추가 금지.

위 패턴에 해당하지 않으면 → net 섹션을 갱신하지 않고 ⚠️ 개발자 확인 필요로 보고한다.

**Plugin 안전장치 (자동 적용 절대 금지)**:

Plugin이 필요한 경우는 매우 제한적이다. 아래 기준을 모두 충족할 때만 제안한다.

Plugin을 제안해야 하는 패턴 (드물다):
- 조건 분기가 3개 이상이고 DSL로 표현 불가
- 외부 테이블 조회가 필요
- 세율별 별도 산식이 필요

Plugin을 제안하면 안 되는 패턴 (DSL로 충분):
- `仕切 - (C1 + C2)` 형태는 무조건 DSL
- CS/個 분기는 `computed_vars.divide_by.when`으로 처리 가능
- teiban 차감은 `needs_teiban: true` + `teiban` 변수로 처리 가능

**Plugin 제안 형식 (승인 없이 적용 금지)**:
```
⚠️ [Plugin 제안 — 개발자 승인 필요]

이유: <DSL로 표현 불가능한 구체적 이유>
제안 plugin 이름: <snake_case 이름>
필요한 로직 설명: <한 줄>

승인 전까지 net 섹션을 갱신하지 않습니다.
```

---

#### 2-B. OCR 정규화 규칙 → `preprocess` 설정

MD에 `÷ 100` 또는 `/ 100` 규칙이 명시된 컬럼이 있으면 preprocess 항목을 추가한다.

```json
{ "field": "컬럼명", "op": "divide_by_100", "guard_fields": [] }
```

guard_fields 는 MD에 명시된 경우만 채우고, 없으면 빈 배열로 둔다.  
(guard_fields 정확성은 개발자 검토 후 수동 보완)

**⚠️ 중요 예외 — Phase 2가 이미 변환하는 경우 preprocess 추가 금지:**

MD에 `÷ 100` 규칙이 있더라도, 같은 섹션에 아래와 같은 표현이 함께 있으면 `preprocess`를 추가하지 않는다.

- "Phase 2 출력 시 \`<컬럼명>\`에는 **실제값**(소수 포함)을 기록한다"
- "Phase 2 출력 시 \`<컬럼명>\`에는 실제값을 기록한다"

이 경우 Phase 2 LLM이 이미 ÷100 변환을 수행해 출력하므로, phase4_calc.py의 `preprocess()`가 또 ÷100을 적용하면 **double-divide 버그**가 발생한다.

예시 — form_04의 未収条件:
- MD에 `実際値 = OCR値 ÷ 100` 규칙 존재 → preprocess 추가 대상처럼 보임
- 그러나 MD에 "Phase 2 출력 시 `columns["未収条件"]`에는 실제값(소수 포함)을 기록한다" 표현도 존재
- → Phase 2가 이미 처리 → **`preprocess: []` 유지, 추가 금지**

---

#### 2-C. `[Phase 2] 추출 컬럼` → `condition_display` 설정

컬럼 목록에서 조건 필드를 감지해 mode 를 결정한다.

| 감지 패턴 | mode | 추가 필드 |
|---------|------|---------|
| `columns["条件区分"]` 행 존재 | `by_kubun` | `kubun_field:"条件区分"`, `pack_kubun:"個"`, `keesu_kubun:"CS"`, `c1:"条件"`, `c2: (아래 규칙)` |
| `columns["条件"]` 존재 (条件区分 없음) | `keesu` | `c1:"条件"`, `c2: (아래 규칙)` |
| `columns["未収条件"]` 존재 | `keesu` | `c1:"未収条件"`, `c2: (아래 규칙)` |
| `columns["個別条件"]` 존재 | `pack` | `c1:"個別条件"`, `c2: (아래 규칙)` |

**c2 결정 규칙**: `c1` 컬럼명 + `"2"` 로 이어붙인 컬럼(예: `条件2`, `未収条件2`, `個別条件2`)이 추출 컬럼 표에 **실제로 존재하는 경우에만** `c2: "<컬럼명>"` 으로 설정한다. 존재하지 않으면 `c2: null`.

---

#### 2-D. `[Phase 2] 추출 컬럼` → `bara_source` 설정

추출 컬럼 표에서 NET 계산에 사용할 수량 컬럼을 감지한다.

| 감지 패턴 | bara_source 값 |
|---------|-------------|
| `columns["数量単位"]` 행 존재 | `"by_unit"` |
| `columns["バラ"]` 행 존재 | `"column:バラ"` |
| `columns["数量"]` 행 존재 (数量単位 없음) | `"column:数量"` |
| 수량 관련 컬럼 없음 | `"null"` |

**`qty_field` 추출:**

추출 컬럼 표에서 `数量` 계열 행이 **2개 이상** 존재하는 경우(예: `数量`과 `請求計上数量`),
해당 컬럼명들을 `qty_field` 배열로 설정한다.

```json
"qty_field": ["数量", "請求計上数量"]
```

- 단일 수량 컬럼이면 `qty_field`를 생략한다 (phase4_calc.py의 기본값 `["数量"]` 적용).
- 컬럼명 순서: 추출 컬럼 표의 위에서 아래 순서를 따른다.
- phase4_calc.py는 `qty_field` 배열을 순서대로 조회해 값이 있는 첫 번째 컬럼을 수량으로 사용한다.

---

#### 2-E. cover 페이지 totals 키 → `cover_totals` 설정

cover 페이지 totals 키 표에서 타입이 `dict` 인 행을 찾는다.

- `dict` 타입 행이 있으면 → `"cover_totals": {"breakdown_key": "<키명>"}`
- 없으면 → `"cover_totals": {}`

---

#### 2-F. `[Phase 4] 교차검증` → `cross_validation` 설정

교차검증 테이블 각 행을 분석해 `cross_validation` 배열을 생성한다.

**패턴 → type 매핑:**

| 좌변 패턴 | 우변 패턴 | type | 추가 파라미터 |
|---------|---------|------|------------|
| detail 全 金額 합산 | cover 세율별 키 2개 (`8%`·`10%` 税抜) | `cover_taxex_vs_detail` | `cover_key_8`, `cover_key_10` |
| detail 全 金額 합산 | cover 단일 합계 키 | `cover_honbai_vs_detail` | `cover_key` |
| `<業務名> 기준 金額 합산` | cover `<KEY>` (지점별 / dict 타입) | `cover_breakdown_vs_detail` | `cover_breakdown_key: KEY`, `detail_group_field: (추출 컬럼 표 역조회)` |
| summary 合計 | cover 단일 합계 키 | `cover_total_vs_summary` | `cover_key` |
| 得意先별 金額 합산 | summary 小計 | `per_customer_vs_summary` | — |
| summary 합계 | detail 합계 | `summary_vs_detail` | — |

**`detail_group_field` 역조회 규칙:**  
교차검증 표에는 업무 언어(예: `入出荷支店 기준 金額 합산`)만 기재된다.  
내부 필드명은 **추출 컬럼 표**에서 역방향으로 조회한다.

1. 교차검증 좌변에서 집계 기준이 되는 업무 명칭을 추출 (예: `入出荷支店`)
2. 추출 컬럼 표의 "원문 필드" 열을 검색해 일치하는 행을 찾음
3. 해당 행의 첫 번째 열(필드명)을 `detail_group_field` 로 사용

예시:
- 교차검증: `入出荷支店 기준 金額 합산`
- 추출 컬럼 표: `jisho | 入出荷支店名 | ...`
- → `detail_group_field: "jisho"`

**label 자동 생성:**
- `cover_breakdown_vs_detail` → `"支店 {key}"`
- 나머지 → `"Cover(<우변키>) vs Detail"` 형식

---

#### 2-G. 문서 구조 + cover totals 키 → `summary` + `summary_cover_keys` 설정

**`summary` 결정:**

| 감지 패턴 | summary 값 |
|---------|-----------|
| cover에 請求書No별 합계 구조 **AND** cover totals 키에 `本体合計金額`·`消費税金額`·`合計ご請求金額` 존재 | `"invoice_totals"` |
| cover에 8%/10% 세율별 내역 **AND** 문서 구조에 `summary` role 페이지 존재 | `"rate_then_customer"` |
| 위 모두 해당 없음 | `"standard"` |

**`summary_cover_keys` 결정:**  
cover totals 키 표에서 아래 패턴으로 semantic role을 매핑한다.  
해당 키가 없는 role은 생략한다.

| 키 패턴 | semantic role |
|--------|-------------|
| `本体合計金額` | `honbai` |
| `消費税金額` | `tax` |
| `合計ご請求金額` | `total` |
| `今回請求金額合計` (販促金請求 계열) | `hasso` |
| `役務提供 今回請求金額合計` | `yakumu` |
| `8%対象 税抜` | `taxex_8` |
| `10%対象 税抜` | `taxex_10` |
| `8%対象 消費税` | `tax_8` |
| `10%対象 消費税` | `tax_10` |

`summary` = `"standard"` 이면 `summary_cover_keys` 는 생략한다.

---

#### 2-I. `[Phase 4] 출력 설정` → `show_sections` + `aggregate_label` 설정

`[Phase 4] 출력 설정` 섹션이 있으면 아래 두 줄을 파싱한다.

- `show_sections: <쉼표 구분 목록>` → 문자열 배열로 변환. 예: `"rate_summary, xv"` → `["rate_summary", "xv"]`
- `aggregate_label: <한 줄 텍스트>` → 그대로 문자열로 저장.

섹션 자체가 없거나 해당 줄이 없으면 해당 키를 갱신하지 않는다.

---

#### 2-H. 계층 구조 → `row_anchor` 설정

**감지 조건:** 문서 내 계층 구조 섹션에 `← 항목 추출 단위` 마커가 있는 항목이 존재하는 경우.

**추출 절차:**

1. `← 항목 추출 단위` 라인에서 블록 식별자 추출 (예: `管理No`)
2. 계층에서 `→ 각 항목에 기록` 표기이고 추출 컬럼 표의 `jisho`에 해당하는 서브그룹 필드 추출 (예: `入出荷支店`)
3. 추출 컬럼 표의 `condition_type` 행 비고에서 조건 타입 목록 추출 (예: `定番条件 / 原価引き条件 / 導入条件`)
4. 계층에서 `— 추출 안 함` 항목 + 블록 식별자 → `header_keywords`의 form 전용 부분
   표준 문서 헤더 키워드는 항상 포함: `請求書`, `作成日`, `ご請求期`, `お支払予定`, `未収取扱`, `発行元`, `販売促進`, `項目`

**패턴 생성 규칙:**

단일 셀 식별자:

| 블록 식별자 | block_pattern |
|------------|--------------|
| `管理No` (뒤에 7자리 숫자) | `"管理No\\s*[：:]\\s*(\\d{5,8})"` |
| `請求書No` | `"請求書No\\.?\\s*[：:]?\\s*(\\d+)"` |
| 그 외 | ⚠️ 개발자 확인 필요 |

복합 셀 식별자 (`A + B` 형태 — 블록 헤더 행이 동시에 첫 번째 product 행):

| 블록 식별자 | block_pattern | 추가 필드 |
|------------|--------------|---------|
| `請求伝票番号 + 計上No` | `"^\\\|\\\\s*(\\\\d+[-][A-Z0-9]\\\\d+)\\\\s*\\\|"` | 아래 참조 |

`請求伝票番号 + 計上No` 복합 패턴의 추가 필드:
```json
"block_includes_product": true,
"product_cell": 2,
"row_id_cell": 2,
"total_pattern": "小計|合計",
"header_keywords": ["<B열 헤더 일본어명>"]
```
- `block_includes_product: true` — A열(伝票番号) 감지 행이 동시에 첫 번째 product 행
- `product_cell: 2` — B열(計上No)로 product 판정
- `row_id_cell: 2` — B열(計上No) 값을 row_id로 사용
- `header_keywords` — B열 헤더명(예: `"計上No"`)을 배열로

- `subgroup_pattern`: `"{서브그룹필드명}\\s*[：:]\\s*(\\S+)"`
- `condition_pattern`: 조건 타입을 `|`로 연결. 예: `"(定番条件|原価引き条件|導入条件)"`
- `total_pattern`: `"計[：:]"` (고정, 복합 패턴은 별도 지정)

**계층 구조 섹션이 없거나 `← 항목 추출 단위` 마커가 없으면 `row_anchor` 키를 생략한다.**

---

#### 2-J. `번들 경계 감지` → `bundle_detection` 설정

**감지 조건:** `## 번들 경계 감지` 섹션이 존재하는 경우.

표의 각 행에서 백틱(`) 안의 값들을 추출해 문자열 배열로 만든다.

| 행 레이블 | bundle_detection 키 |
|-----------|---------------------|
| `cover 필수 키워드` | `cover_required` |
| `cover 제외 키워드` | `cover_excluded` |
| `skip 마커 (번들 경계에서 제외)` | `skip_markers` |
| `skip 예외 (마커 있어도 skip 안 함)` | `skip_excluded` |

**섹션이 없으면:** `bundle_detection` 키를 생략한다 (단일 請求書 양식).

---

### Step 3 — form_types.json 업데이트 (검증 포함)

**3-A. 업데이트 전 백업 확인**

형재 `config/form_types.json` 을 기억해두어 롤백에 대비한다.

**3-B. 파싱 결과 반영**

파싱 결과를 `config/form_types.json` 에 반영한다.

- 기존 항목이 있으면 변경된 필드만 덮어쓴다.
- 새 form_id면 항목을 추가한다.
- label 은 form_XX.md 첫 줄 `# form_XX — {제목}` 에서 추출한다.
- 파싱 실패(⚠️)가 있어도 나머지 필드는 정상 반영한다.
- **Plugin 제안 항목은 저장하지 않는다** (승인 전까지).

**3-C. JSON Schema 검증 (필수)**

저장 후 아래 명령을 실행한다.

```bash
python -c "
import json
from jsonschema import validate, Draft7Validator
schema = json.load(open('config/form_types.schema.json', encoding='utf-8'))
data   = json.load(open('config/form_types.json', encoding='utf-8'))
errors = list(Draft7Validator(schema).iter_errors(data))
if errors:
    for e in errors:
        path = ' → '.join(str(p) for p in e.absolute_path)
        print(f'  [{path}] {e.message}')
    raise SystemExit('Schema 검증 실패 — form_types.json을 저장하지 않거나 롤백')
print('Schema OK')
"
```

검증 실패 시:
- form_types.json 변경사항을 원복한다 (백업으로 덮어쓰기)
- 아래 실패 보고 형식으로 보고한다
- 저장 완료 메시지를 출력하지 않는다

**3-D. 회귀 테스트 (필수)**

Schema 통과 후 회귀 테스트를 실행한다.

```bash
python -m pytest tests/regression/ tests/unit/ -q --tb=short
```

테스트 실패 시:
- form_types.json 변경사항을 원복한다
- 아래 실패 보고 형식으로 보고한다

### Step 4 — 변경 내역 보고

**성공 시 보고 형식:**

```
[sync-form-config 완료] ✅

form_01: 변경 없음
form_04:
  - net.expr: "shikiri - teiban - c1" (DSL)
  - bara_source: "column:数量"
  - cross_validation[1].detail_group_field: "jisho" 추가
  ⚠️ preprocess guard_fields: 개발자 확인 필요

Schema 검증: OK
회귀 테스트: passed (N개)

갱신된 항목이 없으면: "변경 사항 없음"
```

**실패 시 보고 형식:**

```
[sync-form-config 실패] ❌

실패 단계: Schema 검증 / 회귀 테스트 / 파싱 오류 (해당 항목 표시)
form_id: form_XX
실패 내용:
  - [net → expr] <오류 메시지>

조치: form_types.json을 수정 전 상태로 롤백했습니다.
다음 중 하나를 선택해주세요:
  1. form_XX.md 수식을 수정하고 재시도
  2. 개발자에게 ⚠️ 내용 전달
```

---

### Step 3-B — 알 수 없는 패턴 감지 시 코드 자동 생성

Step 2에서 ⚠️ (알 수 없는 패턴)이 하나라도 감지된 경우:

1. `scripts/phase4_calc.py` 전체를 읽는다
2. MD에 기술된 규칙과 기존 코드 구조(calc_net 함수, teiban_map 패턴 등)를 함께 분석해 필요한 코드 변경을 생성한다
   - `calc_net()` 함수에 새 formula 분기 추가가 필요하면 포함
   - pre-pass 로직(teiban_map 등) 변경이 필요하면 포함
   - 새 formula 이름은 기존 네이밍 컨벤션을 따른다 (`subtract_xxx` 형식)
3. 개발자에게 아래 형식으로 제안한다:

```
[코드 생성 제안]

감지된 새 패턴: <패턴 설명>

--- scripts/phase4_calc.py 변경 ---
<추가 또는 수정될 코드 (diff 형식)>

--- config/form_types.json 추가 ---
"formula": "subtract_xxx",
...

승인하시겠습니까? ("네" 또는 "적용해줘"로 응답)
```

4. 승인 확인 후:
   - `scripts/phase4_calc.py` 에 코드 적용
   - `config/form_types.json` 에 새 formula 반영
   - Step 4 보고에 "코드 자동 생성 + 적용" 내역 포함

---

## 제약

- 이 스킬은 form_XX.md 를 수정하지 않는다. form_types.json 과 scripts/phase4_calc.py 만 쓴다.
- 생성된 코드는 반드시 개발자 승인 후 적용한다. 자동 적용하지 않는다.
- **Plugin은 자동 적용 금지.** 제안 형식으로만 보고하고, 사용자 명시 승인 후 적용.
- **DSL 우선.** 신규 양식의 기본 경로는 `formula_type: "expr"`. Legacy named formula 신규 추가 금지.
- **Schema 검증과 회귀 테스트는 필수.** 하나라도 실패하면 form_types.json을 저장하지 않거나 롤백.
