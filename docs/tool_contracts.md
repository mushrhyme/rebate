# Tool Contracts — backend/tools/mapping.py

이 문서는 `lookup_retailer`, `search_product`, `confirm_mapping` 세 Tool의
입력·출력·보장사항·예외 조건을 명문화한다.

Tool-use / Registry / MCP 확장 전에 이 Contract를 고정하여
호출자(phase3.py, orchestrator.py, 미래의 Claude tool_use)가
구현 세부사항에 의존하지 않도록 한다.

---

## 핵심 원칙 (2026-06-05 확정)

**Claude는 판단만 한다. 조회·저장은 Tool Layer가 담당한다.**

| 역할 | 담당 |
|------|------|
| 소매처 코드 후보 조회 | `lookup_retailer` (Tool Layer) |
| 제품 코드 후보 조회 | `search_product` (Tool Layer) |
| 판매처 코드 후보 결정 (1:N) | Claude (판단) |
| 매핑 확정 결과 저장 | `confirm_mapping` (Tool Layer) → CSV upsert |
| 후보 외 코드 저장 | **거부** — contract 위반으로 pending 처리 |

### 저장 원칙

- `confirm_mapping`을 경유하지 않은 CSV 직접 쓰기는 금지
- `allow_side_effects=False` 상태에서는 confirm_mapping이 실행되지 않음 (결정만 캡처)
- 실제 저장은 `_execute_success_path()`에서 phase3_output.json 저장 성공 후 1회만 수행
- 중복 방지: per-file asyncio.Lock + upsert (동일 키 재저장은 덮어씀)

### 후보 외 코드 거부 원칙

- `lookup_retailer`가 반환한 후보 외 retailer_code → pending 처리
- `search_product`가 반환한 후보 외 product_code → pending 처리
- 1:N dist 결정에서 후보 외 dist_code → `needs_confirmation`, 저장 거부
- Claude가 후보 없이 직접 코드를 제시하더라도 검증 통과하지 않으면 저장 안 됨

---

## 공통 Contract

| 항목 | 보장 |
|------|------|
| `confidence` 범위 | 항상 `[0.0, 1.0]` |
| `candidates` 정렬 | `similarity` 내림차순 |
| `candidates` 중복 | code(retailer_code / product_code) 기준 dedup — 동일 코드는 최고 점수 1건만 |
| `basis` 값 | 각 Result dataclass에 정의된 `Literal` 범위 안 |
| CSV 없음 | 예외 없이 `not_found` 또는 빈 후보 반환 |
| CSV 컬럼 누락 | 해당 행·소스를 건너뜀, 예외 없음 |

---

## lookup_retailer

### Input

| 파라미터 | 타입 | 필수 | 설명 |
|---------|------|------|------|
| `ocr_name` | `str` | ✓ | OCR에서 추출한 거래처명 원문 |
| `form_id` | `str` | ✓ | 양식 ID (예: `"form_01"`) |
| `mappings_dir` | `Path` | ✓ | `mappings/` 디렉토리 경로 |
| `form_definitions_dir` | `Path \| None` | — | `form_definitions/` 경로. `None`이면 `get_settings()`에서 로드. 테스트 시 명시적 전달 권장. |
| `top_k` | `int` | — | 유사도 후보 최대 수 (기본값 5) |

### Output — `LookupRetailerResult`

| 필드 | 타입 | 설명 |
|------|------|------|
| `retailer_code` | `str \| None` | 확정 소매처코드. basis="candidate"\|"not_found"일 때 `None` |
| `basis` | `Literal[...]` | 아래 참조 |
| `confidence` | `float` | `[0.0, 1.0]` |
| `candidates` | `list[RetailerCandidate]` | 유사도 후보 목록. basis="candidate"일 때만 비어있지 않음 |

**basis 값:**

