---
name: analyze-invoice
description: 청구서 PDF를 Phase 1~4 파이프라인으로 분석한다. "sample_003 분석해줘", "이 청구서 분석해줘", "NNN 분석", "analyze invoice" 등으로 트리거. OCR txt → page MD 생성 → 항목 추출 → 코드 매핑 + タイプ 분류 → NET 계산 → 결과 표시 순서로 진행하며, 매핑 확인이 필요한 시점에 사용자에게 질문한다.
---

# 청구서 분석 파이프라인

## 전제 확인

경로는 아래 우선순위로 **결정적으로** 도출한다. 사용자가 명시한 정보를 추론으로 덮어쓰지 않는다.

```
우선순위 1 — 사용자가 폴더를 명시한 경우
  "01 폴더의 sample_003"  → samples/01/sample_003
  "samples/02/sample_002" → samples/02/sample_002
  → 이 경로를 그대로 사용. 다른 폴더를 절대 탐색하지 않는다.

우선순위 2 — sample 번호만 명시한 경우 (폴더 언급 없음)
  "sample_003 분석해줘"   → NN = NNN 앞자리 0 하나 제거 (003→03)
                            samples/03/sample_003

우선순위 3 — sample 번호 형식이 아닌 doc_id (발행처명_발행월 등)
  "三菱食品東日本_2025.01 분석해줘"
  → Bash: find samples/ -name "{doc_id}.pdf" 로 탐색
  → 발견 시: 해당 경로 사용
  → 미발견 시: "samples/ 에서 {doc_id}.pdf 를 찾지 못했습니다. 경로를 확인해 주세요." 안내 후 중단

경로 확정 후 Bash로 _pages 폴더 존재 확인:
  ls samples/NN/sample_NNN_pages/

→ 존재: 묻지 않고 즉시 진행. doc_id = "sample_NNN"
→ 없음: 사용자에게 묻거나 중단하지 말고 azure-ocr 스킬을 즉시 실행해
        samples/NN/sample_NNN.pdf를 OCR 추출한다.
        azure-ocr가 samples/NN/sample_NNN_pages/page_NNN.ocr.txt를 직접 생성한다.
        추출 완료 후 자동으로 다음 단계(Phase 1)로 이어서 진행한다.
→ PDF도 없음: "samples/NN/sample_NNN.pdf 파일이 없습니다. 파일을 확인해 주세요." 안내 후 중단.

다른 폴더에 같은 번호의 OCR이 있어도 절대 대안으로 제시하지 않는다.
```

---

## Phase 1 — page MD 생성

**시작 시** Bash로 타임스탬프 기록:
```bash
python3 -c "
import json, time, os
p = 'extracted/{doc_id}/timing.json'
d = json.load(open(p)) if os.path.exists(p) else {'doc_id': '{doc_id}', 'phases': {}}
d['phases'].setdefault('phase1', {})['start'] = time.time()
d['phases']['phase1']['start_str'] = time.strftime('%Y-%m-%d %H:%M:%S')
json.dump(d, open(p, 'w'), ensure_ascii=False, indent=2)
"
```

`docs/phase1-prompt.md`를 읽어 생성 기준을 확인한다.
`samples/NN/sample_NNN_pages/` 폴더의 `page_NNN.ocr.txt` 파일 수를 세어 총 페이지 수를 파악한다.

**[필수] 페이지 수에 따라 처리 방식을 결정한다. 어떤 경우에도 직렬 처리는 금지.**

- 페이지 **4장 이하**: 직접 처리 허용
- 페이지 **5장 이상**: **반드시 Agent를 병렬로 분할 실행**
  - **page-md-generator** 에이전트를 3장씩 묶어 동시 dispatch
    예) 14페이지 → 5개 동시: 1-3 / 4-6 / 7-9 / 10-12 / 13-14
    각 에이전트에 `{page_range, pages_dir, output_dir}` 전달
  - 모든 에이전트 완료 후 다음 단계로 진행

**저장 위치**: `extracted/<doc_id>/page_001.md … page_NNN.md`

이미 해당 디렉토리에 page MD가 존재하면 생성을 건너뛰고 기존 파일을 사용한다.

page MD 생성(또는 재사용) 직후 **반드시** 아래 Bash 명령을 실행한다. 이 단계를 건너뛰면 안 된다:

