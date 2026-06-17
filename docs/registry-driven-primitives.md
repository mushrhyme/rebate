# 연산 레지스트리 — 집계/분해 전략·조회 차원을 설정으로 (DSL 어휘 확장)

**작성일**: 2026-06-17
**대상**: 개발자 — 설계 합의용. 현업이 "해석기에 없던 차원/집계"를 자연어→설정으로 제어하도록 결정적 어휘를 넓힌다.
**관련**: [nl-to-dsl-pipeline.md](nl-to-dsl-pipeline.md)(이 문서가 채우는 갭 = 그 문서 §4의 **G3 · §7의 T3**), [phase4-dsl-readiness.md](phase4-dsl-readiness.md)(DSL 백본), [architecture.md](architecture.md) §3·§7

---

## 1. 배경 — 이 문서가 푸는 문제

현업 요구: **"해석기에 없던 차원도 (개발자 없이) 제어하고 싶다."**

[nl-to-dsl-pipeline.md](nl-to-dsl-pipeline.md)는 *자연어를 산식으로 번역하고 검증·동결하는 층*(authoring/gate)을 이미 세웠다(P0~P4 완료). 그 문서는 마지막 경계를 이렇게 못박았다:

> **G3.** 기존 expr은 *행 단위 NET 수식*만 표현. `product_aggregate` 같은 *그룹 단위 집계 변형*은 새 인터프리터 코드가 필요했음.
> **운영 규칙(§7):** 새 *유형*(T3)은 개발이 화이트리스트에 연산을 추가하는 1회 작업. 그 이후 같은 유형의 *적용*은 자연어+게이트로 내려온다.

즉 방향은 합의됐다. **이 문서는 그 "화이트리스트(연산 레지스트리)"를 현업이 고른 두 축에 대해 구체적으로 설계한다.** 목표는 임의 코드 실행 허용이 ❌ 아니라, **두 축의 *변종*을 설정 한 줄로 흡수하도록 primitive를 일반화**하는 것이다.

### 현업이 고른 두 축 (2026-06-17)

| 축 | 대표 사례 | 현재 상태 |
|---|---|---|
| **A. 집계/분해 전략** | 이중조건 수량 분해(定番 − 추가조건) | `build_product_aggregate`에 분해법 1종 하드코딩 |
| **B. 조회/매핑 차원** | 판매처 매핑 키 `(소매처 × jisho)` | phase3 코드에 키 차원 박힘, 캐시 시트 스키마 고정 |

---

## 2. 설계 원칙 — 이미 검증된 패턴(레지스트리)을 두 축에 확장

핵심: **새 패턴을 발명하지 않는다.** `cross_validation`이 이미 레지스트리로 돈다.

