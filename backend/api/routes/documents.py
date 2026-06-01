"""문서 업로드 + 상태 조회 + SSE 스트리밍."""
import asyncio
import json
import re
import shutil
from pathlib import Path

import aiofiles
import bcrypt
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ...core.auth import get_current_user, get_current_user_sse
from ...core.config import get_settings, get_drive
from ...core.database import get_pool
from ...db.queries import get_document, list_documents
from ...pipeline.orchestrator import run_pipeline

router = APIRouter(prefix="/api/v3/documents", tags=["documents"])


async def _get_hatsu_month(doc_id: str) -> str:
    row = await get_pool().fetchrow("SELECT hatsu_month FROM v3_documents WHERE doc_id = $1", doc_id)
    return (row["hatsu_month"] or "") if row else ""


async def _ensure_pages_local(doc_id: str, pdf_filename: str) -> bool:
    """Restore {doc_id}_pages/ from Drive if missing locally. Returns True if available."""
    import logging
    log = logging.getLogger(__name__)
    settings = get_settings()
    pages_dir = settings.samples_dir / f"{doc_id}_pages"
    if pages_dir.exists() and any(pages_dir.glob("page_*.png")):
        return True

    drive = get_drive()
    if drive:
        hatsu_month = await _get_hatsu_month(doc_id)
        try:
            ok = await asyncio.to_thread(
                drive.pull_pages, settings.drive_root_folder_id, hatsu_month, doc_id, settings.samples_dir
            )
            if ok and pages_dir.exists() and any(pages_dir.glob("page_*.png")):
                return True
            if not ok:
                log.warning("[%s] Drive pages 복원 실패 — hatsu_month=%s", doc_id, hatsu_month)
        except Exception:
            log.exception("[%s] Drive pages 복원 중 예외", doc_id)

    # 폴백: Drive에 PNG가 없거나 pull 실패 → PDF에서 직접 재생성
    if not pdf_filename:
        row = await get_pool().fetchrow("SELECT pdf_filename FROM v3_documents WHERE doc_id = $1", doc_id)
        pdf_filename = row["pdf_filename"] if row else ""
    if pdf_filename:
        pdf_path = settings.samples_dir / pdf_filename
        if not pdf_path.exists() and drive:
            hatsu_month = await _get_hatsu_month(doc_id)
            await asyncio.to_thread(
                drive.pull_pdf, settings.drive_root_folder_id, hatsu_month, doc_id, pdf_filename, settings.samples_dir
            )
        if pdf_path.exists():
            from ...pipeline.ocr import _generate_page_images
            count = await asyncio.to_thread(_generate_page_images, pdf_path, pages_dir)
            if count > 0:
                log.info("[%s] PDF에서 PNG %d장 재생성", doc_id, count)
                return True
    log.error("[%s] pages 복원 불가 — Drive pull 실패, PDF도 없음", doc_id)
    return False


async def _ensure_pdf_local(doc_id: str, pdf_filename: str) -> bool:
    """Restore PDF from Drive if missing locally. Returns True if available."""
    settings = get_settings()
    if (settings.samples_dir / pdf_filename).exists():
        return True
    drive = get_drive()
    if not drive:
        return False
    hatsu_month = await _get_hatsu_month(doc_id)
    ok = await asyncio.to_thread(
        drive.pull_pdf, settings.drive_root_folder_id, hatsu_month, doc_id, pdf_filename, settings.samples_dir
    )
    return ok


async def _ensure_extracted_local(doc_id: str) -> bool:
    """Restore extracted/{doc_id}/ from Drive if missing locally. Returns True if available."""
    settings = get_settings()
    extracted = settings.extracted_dir / doc_id
    if extracted.exists() and any(extracted.iterdir()):
        return True
    drive = get_drive()
    if not drive:
        return False
    hatsu_month = await _get_hatsu_month(doc_id)
    ok = await asyncio.to_thread(
        drive.pull_extracted, settings.drive_root_folder_id, hatsu_month, doc_id, settings.extracted_dir
    )
    return ok

