"""test_phase3_tool_use_experiment.py — tool_use 루프 실험 테스트

실제 Claude API는 mock 처리. tool_use 루프의 구조적 동작을 검증한다.

실행: pytest tests/test_phase3_tool_use_experiment.py -v
"""
import csv
import inspect
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.experiments.phase3_tool_use_experiment import (
    ExperimentResult,
    ToolCallRecord,
    _build_experiment_tools,
    _inject_context,
    _serialize_result,
    run_retailer_mapping_experiment,
)
from backend.tools.metrics import reset_metrics


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


@pytest.fixture
def dirs(tmp_path: Path):
    mappings = tmp_path / "mappings"
    form_defs = tmp_path / "form_definitions"
    mappings.mkdir()
    form_defs.mkdir()
    return mappings, form_defs


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# ── Mock 헬퍼 ─────────────────────────────────────────────────────────────────

def _tool_use_block(tool_id: str, name: str, input_dict: dict) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = input_dict
    return block


def _text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _tool_use_response(*blocks: MagicMock) -> MagicMock:
    r = MagicMock()
    r.stop_reason = "tool_use"
    r.content = list(blocks)
    return r


def _end_turn_response(text: str = "매핑 완료") -> MagicMock:
    r = MagicMock()
    r.stop_reason = "end_turn"
    r.content = [_text_block(text)]
    return r


def _make_client(*side_effects) -> MagicMock:
    """AsyncMock messages.create를 가진 mock 클라이언트."""
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=list(side_effects))
    return client


# ── 1. _build_experiment_tools() ─────────────────────────────────────────────

class TestBuildExperimentTools:
    def test_calls_build_claude_tools(self):
        """_build_experiment_tools()는 build_claude_tools()를 내부적으로 호출한다."""
        with patch(
            "backend.experiments.phase3_tool_use_experiment.build_claude_tools",
            wraps=__import__(
                "backend.tools.claude_adapter", fromlist=["build_claude_tools"]
            ).build_claude_tools,
        ) as mock_build:
            _build_experiment_tools()
            mock_build.assert_called_once()

    def test_only_allowed_tools_included(self):
        tools = _build_experiment_tools()
        names = {t["name"] for t in tools}
        assert names == {"lookup_retailer", "confirm_mapping"}

    def test_path_fields_removed_from_required(self):
        tools = {t["name"]: t for t in _build_experiment_tools()}
        for name, tool in tools.items():
            required = tool["input_schema"].get("required", [])
            assert "mappings_dir" not in required, f"{name}: mappings_dir가 required에 남아 있음"
            assert "form_definitions_dir" not in required

    def test_path_fields_removed_from_properties(self):
        tools = {t["name"]: t for t in _build_experiment_tools()}
        for name, tool in tools.items():
            props = tool["input_schema"].get("properties", {})
            assert "mappings_dir" not in props
            assert "form_definitions_dir" not in props

    def test_lookup_retailer_schema_has_ocr_name_required(self):
        tools = {t["name"]: t for t in _build_experiment_tools()}
        required = tools["lookup_retailer"]["input_schema"]["required"]
        assert "ocr_name" in required

    def test_confirm_mapping_schema_has_mapping_type_enum(self):
        tools = {t["name"]: t for t in _build_experiment_tools()}
        schema = tools["confirm_mapping"]["input_schema"]
        mt = schema["properties"]["mapping_type"]
        assert set(mt["enum"]) == {"retailer", "product", "dist"}


# ── 2. _inject_context() ─────────────────────────────────────────────────────

