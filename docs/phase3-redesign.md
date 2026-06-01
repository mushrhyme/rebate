# Phase 3 재설계 — "MD가 단일 진실 소스" 원칙 복원

**작성일:** 2026-05-19  
**배경:** Phase B 전환 시 Phase 3에 Python CSV 조회 로직이 과도하게 투입돼 form_XX.md와 코드 사이에 drift가 발생했다. 이를 Phase A의 원칙(Claude가 form_XX.md를 읽고 판단)으로 되돌린다.

---

## 진단: 무엇이 잘못됐나

### Phase A 원칙

```
form_XX.md → Claude가 읽고 전부 판단
```

새 양식 추가 = MD 파일 하나. 코드 수정 없음.

### Phase B에서 깨진 것

```
form_XX.md  → Claude (Phase 2: 추출 + 타입분류)
           → Python (Phase 3: CSV 조회, form별 분기)  ← 문제
form_types.json → Python (Phase 4: NET 계산)          ← 이건 맞음
```

Phase 3에 form_id 분기가 3곳 생겼다:

| 위치 | 내용 | 증상 |
|------|------|------|
| `_resolve_retailers_python` | form_01 괄호 코드 추출 하드코딩 | form_01.md 규칙과 별도 구현 |
| `_retailer_csv_context` | form_id별 CSV 파일 선택 | 새 양식 추가 시 코드 수정 필요 |
| `_extract_cover_totals` | form_04 다중 cover 처리 | form_04.md 규칙과 별도 구현 |

판매처 1:N tiebreak(form_01.md에 명시)도 미구현 상태였다(주석만 있고 로직 없음).

### API 전환이 원인이 아니다

Anthropic API는 Claude Code 채팅과 동일한 Claude에 프로그래밍 방식으로 접근하는 인터페이스일 뿐이다. "Claude가 form_XX.md를 읽고 판단한다" 방침을 유지하는 데 어떠한 제약도 없다.

Phase 3에 Python 조회가 들어간 것은 "결정적인 건 Python으로"라는 원칙을 CSV 테이블 조회에까지 과도하게 적용한 결과다. 그 원칙은 Phase 4 NET 계산에만 해당한다.

---

## 설계 원칙 (변경 없음)

| 담당 | 작업 | 근거 |
|------|------|------|
| **Claude** | 분류·식별·매핑 판단 | 비정형 텍스트 이해, 규칙 적용 |
| **Python** | NET 계산·교차검증·SAP 생성 | 회계 재현성. "왜 이 금액인가" = 코드 한 줄 |
| **Python** | 확정 캐시 관리 | 학습 축적, 속도 |

---

## 목표 구조: Phase 3

### 새 흐름

```
[입력] phase2_result (items[], pages[issuer포함]), form_id

① Python — 캐시 조회
   ocr_retailer.csv / ocr_product.csv / ocr_dist.csv
   히트 → 확정
   미스 → ② 로 넘김

② Claude — 매핑 결정 (캐시 미스 항목 전체)
   시스템 프롬프트 (캐싱):
     - Phase 3 매핑 지시문
     - form_XX.md 전체
     - 관련 CSV 전체 (retail_user, unit_price,
                       domae_retail_1 또는 domae_retail_2)
   사용자 메시지:
     - cover 페이지 issuer (name, tel)
     - 캐시 미스 거래처명 목록
     - 캐시 미스 제품명 목록
   
   Claude 반환:
     - 거래처명별: retailer_code, dist_code, confidence, basis
       (판매처 1:N이면 form_XX.md fingerprint_fields로 tiebreak)
     - 제품명별: product_code, confidence, basis
     - 모호 케이스: NEEDS_CONFIRMATION + candidates

③ Python — 캐시 저장 및 아이템 적용
   high confidence → 캐시에 저장
   NEEDS_CONFIRMATION → pending 목록
   모든 아이템에 codes 적용
```

### 무엇이 사라지나

| 제거 대상 | 이유 |
|-----------|------|
| `_resolve_retailers_python` | Claude가 form_XX.md 읽고 처리 |
| `_resolve_products_python` | 동일 |
| `_resolve_dist_codes` | Claude가 issuer 참조해 처리 |
| `_retailer_csv_context` (form_id 분기) | Claude가 필요한 CSV 전체를 받음 |
| `_extract_cover_totals` (form_id 분기) | phase2 pages[] 구조 그대로 활용 |
| `form_types.json` type_rule / type_rule_config | 이미 제거 완료 (2026-05-19) |

### 무엇이 남나

| 유지 대상 | 이유 |
|-----------|------|
| 캐시 조회/저장 (ocr_*.csv) | 속도 + 학습 축적 |
| Phase 4 Python | NET 계산 재현성 |
| pending 목록 생성 | 사용자 확인 UI 연동 |

---

## Phase 3 프롬프트 설계

### 현재 문제

- `retailer-mapper.md` + `product-mapper.md` 두 에이전트로 분리돼 있음
- 각각 form_id에 따라 다른 CSV를 받아 처리
- 판매처 매핑이 별도 Python 함수로 분리돼 있어 issuer 정보를 활용 못 함

### 개선 방향