```bash
python3 scripts/cleanup_phase1.py {doc_id}
```

**검증**: cleanup 완료 후(또는 기존 파일 재사용 시) `docs/phase1-prompt.md`의 **검증 기준** 섹션(C1~C7, N1~N3, T1~T4)을 실행한다.

진행 규칙 (`docs/phase1-prompt.md` 검증 기준 기반):
- ① 자동 수정 후 수정 내역만 보고 (계속 진행): N1, N2, C5, C6, C7
- ② 보고 후 자동 진행 (멈추지 않음): C4, T3
- ③ 목록 보고 후 사용자 확인 (중단점): C1, C2, C3, N3, T1, T2, T4
- 실패 없음 → 한 줄 요약 후 즉시 다음 Phase로 진행

**완료 시** Bash로 타임스탬프 기록:
```bash
python3 -c "
import json, time, os
p = 'extracted/{doc_id}/timing.json'
d = json.load(open(p))
ph = d['phases']['phase1']
ph['end'] = time.time()
ph['end_str'] = time.strftime('%Y-%m-%d %H:%M:%S')
ph['duration_sec'] = round(ph['end'] - ph['start'], 1)
json.dump(d, open(p, 'w'), ensure_ascii=False, indent=2)
"
```

---

## Phase 2 — 양식 식별 + 항목 추출

**시작 시** Bash로 타임스탬프 기록:
```bash
python3 -c "
import json, time, os
p = 'extracted/{doc_id}/timing.json'
d = json.load(open(p))
d['phases'].setdefault('phase2', {})['start'] = time.time()
d['phases']['phase2']['start_str'] = time.strftime('%Y-%m-%d %H:%M:%S')
json.dump(d, open(p, 'w'), ensure_ascii=False, indent=2)
"
```

### 양식 식별

`form_definitions/_index.md`를 읽어 form_id를 확정한다.
- 매칭 성공 → 해당 `form_definitions/form_XX.md` 로드
- 매칭 실패 → cold-start 분기:
  1. "처음 보는 양식입니다. form 정의를 함께 작성하겠습니다." 안내
  2. `.claude/commands/cold-start.md` 플로우를 실행해 `form_definitions/form_XX.md` 초안 작성
     (update-form 스킬은 기존 form 업데이트용이므로 여기서 사용하지 않는다)
  3. 사용자 승인 → `form_definitions/_index.md`에 등록
  4. **작성된 form_XX.md를 즉시 로드해 아래 항목 추출을 계속 진행한다** ← 멈추지 않는다

### 항목 추출

`docs/phase2-prompt.md`와 `form_XX.md`를 시스템 프롬프트 기준으로 삼아
모든 page MD를 종합해 아래 JSON을 출력한다:

```json
{
  "pages": [{"page": 1, "role": "cover|detail|summary", "totals": {}}],
  "items": [{
    "invoice_no": "請求伝票番号/計上No 원문 (예: 00752356 001)",
    "customer": "得意先名(OCR 원문)",
    "product": "商品名(OCR 원문)",
    "columns": {
      "数量": 0,
      "数量単位": "個|CS",
      "ケース入数": 0,
      "条件": 0,
      "条件区分": "円|個|CS",
      "金額": 0,
      "消費税率": "8.00% 外税"
    },
    "applied_conditions": ["조건 토큰"],
    "source_pages": [1]
  }]
}
```

> 스키마 기준: `docs/phase2-prompt.md` + `form_definitions/form_XX.md`의 "추출 컬럼" 목록.
> `columns` 키는 청구서 원문 컬럼명 그대로 사용. 양식마다 다를 수 있음.

**완료 시** Bash로 타임스탬프 기록:
```bash
python3 -c "
import json, time, os
p = 'extracted/{doc_id}/timing.json'
d = json.load(open(p))
ph = d['phases']['phase2']
ph['end'] = time.time()
ph['end_str'] = time.strftime('%Y-%m-%d %H:%M:%S')
ph['duration_sec'] = round(ph['end'] - ph['start'], 1)
json.dump(d, open(p, 'w'), ensure_ascii=False, indent=2)
"
```

---

## Phase 3 — 매핑 + タイプ 분류

