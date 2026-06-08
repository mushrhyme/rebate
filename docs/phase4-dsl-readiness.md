# Phase 4 DSL 운영 준비도 체크리스트

**작성일**: 2026-06-05  
**대상**: 개발자 — form_02·03·05 수령 전 준비 상태 점검 및 신규 양식 추가 가이드

---

## 1. 현재 완료 상태

| 항목 | 상태 |
|------|------|
| `_safe_eval` DSL 평가기 | ✅ 구현 완료 (eval() 미사용, AST 기반) |
| `_eval_expr` expr 경로 | ✅ 구현 완료 (vars + computed_vars + divide_by) |
| `formula_type: "plugin"` 경로 | ✅ 구현 완료 (FORMULA_REGISTRY, 미등록 시 오류) |
| `computed_vars` + `divide_by` | ✅ 구현 완료 (zero_policy: skip_divide / return_none) |
| `needs_teiban` 지원 | ✅ 구현 완료 (teiban 변수 자동 주입) |
| `no_net_kubun` 지원 | ✅ 구현 완료 |
| form_01 DSL 전환 | ✅ `"shikiri - discount"` + computed_vars |
| form_04 DSL 전환 | ✅ `"shikiri - teiban - c1"` + needs_teiban |
| JSON Schema (`form_types.schema.json`) | ✅ 작성 완료 |
| Schema 단위 테스트 | ✅ 19개 (통과) |
| Mock form DSL 테스트 | ✅ 17개 (통과) |
| 회귀 테스트 | ✅ 25개 (통과) |
| DSL 오류 메시지 개선 | ✅ form_id, 변수명, AST 노드, 0나누기 포함 |
| sync-form-config 안전장치 | ✅ DSL 우선, Plugin 승인 필요, Schema 검증, 회귀 테스트 |
| 전체 테스트 | ✅ 645 passed |

**form_02·03·05**: 현업 업무규칙 대기 중. 이 문서의 절차를 따라 추가한다.

---

## 2. 신규 양식 추가 전 확인할 것

form_XX.md가 작성된 후, form_types.json에 반영하기 전 아래를 확인한다.

```bash
# 1. 현재 Schema가 유효한지 확인 (사전 점검)
python -c "
import json
from jsonschema import Draft7Validator
schema = json.load(open('config/form_types.schema.json', encoding='utf-8'))
data   = json.load(open('config/form_types.json', encoding='utf-8'))
errors = list(Draft7Validator(schema).iter_errors(data))
print('현재 config OK' if not errors else f'오류 {len(errors)}개')
"

# 2. 회귀 테스트 현황 (기준선)
python -m pytest tests/regression/ -q --tb=no
```

---

## 3. form_XX.md 작성 후 sync 시 확인할 것

### 3-1. NET 수식 → DSL expr 매핑 확인

`sync-form-config` 스킬이 생성한 `net.expr`이 올바른지 수동으로 검증한다.

```bash
# 임시 검증 스크립트
python -c "
from scripts.phase4_calc import _eval_expr, _safe_eval

# 생성된 net config를 복붙
net_cfg = {
    'formula_type': 'expr',
    'expr': 'shikiri - c1',   # ← 여기를 바꿔서 테스트
    'vars': {'c1': '条件'},
}

# 대표 케이스로 계산
cols = {'条件': 100}
result = _eval_expr(net_cfg, cols, shikiri=1000, teiban_joken=0, _form_id='form_XX')
print(f'결과: {result}')  # 기대값: 900.0
"
```

### 3-2. Schema 검증

```bash
python -c "
import json
from jsonschema import Draft7Validator

schema = json.load(open('config/form_types.schema.json', encoding='utf-8'))
data   = json.load(open('config/form_types.json', encoding='utf-8'))

errors = list(Draft7Validator(schema).iter_errors(data))
if errors:
    for e in errors:
        path = ' → '.join(str(p) for p in e.absolute_path)
        print(f'[{path}] {e.message}')
else:
    print('Schema OK')
"
```

### 3-3. 회귀 테스트

```bash
python -m pytest tests/regression/ tests/unit/ -q --tb=short
```

**모두 통과해야 form_types.json 저장 완료로 인정한다.**

---

## 4. 실패 시 보는 로그/테스트

### DSL 수식 오류

