"""Phase 3 매핑 확인 — 저신뢰도 항목 조회 및 확정."""
import json as _json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...core.auth import get_current_user
from ...core.config import get_settings
from ...db.queries import (
    confirm_mapping,
    get_all_mappings,
    get_document_confirmed,
    upsert_remap_mapping,
)
from ...pipeline.orchestrator import resume_phase4, resume_phase4_for_remap
from ...pipeline.phase3 import (
    _build_issuer_fingerprint,
    _parse_fingerprint_fields,
)
from ...tools.mapping import (
    _upsert_cache_row,
    _upsert_dist_cache_row,
)

_LOCKED_MSG = "확정된 문서입니다. 영업추진부에 확정 해제를 요청하세요"

router = APIRouter(prefix="/api/v3/documents/{doc_id}/mappings", tags=["mappings"])


class ConfirmBody(BaseModel):
    mapping_id: int
    confirmed_code: str
    confirmed_name: str


def _dist_context(doc_id: str, settings) -> tuple[str, str, dict[str, str]]:
    """phase3_output.json에서 dist 캐시 키 컴포넌트 반환.
    반환: (form_id, issuer_fingerprint, {ocr_name: retailer_code})
    """
    p3_path = settings.extracted_dir / doc_id / "phase3_output.json"
    if not p3_path.exists():
        return "", "", {}
    p3 = _json.loads(p3_path.read_text(encoding="utf-8"))
    form_id = p3.get("form_id", "")
    issuer = p3.get("issuer", {})
    form_path = settings.form_definitions_dir / f"{form_id}.md"
    form_md = form_path.read_text(encoding="utf-8") if form_path.exists() else ""
    issuer_fp = _build_issuer_fingerprint(issuer, _parse_fingerprint_fields(form_md))
    rc_map = {
        name: info.get("retailer_code", "")
        for name, info in p3.get("confirmed_retailers", {}).items()
    }
    return form_id, issuer_fp, rc_map


def _upsert_cache(doc_id: str, mapping_type: str, ocr_name: str, confirmed_code: str, confirmed_name: str = "") -> None:
    """remap 시 캐시에 덮어쓰기 (잘못된 값 교정용)."""
    settings = get_settings()
    md = settings.mappings_dir
    if mapping_type == "retailer":
        _upsert_cache_row(
            md / "ocr_retailer.csv", "ocr_name",
            ["ocr_name", "retailer_code", "retailer_name"],
            ocr_name, [ocr_name, confirmed_code, confirmed_name],
        )
    elif mapping_type == "product":
        _upsert_cache_row(
            md / "ocr_product.csv", "ocr_name",
            ["ocr_name", "product_code", "product_name"],
            ocr_name, [ocr_name, confirmed_code, confirmed_name],
        )
    elif mapping_type == "dist":
        form_id, issuer_fp, rc_map = _dist_context(doc_id, settings)
        rc = rc_map.get(ocr_name, "")
        if rc:
            _upsert_dist_cache_row(md / "ocr_dist.csv", form_id, issuer_fp, rc, confirmed_code, confirmed_name)



@router.get("")
async def get_mappings(doc_id: str, user: dict = Depends(get_current_user)):
    """매핑 항목 전체 목록 (확정 여부 무관)."""
    return await get_all_mappings(doc_id)


@router.post("/confirm")
async def confirm_one(doc_id: str, body: ConfirmBody, user: dict = Depends(get_current_user)):
    """항목 하나 확정. CSV 캐시는 Phase 4 시작 시(_merge_confirmed_mappings) 일괄 기록."""
    await confirm_mapping(body.mapping_id, body.confirmed_code, body.confirmed_name, user["user_id"])
    return {"ok": True}


class RemapRetailerBody(BaseModel):
    ocr_name: str
    retailer_code: str
    retailer_name: str


@router.post("/remap-retailer")
async def remap_retailer(doc_id: str, body: RemapRetailerBody, user: dict = Depends(get_current_user)):
    """결과 화면에서 소매처 매핑 수정 → 캐시 교정 + Phase 4 재실행."""
    if await get_document_confirmed(doc_id):
        raise HTTPException(status_code=423, detail=_LOCKED_MSG)
    await upsert_remap_mapping(
        doc_id, "retailer", body.ocr_name,
        body.retailer_code, body.retailer_name, user["user_id"],
    )
    _upsert_cache(doc_id, "retailer", body.ocr_name, body.retailer_code, body.retailer_name)
    await resume_phase4_for_remap(doc_id)
    return {"ok": True, "status": "analyzing"}


class RemapDistBody(BaseModel):
    ocr_name: str
    dist_code: str
    dist_name: str


@router.post("/remap-dist")
async def remap_dist(doc_id: str, body: RemapDistBody, user: dict = Depends(get_current_user)):
    """결과 화면에서 판매처 매핑 수정 → 캐시 교정 + Phase 4 재실행."""
    if await get_document_confirmed(doc_id):
        raise HTTPException(status_code=423, detail=_LOCKED_MSG)
    await upsert_remap_mapping(
        doc_id, "dist", body.ocr_name,
        body.dist_code, body.dist_name, user["user_id"],
    )
    _upsert_cache(doc_id, "dist", body.ocr_name, body.dist_code, body.dist_name)
    await resume_phase4_for_remap(doc_id)
    return {"ok": True, "status": "analyzing"}


class RemapProductBody(BaseModel):
    ocr_name: str
    product_code: str
    product_name: str


@router.post("/remap-product")
async def remap_product(doc_id: str, body: RemapProductBody, user: dict = Depends(get_current_user)):
    """결과 화면에서 제품 매핑 수정 → 캐시 교정 + Phase 4 재실행."""
    if await get_document_confirmed(doc_id):
        raise HTTPException(status_code=423, detail=_LOCKED_MSG)
    await upsert_remap_mapping(
        doc_id, "product", body.ocr_name,
        body.product_code, body.product_name, user["user_id"],
    )
    _upsert_cache(doc_id, "product", body.ocr_name, body.product_code, body.product_name)
    await resume_phase4_for_remap(doc_id)
    return {"ok": True, "status": "analyzing"}


@router.post("/confirm-all")
async def confirm_all_and_run_phase4(doc_id: str, user: dict = Depends(get_current_user)):
    """Phase 4 실행. 프론트엔드가 모든 항목 저장 완료를 보장한 후 호출."""
    await resume_phase4(doc_id)
    return {"ok": True, "status": "analyzing"}