[phase4_calc.py:462](../scripts/phase4_calc.py#L462) — `for rule in cross_validation: if rtype == "cover_honbai_vs_detail": ...` — **이름으로 등록된 검증 종류의 메뉴**. 양식은 config에서 `type`만 고른다. 새 양식이 기존 종류를 쓰면 코드 0. 새 *종류*만 코드 1회.

> **결정 D1.** 집계/분해와 조회/매핑도 동일 구조로 전환한다: **이름 붙은 전략을 레지스트리에 등록, config는 이름으로 선택.** `if form_id == ...` 분기는 영구 금지([phase4_calc.py:13](../scripts/phase4_calc.py#L13) 원칙 유지).

### 3티어 경계(불변)

| 티어 | 두 축에서의 의미 | 반영 경로 |
|---|---|---|
| **T1** 기존 전략의 새 인스턴스 | 같은 분해법 쓰는 새 양식 / N번째 매핑 차원 추가 | **설정만** + nl-to-dsl 게이트 |
| **T2** 기존 전략의 파라미터 변경 | 분해 기준조건·필드 변경 / 키 필드 교체 | 설정만 + 게이트 |
| **T3** 완전히 새로운 전략 | 새 분해 알고리즘 / 새 차원 *해석* 방식 | 레지스트리에 함수 1회 등록(개발) + 회귀 |

**현업이 혼자 못 하는 건 T3뿐이고, T3은 양식이 쌓일수록 빈도가 0에 수렴한다.** 이 문서의 일은 "지금 T3인 두 사례를 일반화해 이후 변종을 T1/T2로 떨어뜨리는 것".

---

## 3. 축 A — 집계/분해 전략 레지스트리

### 3.1 현 구조

[build_product_aggregate](../scripts/phase4_calc.py#L535)는 단일 분해법("subset_subtract": 定番 총수량 − 추가조건, 금액은 수량 비율 배분 + 추가조건 원본)을 코드에 고정. config는 필드명만 파라미터화([form_types.json](../config/form_types.json) `product_aggregate`).

### 3.2 목표 config

```json
"product_aggregate": {
  "strategy": "subset_subtract",      // ← 전략을 이름으로 선택 (필수)
  "base_condition": "定番条件",
  "qty_field": "数量",
  "amount_field": "金額",
  "group_by": ["jisho", "customer_ocr", "product_code"]   // 그룹핑 키도 설정화
}
```

### 3.3 레지스트리 인터페이스

```python
# scripts/aggregate_strategies.py (신규)
AGGREGATE_STRATEGIES = {}   # name -> fn

def register(name):
    def deco(fn): AGGREGATE_STRATEGIES[name] = fn; return fn
    return deco

# 계약: (groups, cfg) -> {"groups":[...], "warnings":[...], "condition_columns":[...]}
#  - groups: (group_key -> {_meta, conds{ctype:{qty,amount}}})  ← 공통 그룹핑이 미리 만들어 전달
#  - 반환 불변식: Σ분해수량 = 원본 그룹 총수량, Σ분해금액 = 원본 총금액 (±0.01)
@register("subset_subtract")
def _subset_subtract(groups, cfg): ...   # 현 build_product_aggregate 본문을 이관
```

`build_product_aggregate`는 **① 공통 그룹핑(현 코드 559~582행) → ② `strategy` 이름으로 레지스트리 디스패치 → ③ display_columns emit**의 얇은 오케스트레이터로 축소된다. 그룹핑·표시 스펙 생성은 전략과 무관하므로 공유.

### 3.4 무엇이 어디로 떨어지나

| 변화 | 티어 | 처리 |
|---|---|---|
| 다른 양식도 定番−추가조건 분해 | T1 | `"strategy":"subset_subtract"` + 필드명. **설정만** |
| 분해 기준조건을 定番→다른 조건 | T2 | `base_condition` 변경. 설정만 |
| 비율 배분이 아닌 **새 분해 알고리즘** | T3 | `@register("새이름")` 함수 1회 + 골든 테스트 |

---

## 4. 축 B — 조회/매핑 차원 선언

### 4.1 현 구조 (가장 손이 가는 축)

판매처 매핑 키 `(form_id, issuer_fingerprint, 소매처코드)` + jisho 분기가 phase3 로직과 캐시 스키마에 분산. 캐시는 **로컬 CSV가 아니라 Google Sheets**(SheetsStore, `GOOGLE_SHEETS_MAPPINGS_ID`)에 있다 — 그래서 jisho 추가가 "시트 컬럼 추가"였다. 차원을 가변화하려면 **코드 + 시트 스키마** 양쪽을 건드린다.

### 4.2 목표 config

```json
"dist_mapping": {
  "key_fields": ["retailer_code", "jisho"],   // ← 키 차원을 선언 (순서 = 우선순위)
  "cache_sheet": "ocr_dist"
}
```

phase3는 키를 코드 상수가 아니라 `key_fields`로 조립: `key = tuple(item[f] for f in key_fields)`. **세 번째 차원(채널·계절 등) 추가 = `key_fields`에 한 줄(T1).**

### 4.3 솔직한 걸림돌 — 캐시 스키마 (이 축의 실제 비용)

현 `ocr_dist` 시트는 고정 키 컬럼. 차원이 가변이면 두 가지 선택:

| 방식 | 내용 | 트레이드오프 |
|---|---|---|
| **B-1 합성키 컬럼** | `key`(필드를 구분자로 join) + `value` 2열 고정 | 시트 1회 마이그레이션. 차원 추가 시 스키마 불변(권장) |
| **B-2 가변 컬럼** | 차원마다 실제 컬럼 | 사람이 읽기 쉬움. 차원 추가마다 시트 스키마 변경(현 방식의 연장) |

> **결정 필요 R-B.** B-1 합성키 권장(차원 추가를 진짜 "설정 한 줄"로 만드는 유일한 길). 단 기존 `ocr_dist` 1회 변환 스크립트 + SheetsStore reader 수정 필요. **이 축은 P2(축 A 검증 후)로 미룬다.**

---

## 5. 안전망 — nl-to-dsl 게이트 재사용 (재발명 금지)

두 축 모두 [nl-to-dsl-pipeline.md](nl-to-dsl-pipeline.md) §6 게이트에 그대로 얹는다:

| 게이트 | 축 A 적용 | 축 B 적용 |
|---|---|---|
| 스키마 + 제어문자 거부 | `strategy` enum·필드 실재성 | `key_fields` 원소가 item 실필드인지 |
| dry-run | 샘플 doc phase4 완주 | 샘플 doc phase3 매핑 완주 |
| **불변식** | Σ분해 = 원본 총량/총액(±0.01) | 키 충돌·미해결률이 전환 전과 동일 |
| 골든 diff | form_04 기존 결과 무변동 확인 | 기존 판매처 매핑 무변동 확인 |

> **결정 D2.** 전략·차원 *전환*은 골든이 **무변동**임을 증명해야 통과(리팩터링이므로). 의도된 신규 양식만 diff 승인.

---

## 6. 로드맵

| Phase | 산출물 | 위험 | 상태 |
|---|---|---|---|
| **P0 (이 문서)** | 설계 합의 + architecture.md §7 의사결정 기록 | — | ⏳ |
| **P1. 축 A 레지스트리** | `aggregate_strategies.py`(register/get_strategy + `subset_subtract`) + `build_product_aggregate`를 그룹핑·표시스펙 오케스트레이터로 축소(분해는 전략 위임). form_04 config에 `"strategy":"subset_subtract"` 명시(무손실). schema에 `ProductAggregate` 정의. 골든 7 + 회귀 그린, 잘못된 전략명은 get_strategy가 명확히 차단 | 낮음(출력 계산만) | ✅ 2026-06-17 |
| **P2. 축 B 차원 선언** | `dist_mapping.key_fields` + 합성키 캐시(B-1) + ocr_dist 변환 스크립트 | 중(매핑·시트) | ⏭ |
| **P3. config 스키마·sync 연계** | `form_types.schema.json`에 `strategy` enum·`key_fields` 추가. sync-form-config가 신규 필드 보존·검증 | 낮음 | ⏭ |

**P1 먼저인 이유:** 자기완결적(행 데이터·시트 무관), 위험 낮음, 레지스트리 패턴을 싸게 검증. 통과하면 그 틀을 축 B에 그대로 적용.

---

## 7. 가드레일 (비협상 — nl-to-dsl §10 상속)

- DSL은 **임의 코드 실행을 절대 허용하지 않는다.** 전략·차원은 *등록된 화이트리스트*로만 확장.
- 회계 숫자는 영원히 결정적 코드. LLM은 *어느 전략·어느 키*인지 설정을 작성할 뿐, 실행하지 않는다.
- 모든 전환은 골든 무변동 증명 후 동결. source of truth = 컴파일된 설정.

---

## 8. 미해결 / 결정 필요

- **R-B**: 캐시 합성키(B-1) vs 가변 컬럼(B-2) — 권장 B-1, P2에서 확정.
- 전략/차원 그룹핑 키(`group_by`)를 어디까지 설정화할지 — 현 3-튜플 고정 해제 범위.
- T3 신규 전략 등록 승인 주체·절차(nl-to-dsl §11과 공통 과제).
- `aggregate_strategies.py`·`dist_mapping` 로직의 운영 승격 시 위치(`scripts/` → `backend/`, nl-to-dsl P3 NOTE와 동일).
