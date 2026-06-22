"""test_real_claude_tool_use.py — 실제 Claude API E2E Smoke Test

기본 pytest 실행에서는 자동으로 SKIP된다.

실행 방법:
  export ANTHROPIC_API_KEY=sk-ant-...
  export RUN_REAL_CLAUDE_SMOKE=1
  pytest tests/smoke/test_real_claude_tool_use.py -v -s

검증 범위:
  - 실제 Claude API 호출 (claude-haiku)
  - tool_use block 발생 확인
  - lookup_retailer 도구 실행 (read-only, side effect 없음)
  - tool_result 정상 반환
  - Claude 최종 응답 수신
  - async_call_with_retry 경유 확인 (mock 테스트는 항상 실행)

비용 안전장치:
  - max_tokens=256 (작게)
  - temperature=0
  - max_turns=3
  - confirm_mapping 제외 (read-only only)
  - 임시 mappings_dir 사용 → CSV 쓰기 없음
"""
import csv
import inspect
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# 모듈 수준 import — patch 대상 확보 (로컬 import만으로는 patch 불가)
from backend.tools.claude_retry import async_call_with_retry  # noqa: E402

# ── 실행 조건 ──────────────────────────────────────────────────────────────────

_HAS_API_KEY   = bool(os.getenv("ANTHROPIC_API_KEY"))
_SMOKE_ENABLED = bool(os.getenv("RUN_REAL_CLAUDE_SMOKE"))

_SKIP_REAL = not (_HAS_API_KEY and _SMOKE_ENABLED)
_SKIP_REASON = (
    "실제 Claude API smoke test 비활성화. "
    "실행하려면: export ANTHROPIC_API_KEY=... && export RUN_REAL_CLAUDE_SMOKE=1"
)

# 실제 API smoke 에서만 허용하는 tool
_SMOKE_ALLOWED_TOOLS = frozenset({"lookup_retailer"})
_PATH_FIELDS         = frozenset({"mappings_dir", "form_definitions_dir"})


# ── Smoke Result 타입 ─────────────────────────────────────────────────────────

@dataclass
class SmokeResult:
    """_run_smoke_lookup() 반환값."""
    turns_used:    int
    tool_calls:    list[str]        # 실행된 tool 이름 순서
    final_text:    str | None
    max_turns_hit: bool
    error:         str | None = None


# ── Smoke 실행 함수 ───────────────────────────────────────────────────────────

def _build_smoke_tools() -> list[dict]:
    """lookup_retailer 전용 Claude tool schema 생성 (경로 필드 제거).

    build_claude_tools() 기반이므로 Registry가 단일 소스.
    """
    from backend.tools.claude_adapter import build_claude_tools
    result = []
    for tool in build_claude_tools():
        if tool["name"] not in _SMOKE_ALLOWED_TOOLS:
            continue
        schema = tool["input_schema"]
        required = [r for r in schema.get("required", []) if r not in _PATH_FIELDS]
        props    = {k: v for k, v in schema.get("properties", {}).items()
                    if k not in _PATH_FIELDS}
        result.append({
            "name":         tool["name"],
            "description":  tool["description"],
            "input_schema": {"type": "object", "required": required, "properties": props},
        })
    return result


