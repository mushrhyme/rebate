# 자동 수식 파이프라인 설계 계획

> **목적**: form_XX.md 변경만으로 phase4_calc.py 코드까지 자동 갱신. 개발자 개입 없음.  
> **상태**: GPT 검토 완료 — 구현 준비  
> **작성일**: 2026-06-05  
> **검토 이력**: 초안(2026-06-05) → GPT 기술 검토 → 수정 반영(2026-06-05)

---

## 1. 현재 문제

`phase4_calc.py`의 `calc_net()`은 닫힌 dispatch table이다.

```python
if formula == "subtract_conditions":     # 아는 것만 처리
    ...
elif formula == "subtract_teiban_and_self":
    ...
raise ValueError("Unknown formula")      # 모르면 파이프라인 중단
```

신규 양식이 이 4개 이름 밖의 수식을 가지면 **개발자가 코드를 직접 수정해야 한다**.  
sync-form-config 스킬이 아무리 잘 작동해도 이 벽을 넘을 수 없다.

---

## 2. 해결 구조 — 2-Layer

```
form_XX.md  →  sync-form-config 스킬  →  form_types.json  +  phase4_calc.py
                    ↑
          Claude가 의미 해석 + DSL 표현식 생성
          Python이 실행 + 검증 (회계 재현성 보장)
```

### Layer 1: 수식 DSL

**NET 계산 수식을 코드가 아닌 문자열로 표현**하고 Python이 안전하게 실행한다.

form_types.json 변경 전후:

```json
// 현재 (named formula — 코드 의존)
"net": {
  "formula": "subtract_conditions",
  "c1": "条件",
  "c2": null,
  "cs_divide_by_case_qty": true
}

// 변경 후 (DSL — 코드 독립)
"net": {
  "formula_type": "expr",
  "expr": "shikiri - discount",
  "vars": { "c1": "条件", "c2": null },
  "computed_vars": {
    "discount": {
      "expr": "c1 + c2",
      "divide_by": {
        "field":       "ケース入数",
        "when":        { "field": "数量単位", "equals": "CS" },
        "default":     1,
        "zero_policy": "skip_divide"
      }
    }
  },
  "no_net_kubun": ["円"]
}
```

**`computed_vars` 방식 채택 이유** (초안의 `post_divide`에서 변경):  
`post_divide`는 "최종 NET 전체"를 나누는 구조였지만, 실제 로직은 **차감액(c1+c2)만** case_qty로 나눠야 한다. `computed_vars`는 중간 변수를 DSL 표현식 안에서 먼저 계산하고 본 표현식에서 참조하는 구조로, 이후 다른 조건부 보정 패턴도 동일 방식으로 수용할 수 있다.

`phase4_calc.py` 안의 `_safe_eval()`이 Python AST로 이 표현식을 실행한다.  
**LLM이 계산하지 않는다. LLM이 수식 문자열을 작성하고 Python이 산수를 한다.**

### Layer 2: Formula Plugin (DSL 불가 케이스)

순수 산술로 표현 불가능한 경우 (조건 분기 등) 격리된 함수를 생성한다.  
**Plugin은 코드이므로 자동 적용하지 않는다. 테스트 통과 후 승인 게이트를 거친다.**

```python
# scripts/phase4_calc.py 내 FORMULA_REGISTRY
FORMULA_REGISTRY: dict[str, callable] = {}

# 인터페이스 고정 — Claude가 생성하는 함수는 항상 이 시그니처
def formula_xxx(cols: dict, shikiri: float, teiban_joken: float, cfg: dict) -> float | None:
    ...

FORMULA_REGISTRY["xxx"] = formula_xxx
```

`calc_net()`은 세 경로를 순서대로 시도한다:

```python
def calc_net(form_id, cols, shikiri, teiban_joken=0.0):
    net_cfg = FORM_TYPES[form_id]["net"]
    formula_type = net_cfg.get("formula_type", "named")

    if formula_type == "expr":
        return _eval_expr(net_cfg, cols, shikiri, teiban_joken)   # Layer 1

    if formula_type == "plugin":
        fn = FORMULA_REGISTRY.get(net_cfg["plugin"])
        if fn:
            return fn(cols, shikiri, teiban_joken, net_cfg)        # Layer 2
        raise ValueError(f"Unknown plugin: {net_cfg['plugin']}")

    return _legacy_calc(net_cfg, cols, shikiri, teiban_joken)      # 하위 호환
```

---

## 3. 핵심 컴포넌트 설계

### 3-A. `_safe_eval` + `_eval_expr` (phase4_calc.py)

Python `ast` 모듈로 파싱 후 허용된 노드 타입만 실행.  
`eval()` 직접 호출 없음. 함수 호출·속성 접근·임포트 불가.

```
허용: 숫자 리터럴, 변수명(ctx에 있는 것만), +, -, *, /, ()
금지: 함수 호출, 비교 연산, 불리언, 문자열, 그 외 모든 것
```

`_eval_expr()` 처리 순서:

