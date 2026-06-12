"""문서 업로드 + 상태 조회 + SSE 스트리밍."""
import asyncio
import json
import logging
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

log = logging.getLogger(__name__)
from ...db.queries import (
    create_document,
    clear_mappings,
    delete_document_data,
    get_document,
    get_user_password_hash,
    list_documents,
    reset_document_for_retry,
    update_document_error,
)
from ...pipeline.orchestrator import run_pipeline, resume_phase3_with_cache

router = APIRouter(prefix="/api/v3/documents", tags=["documents"])


async def _get_hatsu_month(doc_id: str) -> str:
    doc = await get_document(doc_id)
    return (doc.get("hatsu_month") or "") if doc else ""


async def _ensure_pages_local(doc_id: str, pdf_filename: str) -> bool:
    """Restore {doc_id}_pages/ from Drive or S3 if missing locally. Returns True if available."""
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

    # S3 fallback
    try:
        from ...core.s3_store import download_dir
        count = await asyncio.to_thread(
            download_dir, f"documents/{doc_id}/pages", pages_dir
        )
        if count > 0 and any(pages_dir.glob("page_*.png")):
            log.info("[%s] S3에서 pages %d 파일 복원", doc_id, count)
            return True
    except Exception:
        log.warning("[%s] S3 pages 복원 실패", doc_id)

    # 폴백: PDF에서 직접 재생성
    if not pdf_filename:
        doc = await get_document(doc_id)
        pdf_filename = (doc.get("pdf_filename") or "") if doc else ""
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
    log.error("[%s] pages 복원 불가 — Drive/S3 pull 실패, PDF도 없음", doc_id)
    return False


async def _ensure_pdf_local(doc_id: str, pdf_filename: str) -> bool:
    """Restore PDF from Drive or S3 if missing locally. Returns True if available."""
    settings = get_settings()
    local_path = settings.samples_dir / pdf_filename
    if local_path.exists():
        return True

    drive = get_drive()
    if drive:
        hatsu_month = await _get_hatsu_month(doc_id)
        try:
            ok = await asyncio.to_thread(
                drive.pull_pdf, settings.drive_root_folder_id, hatsu_month, doc_id, pdf_filename, settings.samples_dir
            )
            if ok and local_path.exists():
                return True
        except Exception:
            log.exception("[%s] Drive PDF 복원 중 예외", doc_id)

    # S3 fallback
    try:
        from ...core.s3_store import download_file
        s3_key = f"documents/{doc_id}/original.pdf"
        await asyncio.to_thread(download_file, s3_key, local_path)
        if local_path.exists():
            log.info("[%s] S3에서 PDF 복원 완료", doc_id)
            return True
    except Exception:
        log.warning("[%s] S3 PDF 복원 실패", doc_id)

    return False


async def _ensure_extracted_local(doc_id: str) -> bool:
    """Restore extracted/{doc_id}/ from Drive or S3 if missing locally. Returns True if available."""
    settings = get_settings()
    extracted = settings.extracted_dir / doc_id
    if extracted.exists() and any(extracted.iterdir()):
        return True

    drive = get_drive()
    if drive:
        hatsu_month = await _get_hatsu_month(doc_id)
        ok = await asyncio.to_thread(
            drive.pull_extracted, settings.drive_root_folder_id, hatsu_month, doc_id, settings.extracted_dir
        )
        if ok and extracted.exists() and any(extracted.iterdir()):
            return True

    # S3 fallback
    try:
        from ...core.s3_store import download_dir
        count = await asyncio.to_thread(
            download_dir, f"documents/{doc_id}/extracted", extracted
        )
        if count > 0:
            log.info("[%s] S3에서 extracted %d 파일 복원", doc_id, count)
            return True
    except Exception:
        log.warning("[%s] S3 extracted 복원 실패", doc_id)

    return False