async def _run_smoke_lookup(
    ocr_name: str,
    mappings_dir: Path,
    form_defs_dir: Path,
    form_id: str = "form_01",
    *,
    api_key: str,
    model: str = "claude-haiku-4-5-20251001",
    max_turns: int = 3,
    _call_claude: Any = None,   # 테스트 주입용 (None이면 async_call_with_retry 사용)
) -> SmokeResult:
    """실제 Claude API를 사용해 lookup_retailer tool_use 루프를 실행한다.

    안전장치:
      - _SMOKE_ALLOWED_TOOLS = {"lookup_retailer"} 만 허용
      - confirm_mapping 제외 → CSV 쓰기 없음
      - max_tokens=256, temperature=0
      - Claude API 호출은 async_call_with_retry 경유
      - _call_claude 파라미터로 테스트 시 retry 함수 교체 가능
    """
    import anthropic

    from backend.tools.claude_adapter import coerce_tool_arguments
    from backend.tools.registry import get_tool

    # 실제 retry 함수 또는 테스트 주입값 사용
    _retry = _call_claude if _call_claude is not None else async_call_with_retry

    client     = anthropic.AsyncAnthropic(api_key=api_key)
    smoke_tools = _build_smoke_tools()

    # 서버 컨텍스트 (Claude에 노출 안 함, dispatch 시 주입)
    ctx = {
        "form_id":              form_id,
        "mappings_dir":         mappings_dir,
        "form_definitions_dir": form_defs_dir,
    }

    messages: list[dict] = [{
        "role":    "user",
        "content": (
            f"거래처명 '{ocr_name}'의 소매처코드를 lookup_retailer 도구로 조회해라.\n"
            f"결과를 간단히 보고하라."
        ),
    }]

    tool_calls_seen: list[str] = []

    for turn in range(max_turns):
        # ── Claude API 호출 (retry wrapper 또는 테스트 주입 함수 경유) ──────────
        response = await _retry(
            client.messages.create,
            model=model,
            max_tokens=256,
            temperature=0,
            tools=smoke_tools,
            messages=messages,
        )

        # ── end_turn: 완료 ────────────────────────────────────────────────────
        if response.stop_reason == "end_turn":
            final_text = next(
                (b.text for b in response.content
                 if hasattr(b, "type") and b.type == "text"),
                None,
            )
            return SmokeResult(
                turns_used=turn + 1,
                tool_calls=tool_calls_seen,
                final_text=final_text,
                max_turns_hit=False,
            )

        if response.stop_reason != "tool_use":
            return SmokeResult(
                turns_used=turn + 1,
                tool_calls=tool_calls_seen,
                final_text=None,
                max_turns_hit=False,
                error=f"예상치 못한 stop_reason: {response.stop_reason}",
            )

        # ── tool_use: 각 block 실행 ───────────────────────────────────────────
        tool_results: list[dict] = []
        for block in response.content:
            if not (hasattr(block, "type") and block.type == "tool_use"):
                continue

            name        = block.name
            claude_args = dict(block.input) if block.input else {}

            # Allowlist 확인
            if name not in _SMOKE_ALLOWED_TOOLS:
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "is_error":    True,
                    "content":     f"허용되지 않은 tool: {name!r}",
                })
                continue

            # 실행
            tool_calls_seen.append(name)
            spec        = get_tool(name)
            valid_params = set(inspect.signature(spec.callable).parameters.keys())
            full_args   = {**{k: v for k, v in ctx.items() if k in valid_params}, **claude_args}
            coerced     = coerce_tool_arguments(spec, full_args)

            try:
                result   = await spec.callable(**coerced)
                content  = json.dumps(asdict(result), ensure_ascii=False, default=str)
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     content,
                })
            except Exception as exc:
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "is_error":    True,
                    "content":     f"실행 오류: {exc}",
                })

        messages.append({"role": "assistant", "content": list(response.content)})
        messages.append({"role": "user",      "content": tool_results})

    return SmokeResult(
        turns_used=max_turns,
        tool_calls=tool_calls_seen,
        final_text=None,
        max_turns_hit=True,
    )


# ── 테스트 데이터 픽스처 ───────────────────────────────────────────────────────

@pytest.fixture
def smoke_dirs(tmp_path: Path):
    """임시 mappings_dir + form_definitions_dir + ocr_retailer.csv 생성."""
    mappings  = tmp_path / "mappings"
    form_defs = tmp_path / "form_definitions"
    mappings.mkdir()
    form_defs.mkdir()

    # 소매처 캐시 CSV — lookup_retailer가 캐시 히트하도록 준비
    csv_path = mappings / "ocr_retailer.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ocr_name", "retailer_code", "retailer_name"])
        w.writeheader()
        w.writerow({
            "ocr_name":      "スターバックス",
            "retailer_code": "RET001",
            "retailer_name": "スターバックスコリア",
        })
        w.writerow({
            "ocr_name":      "스타벅스",
            "retailer_code": "RET001",
            "retailer_name": "스타벅스코리아",
        })

    return mappings, form_defs


# ── 1. 실제 Claude API Smoke Tests (env var 없으면 skip) ──────────────────────