| 값 | 의미 | `retailer_code` | `confidence` | `candidates` |
|----|------|-----------------|--------------|--------------|
| `"cache"` | `ocr_retailer.csv` 히트 | not None | 1.0 | `[]` |
| `"bracket_code"` | OCR 괄호 코드 → domae_retail CSV 직접 매칭 | not None | 1.0 | `[]` |
| `"candidate"` | 확정 불가, Claude 판단 필요 | `None` | `candidates[0].similarity` | 비어있지 않음 |
| `"not_found"` | 조회 불가 | `None` | 0.0 | `[]` |

**RetailerCandidate 구조:**

```python
{
    "retailer_code": str,
    "retailer_name": str,
    "source": str,        # CSV 파일명 (예: "retail_user.csv")
    "similarity": float,  # (0.3, 1.0], 소수점 3자리
}
```

### Guarantees

- `cache` / `bracket_code` 히트 시 `retailer_code`는 항상 비어있지 않은 문자열
- `candidate` 결과의 `candidates`는 `similarity` 내림차순 정렬, `retailer_code` 기준 dedup
- `not_found` 결과의 `confidence`는 정확히 `0.0`
- 처리 순서: ① 캐시 → ② 괄호코드(양식에 bracket_code_csv 설정 시) → ③ 유사도 검색
- `ocr_retailer.csv`가 없어도 예외 없이 다음 단계로 진행
- `form_definitions/{form_id}.md`가 없어도 `retail_user.csv` 기본값으로 후보 검색

### Errors

| 조건 | 예외 |
|------|------|
| CSV·MD 파일 없음 | 없음 (not_found 반환) |
| CSV 컬럼 누락 | 없음 (해당 행 스킵) |

---

## search_product

### Input

| 파라미터 | 타입 | 필수 | 설명 |
|---------|------|------|------|
| `ocr_name` | `str` | ✓ | OCR에서 추출한 제품명 원문 |
| `mappings_dir` | `Path` | ✓ | `mappings/` 디렉토리 경로 |
| `top_k` | `int` | — | 유사도 후보 최대 수 (기본값 5) |

### Output — `SearchProductResult`

| 필드 | 타입 | 설명 |
|------|------|------|
| `product_code` | `str \| None` | 확정 제품코드. basis="candidate"\|"not_found"일 때 `None` |
| `basis` | `Literal[...]` | 아래 참조 |
| `confidence` | `float` | `[0.0, 1.0]` |
| `candidates` | `list[ProductCandidate]` | 유사도 후보 목록 |

**basis 값:**

| 값 | 의미 | `product_code` | `confidence` | `candidates` |
|----|------|----------------|--------------|--------------|
| `"cache"` | `ocr_product.csv` 히트 | not None | 1.0 | `[]` |
| `"candidate"` | 확정 불가, Claude 판단 필요 | `None` | `candidates[0].similarity` | 비어있지 않음 |
| `"not_found"` | 조회 불가 | `None` | 0.0 | `[]` |

**ProductCandidate 구조:**

```python
{
    "product_code": str,
    "product_name": str,
    "similarity": float,  # (0.3, 1.0], 소수점 3자리
}
```

**비고:** `lookup_retailer`와 달리 `bracket_code` basis 없음 — 제품은 괄호 코드 패턴이 없음.

### Guarantees

- `cache` 히트 시 `product_code`는 항상 비어있지 않은 문자열
- `candidate` 결과의 `candidates`는 `similarity` 내림차순 정렬, `product_code` 기준 dedup
- 유사도 검색 소스는 `unit_price.csv`만 (양식 무관)
- `unit_price.csv`가 없어도 예외 없이 `not_found` 반환
- 처리 순서: ① `ocr_product.csv` 캐시 → ② `unit_price.csv` 유사도 검색

### Errors

| 조건 | 예외 |
|------|------|
| CSV 파일 없음 | 없음 (not_found 반환) |
| CSV 컬럼 누락 | 없음 (빈 후보 반환) |

---

## confirm_mapping

### Input

