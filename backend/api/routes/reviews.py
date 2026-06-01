"""검토 체크 API — 소매처 그룹 단위 1차/2차 확인."""
import csv
import json as _json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...core.auth import get_current_user, require_admin
from ...core.config import get_settings
from ...db.queries import (
    delete_review,
    get_document,
    get_document_confirmed,
    get_reviews,
    set_confirmed,
    unset_confirmed,
    upsert_review,
)

router = APIRouter(prefix="/api/v3", tags=["reviews"])

_LOCKED_MSG = "확정된 문서입니다. 영업추진부에 확정 해제를 요청하세요"


class ReviewIn(BaseModel):
    retailer_code: str
    review_type: str  # "1차" | "2차"


async def _try_auto_confirm(doc_id: str) -> bool:
    """모든 소매처 그룹의 1차+2차가 완료되면 자동 확정. 확정 시 True 반환."""
    settings = get_settings()
    out_path: Path = settings.extracted_dir / doc_id / "phase4_output.json"
    if not out_path.exists():
        return False

    rows = _json.loads(out_path.read_text(encoding="utf-8")).get("rows", [])
    # 代表スーパー가 있는 행만 대상 — 빈 값(消費税計上 등 비소매처 행) 제외
    retailer_codes = {r["代表スーパー"] for r in rows if r.get("代表スーパー")}
    if not retailer_codes:
        return False

    reviews = await get_reviews(doc_id)
    reviewed_pairs = {(r["retailer_code"], r["review_type"]) for r in reviews}
    for code in retailer_codes:
        if (code, "1차") not in reviewed_pairs or (code, "2차") not in reviewed_pairs:
            return False

    await set_confirmed(doc_id)
    return True


@router.get("/documents/{doc_id}/reviews")
async def list_reviews(doc_id: str, user: dict = Depends(get_current_user)):
    """문서의 모든 검토 상태 반환."""
    return await get_reviews(doc_id)


@router.patch("/documents/{doc_id}/review")
async def mark_reviewed(
    doc_id: str,
    body: ReviewIn,
    user: dict = Depends(get_current_user),
):
    """소매처 그룹 1차/2차 검토 완료 표시 (upsert). 확정된 문서는 변경 불가."""
    if body.review_type not in ("1차", "2차"):
        raise HTTPException(status_code=400, detail="review_type은 '1차' 또는 '2차'만 허용됩니다")
    if await get_document_confirmed(doc_id):
        raise HTTPException(status_code=423, detail=_LOCKED_MSG)

    result = await upsert_review(doc_id, body.retailer_code, body.review_type, user["user_id"])
    doc_confirmed = await _try_auto_confirm(doc_id)
    return {**result, "doc_confirmed": doc_confirmed}


@router.delete("/documents/{doc_id}/review")
async def unmark_reviewed(
    doc_id: str,
    body: ReviewIn,
    user: dict = Depends(get_current_user),
):
    """검토 완료 해제 — 본인이 체크한 항목만 취소 가능. 확정된 문서는 변경 불가."""
    if await get_document_confirmed(doc_id):
        raise HTTPException(status_code=423, detail=_LOCKED_MSG)

    result = await delete_review(doc_id, body.retailer_code, body.review_type, user["user_id"])
    if result == "not_found":
        raise HTTPException(status_code=404, detail="검토 기록 없음")
    if result == "not_owner":
        raise HTTPException(status_code=403, detail="본인의 검토만 취소할 수 있습니다")
    return {"ok": True}


@router.post("/documents/{doc_id}/recheck-confirm")
async def recheck_confirm(
    doc_id: str,
    user: dict = Depends(get_current_user),
):
    """확정 조건 재평가 — 이미 저장된 체크가 모두 완료됐는데 확정이 안 된 경우 수동 트리거."""
    if await get_document_confirmed(doc_id):
        return {"doc_confirmed": True, "message": "이미 확정된 문서입니다"}
    confirmed = await _try_auto_confirm(doc_id)
    return {"doc_confirmed": confirmed}


@router.post("/documents/{doc_id}/unconfirm")
async def unconfirm_document(
    doc_id: str,
    user: dict = Depends(get_current_user),
):
    """문서 확정 해제 — 본인 업로드 문서 또는 관리자. 체크 상태 유지, 잠금만 해제."""
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="문서 없음")
    if not doc.get("confirmed_at"):
        raise HTTPException(status_code=400, detail="확정되지 않은 문서입니다")
    if doc.get("uploaded_by") != user["user_id"] and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="본인이 업로드한 문서만 확정 취소할 수 있습니다")
    await unset_confirmed(doc_id)
    return {"ok": True}


@router.get("/retailers/my")
async def my_retailers(user: dict = Depends(get_current_user)):
    """로그인 사용자가 담당하는 소매처 목록.
    retail_user.csv의 ID 컬럼과 username을 대조해 반환한다."""
    settings = get_settings()
    csv_path: Path = settings.mappings_dir / "retail_user.csv"
    if not csv_path.exists():
        return []

    username = user["username"]
    result: list[dict] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("ID", "").strip() == username:
                result.append({
                    "retailer_code": row["소매처코드"],
                    "retailer_name": row["소매처명"],
                    "dist_code":     row.get("판매처코드", ""),
                    "dist_name":     row.get("판매처명", ""),
                })
    return result
