"""registry.py — Tool Registry

backend/tools/mapping.py의 공개 Tool들을 중앙에서 관리한다.

현재 역할:
  - Tool metadata (schema, side_effects, idempotent) 저장
  - 이름으로 Tool 조회 (get_tool, list_tools, get_tool_schema)

향후 확장 예정 (이 파일만 수정하면 됨):
  - Claude tool_use schema 생성 (input_schema → tools=[] 포맷 변환)
  - MCP 서버 도구 목록 노출

등록된 Tool:
  lookup_retailer  — side_effects=False, idempotent=True
  search_product   — side_effects=False, idempotent=True
  confirm_mapping  — side_effects=True,  idempotent=True
"""
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .mapping import confirm_mapping, lookup_retailer, search_product

__all__ = [
    "ToolSpec",
    "TOOL_REGISTRY",
    "list_tools",
    "get_tool",
    "get_tool_schema",
]


@dataclass(frozen=True)
class ToolSpec:
    """Registry에 등록된 Tool의 정적 메타데이터.

    frozen=True: 등록 후 변경 불가 (FrozenInstanceError 발생).
    input_schema는 hash에서 제외 (dict는 hashable하지 않으므로).
    ToolSpec 인스턴스를 dict key나 set 원소로 사용하지 말 것 — 값으로만 사용.

    Fields:
        name:            Tool 식별자 (TOOL_REGISTRY 키와 일치)
        description:     Tool 설명 (Claude / MCP에 노출될 텍스트)
        callable:        실제 async 함수 참조
        input_schema:    JSON Schema 형식 입력 스키마
        output_contract: 반환 타입 이름 문자열 (예: "LookupRetailerResult")
        side_effects:    True = 파일/DB 등 외부 상태를 변경함
        idempotent:      True = 같은 입력 반복 시 결과가 동일 (upsert 포함)
    """
    name: str
    description: str
    callable: Callable[..., Awaitable[Any]]  # noqa: A003 (builtin shadow intentional)
    output_contract: str
    side_effects: bool
    idempotent: bool
    input_schema: dict = field(hash=False)   # dict → hash 제외, eq는 유지


# ── Input Schema 정의 ─────────────────────────────────────────────────────────

_LOOKUP_RETAILER_SCHEMA: dict = {
    "type": "object",
    "required": ["ocr_name", "form_id", "mappings_dir"],
    "properties": {
        "ocr_name": {
            "type": "string",
            "description": "OCR에서 추출한 거래처명 원문",
        },
        "form_id": {
            "type": "string",
            "description": "양식 ID (예: 'form_01', 'form_04')",
        },
        "mappings_dir": {
            "type": "string",
            "description": "mappings/ 디렉토리 절대 경로",
        },
        "form_definitions_dir": {
            "type": "string",
            "description": "form_definitions/ 디렉토리 절대 경로. 미지정 시 get_settings()에서 로드.",
        },
        "top_k": {
            "type": "integer",
            "default": 5,
            "minimum": 1,
            "description": "유사도 후보 최대 수",
        },
    },
}

_SEARCH_PRODUCT_SCHEMA: dict = {
    "type": "object",
    "required": ["ocr_name", "mappings_dir"],
    "properties": {
        "ocr_name": {
            "type": "string",
            "description": "OCR에서 추출한 제품명 원문",
        },
        "mappings_dir": {
            "type": "string",
            "description": "mappings/ 디렉토리 절대 경로",
        },
        "top_k": {
            "type": "integer",
            "default": 5,
            "minimum": 1,
            "description": "유사도 후보 최대 수",
        },
    },
}

_CONFIRM_MAPPING_SCHEMA: dict = {
    "type": "object",
    "required": ["mapping_type", "ocr_name", "confirmed_code", "context", "mappings_dir"],
    "properties": {
        "mapping_type": {
            "type": "string",
            "enum": ["retailer", "product", "dist"],
            "description": "저장 대상 종류",
        },
        "ocr_name": {
            "type": "string",
            "description": "OCR 원문 명칭 (retailer·product의 CSV 키; dist는 참고용)",
        },
        "confirmed_code": {
            "type": "string",
            "description": "확정된 코드 (retailer_code / product_code / dist_code)",
        },
        "context": {
            "type": "object",
            "description": (
                "타입별 추가 정보. "
                "retailer: {retailer_name?}; "
                "product: {product_name?}; "
                "dist: {form_id, issuer_fingerprint, retailer_code, dist_name?}"
            ),
        },
        "mappings_dir": {
            "type": "string",
            "description": "mappings/ 디렉토리 절대 경로",
        },
    },
}


# ── Tool Registry ─────────────────────────────────────────────────────────────

TOOL_REGISTRY: dict[str, ToolSpec] = {
    "lookup_retailer": ToolSpec(
        name="lookup_retailer",
        description=(
            "OCR 거래처명으로 소매처코드 후보를 조회한다. "
            "캐시 → 괄호코드 직접 매칭 → 유사도 검색 순으로 처리. "
            "CSV·MD 파일이 없어도 예외 없이 not_found를 반환."
        ),
        callable=lookup_retailer,
        input_schema=_LOOKUP_RETAILER_SCHEMA,
        output_contract="LookupRetailerResult",
        side_effects=False,
        idempotent=True,
    ),
    "search_product": ToolSpec(
        name="search_product",
        description=(
            "OCR 제품명으로 제품코드 후보를 조회한다. "
            "캐시 → unit_price.csv 유사도 검색 순으로 처리. "
            "CSV 파일이 없어도 예외 없이 not_found를 반환."
        ),
        callable=search_product,
        input_schema=_SEARCH_PRODUCT_SCHEMA,
        output_contract="SearchProductResult",
        side_effects=False,
        idempotent=True,
    ),
    "confirm_mapping": ToolSpec(
        name="confirm_mapping",
        description=(
            "매핑 확정 결과를 캐시 CSV에 기록한다. "
            "mapping_type에 따라 ocr_retailer.csv / ocr_product.csv / ocr_dist.csv에 저장. "
            "upsert 방식이므로 같은 입력 반복 시 row가 증가하지 않는다."
        ),
        callable=confirm_mapping,
        input_schema=_CONFIRM_MAPPING_SCHEMA,
        output_contract="None",
        side_effects=True,
        idempotent=True,
    ),
}


# ── 헬퍼 함수 ─────────────────────────────────────────────────────────────────

def list_tools() -> list[ToolSpec]:
    """등록된 모든 Tool의 ToolSpec 목록을 반환한다.

    Returns:
        TOOL_REGISTRY에 등록된 순서대로 ToolSpec 목록
    """
    return list(TOOL_REGISTRY.values())


def get_tool(name: str) -> ToolSpec:
    """이름으로 Tool을 조회한다.

    Args:
        name: Tool 이름 (예: "lookup_retailer")

    Returns:
        ToolSpec

    Raises:
        KeyError: 등록되지 않은 Tool 이름
    """
    if name not in TOOL_REGISTRY:
        registered = sorted(TOOL_REGISTRY.keys())
        raise KeyError(
            f"Tool '{name}'이 registry에 없음. 등록된 Tool: {registered}"
        )
    return TOOL_REGISTRY[name]


def get_tool_schema(name: str) -> dict:
    """Tool의 input_schema (JSON Schema 형식)를 반환한다.

    Claude tool_use의 tools=[] 파라미터나 MCP schema 생성 시 사용.

    Args:
        name: Tool 이름

    Returns:
        JSON Schema dict

    Raises:
        KeyError: 등록되지 않은 Tool 이름
    """
    return get_tool(name).input_schema