| 파라미터 | 타입 | 필수 | 설명 |
|---------|------|------|------|
| `mapping_type` | `Literal["retailer","product","dist"]` | ✓ | 저장 대상 종류 |
| `ocr_name` | `str` | ✓ | OCR 원문 명칭 (retailer·product의 CSV 키; dist는 참고용) |
| `confirmed_code` | `str` | ✓ | 확정된 코드 |
| `context` | `dict` | ✓ | 타입별 추가 정보 (아래 참조) |
| `mappings_dir` | `Path` | ✓ | `mappings/` 디렉토리 경로 |

**context 키 — mapping_type별:**

| mapping_type | 필수 키 | 선택 키 |
|-------------|---------|---------|
| `"retailer"` | — | `"retailer_name"` |
| `"product"` | — | `"product_name"` |
| `"dist"` | `"form_id"`, `"issuer_fingerprint"`, `"retailer_code"` | `"dist_name"` |

### Output

`None` (성공 시 항상)

### 저장 대상 CSV

| mapping_type | 파일 | 키 컬럼 |
|-------------|------|---------|
| `"retailer"` | `ocr_retailer.csv` | `ocr_name` |
| `"product"` | `ocr_product.csv` | `ocr_name` |
| `"dist"` | `ocr_dist.csv` | `(form_id, issuer_fingerprint, retailer_code)` 복합키 |

### Guarantees

- 반환값은 항상 `None`
- 동일 키로 재저장 시 행이 추가되지 않고 갱신 (upsert 보장)
- CSV 파일이 없으면 새로 생성
- 선택 context 키가 없으면 빈 문자열로 저장
- 기존 행의 다른 필드는 변경하지 않음 (키 기준으로 해당 행만 갱신)

### Errors

| 조건 | 예외 |
|------|------|
| 알 수 없는 `mapping_type` | `ValueError: 알 수 없는 mapping_type: '...'` |
| `dist` + 필수 context 키 누락 | `ValueError: confirm_mapping(dist)에 필요한 context 키가 없음: [...]` |

---

## Tool Registry

### 목적

`backend/tools/registry.py`는 세 Tool의 메타데이터를 중앙에서 관리한다.
호출자(phase3.py, orchestrator.py, 미래의 Claude tool_use 루프)가
구현 세부사항 없이 Tool을 발견(discovery)하고 스키마를 조회할 수 있도록 한다.

### 등록된 Tool

| name | callable | side_effects | idempotent | output_contract |
|------|----------|:---:|:---:|------|
| `lookup_retailer` | `mapping.lookup_retailer` | ✗ | ✓ | `LookupRetailerResult` |
| `search_product` | `mapping.search_product` | ✗ | ✓ | `SearchProductResult` |
| `confirm_mapping` | `mapping.confirm_mapping` | ✓ | ✓ | `None` |

### ToolSpec 필드 설명

| 필드 | 타입 | 설명 |
|------|------|------|
| `name` | `str` | Tool 식별자 (TOOL_REGISTRY 키와 항상 일치) |
| `description` | `str` | Tool 설명 (Claude / MCP에 노출될 텍스트) |
| `callable` | `Callable` | 실제 async 함수 참조 |
| `input_schema` | `dict` | JSON Schema 형식 입력 스키마 |
| `output_contract` | `str` | 반환 타입 이름 문자열 |
| `side_effects` | `bool` | `True` = 파일·DB 등 외부 상태를 변경함 |
| `idempotent` | `bool` | `True` = 같은 입력 반복 시 결과가 동일 |

### side_effects 의미

- `False`: 외부 상태를 변경하지 않음. 재시도·병렬 실행 안전.
- `True`: CSV 파일 쓰기 등 외부 상태를 변경함.
  `confirm_mapping`은 upsert 방식이므로 `idempotent=True`이지만 파일 I/O가 발생.

### idempotent 의미

- `True`: 같은 인자로 여러 번 호출해도 결과가 동일.
  - 조회 Tool(`lookup_retailer`, `search_product`): 항상 읽기 전용.
  - `confirm_mapping`: upsert이므로 동일 키 재저장 시 row가 증가하지 않음.