```
1. vars 해석        → ctx["c1"] = to_f(cols.get("条件"), 0)
2. computed_vars 해석
     → divide_by 조건 평가 (when: cols 필드 비교)
     → divisor 결정 (zero_policy: "skip_divide" → divisor=1 처리)
     → ctx["discount"] = _safe_eval("c1 + c2", ctx) / divisor
3. 본 expr 실행 → _safe_eval("shikiri - discount", ctx)
```

**`no_net_kubun` 체크는 `_eval_expr()` 안에 두지 않는다.**  
이 체크는 `run()` 내부(494줄)에서 `calc_net()` 호출 전에 이미 처리된다.  
`_eval_expr()`에 넣으면 중복 처리가 된다.

`ctx` 고정 변수:
| 변수명 | 값 |
|-------|---|
| `shikiri` | 仕切価格 |
| `teiban` | 定番条件 사전 집계값 (teiban_jokenから) |

---

### 3-B. 테스트 인프라

**`run()` 함수에 이미 `base_dir` 파라미터가 있다 (328줄).**  
`--compare` CLI 옵션을 새로 만들 필요 없이, 회귀 테스트는 `run(doc_id, base_dir=...)`를 직접 호출하고 반환값 `(rows_out, xv)`를 픽스처와 비교하면 된다.

```
tests/
  fixtures/
    form_01_expected.json   ← 현재 phase4 output 고정 (rows + xv)
    form_04_expected.json   ← 현재 phase4 output 고정
  unit/
    test_safe_eval.py       ← DSL 표현식 단위 테스트 (경계값 포함)
  regression/
    test_phase4_regression.py  ← run() 직접 호출 후 픽스처와 비교
```

픽스처에 포함해야 할 케이스:
- 정상 행
- CS 단위 행 (case_qty 나눔)
- 金額 행 (no_net_kubun → None)
- 0값·누락값·"—" 값
- divisor = 0 (zero_policy 검증)

---

### 3-C. sync-form-config 스킬 추가 흐름

기존 Step 2-A (NET 수식 파싱) 이후에 아래 분기를 추가한다.

```
MD에서 수식 패턴 파악
    ↓
산술 표현식으로 표현 가능?
    ├─ YES → formula_type: "expr" + computed_vars DSL 생성
    │         → form_types.json 업데이트
    │         → 회귀 테스트 실행 (--compare)
    │         → 통과 → 자동 적용 ✅
    │         → 실패 → diff 보고
    │
    └─ NO  → formula_type: "plugin", 격리 함수 생성
              → 단위 테스트 생성 + 실행
              → 회귀 테스트 실행
              → 통과 → diff 제시 + 승인 요청 ⚠️ (자동 적용 안 함)
              → 실패 → 오류 + diff 보고
```

**자동 적용 범위 확정 (Q2 결론):**
- DSL (`formula_type: "expr"`) → 테스트 통과 시 자동 적용
- Plugin (`formula_type: "plugin"`) → 테스트 통과 시 diff 제시, **승인 후 적용**

근거: DSL은 데이터(JSON 수식 문자열), Plugin은 코드(Python 함수). 코드는 한 번은 사람이 확인한다.

---

## 4. 기존 수식의 DSL 전환 계획 (GPT 검증 반영)

| 현재 formula 이름 | DSL 가능 여부 | 변환 후 | 주의사항 |
|-----------------|-------------|--------|---------|
| `subtract_conditions` (CS 없음) | ✅ | `"shikiri - (c1 + c2)"` | — |
| `subtract_conditions` (CS 있음) | ✅ | `"shikiri - discount"` + `computed_vars` | `post_divide` 대신 `computed_vars` 방식 사용 |
| `subtract_teiban_and_self` | ✅ (조건부) | `"shikiri - teiban - c1"` | teiban_map 사전 집계 로직은 유지. `needs_teiban: true` 플래그 추가 필요 |
| `subtract_pack_conditions` | ✅ | `"shikiri - (c1 + c2)"` + `computed_vars` | divisor ≤ 0 시 `None` 반환 guard 필요 (`zero_policy: "return_none"`) |
| `subtract_conditions_or_fallback` | ❌ Plugin | `formula_type: "plugin"` | 조건 분기(`joken != 0` 여부) — 순수 산술 범위 밖 |

### `subtract_teiban_and_self` 전환 시 추가 처리

현재 `run()` 함수 내부에서 `formula == "subtract_teiban_and_self"` 조건이 **두 곳**에 있다.

```python
# 394줄 — teiban_map 생성 (formula 조건으로 실행 여부 결정)
if net_cfg.get("formula") == "subtract_teiban_and_self":
    ...

# 495줄 — per-item teiban_joken 계산 후 calc_net() 호출
if net_cfg.get("formula") == "subtract_teiban_and_self":
    tj = 0.0 if ctype == teiban_type else teiban_map.get(...)
    net = calc_net(form_id, cols, shikiri, teiban_joken=tj)
```

DSL 전환 후 `formula` 키가 사라지면 두 블록 모두 실행되지 않아 teiban_joken이 항상 0이 된다.  
**두 곳 모두** `needs_teiban` 플래그로 교체해야 한다.