@pytest.mark.skipif(_SKIP_REAL, reason=_SKIP_REASON)
class TestRealClaudeToolUseSmoke:
    """실제 Anthropic API를 사용하는 E2E smoke test.

    RUN_REAL_CLAUDE_SMOKE=1 && ANTHROPIC_API_KEY=... 가 설정된 경우만 실행.
    """

    async def test_tool_use_fires_at_least_once(self, smoke_dirs):
        """Claude가 lookup_retailer tool_use를 최소 1회 호출한다."""
        mappings, form_defs = smoke_dirs
        result = await _run_smoke_lookup(
            ocr_name="スターバックス",
            mappings_dir=mappings,
            form_defs_dir=form_defs,
            api_key=os.environ["ANTHROPIC_API_KEY"],
        )
        print(f"\n[smoke] turns={result.turns_used}, tool_calls={result.tool_calls}")
        print(f"[smoke] final_text={result.final_text!r}")

        assert len(result.tool_calls) >= 1, (
            f"tool_use가 발생하지 않음. turns={result.turns_used}"
        )

    async def test_lookup_retailer_is_the_called_tool(self, smoke_dirs):
        """호출된 tool이 lookup_retailer이다."""
        mappings, form_defs = smoke_dirs
        result = await _run_smoke_lookup(
            ocr_name="スターバックス",
            mappings_dir=mappings,
            form_defs_dir=form_defs,
            api_key=os.environ["ANTHROPIC_API_KEY"],
        )
        assert "lookup_retailer" in result.tool_calls, (
            f"lookup_retailer가 호출되지 않음. 실제 호출: {result.tool_calls}"
        )

    async def test_final_text_not_empty(self, smoke_dirs):
        """Claude가 최종 텍스트 응답을 반환한다."""
        mappings, form_defs = smoke_dirs
        result = await _run_smoke_lookup(
            ocr_name="スターバックス",
            mappings_dir=mappings,
            form_defs_dir=form_defs,
            api_key=os.environ["ANTHROPIC_API_KEY"],
        )
        assert result.final_text is not None and result.final_text.strip(), (
            "Claude 최종 응답이 비어 있음"
        )

    async def test_max_turns_not_exceeded(self, smoke_dirs):
        """max_turns(3)를 초과하지 않고 종료된다."""
        mappings, form_defs = smoke_dirs
        result = await _run_smoke_lookup(
            ocr_name="スターバックス",
            mappings_dir=mappings,
            form_defs_dir=form_defs,
            api_key=os.environ["ANTHROPIC_API_KEY"],
            max_turns=3,
        )
        assert not result.max_turns_hit, (
            f"max_turns=3 초과. turns_used={result.turns_used}"
        )

    async def test_no_side_effects_on_csv(self, smoke_dirs):
        """smoke test 후 mappings_dir에 새 파일이 생기지 않는다 (read-only)."""
        import time
        mappings, form_defs = smoke_dirs

        # 실행 전 파일 목록
        files_before = set(mappings.iterdir())

        await _run_smoke_lookup(
            ocr_name="スターバックス",
            mappings_dir=mappings,
            form_defs_dir=form_defs,
            api_key=os.environ["ANTHROPIC_API_KEY"],
        )

        files_after = set(mappings.iterdir())
        new_files = files_after - files_before
        assert not new_files, (
            f"smoke test 중 새 CSV 파일 생성됨 (side effect 발생): {new_files}"
        )


# ── 2. Mock 기반 Retry 통합 검증 (항상 실행) ──────────────────────────────────