def _make_doc_id(stem: str) -> str:
    """파일명 stem → 안전한 doc_id. 공백·점·괄호를 정규화하고 [\w\-]만 남긴다."""
    s = re.sub(r"\s*\([^)]*\)", "", stem)   # 괄호·내용 제거: "foo (1)" → "foo"
    s = re.sub(r"[\s.]+", "_", s)           # 공백·점 → 언더스코어
    s = re.sub(r"[^\w\-]", "", s)           # 나머지 특수문자 제거
    s = re.sub(r"_+", "_", s).strip("_")   # 연속 언더스코어 정리
    return s


@router.post("")
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    hatsu_month: str = Form(""),
    user: dict = Depends(get_current_user),
):
    if not file.filename or not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="PDF 파일만 허용됩니다")

    doc_id = _make_doc_id(Path(file.filename).stem)
    if not doc_id:
        raise HTTPException(status_code=400, detail="파일명에서 유효한 doc_id를 만들 수 없습니다")

    settings = get_settings()
    pdf_path = settings.samples_dir / file.filename
    settings.samples_dir.mkdir(parents=True, exist_ok=True)

    async with aiofiles.open(pdf_path, "wb") as f:
        await f.write(await file.read())

    pool = get_pool()
    existing = await pool.fetchrow(
        "SELECT status FROM v3_documents WHERE doc_id = $1", doc_id
    )
    if existing:
        status = existing["status"]
        if status in ("queued", "ocr", "analyzing", "phase1", "phase2", "phase3", "phase4"):
            raise HTTPException(status_code=409, detail=f"이미 분석 중입니다 (상태: {status}). 완료 후 재시도하세요.")
        if status in ("pending", "done"):
            raise HTTPException(status_code=409, detail=f"이미 분석된 문서입니다 (상태: {status}). 재분석은 문서 상세에서 진행하세요.")
        # status == "error" → 재실행 허용: DB 초기화 후 낙하

    await pool.execute(
        """INSERT INTO v3_documents (doc_id, pdf_filename, status, hatsu_month, uploaded_by, analysis_started_at)
           VALUES ($1, $2, 'ocr', $3, $4, NOW())
           ON CONFLICT (doc_id) DO UPDATE
             SET status = 'ocr', error_type = NULL, error_phase = NULL,
                 error_message = NULL, form_id = NULL, token_usage = '{}',
                 hatsu_month = $3, updated_at = NOW(), analysis_started_at = NOW()""",
        doc_id, file.filename, hatsu_month or None, user["user_id"],
    )

    background_tasks.add_task(run_pipeline, doc_id, pdf_path, hatsu_month)
    return {"doc_id": doc_id, "status": "ocr"}


@router.get("")
async def list_docs(user: dict = Depends(get_current_user)):
    return await list_documents()


@router.get("/{doc_id}")
async def get_doc(doc_id: str, user: dict = Depends(get_current_user)):
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="문서 없음")
    settings = get_settings()
    pages_dir = settings.samples_dir / f"{doc_id}_pages"
    local_png_count = len(list(pages_dir.glob("page_*.png"))) if pages_dir.exists() else 0
    if local_png_count > 0:
        doc["pages_count"] = local_png_count
    # local_png_count == 0이면 DB의 pages_count 그대로 유지 (파일이 Drive에 있는 경우)
    return doc


def _resolve_page_file(pages_dir: Path, page: int, glob: str) -> Path | None:
    """pages_dir에서 page번 파일을 찾는다.
    1순위: page_{page:03d}.{ext} 직접 (mapping page_number 등 Azure 번호와 일치하는 경우)
    2순위: glob으로 정렬한 뒤 page번째 파일 (Azure가 비연속 번호 반환 시 폴백)
    """
    exact = pages_dir / glob.replace("*", f"{page:03d}")
    if exact.exists():
        return exact
    files = sorted(pages_dir.glob(glob))
    if 1 <= page <= len(files):
        return files[page - 1]
    return None