```
ValueError: 알 수 없는 변수 [form=form_XX label=net.expr]: 
  expr='shikiri - nonexistent', 변수='nonexistent', 사용 가능한 변수=['c1', 'shikiri', 'teiban']
```

**체크포인트**:
- `vars`에 alias가 정의됐는지
- `computed_vars`에 변수가 정의됐는지
- 오타 여부

### 허용되지 않은 AST 오류

```
ValueError: 허용되지 않은 AST 노드 [form=form_XX]: 
  expr='abs(-1)', 노드=Call (함수 호출 지원 안 함)
```

**DSL 허용 연산**: `+`, `-`, `*`, `/`, `()`, 숫자 리터럴, 변수명  
**DSL 금지**: 함수 호출, 비교 연산, 속성 접근, 문자열

### Plugin 미등록 오류

```
ValueError: Plugin 미등록 [form='form_XX']: plugin='my_plugin' — 
  FORMULA_REGISTRY에 등록하거나 formula_type=expr로 변경하세요.
```

**체크포인트**: Plugin을 쓸 이유가 있는지 재검토. 대부분은 DSL로 해결 가능.

### 회귀 테스트 실패

```
FAILED tests/regression/test_phase4_regression.py::test_net_values_unchanged[form_01]
AssertionError: 행 수 불일치 / NET 값 불일치
```

**체크포인트**:
- `net.expr`이 기존 계산 로직과 수학적으로 동일한지
- `vars`, `computed_vars` 컬럼명이 phase2 output 컬럼명과 일치하는지

---

## 5. JSON Schema 검증 방법

```bash
# jsonschema 설치 (없는 경우)
pip install jsonschema

# 단위 테스트로 전체 검증
python -m pytest tests/unit/test_form_types_schema.py -v

# 수동 검증
python -c "
import json
from jsonschema import Draft7Validator, validate
schema = json.load(open('config/form_types.schema.json', encoding='utf-8'))
data   = json.load(open('config/form_types.json', encoding='utf-8'))
errors = list(Draft7Validator(schema).iter_errors(data))
for e in errors:
    path = ' → '.join(str(p) for p in e.absolute_path)
    print(f'[{path}] {e.message}')
print(f'총 {len(errors)}개 오류' if errors else 'OK')
"
```

**Schema가 잡아내는 것**:
- `formula_type: "expr"` 인데 `expr` 필드 없음
- `formula_type: "plugin"` 인데 `plugin` 필드 없음
- 존재하지 않는 legacy `formula` 이름
- `divide_by.zero_policy`에 허용되지 않은 값
- `computed_var`에 정의되지 않은 추가 키

**Schema가 잡지 않는 것** (의도적):
- form config 최상위의 unknown 키 (미래 확장 허용)
- `expr` 수식의 수학적 정확성
- 컬럼명이 실제 phase2 output에 존재하는지

---

## 6. 회귀 테스트 실행 방법

```bash
# 전체 회귀 테스트
python -m pytest tests/regression/ -v

# 특정 form만
python -m pytest tests/regression/ -k "form_01" -v

# DSL Mock form 테스트 (케이스별 확인)
python -m pytest tests/unit/test_formula_dsl_mock_forms.py -v

# 전체 단위 + 회귀
python -m pytest tests/regression/ tests/unit/ -q
```

**회귀 테스트 기준**: `tests/fixtures/form_01_expected.json`, `form_04_expected.json`  
현재 코드로 생성된 "정답" 출력. NET 수식 변경 시 픽스처도 함께 업데이트해야 함.

---

## 7. Plugin이 필요한 패턴 vs 필요 없는 패턴

### Plugin이 필요한 패턴 (드물다)

- 세율(8%/10%)별로 완전히 다른 산식 적용
- 외부 마스터 테이블(unit_price.csv 외)을 조회해야 하는 계산
- 조건 분기가 5개 이상이고 DSL 표현이 매우 복잡해지는 경우

### Plugin 승인하면 안 되는 패턴 (DSL로 충분)

| 패턴 | DSL 해법 |
|------|---------|
| `仕切 - (C1 + C2)` | `expr: "shikiri - (c1 + c2)"` |
| CS행은 ÷入数 | `computed_vars.divide_by.when.equals: "CS"` |
| 정番 차감 | `needs_teiban: true`, `expr: "shikiri - teiban - c1"` |
| C2가 없는 경우 0 처리 | `vars: {c2: null}` (null이면 0) |
| 0 나누기 방어 | `divide_by.zero_policy: "skip_divide"` |