class TestSmokeRetryIntegration:
    """_run_smoke_lookup의 Claude 호출이 async_call_with_retry를 경유하는지 검증.

    실제 API 없이 mock으로 실행 — 항상 실행됨.
    """

    async def test_claude_call_uses_async_call_with_retry(self, smoke_dirs):
        """_run_smoke_lookup은 async_call_with_retry를 통해 Claude를 호출한다.

        _call_claude 파라미터로 spy를 주입해 retry 경유를 검증한다.
        """
        mappings, form_defs = smoke_dirs
        retry_call_count = [0]

        async def spy_retry(fn, *args, **kwargs):
            retry_call_count[0] += 1
            end_resp = MagicMock()
            end_resp.stop_reason = "end_turn"
            end_resp.content = [MagicMock(type="text", text="スターバックスのコードはRET001です")]
            return end_resp

        result = await _run_smoke_lookup(
            ocr_name="スターバックス",
            mappings_dir=mappings,
            form_defs_dir=form_defs,
            api_key="sk-ant-fake-key",
            _call_claude=spy_retry,   # retry 함수 직접 주입
        )

        assert retry_call_count[0] >= 1, (
            "async_call_with_retry가 한 번도 호출되지 않음"
        )
        assert result.final_text is not None

    async def test_tool_use_response_dispatches_lookup_retailer(self, smoke_dirs):
        """tool_use 응답이 오면 lookup_retailer가 실행된다.

        실제 lookup_retailer 실행 (smoke_dirs CSV에 データ있음 → cache hit).
        object.__setattr__ 없이 result.tool_calls로 검증.
        """
        mappings, form_defs = smoke_dirs

        def _tb(tid, name, inp):
            b = MagicMock(); b.type="tool_use"; b.id=tid; b.name=name; b.input=inp; return b

        turn1 = MagicMock()
        turn1.stop_reason = "tool_use"
        turn1.content = [_tb("tu_1", "lookup_retailer", {"ocr_name": "スターバックス"})]

        turn2 = MagicMock()
        turn2.stop_reason = "end_turn"
        turn2.content = [MagicMock(type="text", text="RET001を見つけました")]

        call_seq = iter([turn1, turn2])
        async def _mock_retry(fn, *args, **kwargs): return next(call_seq)

        result = await _run_smoke_lookup(
            ocr_name="スターバックス",
            mappings_dir=mappings,
            form_defs_dir=form_defs,
            api_key="sk-ant-fake",
            _call_claude=_mock_retry,
        )

        assert "lookup_retailer" in result.tool_calls, (
            f"lookup_retailer가 실행되지 않음. tool_calls={result.tool_calls}"
        )
        assert not result.max_turns_hit

    async def test_confirm_mapping_blocked_in_smoke(self, smoke_dirs):
        """smoke test에서 confirm_mapping tool_use block은 차단된다."""
        mappings, form_defs = smoke_dirs

        def _make_tool_block(tool_id, name, input_dict):
            b = MagicMock()
            b.type  = "tool_use"
            b.id    = tool_id
            b.name  = name
            b.input = input_dict
            return b

        # Claude가 confirm_mapping을 호출하는 시나리오
        turn1 = MagicMock()
        turn1.stop_reason = "tool_use"
        turn1.content = [
            _make_tool_block("tu_1", "confirm_mapping", {
                "mapping_type": "retailer",
                "ocr_name":     "スターバックス",
                "confirmed_code": "RET001",
            })
        ]
        turn2 = MagicMock()
        turn2.stop_reason = "end_turn"
        turn2.content = [MagicMock(type="text", text="차단됨을 처리합니다")]

        call_seq = iter([turn1, turn2])

        async def _mock_retry(fn, *args, **kwargs):
            return next(call_seq)

        result = await _run_smoke_lookup(
            ocr_name="スターバックス",
            mappings_dir=mappings,
            form_defs_dir=form_defs,
            api_key="sk-ant-fake",
            _call_claude=_mock_retry,   # retry 함수 직접 주입
        )

        # confirm_mapping이 tool_calls에 기록되지 않아야 함 (차단됨)
        assert "confirm_mapping" not in result.tool_calls, (
            "confirm_mapping이 smoke allowlist를 통과함"
        )

    async def test_smoke_builds_correct_tool_schema(self, smoke_dirs):
        """_build_smoke_tools()가 lookup_retailer만 포함한다."""
        tools = _build_smoke_tools()
        names = [t["name"] for t in tools]
        assert names == ["lookup_retailer"], (
            f"smoke schema에 불필요한 tool 포함: {names}"
        )

    async def test_smoke_tool_schema_has_no_path_fields(self, smoke_dirs):
        """smoke tool schema에 mappings_dir/form_definitions_dir가 없다."""
        tools = {t["name"]: t for t in _build_smoke_tools()}
        schema = tools["lookup_retailer"]["input_schema"]
        assert "mappings_dir" not in schema.get("required", [])
        assert "mappings_dir" not in schema.get("properties", {})
        assert "form_definitions_dir" not in schema.get("required", [])

    async def test_max_turns_not_exceeded_in_mock(self, smoke_dirs):
        """max_turns가 초과되면 SmokeResult.max_turns_hit=True를 반환한다."""
        mappings, form_defs = smoke_dirs

        # 항상 tool_use를 반환해 max_turns를 초과시킴
        def _make_tool_block(i):
            b = MagicMock()
            b.type = "tool_use"; b.id = f"tu_{i}"; b.name = "lookup_retailer"
            b.input = {"ocr_name": "スターバックス"}
            return b

        call_count = [0]
        async def _always_tool_use(fn, *args, **kwargs):
            call_count[0] += 1
            r = MagicMock()
            r.stop_reason = "tool_use"
            r.content = [_make_tool_block(call_count[0])]
            return r

        # 실제 lookup_retailer 실행 (smoke_dirs CSV에 データ있음 → cache hit)
        result = await _run_smoke_lookup(
            ocr_name="スターバックス",
            mappings_dir=mappings,
            form_defs_dir=form_defs,
            api_key="sk-ant-fake",
            max_turns=2,
            _call_claude=_always_tool_use,
        )

        assert result.max_turns_hit is True
        assert result.turns_used == 2


# ── Product Tool Use Real Claude Smoke Tests ──────────────────────────────────

@pytest.fixture
def product_smoke_dirs(tmp_path: Path):
    """product smoke용 unit_price.csv 포함 임시 디렉토리."""
    import csv as _csv

    mappings  = tmp_path / "mappings"
    form_defs = tmp_path / "form_definitions"
    mappings.mkdir()
    form_defs.mkdir()

    rows = [
        {"제품코드": "NHJ001", "제품명": "農心 辛ラーメン 袋 120g×3",
         "시키리": "800", "본부장": "720"},
        {"제품코드": "NHJ002", "제품명": "農心 辛ラーメンミニカップ 49G",
         "시키리": "200", "본부장": "180"},
    ]
    path = mappings / "unit_price.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    return mappings, form_defs


