"""Admin — 소매처 담당자 배정 관리 (retail_user.csv 직접 읽기/쓰기)."""
import csv
import os
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ...core.auth import require_admin
from ...core.config import get_settings

router = APIRouter(prefix="/api/admin/retail-assignment", tags=["admin-retail"])


def _read_csv(path: Path) -> tuple[list[dict], list[str]]:
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    return rows, list(fieldnames)


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


@router.get("")
async def get_assignments(user=Depends(require_admin), settings=Depends(get_settings)):
    csv_path = settings.mappings_dir / "retail_user.csv"
    rows, _ = _read_csv(csv_path)

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

    rep_list = sorted(reps.values(), key=lambda r: r["rep_name"])
    return {
        "reps": rep_list,
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
    settings=Depends(get_settings),
):
    csv_path = settings.mappings_dir / "retail_user.csv"
    rows, fieldnames = _read_csv(csv_path)

    code_set = set(body.retailer_codes)
    updated = 0
    for row in rows:
        if row.get("소매처코드") in code_set:
            row["담당자ID"] = body.new_rep_id
            row["담당자명"] = body.new_rep_name
            row["ID"] = body.new_system_id
            updated += 1

    _write_csv(csv_path, rows, fieldnames)
    return {"ok": True, "updated": updated}