---

## 8. 다음주 현업 테스트 체크리스트

form_02·03·05 업무규칙이 수령된 후 아래 순서로 진행한다.

```
[ ] 1. form_XX.md 초안 작성 (update-form 스킬)
[ ] 2. NET 계산식 확인:
        - "DSL expr으로 표현 가능한가?" 판단
        - 표현 가능하면: expr, vars, computed_vars 작성
        - 불가능하면: 개발자 검토 요청 (Plugin 제안 금지)
[ ] 3. sync-form-config 스킬 실행
        - Schema 검증 통과 확인
        - 회귀 테스트 통과 확인
[ ] 4. 샘플 문서로 Phase 4 계산 실행
        python scripts/phase4_calc.py {doc_id} --form {form_id}
[ ] 5. NET 값 수동 검증 (Excel 또는 이미지와 대조)
[ ] 6. 회귀 테스트 픽스처 업데이트
        python scripts/phase4_calc.py {doc_id} --save
        → tests/fixtures/form_XX_expected.json 업데이트
[ ] 7. 최종 전체 테스트 통과 확인
        python -m pytest tests/ -q
```

### 현업 테스트 시 오류 발생 시

```bash
# DSL 오류 상세 확인
python -c "
from scripts.phase4_calc import _eval_expr
net_cfg = { ... }  # form_types.json에서 복붙
cols = { ... }      # 실제 문서 데이터
result = _eval_expr(net_cfg, cols, shikiri=..., teiban_joken=0, _form_id='form_XX')
print(result)
"
```

**일반적인 오류 원인**:

1. **컬럼명 불일치**: form_types.json의 `vars.c1: "条件"`이 phase2 output의 실제 컬럼명과 다름  
   → phase2_output.json에서 실제 컬럼명 확인

2. **shikiri 값 없음**: 시키리 단가가 phase2에서 추출되지 않음  
   → phase2 추출 규칙 확인

3. **teiban 미계산**: `needs_teiban: true`인데 teiban 계산 로직에서 해당 형식이 처리 안 됨  
   → `teiban_type` 값과 teiban_map 키 일치 여부 확인

---

## 9. 운영 중 주기 점검

```bash
# Schema 정합성 확인 (form_types.json 수정 후)
python -m pytest tests/unit/test_form_types_schema.py -q

# 전체 단위 + 회귀 (weekly 또는 form 추가 후)
python -m pytest tests/ -q --tb=no
```

---

## 10. Smoke Test 커맨드 모음 (복붙 가능)

### 의존성 설치 확인

```bash
# jsonschema 설치 (없으면)
uv sync   # pyproject.toml의 dev dependencies 일괄 설치
# 또는: pip install jsonschema

# 설치 확인
python -c "import jsonschema; print('jsonschema', jsonschema.__version__)"
```

### Schema 검증

```bash
# Schema 단위 테스트 (19개)
python -m pytest tests/unit/test_form_types_schema.py -v

# 수동 빠른 검증
python -c "
import json
from jsonschema import Draft7Validator
s = json.load(open('config/form_types.schema.json', encoding='utf-8'))
d = json.load(open('config/form_types.json', encoding='utf-8'))
errs = list(Draft7Validator(s).iter_errors(d))
print('OK' if not errs else f'오류 {len(errs)}개: {[e.message for e in errs]}')
"
```

### DSL 단위 테스트

```bash
# _safe_eval 단위 테스트
python -m pytest tests/unit/test_safe_eval.py -v

# DSL Mock form 테스트 (17개)
python -m pytest tests/unit/test_formula_dsl_mock_forms.py -v

# 모든 단위 테스트
python -m pytest tests/unit/ -v
```

### Phase4 회귀 테스트

```bash
# 전체 회귀 (form_01 + form_04)
python -m pytest tests/regression/ -v

# form_01만
python -m pytest tests/regression/ -k "form_01" -v

# form_04만
python -m pytest tests/regression/ -k "form_04" -v

# 빠른 실행 (verbose 없음)
python -m pytest tests/regression/ -q
```

### 전체 테스트