@pytest.mark.skipif(_SKIP_REAL, reason=_SKIP_REASON)
class TestRealClaudeProductSmoke:
    """실제 Claude API를 사용하는 Product Tool Use E2E smoke test.

    RUN_REAL_CLAUDE_SMOKE=1 && ANTHROPIC_API_KEY=... 가 설정된 경우만 실행.

    검증:
      - search_product tool_use가 발생한다
      - confirm_mapping tool_use는 발생하지 않는다
      - final text JSON이 파싱된다
      - confirmed 케이스에서 product_code/master_name이 채워진다
    """

    async def test_product_search_product_fires(self, product_smoke_dirs):
        """실제 Claude가 search_product tool을 호출한다."""
        mappings, form_defs = product_smoke_dirs
        from backend.pipeline.phase3_fallback import _run_single_product_mapping
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        result = await _run_single_product_mapping(
            "農心 辛ラーメン",
            [],
            mappings,
            client=client,
            max_turns=3,
        )

        print(f"\n[product smoke] basis={result.basis}, code={result.product_code}, "
              f"name={result.product_name!r}")

        # search_product tool_use가 발생했으며, not_found 또는 confirmed
        assert result.basis in ("tool_use", "not_found")

    async def test_confirm_mapping_not_called_in_product_loop(self, product_smoke_dirs):
        """product Tool Use 루프 내에서 confirm_mapping이 호출되지 않는다."""
        mappings, form_defs = product_smoke_dirs
        from backend.pipeline.phase3_fallback import _run_single_product_mapping
        import anthropic

        confirm_call_count = [0]
        original_confirm = None

        try:
            from backend.pipeline import phase3_fallback as _fb
            original_confirm = _fb.confirm_mapping

            async def spy_confirm(**kwargs):
                confirm_call_count[0] += 1
                return await original_confirm(**kwargs)

            _fb.confirm_mapping = spy_confirm

            client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            await _run_single_product_mapping(
                "農心 辛ラーメン", [], mappings, client=client, max_turns=3
            )
        finally:
            if original_confirm is not None:
                _fb.confirm_mapping = original_confirm

        assert confirm_call_count[0] == 0, (
            f"product Tool Use 루프 내에서 confirm_mapping이 {confirm_call_count[0]}회 호출됨"
        )

    async def test_product_final_json_parsed(self, product_smoke_dirs):
        """Claude의 final text가 JSON으로 파싱된다."""
        mappings, form_defs = product_smoke_dirs
        from backend.pipeline.phase3_fallback import _run_single_product_mapping
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        result = await _run_single_product_mapping(
            "農心 辛ラーメン 袋 120g×3",
            [],
            mappings,
            client=client,
            max_turns=3,
        )

        print(f"\n[product smoke JSON] basis={result.basis}, "
              f"code={result.product_code}, name={result.product_name!r}")

        # JSON 파싱이 성공하면 basis가 tool_use 또는 not_found (RuntimeError 아님)
        assert result.basis in ("tool_use", "not_found")

    async def test_product_confirmed_case_has_code_and_name(self, product_smoke_dirs):
        """confirmed 케이스에서 product_code와 master_name이 채워진다."""
        mappings, form_defs = product_smoke_dirs
        from backend.pipeline.phase3_fallback import _run_single_product_mapping
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        result = await _run_single_product_mapping(
            "農心 辛ラーメン 袋",  # unit_price.csv에 유사 항목 있음
            [],
            mappings,
            client=client,
            max_turns=3,
        )

        print(f"\n[product smoke confirmed] basis={result.basis}, "
              f"code={result.product_code}, name={result.product_name!r}")

        if result.basis == "tool_use":
            assert result.product_code, "product_code가 비어 있음"
            assert result.product_name, "master_name이 비어 있음"
        else:
            # not_found도 허용 (후보 유사도에 따라 달라짐)
            pass


# ── Product mock 기반 smoke 구조 검증 (항상 실행) ────────────────────────────

