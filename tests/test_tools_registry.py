"""test_tools_registry.py — Tool Registry 단위 테스트

실행: pytest tests/test_tools_registry.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.tools.registry import (
    TOOL_REGISTRY,
    ToolSpec,
    get_tool,
    get_tool_schema,
    list_tools,
)
from backend.tools.mapping import confirm_mapping, lookup_retailer, search_product


# ── 1. Registry 등록 내용 검증 ───────────────────────────────────────────────

class TestRegistryContents:
    """TOOL_REGISTRY에 올바른 Tool들이 등록되어 있는지 검증."""

    def test_exactly_three_tools_registered(self):
        """현재 등록된 Tool은 정확히 3개다."""
        assert len(TOOL_REGISTRY) == 3

    def test_all_expected_tool_names_present(self):
        """lookup_retailer, search_product, confirm_mapping 모두 등록되어 있다."""
        assert "lookup_retailer" in TOOL_REGISTRY
        assert "search_product" in TOOL_REGISTRY
        assert "confirm_mapping" in TOOL_REGISTRY

    def test_registry_key_matches_spec_name(self):
        """Registry dict key와 ToolSpec.name이 항상 일치한다."""
        for key, spec in TOOL_REGISTRY.items():
            assert key == spec.name, (
                f"Registry key '{key}' ≠ ToolSpec.name '{spec.name}'"
            )

    def test_all_specs_are_toolspec_instances(self):
        """모든 Registry 값이 ToolSpec 인스턴스다."""
        for spec in TOOL_REGISTRY.values():
            assert isinstance(spec, ToolSpec)


# ── 2. list_tools() ───────────────────────────────────────────────────────────

class TestListTools:
    def test_returns_list(self):
        assert isinstance(list_tools(), list)

    def test_length_matches_registry(self):
        assert len(list_tools()) == len(TOOL_REGISTRY)

    def test_all_items_are_toolspec(self):
        for spec in list_tools():
            assert isinstance(spec, ToolSpec)

    def test_contains_all_registered_names(self):
        names = {spec.name for spec in list_tools()}
        assert names == set(TOOL_REGISTRY.keys())


# ── 3. get_tool() ─────────────────────────────────────────────────────────────

class TestGetTool:
    def test_lookup_retailer_callable_is_correct(self):
        """get_tool("lookup_retailer").callable은 실제 lookup_retailer 함수다."""
        spec = get_tool("lookup_retailer")
        assert spec.callable is lookup_retailer

    def test_search_product_callable_is_correct(self):
        spec = get_tool("search_product")
        assert spec.callable is search_product

    def test_confirm_mapping_callable_is_correct(self):
        spec = get_tool("confirm_mapping")
        assert spec.callable is confirm_mapping

    def test_missing_tool_raises_keyerror(self):
        with pytest.raises(KeyError, match="registry에 없음"):
            get_tool("nonexistent_tool")

    def test_missing_tool_error_lists_registered_names(self):
        """KeyError 메시지에 등록된 Tool 이름 목록이 포함된다."""
        with pytest.raises(KeyError) as exc_info:
            get_tool("no_such_tool")
        msg = str(exc_info.value)
        assert "lookup_retailer" in msg
        assert "search_product" in msg
        assert "confirm_mapping" in msg


# ── 4. side_effects / idempotent ─────────────────────────────────────────────

class TestToolSpecMetadata:
    def test_side_effects_lookup_retailer(self):
        """lookup_retailer는 파일을 읽기만 하므로 side_effects=False."""
        assert get_tool("lookup_retailer").side_effects is False

    def test_side_effects_search_product(self):
        """search_product는 파일을 읽기만 하므로 side_effects=False."""
        assert get_tool("search_product").side_effects is False

    def test_side_effects_confirm_mapping(self):
        """confirm_mapping은 CSV를 기록하므로 side_effects=True."""
        assert get_tool("confirm_mapping").side_effects is True

    def test_idempotent_all_tools(self):
        """세 Tool 모두 idempotent=True."""
        for name in ("lookup_retailer", "search_product", "confirm_mapping"):
            assert get_tool(name).idempotent is True, f"{name}.idempotent should be True"

    def test_output_contract_lookup_retailer(self):
        assert get_tool("lookup_retailer").output_contract == "LookupRetailerResult"

    def test_output_contract_search_product(self):
        assert get_tool("search_product").output_contract == "SearchProductResult"

    def test_output_contract_confirm_mapping(self):
        assert get_tool("confirm_mapping").output_contract == "None"

    def test_description_nonempty_all_tools(self):
        """모든 Tool의 description이 비어 있지 않다."""
        for spec in list_tools():
            assert spec.description.strip(), f"Tool '{spec.name}'의 description이 비어 있음"


# ── 5. input_schema 검증 ─────────────────────────────────────────────────────

class TestInputSchema:
    def test_all_schemas_are_object_type(self):
        """모든 Tool의 input_schema type이 'object'다."""
        for spec in list_tools():
            assert spec.input_schema.get("type") == "object", (
                f"Tool '{spec.name}'의 schema type이 'object'가 아님"
            )

    def test_lookup_retailer_required_fields(self):
        schema = get_tool("lookup_retailer").input_schema
        required = schema.get("required", [])
        assert "ocr_name" in required
        assert "form_id" in required
        assert "mappings_dir" in required

    def test_search_product_required_fields(self):
        schema = get_tool("search_product").input_schema
        required = schema.get("required", [])
        assert "ocr_name" in required
        assert "mappings_dir" in required

    def test_confirm_mapping_required_fields(self):
        schema = get_tool("confirm_mapping").input_schema
        required = schema.get("required", [])
        assert "mapping_type" in required
        assert "ocr_name" in required
        assert "confirmed_code" in required
        assert "mappings_dir" in required

    def test_confirm_mapping_mapping_type_enum(self):
        """confirm_mapping schema의 mapping_type에 enum이 정의되어 있다."""
        schema = get_tool("confirm_mapping").input_schema
        mt = schema["properties"]["mapping_type"]
        assert "enum" in mt
        assert set(mt["enum"]) == {"retailer", "product", "dist"}

    def test_all_schemas_have_properties(self):
        """모든 Tool의 input_schema에 properties가 있다."""
        for spec in list_tools():
            assert "properties" in spec.input_schema, (
                f"Tool '{spec.name}'의 schema에 properties 없음"
            )

    def test_top_k_is_optional_in_lookup_retailer(self):
        """top_k는 required에 포함되지 않아야 한다."""
        schema = get_tool("lookup_retailer").input_schema
        assert "top_k" not in schema.get("required", [])

    def test_top_k_has_minimum_in_lookup_retailer(self):
        """top_k에 minimum: 1 제약이 있다."""
        props = get_tool("lookup_retailer").input_schema["properties"]
        assert props["top_k"].get("minimum") == 1


# ── 6. get_tool_schema() ─────────────────────────────────────────────────────

class TestGetToolSchema:
    def test_returns_dict(self):
        for name in ("lookup_retailer", "search_product", "confirm_mapping"):
            schema = get_tool_schema(name)
            assert isinstance(schema, dict)

    def test_same_as_spec_input_schema(self):
        """get_tool_schema()는 spec.input_schema와 동일한 객체를 반환한다."""
        for name in ("lookup_retailer", "search_product", "confirm_mapping"):
            assert get_tool_schema(name) is get_tool(name).input_schema

    def test_missing_raises_keyerror(self):
        with pytest.raises(KeyError):
            get_tool_schema("nonexistent")


# ── 7. ToolSpec 불변성 ────────────────────────────────────────────────────────

class TestToolSpecImmutability:
    def test_frozen_prevents_mutation(self):
        """frozen=True이므로 속성 변경 시 FrozenInstanceError(AttributeError)가 발생한다."""
        spec = get_tool("lookup_retailer")
        with pytest.raises(AttributeError):
            spec.name = "modified"  # type: ignore[misc]

    def test_side_effects_cannot_be_mutated(self):
        spec = get_tool("confirm_mapping")
        with pytest.raises(AttributeError):
            spec.side_effects = False  # type: ignore[misc]

    def test_toolspec_is_hashable_despite_dict_field(self):
        """input_schema(dict)를 field(hash=False)로 제외했으므로 hash()가 성공한다."""
        spec = get_tool("lookup_retailer")
        h = hash(spec)
        assert isinstance(h, int)