```bash
# 전체 실행
python -m pytest tests/ -q --tb=no

# 실패 시 상세 보기
python -m pytest tests/ --tb=short -x   # 첫 실패에서 중단
```

### 특정 form DSL 수식 확인 (ad-hoc)

```bash
# form_01 수식 확인
python -c "
from scripts.phase4_calc import _eval_expr
import json

cfg = json.load(open('config/form_types.json', encoding='utf-8'))
net = cfg['form_01']['net']

cols = {'条件': 500, '数量単位': 'CS', 'ケース入数': 10}
result = _eval_expr(net, cols, shikiri=10000, teiban_joken=0, _form_id='form_01')
print(f'form_01 NET: {result}')   # 기대: 10000 - 500/10 = 9950.0
"

# form_04 수식 확인
python -c "
from scripts.phase4_calc import _eval_expr
import json

cfg = json.load(open('config/form_types.json', encoding='utf-8'))
net = cfg['form_04']['net']

cols = {'未収条件': 200}
result = _eval_expr(net, cols, shikiri=5000, teiban_joken=100, _form_id='form_04')
print(f'form_04 NET: {result}')   # 기대: 5000 - 100 - 200 = 4700.0
"

# 임의 수식 테스트
python -c "
from scripts.phase4_calc import _safe_eval
result = _safe_eval('shikiri - (c1 + c2)', {'shikiri': 1000, 'c1': 100, 'c2': 50})
print(result)   # 850.0
"
```

### Phase4 계산 실행 (실제 문서)

```bash
# 특정 문서 계산 (stdout 출력)
python scripts/phase4_calc.py {doc_id}

# 저장 포함
python scripts/phase4_calc.py {doc_id} --save

# 사용 가능한 doc_id 확인
ls extracted/
```

---

## 11. 다음주 현업 테스트 Runbook (form_02·03·05)

현업으로부터 form_02·03·05 업무규칙을 수령한 후 아래 절차를 순서대로 따른다.

### 준비

```bash
# 현재 전체 테스트가 통과하는지 기준선 확인
python -m pytest tests/ -q --tb=no
# → 모두 통과해야 한다

# form_types.json 백업
cp config/form_types.json config/form_types.json.bak
```

### Step 1 — form_XX.md 생성/수정

`update-form` 스킬 또는 직접 작성으로 `form_definitions/form_XX.md` 초안을 작성한다.

필수 섹션:
- `[Phase 4] NET 계산식` — 수식 패턴, 조건 필드명, no_net_kubun
- `[Phase 2] 추출 컬럼` — 컬럼명 목록

### Step 2 — sync-form-config 실행

```
# Claude Code에서
form_XX 동기화해줘
```

스킬이 자동으로:
1. form_XX.md → form_types.json 반영 (DSL expr 생성)
2. Schema 검증 실행
3. 회귀 테스트 실행
4. 성공/실패 보고

실패 시: form_types.json은 자동 롤백됨. 아래 Step 3 수동 확인으로 이어진다.

### Step 3 — Schema 검증 (수동 확인)

```bash
python -c "
import json
from jsonschema import Draft7Validator
s = json.load(open('config/form_types.schema.json', encoding='utf-8'))
d = json.load(open('config/form_types.json', encoding='utf-8'))
errs = list(Draft7Validator(s).iter_errors(d))
if errs:
    for e in errs:
        path = ' → '.join(str(p) for p in e.absolute_path)
        print(f'  [{path}] {e.message}')
else:
    print('Schema OK')
"
```

실패 시 일반적인 원인:
- `net.expr` 필드 누락 (formula_type=expr 이어야 함)
- 알 수 없는 legacy formula 이름
- computed_var에 잘못된 필드

### Step 4 — Phase4 계산 실행

```bash
# 업무규칙에 맞는 샘플 문서가 있으면
python scripts/phase4_calc.py {sample_doc_id}
```

샘플 문서가 없으면 Mock 데이터로 수식만 검증:

```bash
python -c "
from scripts.phase4_calc import _eval_expr
import json

cfg = json.load(open('config/form_types.json', encoding='utf-8'))
net = cfg['form_XX']['net']   # form_XX로 변경

# 업무규칙에서 추출한 대표 케이스
cols = {'条件': 300}          # 실제 필드명으로 변경
result = _eval_expr(net, cols, shikiri=5000, teiban_joken=0, _form_id='form_XX')
print(f'NET: {result}')       # 기대값과 비교
"
```