**시작 시** Bash로 타임스탬프 기록:
```bash
python3 -c "
import json, time, os
p = 'extracted/{doc_id}/timing.json'
d = json.load(open(p))
d['phases'].setdefault('phase3', {})['start'] = time.time()
d['phases']['phase3']['start_str'] = time.strftime('%Y-%m-%d %H:%M:%S')
json.dump(d, open(p, 'w'), ensure_ascii=False, indent=2)
"
```

**소매처 매핑은 문서 단위가 아니라 得意先 단위로 수행한다.**
명세에 등장하는 得意先名이 N개면 소매처 매핑도 N번 각각 실행한다.
(예: ローソントウカイ·ローソンシズオカ·ローソンホクリク·ローソンアイギ → 각각 개별 소매처코드)

**[사전] unique 추출** — items[]에서 unique customer_ocr 목록, unique product_ocr 목록을 추출한다.
이후 ①③ 에이전트가 unique 단위로 처리하고, 확정 결과를 items 전체에 일괄 적용한다.

**중단 원칙**: 에이전트가 `NEEDS_CONFIRMATION`으로 반환한 항목은 결과 적용 전에 반드시 사용자 확인을 받는다.

**사전 분기 — 消費税計上 항목 (得意先コード = `0000000`)**
得意先名에 괄호 코드 `0000000`이 포함된 행은 ①②③을 전부 건너뛴다.
- 소매처·판매처·제품코드 매핑 없음
- タイプ = `非課税` 확정
- NET = `columns["金額"]` 그대로 사용 (Phase 4 Python 계산에서도 동일 처리)
- 결과 표시 시 `[消費税計上]` 태그를 붙여 일반 항목과 구분

### ①③ 에이전트 dispatch (동시 실행)

**retailer-mapper**와 **product-mapper** 에이전트를 동시에 spawn한다.

| 에이전트 | 전달 입력 | 반환 출력 |
|---------|----------|----------|
| retailer-mapper | `{customer_ocr_list, form_id, issuer}` | `{customer_ocr → retailer_code}` 매핑 JSON |
| product-mapper  | `{product_ocr_list}` | `{product_ocr → product_code, candidates}` 매핑 JSON |

> 두 에이전트 모두 캐시 조회 → CSV 검색 → 신뢰도 판정을 스스로 수행한다.  
> `NEEDS_CONFIRMATION` 항목만 아래 절차로 사용자에게 확인한다.

### NEEDS_CONFIRMATION 처리

**소매처 (retailer-mapper 결과):**
OCR 得意先名 + 후보 목록을 나란히 제시 → 선택 확정 → `mappings/ocr_retailer.csv` 추가

**제품 (product-mapper 결과):**

표시 형식:
```
OCR: チャパゲティ 140g — 후보를 선택해 주세요:

  1. 101000551 | チャパゲティー1P      | 140g | 30입 | 시키리 133 | 본부장 91
  2. 101000552 | チャパゲティ 140g×2P  | 280g | 15입 | 시키리 250 | 본부장 180
  3. 101004881 | チャパゲリカップ24入  | 114g | 12입 | 시키리 185 | 본부장 145

  → 번호 입력 또는 "없음" (직접 제품코드 입력)
```

후보 0건 시: "unit_price.csv에서 후보를 찾지 못했습니다. 제품코드를 직접 입력해 주세요."
확정 후 `mappings/ocr_product.csv` 추가.

### ② 판매처코드 (① 확정 후 실행)

캐시 키: `(form_id, issuer_fingerprint, 소매처코드)` — fingerprint는 form_XX.md의 fingerprint_fields 정의를 따름.

```
Step 0: ocr_dist.csv에서 (form_id, issuer_fingerprint, 소매처코드) 조회
        → hit: 판매처코드 확정, 끝

Step 1: retail_user.csv에서 소매처코드로 후보 조회
        → 1건: 확정
        → 2건 이상(1:N): form_XX.md 판매처 결정 규칙 → 일치 시 확정

Step 2 (후보 2건 이상: 1:N):
        → NEEDS_CONFIRMATION — 후보 목록 + cover issuer 정보 함께 표시
        → 사용자가 선택 → 확정 후 ocr_dist.csv 저장
        ※ "판매처코드를 찾지 못했습니다. 직접 입력해 주세요." — retail_user에도 없는 경우
```

### ④ タイプ

