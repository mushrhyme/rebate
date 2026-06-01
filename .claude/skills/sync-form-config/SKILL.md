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
명시하지 않았으면 `form_definitions/_index.md` 를 읽어 등록된 모든 form을 처리한다.

### Step 2 — form_XX.md 파싱

대상 form마다 form_definitions/form_XX.md 를 읽고 아래 섹션을 순서대로 파싱한다.

---

#### 2-A. `[Phase 4] NET 계산식` → `net` 설정

NET 계산 테이블에서 수식 패턴을 읽어 아래 formula 타입 중 하나로 매핑한다.

| MD 수식 패턴 | formula 값 | 추가 파라미터 |
|------------|-----------|------------|
| `仕切 - (C1 + C2)`, CS행 있음 | `subtract_conditions` | `cs_divide_by_case_qty: true` |
| `仕切 - (C1 + C2)`, CS행 없음 | `subtract_conditions` | `cs_divide_by_case_qty: false` |
| `仕切 - teiban - self` 구조 | `subtract_teiban_and_self` | `self_field: 해당 컬럼명` |
| `仕切 - (C1 + C2) / 入数` | `subtract_pack_conditions` | `divisor: 入数` |

`c1`, `c2` 는 수식에 등장하는 컬럼명을 그대로 사용한다 (예: `条件`, `未収条件`).  
단, 수식에 C2가 등장하더라도 MD에 "정의 전까지 0" 또는 "미정" 등 미확정 주석이 있으면 `c2: null` 로 설정한다.

**`no_net_kubun` 추출:**  
NET 계산 테이블에서 "NET 계산 없음" 텍스트가 포함된 행의 条件区分 값을 배열로 수집한다.  
TBD 행은 제외한다. 해당 행이 없으면 `no_net_kubun` 키를 생략한다.

예시 — form_01.md NET 계산 테이블:
| 条件区分 = 円 | NET 계산 없음 | → `no_net_kubun: ["円"]`
| 条件区分 = % | NET 계산 TBD | → 제외 (TBD)

위 패턴에 해당하지 않으면 → net 섹션을 갱신하지 않고 ⚠️ 개발자 확인 필요로 보고한다.

---

#### 2-B. OCR 정규화 규칙 → `preprocess` 설정

MD에 `÷ 100` 또는 `/ 100` 규칙이 명시된 컬럼이 있으면 preprocess 항목을 추가한다.

```json
{ "field": "컬럼명", "op": "divide_by_100", "guard_fields": [] }
```

guard_fields 는 MD에 명시된 경우만 채우고, 없으면 빈 배열로 둔다.  
(guard_fields 정확성은 개발자 검토 후 수동 보완)

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

### Step 3 — form_types.json 업데이트

파싱 결과를 `config/form_types.json` 에 반영한다.

- 기존 항목이 있으면 변경된 필드만 덮어쓴다.
- 새 form_id면 항목을 추가한다.
- label 은 form_XX.md 첫 줄 `# form_XX — {제목}` 에서 추출한다.
- 파싱 실패(⚠️)가 있어도 나머지 필드는 정상 반영한다.

### Step 4 — 변경 내역 보고

아래 형식으로 보고한다.

```
[sync-form-config 완료]

form_01: 변경 없음
form_04:
  - bara_source: "column:対象数量又は金額" → "column:数量"
  - cross_validation[1].detail_group_field: "jisho" 추가
  - summary: "invoice_totals" 확인
  ⚠️ preprocess guard_fields: 개발자 확인 필요

갱신된 항목이 없으면: "변경 사항 없음"
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