class TestInjectContext:
    def test_context_merged_with_claude_args(self, dirs):
        mappings, form_defs = dirs
        ctx = {"form_id": "form_01", "mappings_dir": mappings, "form_definitions_dir": form_defs}
        result = _inject_context("lookup_retailer", {"ocr_name": "テスト"}, ctx)
        assert result["form_id"] == "form_01"
        assert result["ocr_name"] == "テスト"
        assert result["mappings_dir"] == mappings

    def test_claude_args_override_context(self, dirs):
        mappings, form_defs = dirs
        ctx = {"form_id": "form_01", "mappings_dir": mappings, "form_definitions_dir": form_defs}
        result = _inject_context("lookup_retailer", {"form_id": "form_99"}, ctx)
        assert result["form_id"] == "form_99"  # Claude 인자 우선

    def test_confirm_mapping_only_gets_valid_params(self, dirs):
        """confirm_mapping은 form_id를 받지 않으므로 ctx에서 제외된다."""
        mappings, _ = dirs
        ctx = {"form_id": "form_01", "mappings_dir": mappings, "form_definitions_dir": None}
        result = _inject_context("confirm_mapping", {"ocr_name": "テスト"}, ctx)
        # confirm_mapping 시그니처에 form_id 없음 → 주입 안 됨
        assert "form_id" not in result
        assert result["mappings_dir"] == mappings
        assert "context" in result  # 기본값 주입
        assert result["context"] == {}

    def test_confirm_mapping_existing_context_preserved(self, dirs):
        mappings, _ = dirs
        ctx = {"mappings_dir": mappings}
        result = _inject_context("confirm_mapping", {"context": {"retailer_name": "テスト"}}, ctx)
        assert result["context"] == {"retailer_name": "テスト"}


# ── 3. build_claude_tools()가 Claude 호출에 전달되는지 ─────────────────────────

class TestToolsPassedToClaude:
    async def test_experiment_tools_in_claude_call(self, dirs):
        """Claude API 호출 시 tools 파라미터에 실험용 schema가 전달된다."""
        mappings, form_defs = dirs
        # lookup_retailer → end_turn (tool_not_called 방지: 첫 응답에 tool_use 포함)
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト"})),
            _end_turn_response(),
        )

        await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs, client=client
        )

        # 첫 번째 호출의 tools 파라미터 확인
        first_call_kwargs = client.messages.create.call_args_list[0].kwargs
        assert "tools" in first_call_kwargs
        tools = first_call_kwargs["tools"]
        names = {t["name"] for t in tools}
        assert "lookup_retailer" in names
        assert "confirm_mapping" in names

    async def test_path_fields_not_in_claude_schema(self, dirs):
        """Claude에게 전달되는 schema에는 path 필드가 없다."""
        mappings, form_defs = dirs
        # lookup_retailer → end_turn (tool_not_called 방지)
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト"})),
            _end_turn_response(),
        )

        await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs, client=client
        )

        first_call_kwargs = client.messages.create.call_args_list[0].kwargs
        tools = first_call_kwargs["tools"]
        for tool in tools:
            for field in ("mappings_dir", "form_definitions_dir"):
                assert field not in tool["input_schema"].get("required", [])
                assert field not in tool["input_schema"].get("properties", {})

    async def test_tool_choice_forced_on_first_turn(self, dirs):
        """첫 번째 Claude 호출에 tool_choice=lookup_retailer가 포함된다."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト"})),
            _end_turn_response(),
        )

        await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs, client=client
        )

        first_call = client.messages.create.call_args_list[0].kwargs
        assert "tool_choice" in first_call, "첫 번째 호출에 tool_choice가 없음"
        tc = first_call["tool_choice"]
        assert tc.get("type") == "tool"
        assert tc.get("name") == "lookup_retailer"

    async def test_tool_choice_not_on_subsequent_turns(self, dirs):
        """두 번째 이후 Claude 호출에는 tool_choice가 없거나 auto이다."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト"})),
            _tool_use_response(_tool_use_block("tu_2", "confirm_mapping", {
                "mapping_type": "retailer", "ocr_name": "テスト", "confirmed_code": "R001",
            })),
            _end_turn_response(),
        )

        await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs,
            client=client, allow_side_effects=True,
        )

        second_call = client.messages.create.call_args_list[1].kwargs
        tc = second_call.get("tool_choice")
        # 두 번째 호출에는 tool_choice가 없어야 함 (auto 또는 생략)
        assert tc is None or tc.get("type") == "auto", \
            f"두 번째 호출에 tool_choice 강제 남아 있음: {tc}"