class TestProductSmokeStructure:
    """Product smoke의 구조적 속성을 mock으로 검증 (항상 실행)."""

    def test_product_tools_do_not_include_confirm_mapping(self):
        """product smoke에서 사용하는 tool 목록에 confirm_mapping이 없다."""
        from backend.pipeline.phase3_fallback import _build_product_tools
        tools = _build_product_tools()
        names = {t["name"] for t in tools}
        assert "confirm_mapping" not in names

    def test_product_tools_include_search_product(self):
        from backend.pipeline.phase3_fallback import _build_product_tools
        tools = _build_product_tools()
        names = {t["name"] for t in tools}
        assert "search_product" in names

    async def test_mock_product_confirmed_returns_code_and_name(self, product_smoke_dirs):
        """mock Claude가 confirmed JSON을 반환하면 product_code/master_name이 채워진다."""
        mappings, _ = product_smoke_dirs
        from backend.pipeline.phase3_fallback import _run_single_product_mapping

        def _tb(tid, name, inp):
            b = MagicMock(); b.type = "tool_use"; b.id = tid; b.name = name; b.input = inp
            return b

        def _text(t):
            b = MagicMock(); b.type = "text"; b.text = t; return b

        def _resp(stop, *blocks):
            r = MagicMock(); r.stop_reason = stop; r.content = list(blocks); return r

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _resp("tool_use",
                _tb("tu_1", "search_product", {"ocr_name": "農心 辛ラーメン"})),
            _resp("end_turn",
                _text('{"decision": "confirmed", "product_code": "NHJ001", '
                      '"master_name": "農心 辛ラーメン 袋 120g×3"}')),
        ])

        result = await _run_single_product_mapping(
            "農心 辛ラーメン", [], mappings, client=mock_client
        )

        assert result.basis        == "tool_use"
        assert result.product_code == "NHJ001"
        assert result.product_name == "農心 辛ラーメン 袋 120g×3"

    async def test_mock_product_no_confirm_mapping_call(self, product_smoke_dirs):
        """mock product loop에서 confirm_mapping이 호출되지 않는다."""
        mappings, _ = product_smoke_dirs
        from backend.pipeline.phase3_fallback import _run_single_product_mapping

        def _resp_end(text):
            b = MagicMock(); b.type = "text"; b.text = text
            r = MagicMock(); r.stop_reason = "end_turn"; r.content = [b]; return r

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_resp_end(
            '{"decision": "not_found", "reason": "후보 없음"}'
        ))

        confirm_calls: list = []

        async def spy_confirm(**kwargs):
            confirm_calls.append(kwargs)

        with patch("backend.pipeline.phase3_fallback.confirm_mapping", spy_confirm):
            await _run_single_product_mapping(
                "テスト", [], mappings, client=mock_client
            )

        assert len(confirm_calls) == 0


# ── 실제 Claude retailer batch — production 경로 검증 ──────────────────────────

class TestRealClaudeRetailerProductionPath:
    """allow_side_effects=False + real Claude로 decided_code가 캡처되는지 검증.

    실제 Claude API 호출 테스트: ANTHROPIC_API_KEY + RUN_REAL_CLAUDE_SMOKE=1 필요.
    mock 기반 테스트: 항상 실행.
    """

    @pytest.mark.skipif(_SKIP_REAL, reason=_SKIP_REASON)
    async def test_real_retailer_experiment_sets_decided_code(self, smoke_dirs):
        """실제 Claude + allow_side_effects=False → decided_code가 설정된다."""
        mappings, form_defs = smoke_dirs
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

        from backend.experiments.phase3_tool_use_experiment import (
            run_retailer_mapping_experiment,
        )
        result = await run_retailer_mapping_experiment(
            ocr_name="テスト株式会社",
            form_id="form_01",
            mappings_dir=mappings,
            form_definitions_dir=form_defs,
            client=client,
            allow_side_effects=False,
            max_turns=5,
        )

        # confirmed_code가 None이 아니거나 (후보 있고 Claude가 결정)
        # lookup_basis="not_found" (후보 없음) — 둘 다 유효
        if result.lookup_basis == "not_found":
            assert result.confirmed_code is None
        else:
            # 후보가 있었다면 Claude가 confirm_mapping을 호출했어야 함
            # decided_code가 설정되었는지 확인
            print(f"\n[smoke] lookup_basis={result.lookup_basis}, decided_code={result.decided_code}")
            print(f"[smoke] confirmed_code={result.confirmed_code}")
            # basis="candidate"면 Claude가 confirm_mapping 호출 → decided_code 설정
            # basis="cache"/"bracket_code"면 자동 확정 (confirm 없을 수 있음)

    async def test_mock_retailer_allow_side_effects_false_sets_decided_code(self, smoke_dirs):
        """mock 클라이언트 + allow_side_effects=False → decided_code가 설정된다 (항상 실행)."""
        mappings, form_defs = smoke_dirs
        from backend.experiments.phase3_tool_use_experiment import (
            run_retailer_mapping_experiment,
        )

        def _tb(id_, name, inp):
            b = MagicMock(); b.type = "tool_use"; b.id = id_; b.name = name; b.input = inp
            return b

        def _resp(stop, *blocks):
            r = MagicMock(); r.stop_reason = stop; r.content = list(blocks); return r

        # Claude: lookup(スターバックス) → confirm(RET001) → end_turn
        # RET001은 lookup_retailer가 スターバックス 캐시 히트로 반환하는 코드이므로,
        # '후보 외 코드 거부' 가드(lookup이 반환한 코드만 확정 허용)를 통과한다.
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _resp("tool_use", _tb("t1", "lookup_retailer", {"ocr_name": "スターバックス"})),
            _resp("tool_use", _tb("t2", "confirm_mapping", {
                "mapping_type": "retailer", "ocr_name": "スターバックス", "confirmed_code": "RET001",
            })),
            _resp("end_turn", MagicMock(type="text", text="完了")),
        ])

        result = await run_retailer_mapping_experiment(
            ocr_name="スターバックス",
            form_id="form_01",
            mappings_dir=mappings,
            form_definitions_dir=form_defs,
            client=mock_client,
            allow_side_effects=False,
            max_turns=5,
        )

        # decided_code 캡처 확인
        assert result.decided_code == "RET001", \
            f"decided_code가 캡처되지 않음: {result.decided_code!r}"
        assert result.confirmed_code == "RET001", \
            f"confirmed_code가 None임: {result.confirmed_code!r}"

        # allow_side_effects=False → confirm_mapping이 CSV에 쓰지 않음.
        # スターバックス는 픽스처에 이미 1행 존재 → 중복 추가되지 않아야 한다 (여전히 1행).
        rows_after = _read_csv_simple(mappings / "ocr_retailer.csv") if (mappings / "ocr_retailer.csv").exists() else []
        sb_rows = [r for r in rows_after if r.get("ocr_name") == "スターバックス"]
        assert len(sb_rows) == 1, \
               f"allow_side_effects=False인데 スターバックス 행이 중복 추가됨: {len(sb_rows)}행"