```python
# 두 곳 모두 아래로 변경
if net_cfg.get("needs_teiban") or net_cfg.get("formula") == "subtract_teiban_and_self":
```

form_types.json:

```json
"net": {
  "formula_type": "expr",
  "expr": "shikiri - teiban - c1",
  "vars": { "c1": "未収条件" },
  "needs_teiban": true,
  "teiban_type": "定番条件"
}
```

---

## 5. 구현 단계 (GPT 피드백 반영 후 재구성)

### Phase 1 — 테스트 인프라 (선행 필수)

`run()` 함수를 직접 호출하는 방식이므로 CLI 수정 없이 바로 픽스처를 만들 수 있다.

```
1a. tests/fixtures/form_01_expected.json 생성 (현재 run() 출력 고정)
1b. tests/fixtures/form_04_expected.json 생성
1c. tests/regression/test_phase4_regression.py 작성
     → run(doc_id) 호출 → rows_out, xv를 픽스처와 비교
1d. tests/unit/test_safe_eval.py 스텁 생성 (Phase 2에서 채움)
```

완료 기준: `pytest tests/regression/` 통과 (기존 코드 변경 없음)

---

### Phase 2 — DSL 평가기 추가

기존 로직을 **건드리지 않고** 새 경로만 추가한다.

```
2a. _safe_eval() 함수 추가 (~40줄, ast 기반)
2b. _eval_expr() 함수 추가 (computed_vars 처리 포함)
2c. calc_net()에 formula_type == "expr" 분기 추가
2d. tests/unit/test_safe_eval.py 작성 (경계값 포함)
```

완료 기준: 단위 테스트 통과 + 회귀 테스트 통과 (기존 named 경로 무변화 확인)

---

### Phase 3 — form_01 DSL 전환

form_04보다 단순한 form_01을 먼저 전환해 검증한다.

```
3a. form_types.json form_01 → formula_type: "expr" + computed_vars
3b. 회귀 테스트로 숫자 동일 확인
```

완료 기준: 회귀 테스트 통과 (숫자 변화 없음)

---

### Phase 4 — form_04 DSL 전환

teiban_map 사전 집계 처리가 포함되어 별도 단계로 분리한다.

```
4a. form_types.json form_04 → formula_type: "expr" + needs_teiban: true
4b. run()의 teiban_map 계산 조건을 needs_teiban 플래그 기반으로 변경
4c. 회귀 테스트로 숫자 동일 확인
```

완료 기준: 회귀 테스트 통과

---

### Phase 5 — Plugin 레지스트리 + sync 스킬 통합

`subtract_conditions_or_fallback` 플러그인화 및 스킬 자동화 경로 추가.  
Phase 3·4 완료 후 form_02·03·05 실제 패턴을 보고 우선순위 재판단.

```
5a. FORMULA_REGISTRY 패턴 추가
5b. subtract_conditions_or_fallback → 격리 함수로 이전
5c. sync-form-config SKILL.md에 DSL 생성 + 테스트 + 승인 게이트 경로 추가
```

완료 기준: 신규 양식 추가 시 개발자 없이 파이프라인 완주

---

## 6. 확정된 설계 판단

| 항목 | 결정 | 근거 |
|------|------|------|
| Q1. 하위 호환 기간 | **B안** — form_02·03·05 전환 완료까지 named 경로 유지 | 미작성 양식의 수식 패턴 미확인. 롤백 경로 보존 |
| Q2. 자동 적용 범위 | **DSL만 자동, Plugin은 승인 후** | DSL은 데이터, Plugin은 코드. 코드는 한 번 검토 |
| Q3. CS 조건 표현 | **computed_vars 방식** | post_divide는 차감액이 아닌 전체 NET에 적용되는 설계 오류. computed_vars가 확장성 높음 |

---

## 7. 변경 파일 목록

| 파일 | 변경 내용 |
|------|----------|
| `scripts/phase4_calc.py` | `--compare` 옵션, `_safe_eval`, `_eval_expr`, DSL 경로, `FORMULA_REGISTRY`, `calc_net()` 분기 추가 |
| `config/form_types.json` | form_01 → `formula_type: "expr"` + `computed_vars` 전환 (Phase 3) |
| `config/form_types.json` | form_04 → `formula_type: "expr"` + `needs_teiban: true` 전환 (Phase 4) |
| `.claude/skills/sync-form-config/SKILL.md` | DSL 생성 + 자동 테스트 + 승인 게이트 경로 추가 (Phase 5) |
| `tests/fixtures/form_01_expected.json` | 신규 |
| `tests/fixtures/form_04_expected.json` | 신규 |
| `tests/unit/test_safe_eval.py` | 신규 |
| `tests/regression/test_phase4_regression.py` | 신규 |
| `docs/architecture.md` | 의사결정 기록 추가 |

**건드리지 않는 파일**: `phase1.py`, `phase2.py`, `phase3.py`, `orchestrator.py`, `form_XX.md`, `mappings/`

---

**Last Updated:** 2026-06-05 (점검 반영 — teiban 두 곳 수정, no_net_kubun 위치 정정, --compare 불필요 확인)
