"""phase3_tool_result_adapter.py — Tool Use 결과를 phase3 출력 구조로 변환

순수 변환 함수. 파일 I/O 없음, confirm_mapping 호출 없음.

Tool Use path가 성공한 결과를 legacy run_phase3()와 동일한
result/pending 구조로 변환해 downstream consumer(phase4, DB 저장, UI)가
동일하게 처리할 수 있도록 한다.

## 기존 phase3 출력 구조 (contract)

result dict:
  doc_id              str
  form_id             str
  hatsu_month         str
  issuer              dict
  confirmed_retailers {ocr_name: {"retailer_code": str, "dist_code": str, "basis": str}}
  confirmed_products  {ocr_name: {"code": str, "master_name": str, "basis": str}}
  items               list[dict]  — _apply_mappings() 적용 후
  cover_totals        dict

pending list:
  [{"mapping_type": str, "ocrName": str, "candidates": list, "page_number": int|None}]

## Tool Use 결과 구조 (입력)

RetailerMappingDecision:
  ocr_name, retailer_code, dist_code, basis, confidence, candidates, page_number, error

ProductMappingDecision:
  ocr_name, product_code, product_name, basis, confidence, page_number, error

## basis 값 매핑

Tool Use basis       → phase3 result basis
"cache"              → "cache"         (lookup_retailer 캐시 히트)
"bracket_code"       → "bracket_code"  (괄호코드 직접 매칭)
"tool_use"           → "tool_use"      (Claude tool_use 결정)
"not_found"          → pending
"error"              → pending

## 주의: confirm_mapping 호출 타이밍

이 adapter는 변환만 수행한다.
confirm_mapping() 호출은 caller(fallback 래퍼 또는 orchestrator)가 담당한다.
"""
from dataclasses import dataclass, field
from typing import Any

from .phase3 import _apply_mappings, _extract_cover_totals

__all__ = [
    "RetailerMappingDecision",
    "ProductMappingDecision",
    "ToolUseContractError",
    "convert_tool_use_result_to_phase3_output",
    "retailer_decision_from_lookup_result",
    "product_decision_from_search_result",
]

# ── Contract 예외 ─────────────────────────────────────────────────────────────

class ToolUseContractError(ValueError):
    """Tool Use 결과가 phase3 출력 contract를 위반할 때 발생.

    basis 값 범위 위반, confirmed 결과에 code 없음 등.
    """


# ── 입력 타입 정의 ────────────────────────────────────────────────────────────

_RETAILER_MAPPED_BASES  = frozenset({"cache", "bracket_code", "exact_match", "tool_use"})
_RETAILER_UNMAPPED_BASES = frozenset({"not_found", "error"})
_RETAILER_VALID_BASES    = _RETAILER_MAPPED_BASES | _RETAILER_UNMAPPED_BASES

_PRODUCT_MAPPED_BASES   = frozenset({"cache", "tool_use"})
_PRODUCT_UNMAPPED_BASES = frozenset({"not_found", "error"})
_PRODUCT_VALID_BASES    = _PRODUCT_MAPPED_BASES | _PRODUCT_UNMAPPED_BASES


@dataclass
class RetailerMappingDecision:
    """Tool Use runtime에서 결정된 단일 retailer 매핑.

    basis 값:
      "cache"        — ocr_retailer.csv 캐시 히트
      "bracket_code" — domae_retail CSV 괄호코드 직접 매칭
      "tool_use"     — Claude tool_use 루프가 결정한 코드
      "not_found"    — 조회 불가 (retailer_code=None → pending)
      "error"        — 실행 오류 (retailer_code=None → pending)
    """
    ocr_name:      str
    retailer_code: str | None          # mapped이면 non-None
    dist_code:     str                 # 판매처코드 (없으면 "")
    basis:         str
    confidence:    float               # [0.0, 1.0]
    candidates:    list[dict] = field(default_factory=list)
    page_number:   int | None = None
    error:         str | None = None