@router.get("/{doc_id}/page-image")
async def get_page_image(
    doc_id: str,
    page: int = Query(..., ge=1),
    user: dict = Depends(get_current_user_sse),
):
    """페이지 PNG 이미지 반환. ?sid= 쿼리 파라미터로 인증 (img src 용)."""
    settings = get_settings()
    pages_dir = settings.samples_dir / f"{doc_id}_pages"
    if not (pages_dir.exists() and any(pages_dir.glob("page_*.png"))):
        await _ensure_pages_local(doc_id, "")
    img_path = _resolve_page_file(pages_dir, page, "page_*.png")
    if not img_path:
        raise HTTPException(status_code=404, detail=f"페이지 {page} 이미지 없음")
    return FileResponse(str(img_path), media_type="image/png")


@router.get("/{doc_id}/page-bbox")
async def get_page_bbox(
    doc_id: str,
    page: int = Query(..., ge=1),
    user: dict = Depends(get_current_user),
):
    """페이지 line bbox JSON 반환."""
    settings = get_settings()
    pages_dir = settings.samples_dir / f"{doc_id}_pages"
    if not (pages_dir.exists() and any(pages_dir.glob("page_*.ocr.json"))):
        await _ensure_pages_local(doc_id, "")
    json_path = _resolve_page_file(pages_dir, page, "page_*.ocr.json")
    if not json_path:
        raise HTTPException(status_code=404, detail=f"페이지 {page} bbox 없음")
    import json as _json
    content = json_path.read_text(encoding="utf-8")
    if not content.strip():
        return {"page": page, "width": 0, "height": 0, "lines": []}
    return _json.loads(content)


@router.get("/{doc_id}/pdf")
async def get_pdf(doc_id: str, user: dict = Depends(get_current_user_sse)):
    """PDF 원본 파일 제공 — iframe 렌더링용. ?sid= 쿼리 파라미터로 인증."""
    pool = get_pool()
    row = await pool.fetchrow("SELECT pdf_filename FROM v3_documents WHERE doc_id = $1", doc_id)
    if not row:
        raise HTTPException(status_code=404, detail="문서 없음")
    settings = get_settings()
    pdf_path = settings.samples_dir / row["pdf_filename"]
    if not pdf_path.exists():
        await _ensure_pdf_local(doc_id, row["pdf_filename"])
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF 파일이 로컬·Drive 모두에 없습니다")
    return FileResponse(str(pdf_path), media_type="application/pdf")


@router.get("/{doc_id}/stream")
async def stream_status(doc_id: str, user: dict = Depends(get_current_user_sse)):
    """SSE — 파이프라인 진행 상태를 실시간으로 스트리밍."""

    async def event_gen():
        terminal = {"done", "error", "pending"}
        while True:
            doc = await get_document(doc_id)
            if not doc:
                yield _sse({"error": "문서 없음"})
                break

            payload = {
                "status": doc["status"],
                "form_id": doc.get("form_id"),
                "error_type": doc.get("error_type"),
                "error_phase": doc.get("error_phase"),
                "error_message": doc.get("error_message"),
            }
            yield _sse(payload)

            if doc["status"] in terminal:
                break
            await asyncio.sleep(2)

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/{doc_id}/results")
async def get_results(doc_id: str, user: dict = Depends(get_current_user)):
    """Phase 4 결과 반환. phase1_warnings가 있으면 함께 포함."""
    settings = get_settings()
    out_path = settings.extracted_dir / doc_id / "phase4_output.json"
    if not out_path.exists():
        await _ensure_extracted_local(doc_id)
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="결과 없음 — Phase 4 미완료")
    import json as _json
    result = _json.loads(out_path.read_text(encoding="utf-8"))

    warnings_path = settings.extracted_dir / doc_id / "phase1_warnings.json"
    if warnings_path.exists():
        result["phase1_warnings"] = _json.loads(warnings_path.read_text(encoding="utf-8"))

    p2_path = settings.extracted_dir / doc_id / "phase2_output.json"
    if p2_path.exists():
        p2 = _json.loads(p2_path.read_text(encoding="utf-8"))
        if "bundles" in p2:
            result["bundles"] = p2["bundles"]

    form_id = result.get("form_id", "")
    form_types_path = settings.workspace_root / "config" / "form_types.json"
    if form_types_path.exists():
        form_cfg = _json.loads(form_types_path.read_text(encoding="utf-8")).get(form_id, {})
        result["show_sections"] = form_cfg.get("show_sections", ["rate_summary", "xv", "retailer"])
        result["aggregate_label"] = form_cfg.get("aggregate_label", "소매처별 집계")

    return result