### 헬퍼 함수

| 함수 | 반환 | 설명 |
|------|------|------|
| `list_tools()` | `list[ToolSpec]` | 등록된 모든 ToolSpec 목록 |
| `get_tool(name)` | `ToolSpec` | 이름으로 조회. 없으면 `KeyError` |
| `get_tool_schema(name)` | `dict` | Tool의 input_schema 반환 |

### 주의: Claude tool_use / MCP는 아직 연결하지 않는다

이 Registry는 Tool metadata의 **중앙 관리** 목적만 가진다.
Claude의 `tools=[]` 파라미터 연결 및 MCP 서버 노출은 별도 단계에서 진행한다.
현재 단계에서 이 파일을 수정해 Tool을 추가하거나 schema를 변경할 수 있으며,
Workflow 코드(phase3.py, orchestrator.py)는 영향받지 않는다.

---

## Tool Metrics

### 목적

Claude tool_use 전환 전에 Tool 사용량과 결과 품질을 측정 가능하게 한다.
비즈니스 로직은 변경하지 않으며 observability만 추가한다.

### 측정 가능한 이벤트

| Tool | 이벤트 | 집계 필드 |
|------|--------|---------|
| `lookup_retailer` | 캐시(`ocr_retailer.csv`) 히트 | `cache_hits`, `success` |
| `lookup_retailer` | 괄호코드 직접 매칭 히트 | `cache_hits`, `success` |
| `lookup_retailer` | 유사도 후보 반환 | `success` |
| `lookup_retailer` | 조회 실패 | `not_found` |
| `search_product` | 캐시(`ocr_product.csv`) 히트 | `cache_hits`, `success` |
| `search_product` | 유사도 후보 반환 | `success` |
| `search_product` | 조회 실패 | `not_found` |
| `confirm_mapping` | CSV 저장 완료 | `success` |
| `confirm_mapping` | 예외 발생 | `failures` |

### ToolMetrics 필드

```python
@dataclass
class ToolMetrics:
    calls:      int  # 총 호출 횟수
    success:    int  # 결과를 찾거나 저장에 성공한 횟수
    failures:   int  # 예외가 발생한 횟수
    cache_hits: int  # success 중 캐시·괄호코드 직접 매칭 횟수 (cache_hits ⊆ success)
    not_found:  int  # 결과를 찾지 못한 횟수 (lookup/search 전용)
```

**불변 관계:**
- `lookup_retailer`, `search_product`: `calls = success + not_found + failures`
- `confirm_mapping`: `calls = success + failures`
- 모든 Tool: `cache_hits ≤ success`

### 공개 API

| 함수 | 설명 |
|------|------|
| `get_metrics()` | 전체 Tool 메트릭 스냅샷 dict 반환 |
| `get_metrics("tool_name")` | 특정 Tool 메트릭 스냅샷 반환 |
| `reset_metrics()` | 전체 초기화 |
| `reset_metrics("tool_name")` | 특정 Tool 초기화 |

**스냅샷 반환**: `get_metrics()`는 내부 상태의 복사본을 반환한다. 반환값 변경이 내부 상태에 영향을 주지 않는다.

### 저장소

인메모리(프로세스 재시작 시 초기화). DB 영속화 및 외부 모니터링 시스템 연결은 향후 별도 단계에서 진행한다.

---

## Claude Adapter

### 목적

`backend/tools/claude_adapter.py`는 Tool Registry와 Claude tool_use API 사이의 어댑터다.
**현재는 실험용이며 production phase3.py에는 연결하지 않는다.**

### 함수

#### `build_claude_tools() -> list[dict]`

TOOL_REGISTRY를 Anthropic `messages.create(tools=...)` 파라미터 형식으로 변환한다.

```python
# 미래 phase3.py 사용 예시
client.messages.create(
    model="claude-haiku-4-5-20251001",
    tools=build_claude_tools(),
    messages=[...],
)
```

