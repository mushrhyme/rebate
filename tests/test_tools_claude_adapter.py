"""test_tools_claude_adapter.py — Claude Adapter 단위 테스트

실행: pytest tests/test_tools_claude_adapter.py -v
"""
import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.tools.claude_adapter import (
    build_claude_tools,
    coerce_tool_arguments,
    dispatch_tool_call,
)
from backend.tools.metrics import reset_metrics
from backend.tools.registry import TOOL_REGISTRY, get_tool


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_metrics():
    """dispatch 호출이 metrics를 오염하지 않도록 각 테스트 전후 리셋."""
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


# ── 1. build_claude_tools() ───────────────────────────────────────────────────

class TestBuildClaudeTools:
    def test_returns_list(self):
        result = build_claude_tools()
        assert isinstance(result, list)

    def test_contains_all_registered_tools(self):
        tools = build_claude_tools()
        names = {t["name"] for t in tools}
        assert names == set(TOOL_REGISTRY.keys())

    def test_exactly_three_tools(self):
        assert len(build_claude_tools()) == len(TOOL_REGISTRY)

    def test_each_tool_has_required_keys(self):
        """각 tool dict에 Anthropic API가 요구하는 name, description, input_schema가 있다."""
        for tool in build_claude_tools():
            assert "name" in tool, f"'name' 키 없음: {tool}"
            assert "description" in tool, f"'description' 키 없음: {tool}"
            assert "input_schema" in tool, f"'input_schema' 키 없음: {tool}"

    def test_name_matches_registry_key(self):
        """tool dict의 name이 registry key와 일치한다."""
        for tool in build_claude_tools():
            assert tool["name"] in TOOL_REGISTRY

    def test_description_nonempty(self):
        for tool in build_claude_tools():
            assert tool["description"].strip(), f"Tool '{tool['name']}' description 비어 있음"

    def test_input_schema_is_object_type(self):
        """Anthropic API 요구사항: input_schema.type == 'object'."""
        for tool in build_claude_tools():
            assert tool["input_schema"].get("type") == "object", (
                f"Tool '{tool['name']}': input_schema.type이 'object'가 아님"
            )

    def test_input_schema_has_properties(self):
        """input_schema에 properties가 포함되어야 한다."""
        for tool in build_claude_tools():
            assert "properties" in tool["input_schema"], (
                f"Tool '{tool['name']}': input_schema에 properties 없음"
            )

    def test_input_schema_same_object_as_registry(self):
        """input_schema는 registry ToolSpec.input_schema와 동일한 객체다."""
        tools_by_name = {t["name"]: t for t in build_claude_tools()}
        for name, spec in TOOL_REGISTRY.items():
            assert tools_by_name[name]["input_schema"] is spec.input_schema

    def test_lookup_retailer_schema_has_required(self):
        tools = {t["name"]: t for t in build_claude_tools()}
        schema = tools["lookup_retailer"]["input_schema"]
        assert "ocr_name" in schema["required"]
        assert "form_id" in schema["required"]

    def test_confirm_mapping_schema_has_enum(self):
        tools = {t["name"]: t for t in build_claude_tools()}
        schema = tools["confirm_mapping"]["input_schema"]
        mt = schema["properties"]["mapping_type"]
        assert set(mt["enum"]) == {"retailer", "product", "dist"}

    def test_no_extra_unexpected_keys(self):
        """Anthropic API 호환: tool dict에 name/description/input_schema 외 키 없음."""
        expected = {"name", "description", "input_schema"}
        for tool in build_claude_tools():
            assert set(tool.keys()) == expected, (
                f"Tool '{tool['name']}': 예상 외 키 발견 {set(tool.keys()) - expected}"
            )


# ── 2. dispatch_tool_call() ───────────────────────────────────────────────────

