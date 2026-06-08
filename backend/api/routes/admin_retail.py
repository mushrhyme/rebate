"""Admin — 소매처 담당자 배정 관리 (Google Sheets retail_user 탭)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...core.auth import require_admin
from ...core.sheets_store import get_sheets_store

router = APIRouter(prefix="/api/admin/retail-assignment", tags=["admin-retail"])

_CSV = "retail_user.csv"


def _get_store():
    store = get_sheets_store()
    if store is None:
        raise HTTPException(status_code=503, detail="Google Sheets 연동이 설정되지 않았습니다.")
    return store


@router.get("")
async def get_assignments(user=Depends(require_admin)):
    store = _get_store()
    rows = store.read_csv(_CSV)

    reps: dict[str, dict] = {}
    for row in rows:
        rep_id = row.get("담당자ID", "") or ""
        rep_name = row.get("담당자명", "") or ""
        system_id = row.get("ID", "") or ""
        key = rep_id if rep_id else "__unassigned__"

        if key not in reps:
            reps[key] = {
                "rep_id": rep_id,
                "rep_name": rep_name,
                "system_id": system_id,
                "retailers": [],
            }
        reps[key]["retailers"].append({
            "retailer_code": row.get("소매처코드", ""),
            "retailer_name": row.get("소매처명", ""),
            "dist_code": row.get("판매처코드", ""),
            "dist_name": row.get("판매처명", ""),
        })

    return {
        "reps": sorted(reps.values(), key=lambda r: r["rep_name"]),
        "total_retailers": len(rows),
    }


class PatchAssignment(BaseModel):
    retailer_codes: list[str]
    new_rep_id: str
    new_rep_name: str
    new_system_id: str


@router.patch("")
async def patch_assignment(
    body: PatchAssignment,
    user=Depends(require_admin),
):
    store = _get_store()
    rows = store.read_csv(_CSV)
    if not rows:
        raise HTTPException(status_code=404, detail="retail_user 데이터가 없습니다.")

    fieldnames = list(rows[0].keys())
    code_set = set(body.retailer_codes)
    updated = 0
    for row in rows:
        if row.get("소매처코드") in code_set:
            row["담당자ID"] = body.new_rep_id
            row["담당자명"] = body.new_rep_name
            row["ID"] = body.new_system_id
            updated += 1

    store.write_all(_CSV, rows, fieldnames)
    return {"ok": True, "updated": updated}