def _make_doc_id(stem: str) -> str:
    """파일명 stem → 안전한 doc_id. 공백·점·괄호를 정규화하고 [\w\-]만 남긴다."""
    import unicodedata
    stem = unicodedata.normalize("NFC", stem)
    s = re.sub(r"\s*\(\d+\)", "", stem)  # Windows 복사 번호 " (1)" 만 제거
    s = re.sub(r"[\s.]+", "_", s)
    s = re.sub(r"[^\w\-]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


async def _push_pdf_to_drive(doc_id: str, hatsu_month: str, pdf_path: Path) -> None:
    """업로드 직후 백그라운드 — 원본 PDF를 Drive에 올린다 (Drive 미설정 시 no-op)."""
    drive = get_drive()
    if not drive:
        return
    try:
        settings = get_settings()
        await asyncio.to_thread(
            drive.push_pdf,
            settings.drive_root_folder_id,
            hatsu_month,
            pdf_path,
            doc_id,
        )
        log.info("[%s] Drive PDF 업로드 완료", doc_id)
    except Exception:
        log.exception("[%s] Drive PDF 업로드 실패 (무시)", doc_id)


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

    existing = await get_document(doc_id)
    if existing:
        status = existing["status"]
        if status in ("queued", "ocr", "analyzing", "phase1", "phase2", "phase3", "phase4"):
            raise HTTPException(status_code=409, detail=f"이미 분석 중입니다 (상태: {status}). 완료 후 재시도하거나, 서버 재시작 등으로 멈춘 경우 문서 상세에서 '강제 재시작'을 사용하세요.")
        if status in ("pending", "done", "xv_warning"):
            raise HTTPException(status_code=409, detail=f"이미 분석된 문서입니다 (상태: {status}). 재분석은 문서 상세에서 진행하세요.")
        # status == "error" → 재실행 허용

    await create_document(
        doc_id=doc_id,
        pdf_filename=file.filename,
        hatsu_month=hatsu_month,
        user_id=user["user_id"],
        uploaded_by_username=user.get("username", ""),
        uploaded_by_name_ja=user.get("display_name_ja", ""),
    )

    # PDF → S3 (S3 기반 저장 구조 유지)
    try:
        from ...core.s3_store import upload_file as s3_upload
        pdf_s3_key = f"documents/{doc_id}/original.pdf"
        await asyncio.to_thread(s3_upload, pdf_path, pdf_s3_key)
        log.info("[%s] PDF → S3 업로드 완료", doc_id)
    except Exception:
        log.warning("[%s] PDF S3 업로드 실패 (로컬 실행으로 계속)", doc_id)

    # 원본 PDF → Drive (hatsu_month 있을 때만, 실패해도 계속)
    if hatsu_month:
        background_tasks.add_task(_push_pdf_to_drive, doc_id, hatsu_month, pdf_path)

    background_tasks.add_task(run_pipeline, doc_id, pdf_path, hatsu_month)

    return {"doc_id": doc_id, "status": "ocr"}


def _is_rules_stale(stored: dict, current: dict) -> bool | None:
    """분석 시점 규칙 해시 vs 현재 해시 비교.

    비교 가능한 키가 없으면 None (구버전 분석 — 판단 불가)."""
    keys = ("form_definition_hash", "form_types_hash")
    comparable = [k for k in keys if stored.get(k) and current.get(k)]
    if not comparable:
        return None
    return any(stored[k] != current[k] for k in comparable)


def _annotate_stale_rules(docs: list[dict]) -> None:
    """MD·form_types가 바뀌었는데 재분석되지 않은 문서에 stale_rules=True.

    현업이 '규칙 변경 후 어떤 문서를 다시 돌려야 하는지'를 시스템이 알려주는 고리."""
    from ...pipeline.orchestrator import _compute_pipeline_hashes
    settings = get_settings()
    cache: dict[str, dict] = {}
    for d in docs:
        stored = d.get("pipeline_hashes") or {}
        form_id = d.get("form_id") or ""
        if not form_id or not stored:
            d["stale_rules"] = None
            continue
        if form_id not in cache:
            try:
                cache[form_id] = _compute_pipeline_hashes(form_id, settings)
            except Exception:
                log.warning("현재 규칙 해시 계산 실패: %s", form_id, exc_info=True)
                cache[form_id] = {}
        d["stale_rules"] = _is_rules_stale(stored, cache[form_id])


@router.get("")
async def list_docs(user: dict = Depends(get_current_user)):
    docs = await list_documents()
    _annotate_stale_rules(docs)
    return docs


@router.get("/{doc_id}")
async def get_doc(doc_id: str, user: dict = Depends(get_current_user)):
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="문서 없음")
    _annotate_stale_rules([doc])
    settings = get_settings()
    pages_dir = settings.samples_dir / f"{doc_id}_pages"
    local_png_count = len(list(pages_dir.glob("page_*.png"))) if pages_dir.exists() else 0
    if local_png_count > 0:
        doc["pages_count"] = local_png_count
    return doc