출력 형식 (Anthropic 규격):
```json
[
  {
    "name": "lookup_retailer",
    "description": "OCR 거래처명으로 소매처코드 후보를 조회한다...",
    "input_schema": {
      "type": "object",
      "required": ["ocr_name", "form_id", "mappings_dir"],
      "properties": { ... }
    }
  }
]
```

보장:
- 정확히 3개의 Tool 포함 (TOOL_REGISTRY 크기와 동일)
- 각 dict에 `name`, `description`, `input_schema` 키만 존재 (Anthropic 규격 준수)
- `input_schema.type == "object"` (Anthropic API 요구사항)

#### `dispatch_tool_call(name: str, arguments: dict) -> Any`

Claude의 tool_use content block을 받아 실제 Tool을 실행한다.

```python
# Claude 응답 처리 예시 (미래 tool_use loop)
for block in message.content:
    if block.type == "tool_use":
        result = await dispatch_tool_call(block.name, block.input)
```

동작:
1. `get_tool(name)` 으로 ToolSpec 조회
2. `side_effects=True` Tool은 INFO 로그 기록
3. `spec.callable(**arguments)` 실행

| 조건 | 결과 |
|------|------|
| 등록된 tool | callable 실행 후 결과 반환 |
| 미등록 tool | `KeyError` (메시지에 등록된 tool 목록 포함) |

### side_effects=True Tool 주의사항

`confirm_mapping`은 `side_effects=True`이며 dispatch_tool_call로 실행 가능하다.
실행 시 `ocr_*.csv` 파일에 실제로 쓰기가 발생한다.

Claude tool_use loop에서 `confirm_mapping`을 사용할 때는:
- **사람이 확인한 매핑만 confirm_mapping을 호출할 것**
- Claude가 자율적으로 `confirm_mapping`을 호출하도록 허용하면 미확인 매핑이 캐시에 기록될 수 있음
- `idempotent=True`이므로 재실행은 안전하지만, 잘못된 매핑이 기록되면 캐시 오염 주의

### Path Coercion (str → Path 자동 변환)

Claude tool_use input은 JSON이므로 Path 객체를 직접 전달할 수 없다.
`dispatch_tool_call()`은 Tool callable의 파라미터 서명을 검사해 자동으로 변환한다.
**이 변환은 Claude Adapter 레이어의 책임이다.**

변환 규칙 (`coerce_tool_arguments()` 내부):

| 파라미터 타입 | 입력 | 변환 결과 |
|-------------|------|---------|
| `Path` | `str` | `Path(str)` |
| `Path` | `Path` | 그대로 |
| `Path \| None` | `str` | `Path(str)` |
| `Path \| None` | `None` | `None` 유지 |
| `Path \| None` | `Path` | 그대로 |
| `Path` | `int` / 기타 | `TypeError` |
| Tool에 없는 여분 인자 | any | 경고 후 제거 |

```python
# Claude JSON 응답 (string path) → 자동 변환 → Tool 실행
result = await dispatch_tool_call("lookup_retailer", {
    "ocr_name": "テスト店",
    "form_id": "form_01",
    "mappings_dir": "/path/to/mappings",       # str → Path 자동 변환
    "form_definitions_dir": "/path/to/forms",  # str → Path 자동 변환
})
```

### 현재 미구현 (향후 추가 예정)

| 항목 | 설명 |
|------|------|
| MCP 연결 | registry → MCP 서버 도구 목록 노출 |

---

## Claude tool_use 실험 (backend/experiments/)

### 목적

`backend/experiments/phase3_tool_use_experiment.py`는 Claude가 tool_use로
`lookup_retailer` / `confirm_mapping`을 실제로 호출하는 루프를 검증하는 실험 파일이다.
**production phase3.py와 완전히 분리되어 있으며, phase3.py를 수정하지 않는다.**

### 핵심 함수

#### `run_retailer_mapping_experiment(...) -> ExperimentResult`

