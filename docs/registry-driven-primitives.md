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
| 다른 양식도 定番−추가조건 분해 | T1 | `"relationship":"subset"`(기본) + 필드명. **설정만** |
| 분해 기준조건을 定番→다른 조건 | T2 | `base_condition` 변경. 설정만 |
| 조건이 **독립**(부분집합 아님) → 차감 없이 나열 | T2 | `"relationship":"independent"`. **설정만** |
| 묶음 기준 차원 변경(지점 빼고 거래처·제품만 등) | T2 | `"group_by":["customer","product"]`. **설정만** |
| 비율 배분이 아닌 **새 분해 알고리즘** | T3 | `@register("새이름")` 함수 1회 + 골든 테스트 |

> **파라미터화(2026-06-18):** `subset_subtract`에 박혀 있던 선택을 선언적 키로 노출했다 —
> `relationship`(subset|independent)은 등록된 전략에 매핑되고(`_RELATIONSHIP_STRATEGY`, 단일 출처),
> `group_by`는 묶음 차원을 바꾼다. 이미 아는 변형은 코드 0(설정만). **새 분해 알고리즘만** 여전히 T3(@register).
> 의도적으로 일반 reduce 엔진은 만들지 않았다 — 예시 1개로 추상을 추측하면 틀이 어긋나기 때문(두 번째 실제 케이스에서 설계).

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

### 4.3 P2 구현 시 드러난 현실 — 진짜 문제는 "드리프트"였다

설계 단계에선 캐시 합성키 마이그레이션(B-1)을 비용으로 봤다. 구현하며 코드를 보니 현실은 달랐다:

- `ocr_dist` 시트는 **이미 jisho 컬럼을 가진 이산 컬럼 스키마**다(`form_id, issuer_fingerprint, retailer_code, jisho, dist_code, dist_name`). 현업이 jisho를 추가한 그 작업으로 스키마는 이미 N-컬럼 형태였다. → **합성키 마이그레이션 불필요.**
- 진짜 문제는 **키가 세 곳(빌드·조회·쓰기)에 각자 하드코딩**돼 있어 드리프트가 났다는 것: 파일 I/O 조회 경로는 `(form_id, issuer_fp, retailer_code)` 3튜플(jisho 무시), 운영 preloaded 경로와 쓰기 경로는 4튜플. jisho 추가가 일부 경로에만 반영된 흔적.

> **결정 D3 (R-B 대체).** 합성키(B-1) 대신 **키 스키마 단일 출처 모듈**([backend/core/dist_cache_key.py](../backend/core/dist_cache_key.py))을 도입. `CONTEXT_FIELDS`(form_id·issuer_fp) + `DIMENSION_FIELDS`(retailer_code·jisho)로 키·헤더·인덱스를 한 곳에서 정의하고, 세 경로 모두 여기서 받아 쓴다. 차원 추가 = `DIMENSION_FIELDS` 한 줄 + 시트 컬럼 + (그 차원 순회 plumbing). 앞 둘은 자동 일관 적용, 셋째(plumbing)가 차원별 본질 비용(T3).

**솔직한 경계:** "현업이 설정 한 줄로 새 차원"까지는 못 간다 — `DIMENSION_FIELDS`는 코드 상수(한 줄)고, 새 차원 값을 items에 채워 순회하는 plumbing은 여전히 개발 작업이다. 다만 **키 스키마가 단 한 곳**이 되어 드리프트(이번 3/4튜플 버그)가 구조적으로 불가능해졌고, 차원 추가의 표면적이 최소화됐다. 파일 I/O 레거시 헬퍼(`resolve_dist_code_for_retailer`, 테스트 전용)는 자체 3필드 매치를 유지(운영 무관).

---

## 4-C. 축 C — 조건부 override (S6) — 진단으로 발견된 세 번째 축

`/diagnose` 전수조사 결과, 현업이 "신라면→000"처럼 상상하던 **조건부 override(S6: 술어 → 값)** 모양이 사실 **form_04에 이미 실재**하고 있었다 — 단지 form md 산문 + LLM 프롬프트에 하드코딩된 채로:

- "jisho=CVS営業部 → 특정 판매처 강제" (판매처 1:N override)
- "条件 타입만 제품매핑" (이건 이미 `item_type` 기반 결정적이라 제외)

### 4-C.1 진짜 문제 — 비결정적이었다

판매처 jisho override는 결정적 코드가 **없었다.** form_md를 dist 1:N Claude 프롬프트에 통째로 주입해 **LLM이 후보 중 고르게** 하고 있었다 — 즉 (a) 1:N 모호 케이스에서만 (b) LLM 판단으로 (c) 비결정적. 회계 매핑이 이런 식이면 재현성 원칙에 위배된다.

### 4-C.2 결정 D4 — 결정적 override primitive

> 양식이 선언한 규칙으로 1:N 후보를 **코드가 결정적으로** 고른다. LLM을 대체(재현성↑).

config([form_types.json](../config/form_types.json) form_04):
```json
"dist_overrides": [
  { "when": { "jisho": "CVS営業部" },
    "pick_candidate_name_contains": "広域リテール" }
]
```
- `when`: item 필드 → 값 **정확 일치**(AND). 임의 표현식 없음(결정적·감사가능).
- 액션: `pick_candidate_name_contains`(후보명 부분일치 유일후보) 또는 `dist_code`(코드 유일후보).
- 구현: [backend/pipeline/dist_overrides.py](../backend/pipeline/dist_overrides.py) `resolve_dist_override`, [phase3_dist_resolver.py](../backend/pipeline/phase3_dist_resolver.py) 1:N 분기 단일 훅.

### 4-C.3 안전 — 폴백 우선 + 기본 OFF

매칭 실패·**모호(0개·2개+ 일치)면 None** → 기존 LLM 경로로 폴백. override는 *확실할 때만* 개입하고 애매하면 추측 안 한다. `dist_overrides` 미선언 양식은 항상 무동작 → **form_01 등 무영향(기본 OFF).** 최악=기존 동작, 최선=결정적 확정.

> **확장점(향후):** 술어에 `product`를 넣으면 "신라면→000"이 그대로 동작한다. 지금은 form_04에 실재하는 jisho 기반 규칙만 wire(추측 금지). 같은 모양이라 product 추가는 술어 dict에 필드 하나.

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
| **P2. 축 B 차원 선언** | (B-1 합성키 대신) 키 스키마 단일 출처 `backend/core/dist_cache_key.py` — 빌드·조회·쓰기 세 경로를 통일, 3/4튜플 드리프트 제거. 차원 추가 = `DIMENSION_FIELDS` 한 줄 + 시트 컬럼(+plumbing). 계약 테스트 4 + 기존 dist 212 그린 | 중(매핑·시트) → 무손실 | ✅ 2026-06-17 |
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
