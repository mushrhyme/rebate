"""Phase 3 매핑 확인 — 저신뢰도 항목 조회 및 확정."""
import json as _json
import logging
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

log = logging.getLogger(__name__)

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
    dist_form_id = dist_issuer_fp = dist_rc = ""

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
        dist_form_id, dist_issuer_fp, rc_map = _dist_context(doc_id, settings)
        dist_rc = rc_map.get(ocr_name, "")
        if dist_rc:
            _upsert_dist_cache_row(md / "ocr_dist.csv", dist_form_id, dist_issuer_fp, dist_rc, confirmed_code, confirmed_name)

    # Sheets write (primary cache for new documents)
    try:
        from ...core.sheets_store import get_sheets_store
        store = get_sheets_store()
        if store is None:
            return
        # upsert: 같은 키를 재수정할 때 append로 모순 행이 누적되지 않도록
        if mapping_type == "retailer":
            store.upsert_row("ocr_retailer.csv", [0], [ocr_name, confirmed_code, confirmed_name])
        elif mapping_type == "product":
            store.upsert_row("ocr_product.csv", [0], [ocr_name, confirmed_code, confirmed_name])
        elif mapping_type == "dist" and dist_rc and dist_form_id and dist_issuer_fp:
            # ocr_dist 키에 jisho 컬럼(4번째)이 추가됨. 결과화면 remap은 소매처 단위라
            # jisho="" 로 기록한다(레거시 granularity). 컬럼 정합성 유지가 목적.
            store.upsert_row(
                "ocr_dist.csv", [0, 1, 2, 3],
                [dist_form_id, dist_issuer_fp, dist_rc, "", confirmed_code, confirmed_name],
            )
    except Exception:
        # Sheets는 신규 문서의 1차 캐시 — 기록 실패가 묻히면 다음 분석부터 같은
        # 매핑을 다시 묻게 되므로 반드시 로그를 남긴다 (파이프라인은 계속 진행)
        log.warning(
            "[%s] Sheets 캐시 기록 실패 — %s '%s' → '%s' (로컬 캐시만 갱신됨)",
            doc_id, mapping_type, ocr_name, confirmed_code, exc_info=True,
        )



@router.get("")
async def get_mappings(doc_id: str, user: dict = Depends(get_current_user)):
    """매핑 항목 전체 목록 (확정 여부 무관)."""
    return await get_all_mappings(doc_id)


@router.post("/confirm")
async def confirm_one(doc_id: str, body: ConfirmBody, user: dict = Depends(get_current_user)):
    """항목 하나 확정. CSV 캐시는 Phase 4 시작 시(_merge_confirmed_mappings) 일괄 기록."""
    await confirm_mapping(doc_id, body.mapping_id, body.confirmed_code, body.confirmed_name, user["user_id"])
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