@router.post("/{doc_id}/backfill-images")
async def backfill_images(doc_id: str, user: dict = Depends(get_current_user)):
    """기존 문서에 PNG 페이지 이미지 생성 — Azure DI 재호출 없음.
    OCR은 이미 완료됐으나 .png가 없는 문서에 사용."""
    pool = get_pool()
    row = await pool.fetchrow("SELECT pdf_filename FROM v3_documents WHERE doc_id = $1", doc_id)
    if not row:
        raise HTTPException(status_code=404, detail="문서 없음")

    settings = get_settings()
    pdf_path = settings.samples_dir / row["pdf_filename"]
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF 파일 없음")

    pages_dir = settings.samples_dir / f"{doc_id}_pages"
    if not pages_dir.exists():
        raise HTTPException(status_code=400, detail="OCR 파일 없음 — 먼저 분석을 실행하세요")

    raw_path = pages_dir / "_azure_raw.json"
    if raw_path.exists():
        # raw 결과가 있으면 PNG + .ocr.json 모두 재생성
        import json as _json
        from ...pipeline.ocr import _write_page_files
        result_json = _json.loads(raw_path.read_text(encoding="utf-8"))
        await asyncio.to_thread(_write_page_files, result_json, pages_dir, pdf_path)
        count = len(list(pages_dir.glob("page_*.png")))
    else:
        # raw 없으면 PNG만 생성
        from ...pipeline.ocr import _generate_page_images
        count = await asyncio.to_thread(_generate_page_images, pdf_path, pages_dir)
    return {"doc_id": doc_id, "pages_generated": count}