class TestDispatchToolCall:

    # ── lookup_retailer ───────────────────────────────────────────────────────

    async def test_dispatch_lookup_retailer_returns_result(self, dirs):
        """dispatch_tool_call이 실제 lookup_retailer를 실행하고 결과를 반환한다."""
        mappings, form_defs = dirs
        from backend.tools.mapping import LookupRetailerResult
        result = await dispatch_tool_call("lookup_retailer", {
            "ocr_name": "テスト店舗",
            "form_id": "form_01",
            "mappings_dir": mappings,
            "form_definitions_dir": form_defs,
        })
        assert isinstance(result, LookupRetailerResult)

    async def test_dispatch_lookup_retailer_cache_hit(self, dirs):
        """캐시 데이터가 있을 때 dispatch 결과가 cache hit이다."""
        mappings, form_defs = dirs
        write_csv(mappings / "ocr_retailer.csv", [
            {"ocr_name": "テスト店", "retailer_code": "R001", "retailer_name": "テスト"},
        ])
        result = await dispatch_tool_call("lookup_retailer", {
            "ocr_name": "テスト店",
            "form_id": "form_01",
            "mappings_dir": mappings,
            "form_definitions_dir": form_defs,
        })
        assert result.basis == "cache"
        assert result.retailer_code == "R001"

    async def test_dispatch_lookup_retailer_result_matches_direct_call(self, dirs):
        """dispatch 결과가 callable 직접 호출 결과와 동일하다."""
        mappings, form_defs = dirs
        from backend.tools.mapping import lookup_retailer
        args = {
            "ocr_name": "ZZZZZZ",
            "form_id": "form_01",
            "mappings_dir": mappings,
            "form_definitions_dir": form_defs,
        }
        result_dispatch = await dispatch_tool_call("lookup_retailer", args)
        result_direct  = await lookup_retailer(**args)

        assert result_dispatch.basis == result_direct.basis
        assert result_dispatch.retailer_code == result_direct.retailer_code
        assert result_dispatch.confidence == result_direct.confidence

    # ── search_product ────────────────────────────────────────────────────────

    async def test_dispatch_search_product_returns_result(self, dirs):
        """dispatch_tool_call이 실제 search_product를 실행하고 결과를 반환한다."""
        mappings, _ = dirs
        from backend.tools.mapping import SearchProductResult
        result = await dispatch_tool_call("search_product", {
            "ocr_name": "存在しない製品",
            "mappings_dir": mappings,
        })
        assert isinstance(result, SearchProductResult)

    async def test_dispatch_search_product_not_found(self, dirs):
        mappings, _ = dirs
        result = await dispatch_tool_call("search_product", {
            "ocr_name": "ZZZZZZZZ",
            "mappings_dir": mappings,
        })
        assert result.basis == "not_found"

    async def test_dispatch_search_product_result_matches_direct_call(self, dirs):
        """dispatch 결과가 callable 직접 호출 결과와 동일하다."""
        mappings, _ = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P001", "제품명": "農心 辛ラーメン 120g", "시키리": "100", "본부장": "90"},
        ])
        from backend.tools.mapping import search_product
        args = {"ocr_name": "農心 辛ラーメン 120g", "mappings_dir": mappings}
        result_dispatch = await dispatch_tool_call("search_product", args)
        result_direct   = await search_product(**args)

        assert result_dispatch.basis == result_direct.basis
        assert result_dispatch.product_code == result_direct.product_code

    # ── confirm_mapping (side_effects=True) ───────────────────────────────────

    async def test_dispatch_confirm_mapping_returns_none(self, dirs):
        """side_effects=True tool도 dispatch_tool_call로 실행 가능하며 None을 반환한다."""
        mappings, _ = dirs
        result = await dispatch_tool_call("confirm_mapping", {
            "mapping_type": "retailer",
            "ocr_name": "テスト店",
            "confirmed_code": "R001",
            "context": {"retailer_name": "テスト"},
            "mappings_dir": mappings,
        })
        assert result is None

    async def test_dispatch_confirm_mapping_writes_csv(self, dirs):
        """dispatch로 confirm_mapping을 실행하면 실제 CSV에 기록된다."""
        import csv as _csv
        mappings, _ = dirs
        await dispatch_tool_call("confirm_mapping", {
            "mapping_type": "retailer",
            "ocr_name": "テスト店",
            "confirmed_code": "R001",
            "context": {"retailer_name": "テスト"},
            "mappings_dir": mappings,
        })
        rows = list(_csv.DictReader(
            (mappings / "ocr_retailer.csv").open(encoding="utf-8-sig")
        ))
        assert len(rows) == 1
        assert rows[0]["retailer_code"] == "R001"

    async def test_dispatch_confirm_mapping_side_effects_flag(self):
        """confirm_mapping은 side_effects=True이지만 dispatch로 실행 가능하다."""
        from backend.tools.registry import get_tool
        spec = get_tool("confirm_mapping")
        assert spec.side_effects is True
        # side_effects=True라도 dispatch는 허용됨 — 위 test들에서 이미 검증

    # ── 없는 tool name ────────────────────────────────────────────────────────

    async def test_unknown_tool_name_raises_keyerror(self):
        """등록되지 않은 tool name이면 KeyError가 발생한다."""
        with pytest.raises(KeyError, match="registry에 없음"):
            await dispatch_tool_call("nonexistent_tool", {})

    async def test_unknown_tool_error_is_descriptive(self):
        """KeyError 메시지에 등록된 tool 이름 목록이 포함된다."""
        with pytest.raises(KeyError) as exc_info:
            await dispatch_tool_call("no_such_tool", {})
        msg = str(exc_info.value)
        assert "lookup_retailer" in msg

    # ── callable 일치 검증 ────────────────────────────────────────────────────

    async def test_dispatch_uses_registry_callable(self, dirs):
        """dispatch_tool_call은 TOOL_REGISTRY의 callable을 그대로 사용한다."""
        from backend.tools.mapping import lookup_retailer
        from backend.tools.registry import get_tool
        # Registry의 callable이 실제 lookup_retailer 함수인지 확인
        assert get_tool("lookup_retailer").callable is lookup_retailer

    async def test_all_tools_dispatchable(self, dirs):
        """등록된 모든 Tool이 dispatch_tool_call로 실행 가능하다."""
        mappings, form_defs = dirs

        # lookup_retailer
        result = await dispatch_tool_call("lookup_retailer", {
            "ocr_name": "テスト",
            "form_id": "form_01",
            "mappings_dir": mappings,
            "form_definitions_dir": form_defs,
        })
        assert result.basis in ("cache", "bracket_code", "candidate", "not_found")

        # search_product
        result = await dispatch_tool_call("search_product", {
            "ocr_name": "テスト製品",
            "mappings_dir": mappings,
        })
        assert result.basis in ("cache", "candidate", "not_found")

        # confirm_mapping
        result = await dispatch_tool_call("confirm_mapping", {
            "mapping_type": "product",
            "ocr_name": "テスト製品",
            "confirmed_code": "P001",
            "context": {"product_name": "テスト"},
            "mappings_dir": mappings,
        })
        assert result is None