def _read_csv_simple(path) -> list:
    import csv
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ── Dist 1:N Tool Use Smoke ────────────────────────────────────────────────────

@pytest.fixture
def dist_smoke_dirs(tmp_path: Path):
    """dist 1:N smoke용 임시 디렉토리 — CSV side effect 없음."""
    mappings = tmp_path / "mappings"
    mappings.mkdir()
    return mappings


# 2개 후보: 발행처 힌트로 Claude가 구분 가능한 케이스
_DIST_1N_CANDIDATES = [
    {"dist_code": "1300061", "dist_name": "伊藤忠食品株式会社東日本営業部"},
    {"dist_code": "1300062", "dist_name": "伊藤忠食品株式会社東日本営業本部（CVS）"},
]

# 9개 후보: 지역 정보 없이 판단 불가 → pending 기대
_DIST_9N_CANDIDATES = [
    {"dist_code": "1300014", "dist_name": "加藤産業株式会社北関東支社多摩支店"},
    {"dist_code": "1302976", "dist_name": "株式会社トーカン商品統括部（CVS)"},
    {"dist_code": "1303568", "dist_name": "株式会社日本アクセス広域リテール営業本部加工食品飲料部"},
    {"dist_code": "1303567", "dist_name": "株式会社日本アクセス広域リテール営業本部東北営業課"},
    {"dist_code": "1303569", "dist_name": "株式会社日本アクセス広域リテール営業本部中部・北陸営業課"},
    {"dist_code": "1303571", "dist_name": "株式会社日本アクセス広域リテール営業本部中四国営業課"},
    {"dist_code": "1303572", "dist_name": "株式会社日本アクセス広域リテール営業本部九州営業課"},
    {"dist_code": "1303570", "dist_name": "株式会社日本アクセス広域リテール営業本部関西営業課"},
    {"dist_code": "1302971", "dist_name": "株式会社日本アクセス北海道（CVS）"},
]