# ── 4. tool_use block → dispatch_tool_call ────────────────────────────────────

class TestToolUseDispatched:
    async def test_lookup_retailer_dispatched(self, dirs):
        """Claude의 lookup_retailer tool_use block이 dispatch_tool_call로 실행된다."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト"})),
            _end_turn_response(),
        )

        with patch(
            "backend.experiments.phase3_tool_use_experiment.dispatch_tool_call",
            wraps=__import__(
                "backend.tools.claude_adapter", fromlist=["dispatch_tool_call"]
            ).dispatch_tool_call,
        ) as mock_dispatch:
            await run_retailer_mapping_experiment(
                "テスト", "form_01", mappings, form_defs, client=client
            )
            mock_dispatch.assert_called_once()
            call_args = mock_dispatch.call_args
            assert call_args.args[0] == "lookup_retailer"

    async def test_dispatch_receives_injected_context(self, dirs):
        """dispatch_tool_call에 mappings_dir 등 컨텍스트가 주입되어 있다."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト"})),
            _end_turn_response(),
        )

        captured_args = {}

        async def _capture_dispatch(name, arguments):
            captured_args.update({"name": name, "arguments": arguments})
            from backend.tools.mapping import lookup_retailer
            return await lookup_retailer(**arguments)

        with patch(
            "backend.experiments.phase3_tool_use_experiment.dispatch_tool_call",
            new=_capture_dispatch,
        ):
            await run_retailer_mapping_experiment(
                "テスト", "form_01", mappings, form_defs, client=client
            )

        assert captured_args["arguments"]["mappings_dir"] == mappings
        assert captured_args["arguments"]["form_id"] == "form_01"


# ── 5. tool_result가 다음 메시지에 포함되는지 ────────────────────────────────

class TestToolResultInMessages:
    async def test_tool_result_included_in_next_call(self, dirs):
        """tool_use 실행 결과가 다음 Claude 호출의 messages에 tool_result로 포함된다."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト"})),
            _end_turn_response(),
        )

        await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs, client=client
        )

        # 두 번째 Claude 호출 확인
        assert client.messages.create.call_count == 2
        second_call_messages = client.messages.create.call_args_list[1].kwargs["messages"]

        # assistant 메시지 (tool_use block 포함) 다음에 user 메시지 (tool_result)가 있어야 한다
        roles = [m["role"] for m in second_call_messages]
        assert "assistant" in roles
        assert roles[-1] == "user"  # 마지막이 tool_result를 담은 user 메시지

        # tool_result content 확인
        last_user_content = second_call_messages[-1]["content"]
        assert isinstance(last_user_content, list)
        assert any(r.get("type") == "tool_result" for r in last_user_content)

    async def test_tool_result_has_correct_tool_use_id(self, dirs):
        """tool_result의 tool_use_id가 tool_use block의 id와 일치한다."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(_tool_use_block("ID_ABC", "lookup_retailer", {"ocr_name": "テスト"})),
            _end_turn_response(),
        )

        await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs, client=client
        )

        second_call_messages = client.messages.create.call_args_list[1].kwargs["messages"]
        tool_results = [
            r for r in second_call_messages[-1]["content"]
            if r.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_use_id"] == "ID_ABC"

    async def test_lookup_result_serialized_as_json(self, dirs):
        """lookup_retailer 결과가 JSON 문자열로 직렬화되어 tool_result content에 들어간다."""
        mappings, form_defs = dirs
        # 캐시 히트 설정
        write_csv(mappings / "ocr_retailer.csv", [
            {"ocr_name": "テスト店", "retailer_code": "R001", "retailer_name": "テスト"},
        ])
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト店"})),
            _end_turn_response(),
        )

        await run_retailer_mapping_experiment(
            "テスト店", "form_01", mappings, form_defs, client=client
        )

        second_call_messages = client.messages.create.call_args_list[1].kwargs["messages"]
        tool_results = [
            r for r in second_call_messages[-1]["content"]
            if r.get("type") == "tool_result"
        ]
        content = tool_results[0]["content"]
        # JSON으로 파싱 가능한지 확인
        parsed = json.loads(content)
        assert parsed["basis"] == "cache"
        assert parsed["retailer_code"] == "R001"