# ── Path Coercion: coerce_tool_arguments() ────────────────────────────────────

class TestCoerceToolArguments:
    """coerce_tool_arguments() — str → Path 변환 및 여분 인자 제거."""

    def test_string_mappings_dir_converted_to_path_for_lookup_retailer(self, dirs):
        """lookup_retailer의 mappings_dir str 입력이 Path로 변환된다."""
        mappings, form_defs = dirs
        spec = get_tool("lookup_retailer")
        result = coerce_tool_arguments(spec, {
            "ocr_name": "テスト",
            "form_id": "form_01",
            "mappings_dir": str(mappings),           # str 입력
            "form_definitions_dir": str(form_defs),  # str 입력
        })
        assert isinstance(result["mappings_dir"], Path)
        assert result["mappings_dir"] == mappings
        assert isinstance(result["form_definitions_dir"], Path)
        assert result["form_definitions_dir"] == form_defs

    def test_string_mappings_dir_converted_for_search_product(self, dirs):
        """search_product의 mappings_dir str 입력이 Path로 변환된다."""
        mappings, _ = dirs
        spec = get_tool("search_product")
        result = coerce_tool_arguments(spec, {
            "ocr_name": "テスト製品",
            "mappings_dir": str(mappings),  # str 입력
        })
        assert isinstance(result["mappings_dir"], Path)
        assert result["mappings_dir"] == mappings

    def test_string_mappings_dir_converted_for_confirm_mapping(self, dirs):
        """confirm_mapping의 mappings_dir str 입력이 Path로 변환된다."""
        mappings, _ = dirs
        spec = get_tool("confirm_mapping")
        result = coerce_tool_arguments(spec, {
            "mapping_type": "retailer",
            "ocr_name": "テスト",
            "confirmed_code": "R001",
            "context": {},
            "mappings_dir": str(mappings),  # str 입력
        })
        assert isinstance(result["mappings_dir"], Path)
        assert result["mappings_dir"] == mappings

    def test_path_object_unchanged(self, dirs):
        """이미 Path 객체이면 변환 없이 그대로 유지된다."""
        mappings, form_defs = dirs
        spec = get_tool("lookup_retailer")
        result = coerce_tool_arguments(spec, {
            "ocr_name": "テスト",
            "form_id": "form_01",
            "mappings_dir": mappings,           # Path 객체 직접 전달
            "form_definitions_dir": form_defs,
        })
        assert result["mappings_dir"] is mappings  # 동일 객체 보장

    def test_none_allowed_path_stays_none(self, dirs):
        """Optional[Path] (Path | None) 파라미터에 None을 넘기면 그대로 None이다."""
        mappings, _ = dirs
        spec = get_tool("lookup_retailer")
        result = coerce_tool_arguments(spec, {
            "ocr_name": "テスト",
            "form_id": "form_01",
            "mappings_dir": str(mappings),
            "form_definitions_dir": None,  # Optional[Path] → None 허용
        })
        assert result["form_definitions_dir"] is None

    def test_extra_arguments_are_dropped(self, dirs):
        """Tool이 받지 않는 여분의 인자는 경고 후 제거된다."""
        mappings, _ = dirs
        spec = get_tool("search_product")
        result = coerce_tool_arguments(spec, {
            "ocr_name": "テスト製品",
            "mappings_dir": str(mappings),
            "unexpected_key": "이것은 없는 파라미터",  # extra
            "another_extra": 42,
        })
        assert "unexpected_key" not in result
        assert "another_extra" not in result
        assert "ocr_name" in result
        assert isinstance(result["mappings_dir"], Path)

    def test_invalid_path_type_raises_typeerror(self, dirs):
        """Path 파라미터에 str/Path/None 이외의 타입을 넘기면 TypeError가 발생한다."""
        mappings, _ = dirs
        spec = get_tool("search_product")
        with pytest.raises(TypeError, match="타입이 잘못됨"):
            coerce_tool_arguments(spec, {
                "ocr_name": "テスト製品",
                "mappings_dir": 12345,  # int → TypeError
            })

    def test_non_path_str_params_unchanged(self, dirs):
        """Path가 아닌 str 파라미터(ocr_name 등)는 변환하지 않는다."""
        mappings, _ = dirs
        spec = get_tool("search_product")
        result = coerce_tool_arguments(spec, {
            "ocr_name": "テスト製品",
            "mappings_dir": str(mappings),
        })
        assert isinstance(result["ocr_name"], str)
        assert result["ocr_name"] == "テスト製品"

    def test_top_k_int_unchanged(self, dirs):
        """int 파라미터(top_k)는 변환 없이 그대로 유지된다."""
        mappings, _ = dirs
        spec = get_tool("search_product")
        result = coerce_tool_arguments(spec, {
            "ocr_name": "テスト",
            "mappings_dir": str(mappings),
            "top_k": 3,
        })
        assert result["top_k"] == 3
        assert isinstance(result["top_k"], int)