```
결정적 룰 우선 적용:
  消費税率 = 非課税 → 非課税 확정
  그 외 → 条件 기본값으로 설정, 사용자 검토 시 수정 가능

(분류 기준은 업무규칙 수령 후 양식별로 추가 예정)
```

**완료 시** ①~④ 확정값을 `extracted/{doc_id}/phase3_output.json`으로 저장한다:

```json
{
  "doc_id": "{doc_id}",
  "form_id": "form_01",
  "hatsu_month": "YYYY.MM",
  "issuer": {
    "name": "cover 페이지 issuer.name",
    "tel": "cover 페이지 issuer.tel"
  },
  "items": [
    {
      "invoice_no":         "청구서 번호",
      "customer_ocr":       "得意先名(OCR 원문)",
      "product_ocr":        "商品名(OCR 원문)",
      "retailer_code":      "Phase 3 ① 확정값",
      "dist_code":          "Phase 3 ② 확정값",
      "product_code":       "Phase 3 ③ 확정값 (미확정 시 null)",
      "unconfirmed":        false,
      "columns":            {},
      "applied_conditions": []
    }
  ]
}
```

그 다음 Bash로 타임스탬프 기록:
```bash
python3 -c "
import json, time, os
p = 'extracted/{doc_id}/timing.json'
d = json.load(open(p))
ph = d['phases']['phase3']
ph['end'] = time.time()
ph['end_str'] = time.strftime('%Y-%m-%d %H:%M:%S')
ph['duration_sec'] = round(ph['end'] - ph['start'], 1)
json.dump(d, open(p, 'w'), ensure_ascii=False, indent=2)
"
```

---

## Phase 4 — NET 계산

**산수는 Claude가 직접 하지 않는다. 반드시 Python 코드를 작성해 Bash로 실행하고 그 결과를 사용한다.**

### 실행 방식

Bash tool로 아래 명령어를 실행한다. 타이밍 기록은 스크립트가 자동으로 처리한다.

```bash
python3 scripts/phase4_calc.py --doc {doc_id} --save
```

- `--save` 옵션: `extracted/{doc_id}/phase4_output.json` 저장
- `scripts/phase4_calc.py`는 `phase3_output.json`을 입력으로 받는다.
- `unit_price.csv`에서 시키리·본부장 재조회, form별 NET 계산, 교차검증, timing.json 기록을 일괄 수행한다.
- **이 스크립트를 직접 수정하거나 대체 스크립트를 작성하지 않는다.** 계산 로직 변경이 필요하면 `scripts/phase4_calc.py`를 수정한다.

---

## Phase 5 — 결과 표시

사용자가 특정 得意先名을 지정하면 해당 행만, 지정하지 않으면 전체를 표시한다.
得意先별로 그룹핑하고, 그룹 헤더에 **매핑 근거**를 함께 표시한다.
사용자는 코드 체계를 모르므로 반드시 마스터의 명칭을 함께 보여줘서 매핑이 맞는지 스스로 판단할 수 있게 한다.

```
■ {得意先名(OCR)} ({domae코드})
   소매처: {소매처코드} → {retail_user.csv의 소매처명}  [근거: domae_retail_1.csv {domae코드} 직접 매칭]
   판매처: {판매처코드} → {retail_user.csv의 판매처명}

| 商品名(OCR) | 제품코드 | マスタ商品名 | タイプ | 条件 | 仕切 | NET | 수량 | 최종금액 |
...
小計: {합계}
```

**매핑 근거 표기 규칙:**
- domae_retail_1.csv 직접 매칭 → `[domae코드 직접매칭]`
- retail_user.csv 유사도 → `[유사도 매칭: {매칭된 마스터명}]`
- 캐시 적중 → `[캐시]`
- 제품코드도 동일하게 OCR명 옆에 `→ unit_price.csv: {마스터 제품명}` 표시

⚠️ 행은 해당 그룹 내 상단에 표시한다.

---

## 진행 원칙

- **산수는 Python 코드로만 한다.** Claude가 직접 금액을 계산하지 않는다.
- **매핑 캐시(ocr_retailer.csv, ocr_product.csv)는 사용자가 확인한 것만 추가한다.**
- 신규 양식(cold-start)은 form 정의 초안 작성 후 사용자 승인을 받고 계속 진행한다.
- 각 Phase 완료 후 한 줄 진행 상황을 보고한다 ("Phase 1 완료 — 9페이지 MD 생성").
