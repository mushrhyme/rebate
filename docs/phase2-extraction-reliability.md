# Phase 2 추출 신뢰성 개선 결정 기록

**최종 갱신**: 2026-06-01  
**대상**: Phase 2 page MD → items JSON 추출  
**확인 샘플**: `extracted/2월日本アクセスＣＶＳ①`, `extracted/4월日本アクセスCVS①`

---

## 1. 현재 구조 개요

Phase 2는 LLM 추출 + Python 감사 2단계로 구성된다.

```
page_*.md
  ↓
[form_04 전용] row anchor 생성 (phase2_row_anchor.py)
  - row_id, page, kanri_no_hint, jisho_hint, raw_row, amount_hint
  ↓
Phase 2 LLM (Sonnet)
  - row_id별 item 해석
  - item이 아니면 not_item으로 표시
  - item에는 반드시 row_id 포함
  ↓
Phase 2 Verify (phase2_verify.py)
  1차: row anchor 커버리지 — 누락 row_id를 Python으로 복구
  2차: 管理No計 역산 검증 — 결정적 복구 → Haiku 재요청
  → phase2_verify_report.json 저장
```

설계 원칙:
> LLM은 행의 의미를 읽고, Python은 행의 존재와 누락을 감사한다.

---

## 2. 확인된 문제 유형

| 문제 유형 | 현재 방어선 | 비고 |
|-----------|------------|------|
| LLM이 반복 표에서 상품 행 누락 | row anchor 1차 복구 | 구조적으로 탐지 가능 |
| 管理No 헤더 직후 첫 상품행 누락 | row anchor 1차 복구 | |
| 소액·소량 행 누락 | row anchor 1차 복구 | |
| 동일 제품명 cross-kanri dedup 오류 | dedup hash에 kanri_no 포함 | §3.2 참조 |
| phantom item (문서 헤더 행 오인식) | anchor 키워드 필터 + 페이지 리셋 | §3.1 참조 |
| OCR/Phase 1 숫자 오독 | 管理No計 역산 + OCR 오독 탐지 | upstream 문제. row anchor로 해결 불가 |
| MD 자체가 심하게 깨짐 | 미대응 | OCR JSON fallback 필요 시 별도 검토 |

---

## 3. 버그 기록 (4월日本アクセスCVS①, 2026-06-01)

`4월日本アクセスCVS①.pdf` 분석 과정에서 발견·수정된 버그 3건.

---

### 3.1 Bug: 文書ヘッダ행 phantom item → CVS営業部 합계 오버카운팅

**현상**

- Phase 2 output에 `product: "請求書"` 또는 `product: "No."` 같은 문서 메타데이터가 item으로 등록됨.
- CVS営業部 합계가 14,584,352 → 4,864,654 (정상)보다 약 2× 초과.

**근본 원인 (3중 연쇄)**

1. `build_row_anchors_form04`에서 `current_kanri`가 페이지 경계를 넘어 유지됨.  
   페이지 N이 管理No:XXXX로 끝나면, 페이지 N+1 최상단의 문서 헤더 표  
   (`| 請求書No. | 004859849 |`, `| 作成日 | 2026年5月13日 |` 등)가  
   이전 페이지의 kanri_no에 귀속된 row_id(`p003:k1710151:r01` 등)를 받음.

2. Phase 2 LLM이 이 row_id를 정상 item으로 추출 → phantom item 생성.

3. phantom 제거 코드(`valid_row_ids` 체크)가 작동 못함 — phantom row_id가 anchor에 존재하기 때문.

**수정 내용**

| 파일 | 수정 |
|------|------|
| `backend/pipeline/phase2_row_anchor.py` | 페이지 루프 진입 시 `current_kanri = None; row_idx = 0` 리셋 |
| `backend/pipeline/phase2_row_anchor.py` | `_HEADER_KEYWORDS`에 `'請求書', '作成日', 'ご請求期', 'お支払予定', '未収取扱', '発行元', '販売促進', '項目'` 추가 |
| `docs/phase2-prompt.md` | Rule 10 추가: 文書ヘッダ行（請求書No./作成日等）はitemとして抽出しない |

---

### 3.2 Bug: 동일 제품명 cross-kanri dedup → 1710201 항목 소실

**현상**