# ── 6. confirm_mapping 호출 검증 (allow_side_effects=True) ────────────────────

class TestConfirmMappingExecution:
    async def test_confirm_mapping_executed_when_allowed(self, dirs):
        """allow_side_effects=True일 때 confirm_mapping tool이 실제로 실행된다."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(
                _tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト"}),
            ),
            _tool_use_response(
                _tool_use_block("tu_2", "confirm_mapping", {
                    "mapping_type": "retailer",
                    "ocr_name": "テスト",
                    "confirmed_code": "R001",
                }),
            ),
            _end_turn_response("매핑 완료: テスト → R001"),
        )

        result = await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs,
            allow_side_effects=True,
            client=client,
        )

        # ExperimentResult에 confirm_mapping 기록이 있어야 한다
        confirm_calls = [tc for tc in result.tool_calls if tc.name == "confirm_mapping"]
        assert len(confirm_calls) == 1
        assert confirm_calls[0].error is None

    async def test_confirm_mapping_writes_csv(self, dirs):
        """confirm_mapping 실행 시 실제 ocr_retailer.csv에 기록된다."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(
                _tool_use_block("tu_1", "confirm_mapping", {
                    "mapping_type": "retailer",
                    "ocr_name": "テスト店",
                    "confirmed_code": "R999",
                }),
            ),
            _end_turn_response(),
        )

        await run_retailer_mapping_experiment(
            "テスト店", "form_01", mappings, form_defs,
            allow_side_effects=True,
            client=client,
        )

        rows = list(csv.DictReader(
            (mappings / "ocr_retailer.csv").open(encoding="utf-8-sig")
        ))
        assert any(r["retailer_code"] == "R999" for r in rows)

    async def test_confirm_mapping_blocked_by_default(self, dirs):
        """allow_side_effects=False(기본값)일 때 confirm_mapping은 차단된다."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(
                _tool_use_block("tu_1", "confirm_mapping", {
                    "mapping_type": "retailer",
                    "ocr_name": "テスト",
                    "confirmed_code": "R001",
                }),
            ),
            _end_turn_response(),
        )

        result = await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs,
            allow_side_effects=False,  # 기본값
            client=client,
        )

        # confirm_mapping이 tool_calls에 추가되지 않아야 한다 (CSV 쓰기 없음)
        confirm_calls = [tc for tc in result.tool_calls if tc.name == "confirm_mapping"]
        assert len(confirm_calls) == 0

        # Claude에게 success 응답 반환 (is_error 아님) — 정상 end_turn 유도
        second_messages = client.messages.create.call_args_list[1].kwargs["messages"]
        tool_results = [
            r for r in second_messages[-1]["content"]
            if r.get("type") == "tool_result"
        ]
        # error가 아닌 성공 응답 → Claude가 정상 종료 가능
        assert not any(r.get("is_error") is True for r in tool_results)

        # Claude의 결정(confirmed_code)은 decided_code를 통해 캡처됨
        assert result.confirmed_code == "R001"


# ── 7. max_turns 초과 ─────────────────────────────────────────────────────────

class TestMaxTurns:
    async def test_max_turns_raises_runtime_error(self, dirs):
        """tool_use 루프가 max_turns를 초과하면 RuntimeError가 발생한다."""
        mappings, form_defs = dirs
        # Claude가 항상 tool_use를 반환 (루프 무한)
        client = _make_client(
            *[
                _tool_use_response(
                    _tool_use_block(f"tu_{i}", "lookup_retailer", {"ocr_name": "テスト"})
                )
                for i in range(10)
            ]
        )

        with pytest.raises(RuntimeError, match="max_turns"):
            await run_retailer_mapping_experiment(
                "テスト", "form_01", mappings, form_defs,
                max_turns=3,
                client=client,
            )

    async def test_max_turns_error_message_contains_limit(self, dirs):
        """RuntimeError 메시지에 max_turns 값이 포함된다."""
        mappings, form_defs = dirs
        client = _make_client(
            *[_tool_use_response(_tool_use_block(f"tu_{i}", "lookup_retailer", {"ocr_name": "テスト"})) for i in range(5)]
        )

        with pytest.raises(RuntimeError, match="2"):
            await run_retailer_mapping_experiment(
                "テスト", "form_01", mappings, form_defs,
                max_turns=2,
                client=client,
            )

    async def test_turns_used_matches_actual(self, dirs):
        """ExperimentResult.turns_used가 실제 루프 횟수와 일치한다."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト"})),
            _end_turn_response(),
        )

        result = await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs, client=client
        )
        assert result.turns_used == 2