@router.post("/{doc_id}/retry")
async def retry_document(
    doc_id: str,
    background_tasks: BackgroundTasks,
    force: bool = False,
    user: dict = Depends(get_current_user),
):
    """문서 재분석.
    - 기본(force=false): OCR 캐시 유지, Phase 2+ 재실행. error/done/pending 모두 가능.
    - force=true: OCR 캐시 포함 전체 초기화 후 재수행 (Azure DI 재호출 → 비용 발생)."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT pdf_filename, status, hatsu_month FROM v3_documents WHERE doc_id = $1", doc_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="문서 없음")
    if row["status"] in ("queued", "ocr", "analyzing", "phase1", "phase2", "phase3", "phase4"):
        raise HTTPException(status_code=400, detail="현재 분석 중인 문서입니다. 완료 후 재분석하세요.")

    settings = get_settings()
    pdf_path = settings.samples_dir / row["pdf_filename"]
    if not pdf_path.exists():
        await _ensure_pdf_local(doc_id, row["pdf_filename"])
    if not pdf_path.exists():
        raise HTTPException(
            status_code=404,
            detail="PDF 파일이 서버·Drive 모두에 없습니다. 파일을 다시 업로드하세요.",
        )

    extracted_dir = settings.extracted_dir / doc_id
    pages_dir = settings.samples_dir / f"{doc_id}_pages"

    # OCR 캐시 유지 재분석 시 pages_dir이 Drive에만 있으면 복원
    if not force and not pages_dir.exists():
        await _ensure_pages_local(doc_id, row["pdf_filename"])

    if force:
        # OCR 캐시 포함 전체 초기화
        import shutil
        if pages_dir.exists():
            shutil.rmtree(pages_dir)
        if extracted_dir.exists():
            shutil.rmtree(extracted_dir)
    else:
        # Phase 2+ 산출물만 삭제 (OCR + Phase 1 MD 유지 — 재과금 방지)
        if extracted_dir.exists():
            for fname in ("phase2_output.json", "phase3_output.json", "phase4_output.json"):
                p = extracted_dir / fname
                if p.exists():
                    p.unlink()
            for f in extracted_dir.glob("phase2_partial_*.json"):
                f.unlink()

    # 미확정 매핑 초기화
    await pool.execute("DELETE FROM v3_mappings WHERE doc_id = $1", doc_id)

    # 상태 + 토큰 사용량 초기화
    await pool.execute(
        """UPDATE v3_documents
           SET status = 'ocr', error_type = NULL, error_phase = NULL,
               error_message = NULL, token_usage = '{}', updated_at = NOW(),
               analysis_started_at = NOW()
           WHERE doc_id = $1""",
        doc_id,
    )

    background_tasks.add_task(run_pipeline, doc_id, pdf_path, row["hatsu_month"] or "")
    return {"doc_id": doc_id, "status": "ocr"}


class DeleteBody(BaseModel):
    password: str


@router.delete("/{doc_id}")
async def delete_document(doc_id: str, body: DeleteBody, user: dict = Depends(get_current_user)):
    """문서 삭제 — 본인 업로드 문서만 가능, 비밀번호 확인 필수."""
    pool = get_pool()

    row = await pool.fetchrow(
        "SELECT pdf_filename, status, uploaded_by, hatsu_month FROM v3_documents WHERE doc_id = $1", doc_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="문서 없음")

    if not user.get("is_admin") and row["uploaded_by"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="본인이 업로드한 문서만 삭제할 수 있습니다")

    if row["status"] in ("queued", "ocr", "analyzing", "phase1", "phase2", "phase3", "phase4"):
        raise HTTPException(status_code=400, detail="분석 중인 문서는 삭제할 수 없습니다")

    pw_row = await pool.fetchrow("SELECT password_hash FROM users WHERE user_id = $1", user["user_id"])
    if not pw_row or not bcrypt.checkpw(body.password.encode()[:72], pw_row["password_hash"].encode()):
        raise HTTPException(status_code=400, detail="비밀번호가 올바르지 않습니다")

    # DB 삭제
    await pool.execute("DELETE FROM v3_reviews  WHERE doc_id = $1", doc_id)
    await pool.execute("DELETE FROM v3_mappings WHERE doc_id = $1", doc_id)
    await pool.execute("DELETE FROM v3_documents WHERE doc_id = $1", doc_id)

    # 로컬 파일 삭제
    settings = get_settings()
    pdf_path   = settings.samples_dir / row["pdf_filename"]
    pages_dir  = settings.samples_dir / f"{doc_id}_pages"
    extracted  = settings.extracted_dir / doc_id

    if pdf_path.exists():
        pdf_path.unlink()
    if pages_dir.exists():
        shutil.rmtree(pages_dir)
    if extracted.exists():
        shutil.rmtree(extracted)

    # Drive 파일 삭제 (실패해도 삭제 자체는 성공 처리)
    drive = get_drive()
    if drive:
        try:
            await asyncio.to_thread(
                drive.delete_doc,
                settings.drive_root_folder_id,
                row["hatsu_month"] or "",
                doc_id,
                row["pdf_filename"],
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception("[%s] Drive 삭제 실패 (무시)", doc_id)

    return {"ok": True}


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