# ── Path Coercion: dispatch_tool_call()과의 통합 ─────────────────────────────

class TestDispatchWithStringPaths:
    """dispatch_tool_call()에 string 경로를 넘겨도 실제 Tool이 실행되는지 검증."""

    async def test_lookup_retailer_with_string_paths_executes(self, dirs):
        """dispatch_tool_call에 str 경로를 넘겨도 lookup_retailer가 실행된다."""
        mappings, form_defs = dirs
        from backend.tools.mapping import LookupRetailerResult
        result = await dispatch_tool_call("lookup_retailer", {
            "ocr_name": "テスト店",
            "form_id": "form_01",
            "mappings_dir": str(mappings),           # str 경로
            "form_definitions_dir": str(form_defs),  # str 경로
        })
        assert isinstance(result, LookupRetailerResult)
        assert result.basis in ("cache", "bracket_code", "candidate", "not_found")

    async def test_search_product_with_string_paths_executes(self, dirs):
        """dispatch_tool_call에 str 경로를 넘겨도 search_product가 실행된다."""
        mappings, _ = dirs
        from backend.tools.mapping import SearchProductResult
        result = await dispatch_tool_call("search_product", {
            "ocr_name": "テスト製品",
            "mappings_dir": str(mappings),  # str 경로
        })
        assert isinstance(result, SearchProductResult)

    async def test_confirm_mapping_with_string_paths_executes(self, dirs):
        """dispatch_tool_call에 str 경로를 넘겨도 confirm_mapping이 실행된다."""
        mappings, _ = dirs
        result = await dispatch_tool_call("confirm_mapping", {
            "mapping_type": "retailer",
            "ocr_name": "テスト店",
            "confirmed_code": "R001",
            "context": {"retailer_name": "テスト"},
            "mappings_dir": str(mappings),  # str 경로
        })
        assert result is None

    async def test_dispatch_string_path_result_matches_path_object(self, dirs):
        """str 경로로 dispatch한 결과와 Path 객체로 직접 호출한 결과가 일치한다."""
        mappings, form_defs = dirs
        from backend.tools.mapping import lookup_retailer

        result_str = await dispatch_tool_call("lookup_retailer", {
            "ocr_name": "テスト店",
            "form_id": "form_01",
            "mappings_dir": str(mappings),
            "form_definitions_dir": str(form_defs),
        })
        result_path = await lookup_retailer(
            ocr_name="テスト店",
            form_id="form_01",
            mappings_dir=mappings,
            form_definitions_dir=form_defs,
        )
        assert result_str.basis == result_path.basis
        assert result_str.retailer_code == result_path.retailer_code

    async def test_none_form_definitions_dir_allowed(self, dirs):
        """form_definitions_dir=None (Optional[Path])을 str 경로와 혼용해도 동작한다."""
        mappings, _ = dirs
        from backend.tools.mapping import LookupRetailerResult
        result = await dispatch_tool_call("lookup_retailer", {
            "ocr_name": "テスト店",
            "form_id": "form_01",
            "mappings_dir": str(mappings),
            "form_definitions_dir": None,  # Optional → None 허용
        })
        assert isinstance(result, LookupRetailerResult)