### Step 5 — 수동 검산

계산 결과를 업무규칙 문서(Excel·이미지)와 대조한다.

```
검산 항목:
  [ ] 표준 케이스 (CS/個 구분 없음)
  [ ] CS 케이스 (ケース入数 나누기 있는 경우)
  [ ] no_net_kubun 해당 행 → NET = 0 또는 skip
  [ ] 조건 필드 값이 0인 경우
  [ ] teiban이 있는 경우 (needs_teiban: true)
```

오차 허용: `abs(계산값 - 기대값) < 0.01` (소수점 반올림 허용)

### Step 6 — 회귀 픽스처 생성

샘플 문서가 있다면:

```bash
# phase4_output.json 저장
python scripts/phase4_calc.py {doc_id} --save

# 픽스처로 복사
cp extracted/{doc_id}/phase4_output.json tests/fixtures/form_XX_expected.json
```

픽스처 형식 확인:

```bash
python -c "
import json
f = json.load(open('tests/fixtures/form_XX_expected.json', encoding='utf-8'))
print(f'행 수: {len(f[\"rows\"])}')
print(f'샘플: {f[\"rows\"][0]}')
"
```

### Step 7 — 회귀 테스트 추가

`tests/regression/test_phase4_regression.py`의 `CASES` 목록에 추가:

```python
CASES = [
    # 기존 케이스들...
    (
        "{sample_doc_id}",   # ← 실제 doc_id
        "form_XX",
        "form_XX_expected.json",
    ),
]
```

```bash
# 새 케이스 포함 회귀 테스트 실행
python -m pytest tests/regression/ -v
```

### Step 8 — 전체 테스트

```bash
python -m pytest tests/ -q --tb=no
# → 모두 통과해야 완료
```

### Step 9 — 이상 시 rollback

```bash
# form_types.json 롤백
cp config/form_types.json.bak config/form_types.json

# 확인
python -m pytest tests/ -q --tb=no
```

회귀 테스트 실패 시 일반적인 원인:
- `net.expr` 수식이 기존 named formula와 수학적으로 다름
- `vars` 컬럼명이 phase2 output 컬럼명과 불일치
- `computed_vars` 로직 오류 (divide_by when 조건)

---

## 12. 최종 상태 보고 템플릿

form_02·03·05 테스트 완료 후 아래 템플릿으로 보고한다.

```
=== Phase 4 DSL 신규 양식 추가 결과 ===

날짜:
담당자:
대상 form: form_XX (발행처명)

─── 수식 ──────────────────────────────────────
수식 유형: [ ] DSL expr  [ ] Plugin  [ ] Legacy
DSL expr: "shikiri - ..."
computed_vars 사용: [ ] 있음  [ ] 없음
  └ 변수명: (예: discount)
  └ divide_by: [ ] 있음  [ ] 없음
Plugin 필요: [ ] 없음  [ ] 있음 → 개발자 승인 필요
needs_teiban: [ ] true  [ ] false
no_net_kubun: (해당 값 목록, 없으면 빈칸)

─── 검증 ──────────────────────────────────────
Schema 검증: [ ] OK  [ ] 실패 → 사유:
Phase4 계산 실행: [ ] 성공  [ ] 실패 → 오류 메시지:
수동 검산:
  표준 케이스: 기대 ____  실제 ____  [ ] 일치
  CS 케이스:   기대 ____  실제 ____  [ ] 일치 / [ ] 해당 없음
  no_net 케이스: [ ] 0 반환 확인 / [ ] 해당 없음
  teiban 케이스: [ ] 정상 차감 확인 / [ ] 해당 없음

─── 테스트 ────────────────────────────────────
회귀 픽스처 생성: [ ] 완료  [ ] 샘플 문서 없음
회귀 테스트: [ ] 통과  [ ] 실패 → 내용:
전체 테스트: ____  passed, ____  skipped

─── 남은 이슈 ────────────────────────────────
(없으면 "없음")

─── 다음 단계 ────────────────────────────────
[ ] form_types.json 백업 삭제
[ ] sync-form-config 스킬 정상 동작 확인
[ ] PR / commit
```