# ── 8. 안전장치: unknown tool / allowlist ─────────────────────────────────────

class TestSafetyGuards:
    async def test_unknown_tool_blocked_with_error_result(self, dirs):
        """허용 목록 외 tool 호출은 is_error: True tool_result로 차단된다."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "search_product", {"ocr_name": "テスト"})),
            _end_turn_response(),
        )

        result = await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs, client=client
        )

        # search_product는 실행되지 않아야 한다 (tool_calls에 없음)
        assert all(tc.name != "search_product" for tc in result.tool_calls)

    async def test_tool_execution_error_sends_error_result(self, dirs):
        """tool 실행 중 예외 발생 시 error tool_result를 Claude에게 전달하고 루프를 계속한다."""
        mappings, form_defs = dirs

        async def _failing_dispatch(name, arguments):
            raise ValueError("강제 실패")

        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト"})),
            _end_turn_response("오류 처리 후 완료"),
        )

        with patch(
            "backend.experiments.phase3_tool_use_experiment.dispatch_tool_call",
            new=_failing_dispatch,
        ):
            result = await run_retailer_mapping_experiment(
                "テスト", "form_01", mappings, form_defs, client=client
            )

        # 루프가 종료되고 error 기록이 있어야 한다
        assert result.turns_used == 2
        error_calls = [tc for tc in result.tool_calls if tc.error is not None]
        assert len(error_calls) == 1
        assert "강제 실패" in error_calls[0].error


# ── 9. ExperimentResult 속성 ─────────────────────────────────────────────────

class TestExperimentResult:
    def test_confirmed_code_extracted_from_tool_calls(self):
        result = ExperimentResult(
            tool_calls=[
                ToolCallRecord(name="lookup_retailer", input={}, output=None),
                ToolCallRecord(
                    name="confirm_mapping",
                    input={"confirmed_code": "R001", "ocr_name": "テスト"},
                    output=None,
                ),
            ],
            final_text="완료",
            turns_used=2,
        )
        assert result.confirmed_code == "R001"

    def test_confirmed_code_none_when_no_confirm(self):
        result = ExperimentResult(
            tool_calls=[ToolCallRecord(name="lookup_retailer", input={}, output=None)],
            final_text="불가",
            turns_used=1,
        )
        assert result.confirmed_code is None

    def test_confirmed_code_none_on_error(self):
        result = ExperimentResult(
            tool_calls=[
                ToolCallRecord(
                    name="confirm_mapping",
                    input={"confirmed_code": "R001"},
                    output=None,
                    error="실패",
                )
            ],
            final_text=None,
            turns_used=1,
        )
        assert result.confirmed_code is None  # error가 있으면 무효

    def test_decided_code_takes_priority_over_tool_calls(self):
        """decided_code (allow_side_effects=False 캡처)가 tool_calls보다 우선한다."""
        result = ExperimentResult(
            tool_calls=[
                ToolCallRecord(
                    name="confirm_mapping",
                    input={"confirmed_code": "R999"},
                    output=None,
                ),
            ],
            final_text="완료",
            turns_used=2,
            decided_code="R001",  # allow_side_effects=False 시 캡처된 값
        )
        assert result.confirmed_code == "R001"  # decided_code 우선

    def test_decided_code_used_when_no_tool_calls(self):
        """tool_calls 없어도 decided_code가 있으면 confirmed_code를 반환한다."""
        result = ExperimentResult(
            tool_calls=[],
            final_text="완료",
            turns_used=2,
            decided_code="R002",
        )
        assert result.confirmed_code == "R002"

    def test_decided_code_empty_falls_through_to_tool_calls(self):
        """decided_code가 빈 문자열이면 tool_calls에서 확인한다."""
        result = ExperimentResult(
            tool_calls=[
                ToolCallRecord(
                    name="confirm_mapping",
                    input={"confirmed_code": "R003"},
                    output=None,
                ),
            ],
            final_text="완료",
            turns_used=2,
            decided_code="",  # 빈 문자열 → tool_calls fallback
        )
        assert result.confirmed_code == "R003"


# ── 10. production phase3.py 불변성 검증 ─────────────────────────────────────

class TestProductionPhase3NotModified:
    def test_run_phase3_signature_unchanged(self):
        """run_phase3 함수의 파라미터 목록이 변경되지 않았다."""
        from backend.pipeline.phase3 import run_phase3
        sig = inspect.signature(run_phase3)
        params = list(sig.parameters.keys())
        expected = ["doc_id", "phase2_result", "output_dir", "form_id", "hatsu_month", "run_id"]
        assert params == expected, (
            f"run_phase3 시그니처 변경 감지: {params} (expected: {expected})"
        )

    def test_phase3_does_not_import_experiment(self):
        """phase3.py가 experiment 모듈을 import하지 않는다."""
        import ast
        src = Path("backend/pipeline/phase3.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "experiment" not in node.module, (
                    f"phase3.py가 experiment 모듈을 import함: {node.module}"
                )

    def test_experiment_is_in_separate_package(self):
        """experiment 파일이 backend/experiments/ 패키지에 격리되어 있다."""
        exp_path = Path("backend/experiments/phase3_tool_use_experiment.py")
        assert exp_path.exists(), "experiment 파일이 없음"
        phase3_path = Path("backend/pipeline/phase3.py")
        assert exp_path.parent != phase3_path.parent, "experiment가 pipeline 패키지 안에 있음"


# ── 새 테스트: tool_not_called 및 tool_choice 검증 ──────────────────────────────

class TestToolNotCalled:
    """end_turn-only (tool 미호출) 케이스와 tool_not_called 처리 검증."""

    async def test_end_turn_at_turn0_without_lookup_raises_runtime_error(self, dirs):
        """첫 응답이 tool_use 없이 end_turn이면 RuntimeError(tool_not_called)를 raise한다."""
        mappings, form_defs = dirs
        client = _make_client(_end_turn_response("도움이 필요하지 않습니다"))

        with pytest.raises(RuntimeError, match="tool_not_called"):
            await run_retailer_mapping_experiment(
                "テスト", "form_01", mappings, form_defs, client=client
            )

    async def test_lookup_then_end_turn_is_valid(self, dirs):
        """lookup_retailer 호출 후 end_turn은 정상 종료 (not_found 케이스)."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト"})),
            _end_turn_response("매핑 불가"),
        )

        result = await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs, client=client
        )

        assert result is not None
        # lookup은 호출됐으나 confirm 없음 → confirmed_code=None (not_found)
        assert result.confirmed_code is None
        lookup_calls = [tc for tc in result.tool_calls if tc.name == "lookup_retailer"]
        assert len(lookup_calls) >= 1

    async def test_end_turn_after_lookup_and_confirm_is_valid(self, dirs):
        """lookup → confirm → end_turn은 정상 성공 케이스다."""
        mappings, form_defs = dirs
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "lookup_retailer", {"ocr_name": "テスト"})),
            _tool_use_response(_tool_use_block("tu_2", "confirm_mapping", {
                "mapping_type": "retailer", "ocr_name": "テスト", "confirmed_code": "R001",
            })),
            _end_turn_response("완료"),
        )

        result = await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs,
            client=client, allow_side_effects=True,
        )

        assert result.confirmed_code == "R001"

    async def test_tool_not_called_in_batch_stats(self, tmp_path):
        """_run_one이 tool_not_called=True인 결과를 반환하면 BatchStats에 반영된다."""
        from backend.experiments.batch_tool_use_experiment import (
            RetailerBatchResult, SCENARIO_SUCCESS, run_batch_retailer_experiment,
        )

        # _run_one이 tool_not_called=True인 결과 반환하도록 patch
        async def _fake_run_one(**kwargs):
            return RetailerBatchResult(
                ocr_name=kwargs.get("ocr_name", "テスト"),
                success=False, confirmed_code=None, lookup_basis=None,
                tool_call_count=0, lookup_call_count=0, confirm_call_count=0,
                turns_used=1, max_turns_hit=False, elapsed_ms=10.0,
                error="tool_not_called: ...",
                tool_not_called=True,
            )

        with patch("backend.experiments.batch_tool_use_experiment._run_one",
                   side_effect=_fake_run_one):
            result = await run_batch_retailer_experiment(
                ocr_names=["テスト"],
                mappings_dir=tmp_path,
                scenario=SCENARIO_SUCCESS,
                allow_side_effects=False,
            )

        assert result.stats.tool_not_called_count == 1, \
            f"tool_not_called_count가 0: {result.stats}"
        assert result.stats.failure_count == 1

    async def test_decided_code_prevents_tool_not_called_error(self, dirs):
        """decided_code가 있으면 turn 0 end_turn도 허용된다 (캐시 결정 케이스)."""
        mappings, form_defs = dirs
        # 첫 응답: confirm_mapping 호출 (decided_code 캡처) → end_turn
        client = _make_client(
            _tool_use_response(_tool_use_block("tu_1", "confirm_mapping", {
                "mapping_type": "retailer", "ocr_name": "テスト", "confirmed_code": "R001",
            })),
            _end_turn_response("완료"),
        )

        # confirm_mapping이 allow_side_effects=False로 차단 → decided_code="R001" 캡처
        result = await run_retailer_mapping_experiment(
            "テスト", "form_01", mappings, form_defs,
            client=client, allow_side_effects=False,
        )

        assert result.decided_code == "R001"
        assert result.confirmed_code == "R001"