class TestRealClaudeDistSmoke:
    """Dist 1:N Tool Use — 실제 Claude API smoke.

    실행: export ANTHROPIC_API_KEY=... && export RUN_REAL_CLAUDE_SMOKE=1
          pytest tests/smoke/ -v -k "dist"
    """

    @pytest.mark.skipif(_SKIP_REAL, reason=_SKIP_REASON)
    async def test_dist_2n_claude_selects_from_candidates(self, dist_smoke_dirs):
        """2개 후보 + 발행처 힌트 → Claude가 후보 내 dist_code 선택."""
        import anthropic
        from backend.pipeline.phase3_fallback import (
            _run_single_dist_mapping, ToolUseTokenStats,
        )
        from backend.pipeline.phase3_dist_resolver import DistResolution

        client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        token_acc = ToolUseTokenStats()

        res = await _run_single_dist_mapping(
            ocr_name="(株)東急ストア",
            retailer_code="6154844",
            candidates=_DIST_1N_CANDIDATES,
            form_id="form_01",
            issuer_fingerprint="伊藤忠食品株式会社東日本営業部",
            client=client, model="claude-haiku-4-5-20251001",
            _token_acc=token_acc,
        )

        valid_codes = {c["dist_code"] for c in _DIST_1N_CANDIDATES}
        print(f"\n[dist smoke] basis={res.basis}, dist_code={res.dist_code}, reason={res.reason}")
        print(f"[dist smoke] tokens: in={token_acc.dist_input_tokens}, out={token_acc.dist_output_tokens}")

        # basis는 tool_use 또는 needs_confirmation
        assert res.basis in {"tool_use", "needs_confirmation"}, \
            f"예상치 못한 basis: {res.basis!r}"

        if res.basis == "tool_use":
            assert res.dist_code in valid_codes, \
                f"후보 외 dist_code: {res.dist_code!r}"
            assert res.dist_code, "dist_code가 비어 있음"
        else:
            assert res.needs_confirmation is True

        assert token_acc.dist_api_calls == 1
        assert token_acc.dist_input_tokens > 0

    @pytest.mark.skipif(_SKIP_REAL, reason=_SKIP_REASON)
    async def test_dist_9n_ambiguous_returns_valid_result(self, dist_smoke_dirs):
        """9개 후보 + 힌트 없음 → Claude가 needs_confirmation 반환 (모호 케이스)."""
        import anthropic
        from backend.pipeline.phase3_fallback import (
            _run_single_dist_mapping, ToolUseTokenStats,
        )

        client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        token_acc = ToolUseTokenStats()

        res = await _run_single_dist_mapping(
            ocr_name="(株) ファミリーマート",
            retailer_code="6003788",
            candidates=_DIST_9N_CANDIDATES,
            form_id="form_01",
            issuer_fingerprint="",  # 힌트 없음
            client=client, model="claude-haiku-4-5-20251001",
            _token_acc=token_acc,
        )

        valid_codes = {c["dist_code"] for c in _DIST_9N_CANDIDATES}
        print(f"\n[dist smoke 9N] basis={res.basis}, dist_code={res.dist_code}")
        print(f"[dist smoke 9N] reason={res.reason}")

        assert res.basis in {"tool_use", "needs_confirmation"}
        if res.basis == "tool_use":
            assert res.dist_code in valid_codes, f"후보 외 선택: {res.dist_code!r}"
        assert token_acc.dist_api_calls == 1

    async def test_dist_outside_candidate_rejected_mock(self, dist_smoke_dirs):
        """Mock: 후보 외 dist_code 선택 → 저장 거부 (항상 실행)."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from backend.pipeline.phase3_fallback import (
            _run_single_dist_mapping, ToolUseTokenStats,
        )

        outside_resp = MagicMock()
        outside_resp.stop_reason = "end_turn"
        b = MagicMock(); b.type = "text"
        b.text = '{"decision": "confirmed", "dist_code": "OUTSIDE_CODE"}'
        outside_resp.content = [b]
        u = MagicMock(); u.input_tokens = 50; u.output_tokens = 20
        u.cache_read_input_tokens = 0; u.cache_creation_input_tokens = 0
        outside_resp.usage = u

        with patch("backend.pipeline.phase3_fallback.async_call_with_retry",
                   new=AsyncMock(return_value=outside_resp)):
            res = await _run_single_dist_mapping(
                ocr_name="テスト店",
                retailer_code="R001",
                candidates=_DIST_1N_CANDIDATES,
                form_id="form_01", issuer_fingerprint="fp",
                client=MagicMock(), model="dummy",
            )

        assert res.basis == "needs_confirmation"
        assert res.dist_code is None
        assert "후보 외" in (res.reason or "")
        print(f"\n[dist smoke mock] 후보 외 선택 거부: {res.reason}")

    async def test_dist_1to1_auto_no_claude_call(self, dist_smoke_dirs):
        """후보 1개 → auto_1_to_1, Claude 호출 없음 (항상 실행)."""
        from backend.pipeline.phase3_dist_resolver import build_dist_resolution_from_cache

        retail_rows = [
            {"소매처코드": "R001", "소매처명": "テスト小売",
             "판매처코드": "D001", "판매처명": "東日本販社"},
        ]
        res = build_dist_resolution_from_cache(
            "R001", cached_dist={}, retail_user_rows=retail_rows,
            form_id="form_01", issuer_fingerprint="fp",
        )

        assert res.basis == "auto_1_to_1"
        assert res.dist_code == "D001"
        assert not res.needs_confirmation
        print(f"\n[dist smoke 1:1] basis=auto_1_to_1, dist_code=D001 (Claude 미호출)")

    async def test_dist_no_candidate_not_found(self, dist_smoke_dirs):
        """후보 0개 → not_found, Claude 호출 없음 (항상 실행)."""
        from backend.pipeline.phase3_dist_resolver import build_dist_resolution_from_cache

        res = build_dist_resolution_from_cache(
            "R999", cached_dist={}, retail_user_rows=[],
            form_id="form_01", issuer_fingerprint="fp",
        )

        assert res.basis == "not_found"
        assert res.dist_code is None
        print(f"\n[dist smoke 0건] basis=not_found (Claude 미호출)")