- 管理No:1710201 (page 7)의 `農心 辛ラーメントゥーンバカップ 113g` (数量:204, 金額:2,550) 누락.
- verify report: `expected: 2550, actual: 0`.

**근본 원인**

`_dedup_after_recovery`의 content hash에 `kanri_no`가 없었음.  
管理No:1710186 (page 5)에 동일 제품명 + 동일 数量 + 동일 金額 항목이 존재.  
두 항목이 같은 hash → dedup이 1710201을 1710186의 중복으로 제거.

**수정 내용**

`phase2_verify.py` — `_dedup_after_recovery` hash에 `kanri_no` 추가:

```python
# 수정 전
key = hashlib.md5(json.dumps({
    'customer': ..., 'product': ..., 'columns': ...,
}, ...).encode()).hexdigest()

# 수정 후
key = hashlib.md5(json.dumps({
    'kanri_no': item.get('kanri_no', ''),
    'customer': ..., 'product': ..., 'columns': ...,
}, ...).encode()).hexdigest()
```

---

### 3.3 Bug: jisho_hint 형식 불일치 → Haiku 폴백 → 비표준 item 구조

**현상**

1차 anchor 복구를 통과한 뒤 2차 검증에서 Haiku가  
`management_no`, `amount` 같은 비표준 필드로 item을 생성.  
`columns.金額`이 null → 집계 누락.

**근본 원인**

`_RE_JISHO`가 `[^|]+`로 전체 셀 텍스트를 캡처해  
`jisho_hint = "R営業中四国 入出荷センター:RC新居浜常温C 得意先:(株) ファミリーマート"` 로 저장됨.  
`jisho_template` 키는 LLM이 저장한 짧은 `"R営業中四国"` 형식 → 키 불일치 → lookup 실패  
→ `_check_anchor_coverage`에서 `continue` 스킵 → 1차 복구 미실행  
→ 2차 Haiku 폴백 → 비표준 구조 item 생성.

**수정 내용**

`phase2_row_anchor.py` — `_RE_JISHO`를 지소명만 캡처하도록 변경:

```python
# 수정 전
_RE_JISHO = re.compile(r'入出荷支店\s*[：:]\s*([^|]+)')

# 수정 후
_RE_JISHO = re.compile(r'入出荷支店\s*[：:]\s*(\S+)')
```

`jisho_hint`가 `"R営業中四国"`으로 저장되어 `jisho_template` lookup이 성공.  
1차 anchor 복구가 정상 실행 → Haiku 불필요.

---

### 3.4 Bug: phantom 제거 후 파일 미기록

**현상**

phantom item이 메모리에서 제거됐으나, 이후 anchor 복구가 없으면 파일에 반영되지 않음.

**근본 원인**

phantom 제거 블록이 파일 write를 `if anchor_recovered:` 분기 안에서만 수행.  
phantom만 있고 복구할 anchor가 없으면 수정 사항이 파일에 쓰이지 않음.

**수정 내용**

`phase2_verify.py` — phantom 제거 직후 즉시 파일 write:

```python
if phantom_removed:
    items = cleaned
    phase2_result['items'] = items
    out_path.write_text(...)   # anchor 복구 여부와 무관하게 즉시 기록
```

---

### 3.5 form_04.md 규칙 보강

위 디버깅 과정에서 함께 보강된 규칙:

- **Rule 1 (집계행 제외)**: 반각 `*`도 전각 `＊`와 동일하게 집계행으로 처리함을 명시.
- **Rule 9 (동명 제품 반복)**: cross-page 케이스 명시. 예: 1710186 (page 5)와 1710201 (page 7)이 동일 제품명·数量·金額이어도 각각 독립 item으로 추출.

---

## 4. 왜 기존 후처리만으로 부족한가

`phase2_verify.py`에는 이미 재추출/복구 레이어가 있다.

- `_parse_kanri_totals`: MD에서 管理No 블록과 `管理No 計` 추출
- `_try_deterministic_recovery`: MD 블록에서 누락 상품 행을 Python으로 복구 시도
- `_retry_missing_items`: 결정적 복구 실패 시 Haiku에 해당 블록만 재요청
- `_insert_after_kanri`: 복구 항목을 원래 管理No 위치에 삽입

하지만 합계 기반 후처리의 한계:

1. LLM이 행을 빠뜨린 뒤에야 발견한다.
2. 무엇이 빠졌는지 row 단위로 직접 알지 못하고, 금액 diff로 추정한다.
3. diff와 누락 후보 합계가 정확히 맞지 않으면 결정적 복구가 어렵다.
4. OCR/Phase 1 숫자 오독이 섞이면 누락과 숫자 오류를 구분하기 어렵다.
5. Haiku 폴백은 비표준 item 구조를 생성할 수 있다 (§3.3).

row anchor 방식은 직접적이다:

```
管理No 합계가 안 맞는다 → 어떤 행이 빠진 것 같다 → diff와 맞는 행을 찾아본다
                                ↓ (row anchor)
MD에 후보 row_id가 3개 있다 → LLM 결과에 row_id가 2개만 있다 → p007:k1710201:r00이 빠졌다
```

---

## 5. LLM 위임을 유지해야 하는 이유

Phase 2 전체를 Python 파서로 대체하는 것은 맞지 않다.

| 영역 | 이유 |
|------|------|
| 페이지 역할 판단 | cover/detail/summary/payment_form 구분은 문서별 문맥이 필요 |
| 계층 상속 | 入出荷支店, 得意先, 管理No, 条件タイプ의 상속 관계 판단 필요 |
| 깨진 MD 해석 | OCR/Phase 1 결과가 항상 정형 표로 나오지 않을 수 있음 |
| 신규 양식 cold-start | 처음 보는 양식의 의미 구조를 코드 없이 파악해야 함 |
| 조응·문맥 해석 | 上記, 同条件, 前記, 페이지 넘김 등은 규칙만으로 취약 |
| 양식별 유연성 | form_XX.md 가이드에 따라 추출 구조가 달라짐 |

---

## 6. row anchor 감사 레이어 설계

### 핵심 원칙

> Python은 감사 가능한 후보 row anchor를 넓게 만들고, LLM이 item/not_item 및 의미를 판단한다.

row anchor를 "Python이 상품행을 확정한다"로 설계하면 틀린다. LLM이 not_item으로 분류할 여지를 반드시 남긴다.

### anchor 필드

| 필드 | 설명 |
|------|------|
| `row_id` | `"p{page:03d}:k{kanri_no}:r{idx:02d}"` — 문서 내 유일 |
| `page` | 페이지 번호 |
| `kanri_no_hint` | 현재 管理No |
| `condition_type_hint` | 定番条件 / 原価引き条件 / 導入条件 (없으면 None) |
| `jisho_hint` | 지소명 (短形式, 예: `"R営業中四国"`) |
| `raw_row` | 원본 MD 행 문자열 |
| `amount_hint` | 마지막 양수 정수 셀 = 金額 추정값 |
| `row_index_in_kanri` | 블록 내 0-기반 인덱스 |

### 위험과 대응

| 위험 | 설명 | 대응 |
|------|------|------|
| false negative | Python이 상품행을 후보로 못 잡으면 row_id가 생기지 않는다 | form_04에만 적용. 管理No計 2차 감사로 보완 |
| false positive | 소계행, 헤더행을 후보로 넣을 수 있다 | LLM이 `not_item`으로 표시. 키워드 필터로 사전 차단 |
| jisho_hint 형식 불일치 | anchor와 item의 jisho 표현 불일치 → template lookup 실패 | `_RE_JISHO = re.compile(r'(\S+)')` 로 짧은 형식 저장 (§3.3) |
| cross-page kanri 오염 | 페이지 경계에서 이전 kanri 상태가 다음 페이지 헤더 행에 귀속 | 페이지 루프 시작 시 `current_kanri = None` 리셋 (§3.1) |
| MD 품질 의존 | Phase 1이 숫자를 잘못 만들면 anchor도 잘못된다 | 管理No 합계·산술 검증으로 별도 플래그 |

---

## 7. 남는 문제

- OCR/Phase 1 숫자 오독은 row anchor만으로 해결되지 않는다.
- MD가 심하게 깨지면 row anchor도 실패할 수 있다.
- form_04 외 양식에는 row anchor를 적용하지 않는다.
- Haiku 폴백이 여전히 비표준 구조를 만들 가능성이 있다. 1차 anchor 복구가 최대한 먼저 처리해야 Haiku까지 내려가지 않는다.