@dataclass
class ProductMappingDecision:
    """Tool Use runtime에서 결정된 단일 product 매핑.

    basis 값:
      "cache"    — ocr_product.csv 캐시 히트
      "tool_use" — Claude tool_use 또는 search_product 결과 기반 결정
      "not_found" — 조회 불가 (product_code=None → pending)
      "error"    — 실행 오류
    """
    ocr_name:     str
    product_code: str | None
    product_name: str
    basis:        str
    confidence:   float
    page_number:  int | None = None
    error:        str | None = None


# ── Contract 검증 ─────────────────────────────────────────────────────────────

def _validate_retailer(d: RetailerMappingDecision) -> None:
    if d.basis not in _RETAILER_VALID_BASES:
        raise ToolUseContractError(
            f"retailer '{d.ocr_name}': 알 수 없는 basis {d.basis!r}. "
            f"허용값: {sorted(_RETAILER_VALID_BASES)}"
        )
    if d.basis in _RETAILER_MAPPED_BASES and d.retailer_code is None:
        raise ToolUseContractError(
            f"retailer '{d.ocr_name}': basis={d.basis!r}이지만 retailer_code가 None"
        )
    if not (0.0 <= d.confidence <= 1.0):
        raise ToolUseContractError(
            f"retailer '{d.ocr_name}': confidence={d.confidence}이 [0,1] 범위 밖"
        )


def _validate_product(d: ProductMappingDecision) -> None:
    if d.basis not in _PRODUCT_VALID_BASES:
        raise ToolUseContractError(
            f"product '{d.ocr_name}': 알 수 없는 basis {d.basis!r}. "
            f"허용값: {sorted(_PRODUCT_VALID_BASES)}"
        )
    if d.basis in _PRODUCT_MAPPED_BASES and d.product_code is None:
        raise ToolUseContractError(
            f"product '{d.ocr_name}': basis={d.basis!r}이지만 product_code가 None"
        )
    if not (0.0 <= d.confidence <= 1.0):
        raise ToolUseContractError(
            f"product '{d.ocr_name}': confidence={d.confidence}이 [0,1] 범위 밖"
        )


# ── 편의 생성자 ───────────────────────────────────────────────────────────────

def retailer_decision_from_lookup_result(
    ocr_name: str,
    lookup_result: Any,  # LookupRetailerResult
    dist_code: str = "",
    page_number: int | None = None,
    claude_decided_code: str | None = None,
    dist_resolution: Any = None,   # DistResolution (선택 — 제공 시 dist_code 자동 채움)
) -> RetailerMappingDecision:
    """LookupRetailerResult → RetailerMappingDecision 변환 헬퍼.

    Args:
        lookup_result:       LookupRetailerResult 인스턴스
        dist_code:           판매처코드 (별도 조회 결과)
        claude_decided_code: lookup이 candidate를 반환했지만 Claude가 선택한 코드
    """
    basis         = getattr(lookup_result, "basis", "not_found")
    retailer_code = getattr(lookup_result, "retailer_code", None)
    confidence    = getattr(lookup_result, "confidence", 0.0)
    candidates    = list(getattr(lookup_result, "candidates", []))

    # DistResolution이 제공된 경우 dist_code 자동 채움 (1:1 자동 확정만)
    if dist_resolution is not None:
        resolved_dist = getattr(dist_resolution, "dist_code", None)
        if resolved_dist:
            dist_code = resolved_dist

    # candidate인데 Claude가 결정을 내린 경우
    if basis == "candidate" and claude_decided_code:
        return RetailerMappingDecision(
            ocr_name=ocr_name,
            retailer_code=claude_decided_code,
            dist_code=dist_code,
            basis="tool_use",
            confidence=confidence,
            candidates=candidates,
            page_number=page_number,
        )

    return RetailerMappingDecision(
        ocr_name=ocr_name,
        retailer_code=retailer_code,
        dist_code=dist_code,
        basis=basis if basis in _RETAILER_VALID_BASES else "not_found",
        confidence=confidence,
        candidates=candidates,
        page_number=page_number,
    )