1. `_build_experiment_tools()` — `build_claude_tools()` 기반으로 경로 필드 제거
2. Claude API 호출 (`tools=experiment_tools`)
3. `stop_reason="tool_use"` → 각 block을 `dispatch_tool_call()`로 실행
4. tool_result를 다음 메시지에 추가
5. `stop_reason="end_turn"` → `ExperimentResult` 반환

#### 컨텍스트 주입

경로(`mappings_dir`, `form_definitions_dir`)와 `form_id`는 Claude에게 노출하지 않고
각 Tool의 파라미터 서명을 검사해 맞는 필드만 dispatch 시점에 자동 주입한다.
Claude는 시맨틱 파라미터(`ocr_name`, `confirmed_code` 등)만 제공한다.

### 안전장치

| 장치 | 설명 |
|------|------|
| `max_turns` | 기본값 5. 초과 시 `RuntimeError` |
| `_ALLOWED_TOOLS` | `{"lookup_retailer", "confirm_mapping"}` — allowlist 외 차단 |
| `allow_side_effects=False` | 기본값. `confirm_mapping` 등 쓰기 tool 차단. `True`로 명시해야 실행 |
| error tool_result | tool 실행 실패 시 Claude에게 `is_error: True` 결과 전달 후 루프 계속 |

### production 연결 시 주의

- `confirm_mapping`은 `side_effects=True` — 잘못된 매핑이 캐시에 기록될 수 있음
- 반드시 사람이 확인한 결과만 `confirm_mapping`을 호출하도록 시스템 프롬프트 설계
- 현재 실험은 `allow_side_effects=True`를 명시적으로 설정한 경우만 실행 허용

---

## 변경 이력

| 날짜 | 내용 |
|------|------|
| 2026-06-05 | Contract 최초 명문화. TypedDict 추가, dist 필수키 ValueError, 캐시 컬럼 누락 안전 처리. |
| 2026-06-05 | Tool Registry 추가. ToolSpec, TOOL_REGISTRY, 헬퍼 함수 정의. |
| 2026-06-05 | Tool Metrics 추가. ToolMetrics, get_metrics, reset_metrics. 7개 이벤트 측정. |
| 2026-06-05 | Claude tool_use 실험 추가. backend/experiments/phase3_tool_use_experiment.py. production 미연결. |
| 2026-06-05 | Path Coercion 추가. coerce_tool_arguments(). str→Path 자동 변환. P0-1 완료. |
| 2026-06-05 | CSV 동시 쓰기 안전성 확보. _CSV_LOCKS(per-file asyncio.Lock) + _get_csv_lock() 추가. confirm_mapping 내부를 `async with lock + asyncio.to_thread()` 패턴으로 교체. 이벤트 루프 블로킹 동시 해소. |
| 2026-06-05 | 동시성 테스트 추가. TestCsvLockSafety — 25개 동시 쓰기 row 유실 없음, 동일 키 10회 upsert row 중복 없음, resolve() 경로 정규화 검증. |
| 2026-06-05 | 중복 제거. phase3.py의 _read_csv를 tools.mapping._read_csv로 통일, import csv 제거. normalize_ocr_name 주석을 re-export 의도로 명확화. |
| 2026-06-05 | Signature 캐시 추가. claude_adapter.py에 _SIG_CACHE + _get_signature() 도입. coerce_tool_arguments 호출마다 inspect.signature 재계산 제거. |
| 2026-06-05 | Dist 1:N Tool Use 추가. _run_single_dist_mapping / _build_dist_decisions_with_tool_use 구현. confirm_mapping(dist, basis="tool_use") 경로 연결. 후보 외 dist_code 거부 검증, pending 허용. ToolUseTokenStats에 dist 필드 추가. |
| 2026-06-05 | 핵심 원칙 명문화. Tool Layer 역할 분리(조회·저장), Claude 역할(판단만), 후보 외 코드 거부, allow_side_effects=False 캡처 방식, confirm_mapping 1회 저장 원칙 추가. Phase3 Tool Use 전환 완전 완료. |