하나의 Phase 3 매핑 프롬프트로 통합:
- `docs/phase3-prompt.md` 신규 작성
- 거래처명 → 소매처코드 + 판매처코드 (issuer.name으로 판매처 tiebreak)
- 제품명 → 제품코드
- 세 가지를 한 번의 Claude 호출로 처리 (병렬 분리보다 컨텍스트 공유가 유리)

### 출력 포맷

```json
{
  "retailers": [
    {
      "ocr_name": "小田急商事(株) OXストアー (13120769)",
      "retailer_code": "R1234",
      "dist_code": "D5678",
      "confidence": "high",
      "basis": "form_01 괄호 코드 → domae_retail_1 조회 + issuer.name 매칭"
    }
  ],
  "products": [
    {
      "ocr_name": "農心 辛ラーメン 袋(農心) 120g",
      "product_code": "P001",
      "confidence": "high",
      "basis": "unit_price 정규화 매칭"
    },
    {
      "ocr_name": "統計別商品(農心 食品(軽)",
      "product_code": null,
      "confidence": "low",
      "status": "NEEDS_CONFIRMATION",
      "candidates": []
    }
  ]
}
```

---

## cover_totals 구조 처리

`_extract_cover_totals`의 form_04 분기(다중 cover)를 제거하기 위해:

- Phase 2 출력 `pages[]`에 cover가 복수인 경우 `bundle_id`가 이미 포함됨
- Phase 3는 `pages[]`를 그대로 `cover_totals` 키로 전달
- Phase 4는 `pages[]` 기준으로 교차검증 (bundle_id로 매핑)

form_types.json의 `"summary"` 필드가 Phase 4의 교차검증 방식을 결정하므로, cover 구조 해석은 Phase 4 책임으로 이관.

---

## 구현 순서

1. **`docs/phase3-prompt.md` 작성** — 통합 매핑 프롬프트 (retailer + dist + product) ✅
2. **`backend/pipeline/phase3.py` 리팩터** ✅
   - Python 결정적 조회 함수 3개 제거
   - Claude 단일 호출로 교체
   - `_extract_cover_totals` form_id 분기 제거
3. **`.claude/agents/retailer-mapper.md`, `product-mapper.md` 정리** ✅
4. **잔여 이슈 수정** (아래 참조)
5. **검증** — 기존 샘플 문서로 매핑 결과 비교

---

## 잔여 이슈 (2026-05-19 점검 결과)

1차 리팩터 후 점검에서 발견된 추가 수정 사항.

### [Critical] 제품 매핑 대상 필터가 Python에 하드코딩됨

**위치**: `phase3.py:288` — `unique_products` 구성

```python
unique_products = list({
    i["product"] for i in items
    if i.get("product") and i.get("item_type") != "非課税"  # 非課税만 제외
})
```

`販促費8%`, `販促費10%`, `CF8%`, `CF10%` 타입 항목(예: `統計別商品(農心 食品(軽)`)도 제품 매핑 대상에서 제외해야 하는데, 이 규칙이 form_XX.md에 있지 않고 Python에도 구현되지 않았다. 결과적으로 매핑 불가 항목이 불필요한 NEEDS_CONFIRMATION pending으로 올라간다. (이것이 이번 리팩터의 원래 트리거가 된 버그.)

**해결책**: `uncached_products`를 `[{product, item_type}, ...]` 형태로 Claude에게 전달. Claude가 form_XX.md의 タイプ분류 규칙과 제품 매핑 필요 여부를 함께 판단.

변경 필요 파일:
- `phase3-prompt.md`: `uncached_products`에 `item_type` 포함, 타입별 매핑 필요 여부 판단 지시 추가
- `phase3.py`: `uncached_products` 구성 시 `item_type` 포함. `_call_mapper_claude` 시그니처 변경
- `form_01.md`: タイプ별 제품 매핑 필요 여부 명시 (예: `条件`만 필요, `販促費`·`CF`는 불필요)
- `form_04.md`: 동일

### [Medium] `_get_system_prompt` 코드블록 파싱이 취약

**위치**: `phase3.py:97-99`

```python
if "```\n당신은" in raw:
    raw = raw.split("```\n당신은", 1)[1].split("\n```", 1)[0]
    raw = "당신은" + raw
```

phase3-prompt.md의 특정 문자열 패턴에 의존. phase2.py는 파일 전체를 그대로 사용.

**해결책**: `phase3-prompt.md`를 코드블록 없이 프롬프트를 직접 기술하는 구조로 변경(문서 메타데이터는 주석 형태 또는 섹션 구분). `_get_system_prompt`는 파일 전체를 읽도록 단순화.

### [Low] form_04 복수 issuer 미처리

**위치**: `phase3.py:270-275`

form_04는 한 PDF에 cover가 복수(請求書No. 별). 현재 `break`로 첫 번째 cover의 issuer만 사용. 나머지 請求書의 판매처 tiebreak가 오결정될 수 있음.

**전제 조건**: form_04.md에 Phase 3 판매처 결정 규칙 명시 필요. 현재 form_04.md에는 해당 섹션 없음. 규칙 확인 후 대응.

---

## 변경하지 않는 것

- Phase 1, Phase 2 파이프라인
- Phase 4 NET 계산 로직
- form_XX.md 내용 (타입분류 규칙, 판매처 결정 규칙이 이미 올바르게 기술됨)
- mappings/*.csv 파일 구조
- DB 스키마, API 라우터, 프론트엔드