def _resolve_page_file(pages_dir: Path, page: int, glob: str) -> Path | None:
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
    return FileResponse(
        str(img_path),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


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
    from fastapi.responses import Response as _Response
    content = json_path.read_text(encoding="utf-8")
    if not content.strip():
        return {"page": page, "width": 0, "height": 0, "lines": []}
    return _Response(
        content=content,
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/{doc_id}/pdf")
async def get_pdf(doc_id: str, user: dict = Depends(get_current_user_sse)):
    """PDF 원본 파일 제공 — iframe 렌더링용. ?sid= 쿼리 파라미터로 인증."""
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="문서 없음")
    pdf_filename = doc.get("pdf_filename", "")
    settings = get_settings()
    pdf_path = settings.samples_dir / pdf_filename
    if not pdf_path.exists():
        await _ensure_pdf_local(doc_id, pdf_filename)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF 파일이 로컬·Drive 모두에 없습니다")
    return FileResponse(str(pdf_path), media_type="application/pdf")


@router.get("/{doc_id}/stream")
async def stream_status(doc_id: str, user: dict = Depends(get_current_user_sse)):
    """SSE — 파이프라인 진행 상태를 실시간으로 스트리밍."""

    async def event_gen():
        terminal = {"done", "error", "pending", "xv_warning"}
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
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="문서 없음")

    settings = get_settings()
    pdf_path = settings.samples_dir / doc["pdf_filename"]
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF 파일 없음")

    pages_dir = settings.samples_dir / f"{doc_id}_pages"
    if not pages_dir.exists():
        raise HTTPException(status_code=400, detail="OCR 파일 없음 — 먼저 분석을 실행하세요")

    raw_path = pages_dir / "_azure_raw.json"
    if raw_path.exists():
        import json as _json
        from ...pipeline.ocr import _write_page_files
        result_json = _json.loads(raw_path.read_text(encoding="utf-8"))
        await asyncio.to_thread(_write_page_files, result_json, pages_dir, pdf_path)
        count = len(list(pages_dir.glob("page_*.png")))
    else:
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
    """문서 재분석."""
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="문서 없음")
    if doc["status"] in ("queued", "ocr", "analyzing", "phase1", "phase2", "phase3", "phase4"):
        if not force:
            raise HTTPException(status_code=400, detail="현재 분석 중인 문서입니다. 완료 후 재분석하거나 ?force=true 로 강제 재시작하세요.")
        log.warning("[%s] force retry: 분석 중 상태(%s)를 강제 초기화합니다", doc_id, doc["status"])

    settings = get_settings()
    pdf_filename = doc.get("pdf_filename", "")
    pdf_path = settings.samples_dir / pdf_filename
    if not pdf_path.exists():
        await _ensure_pdf_local(doc_id, pdf_filename)
    if not pdf_path.exists():
        raise HTTPException(
            status_code=404,
            detail="PDF 파일이 서버·Drive 모두에 없습니다. 파일을 다시 업로드하세요.",
        )

    extracted_dir = settings.extracted_dir / doc_id
    pages_dir = settings.samples_dir / f"{doc_id}_pages"

    if not force and not pages_dir.exists():
        await _ensure_pages_local(doc_id, pdf_filename)

    if force:
        if pages_dir.exists():
            shutil.rmtree(pages_dir)
        if extracted_dir.exists():
            shutil.rmtree(extracted_dir)
    else:
        if extracted_dir.exists():
            for fname in ("phase2_output.json", "phase3_output.json", "phase4_output.json"):
                p = extracted_dir / fname
                if p.exists():
                    p.unlink()
            for f in extracted_dir.glob("phase2_partial_*.json"):
                f.unlink()

    # S3 extracted 무효화 — 로컬만 지우면 서버 재시작·resume 시
    # S3의 이전 실행 산출물이 복원되어 stale 결과가 부활할 수 있다
    def _purge_s3_extracted() -> None:
        from ...core.s3_store import delete_key, list_keys
        for key in list_keys(f"documents/{doc_id}/extracted/"):
            delete_key(key)
    try:
        await asyncio.to_thread(_purge_s3_extracted)
    except Exception:
        log.warning("[%s] S3 extracted 무효화 실패 (무시하고 계속)", doc_id, exc_info=True)

    await clear_mappings(doc_id)
    await reset_document_for_retry(doc_id)

    hatsu_month = doc.get("hatsu_month") or ""
    background_tasks.add_task(run_pipeline, doc_id, pdf_path, hatsu_month)
    return {"doc_id": doc_id, "status": "ocr"}