class TestToolNotCalledFallback:
    """tool_not_called가 _attempt_tool_use_phase에서 ToolUseContractError를 발생시키는지 검증."""

    async def test_tool_not_called_triggers_contract_error_in_attempt_phase(self, tmp_path):
        """batch stats.tool_not_called_count > 0 → ToolUseContractError → fallback."""
        from backend.experiments.batch_tool_use_experiment import (
            BatchExperimentResult, BatchStats,
        )
        from backend.pipeline.phase3_fallback import (
            Phase3FallbackStats, ToolUseContractError, _attempt_tool_use_phase,
        )

        # tool_not_called_count=1인 BatchExperimentResult
        stats_obj = BatchStats(
            batch_size=1, success_count=0, failure_count=1,
            max_turns_hit_count=0, not_found_count=0,
            total_tool_calls=0, total_lookup_calls=0, total_confirm_calls=0,
            total_turns=0, avg_turns=0.0, elapsed_ms=100.0,
            tool_not_called_count=1,  # ← 핵심
        )
        mock_batch = BatchExperimentResult(
            scenario="test", batch_size=1,
            stats=stats_obj, per_retailer=[],
        )

        fallback_stats = Phase3FallbackStats(
            enable_tool_use=True, used_tool_use=True, fallback_triggered=False,
            fallback_reason=None, fallback_class=None,
            tool_use_elapsed_ms=0, legacy_elapsed_ms=0, total_elapsed_ms=0,
            max_turns_hit=False, api_retry_failed=False, batch_size=0, batch_failure_count=0,
        )

        phase2 = {"items": [{"customer": "テスト店"}]}

        with patch(
            "backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
            new=AsyncMock(return_value=mock_batch),
        ):
            with pytest.raises(ToolUseContractError, match="tool 미호출"):
                await _attempt_tool_use_phase(
                    phase2_result=phase2,
                    mappings_dir=tmp_path,
                    form_definitions_dir=tmp_path,
                    form_id="form_01",
                    max_turns=5,
                    stats=fallback_stats,
                    anthropic_api_key="fake",
                )