def product_decision_from_search_result(
    ocr_name: str,
    search_result: Any,  # SearchProductResult
    page_number: int | None = None,
    claude_decided_code: str | None = None,
    claude_decided_name: str = "",
) -> ProductMappingDecision:
    """SearchProductResult → ProductMappingDecision 변환 헬퍼."""
    basis        = getattr(search_result, "basis", "not_found")
    product_code = getattr(search_result, "product_code", None)
    confidence   = getattr(search_result, "confidence", 0.0)

    if basis == "candidate" and claude_decided_code:
        return ProductMappingDecision(
            ocr_name=ocr_name,
            product_code=claude_decided_code,
            product_name=claude_decided_name,
            basis="tool_use",
            confidence=confidence,
            page_number=page_number,
        )

    product_name = ""
    candidates = getattr(search_result, "candidates", [])
    if candidates:
        product_name = candidates[0].get("product_name", "") if isinstance(candidates[0], dict) else ""

    return ProductMappingDecision(
        ocr_name=ocr_name,
        product_code=product_code,
        product_name=product_name,
        basis=basis if basis in _PRODUCT_VALID_BASES else "not_found",
        confidence=confidence,
        page_number=page_number,
    )


# ── 핵심 변환 함수 ─────────────────────────────────────────────────────────────

def convert_tool_use_result_to_phase3_output(
    *,
    doc_id: str,
    form_id: str,
    hatsu_month: str,
    issuer: dict,
    phase2_result: dict,
    retailer_decisions: list[RetailerMappingDecision],
    product_decisions: list[ProductMappingDecision],
) -> tuple[dict, list[dict]]:
    """Tool Use 매핑 결정 목록을 phase3 result / pending 구조로 변환한다.

    Side-effect 금지:
      - confirm_mapping() 호출 없음
      - 파일 I/O 없음
      - DB 접근 없음
      - 순수 변환 함수

    Args:
        doc_id, form_id, hatsu_month, issuer:
            phase3 result에 그대로 포함될 메타데이터.
        phase2_result:
            Phase 2 출력 dict. items[]와 cover_totals 계산에 사용.
        retailer_decisions:
            소매처 매핑 결정 목록. validated 후 confirmed/pending으로 분류.
        product_decisions:
            제품 매핑 결정 목록.

    Returns:
        (result, pending) — run_phase3()와 동일한 반환 형식.

    Raises:
        ToolUseContractError: basis 범위 위반, mapped basis에 code=None 등.
    """
    # ── 1. Validate all inputs ────────────────────────────────────────────────
    for r in retailer_decisions:
        _validate_retailer(r)
    for p in product_decisions:
        _validate_product(p)

    # ── 2. Retailer: confirmed / pending 분류 ─────────────────────────────────
    confirmed_retailers: dict[str, dict] = {}
    pending: list[dict] = []

    for r in retailer_decisions:
        if r.basis in _RETAILER_MAPPED_BASES:
            confirmed_retailers[r.ocr_name] = {
                "retailer_code": r.retailer_code,
                "dist_code":     r.dist_code,
                "basis":         r.basis,
            }
        else:
            # not_found / error → pending (사용자 확인 필요)
            pending.append({
                "mapping_type": "retailer",
                "ocrName":      r.ocr_name,
                "candidates":   r.candidates,
                "page_number":  r.page_number,
            })

    # ── 3. Product: confirmed / pending 분류 ─────────────────────────────────
    confirmed_products: dict[str, dict] = {}

    for p in product_decisions:
        if p.basis in _PRODUCT_MAPPED_BASES:
            confirmed_products[p.ocr_name] = {
                "code":        p.product_code,
                "master_name": p.product_name,
                "basis":       p.basis,
            }
        else:
            pending.append({
                "mapping_type": "product",
                "ocrName":      p.ocr_name,
                "candidates":   [],
                "page_number":  p.page_number,
            })

    # ── 4. 아이템에 코드 적용 (legacy와 동일 로직) ────────────────────────────
    items = phase2_result.get("items", [])
    items_out = _apply_mappings(items, confirmed_retailers, confirmed_products)

    # ── 5. result dict 조립 (legacy run_phase3()와 동일한 key 구조) ───────────
    result: dict[str, Any] = {
        "doc_id":              doc_id,
        "form_id":             form_id,
        "hatsu_month":         hatsu_month,
        "issuer":              issuer,
        "confirmed_retailers": confirmed_retailers,
        "confirmed_products":  confirmed_products,
        "items":               items_out,
        "cover_totals":        _extract_cover_totals(phase2_result),
    }

    return result, pending