@router.post("/{doc_id}/remap-cached")
async def remap_cached(
    doc_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """pending 문서에 대해 캐시 재조회만으로 Phase 3 재실행. Claude 호출 없음."""
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="문서 없음")
    if doc["status"] not in ("pending", "done", "xv_warning"):
        raise HTTPException(
            status_code=400,
            detail=f"pending/done 상태 문서에서만 사용할 수 있습니다 (현재: {doc['status']})",
        )
    background_tasks.add_task(resume_phase3_with_cache, doc_id)
    return {"doc_id": doc_id, "status": "phase3"}


_IN_PROGRESS = {"queued", "ocr", "analyzing", "phase1", "phase2", "phase3", "phase4"}


@router.post("/{doc_id}/cancel")
async def cancel_document(doc_id: str, user: dict = Depends(get_current_user)):
    """진행 중인 분석을 취소하고 error 상태로 전환."""
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="문서 없음")
    if doc["status"] not in _IN_PROGRESS:
        raise HTTPException(status_code=400, detail=f"진행 중 문서가 아닙니다 (현재: {doc['status']})")
    await update_document_error(doc_id, "cancelled", doc["status"], "사용자가 분석을 취소했습니다")
    log.info("[%s] 분석 취소 — 요청자: %s", doc_id, user.get("username"))
    return {"doc_id": doc_id, "status": "error"}


class DeleteBody(BaseModel):
    password: str


@router.delete("/{doc_id}")
async def delete_document(doc_id: str, body: DeleteBody, user: dict = Depends(get_current_user)):
    """문서 삭제 — 본인 업로드 문서만 가능, 비밀번호 확인 필수."""
    doc = await get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="문서 없음")

    if not user.get("is_admin") and doc.get("uploaded_by") != user["user_id"]:
        raise HTTPException(status_code=403, detail="본인이 업로드한 문서만 삭제할 수 있습니다")

    if doc["status"] in ("queued", "ocr", "analyzing", "phase1", "phase2", "phase3", "phase4"):
        raise HTTPException(status_code=400, detail="분석 중인 문서는 삭제할 수 없습니다")

    if not user.get("is_admin"):
        pw_hash = await get_user_password_hash(user["user_id"])
        if not pw_hash or not bcrypt.checkpw(body.password.encode()[:72], pw_hash.encode()):
            raise HTTPException(status_code=400, detail="비밀번호가 올바르지 않습니다")

    # S3 JSON 삭제
    await delete_document_data(doc_id)

    # 로컬 파일 삭제
    settings = get_settings()
    pdf_filename = doc.get("pdf_filename", "")
    pdf_path = settings.samples_dir / pdf_filename
    pages_dir = settings.samples_dir / f"{doc_id}_pages"
    extracted = settings.extracted_dir / doc_id

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
                doc.get("hatsu_month") or "",
                doc_id,
                pdf_filename,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception("[%s] Drive 삭제 실패 (무시)", doc_id)

    return {"ok": True}


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


