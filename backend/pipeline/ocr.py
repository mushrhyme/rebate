"""Azure Document Intelligence (prebuilt-layout) — async Python implementation."""
import asyncio
import json
import logging
import os
import random
from pathlib import Path

import httpx

from ..core.config import get_settings

log = logging.getLogger(__name__)

MODEL = "prebuilt-layout"
API_VER = "2024-11-30"
POLL_INTERVAL = 3
MAX_POLLS = 60

# Azure 429/5xx/네트워크 일시 장애 backoff (동시 분석 시 throttle 대비)
_OCR_MAX_RETRIES = int(os.getenv("OCR_MAX_RETRIES", "4"))
_OCR_RETRY_BASE_DELAY = float(os.getenv("OCR_RETRY_BASE_DELAY", "2.0"))
_OCR_RETRY_MAX_DELAY = float(os.getenv("OCR_RETRY_MAX_DELAY", "30.0"))
_OCR_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


async def _send_with_retry(send, *, what: str) -> httpx.Response:
    """send() → httpx.Response 호출 + raise_for_status.

    429/5xx 응답과 일시적 네트워크 오류(timeout·connection)는 Retry-After 우선
    exponential backoff로 재시도한다. 그 외 4xx 등은 즉시 raise.
    send는 매 시도마다 요청을 새로 보내는 팩토리여야 한다 (httpx Response는 1회용).
    """
    delay = _OCR_RETRY_BASE_DELAY
    for attempt in range(_OCR_MAX_RETRIES + 1):
        try:
            resp = await send()
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status not in _OCR_RETRYABLE_STATUS or attempt == _OCR_MAX_RETRIES:
                raise
            ra = e.response.headers.get("Retry-After")
            try:
                wait = float(ra) if ra else delay
            except (TypeError, ValueError):
                wait = delay
        except httpx.TransportError as e:
            # timeout·connection·protocol 등 일시 네트워크 오류
            if attempt == _OCR_MAX_RETRIES:
                raise
            wait = delay
            log.warning("OCR %s 네트워크 오류 — 재시도: %s", what, e)
        wait = min(wait, _OCR_RETRY_MAX_DELAY) + random.uniform(0, 0.5)
        log.warning("OCR %s 일시 실패 → %.1fs 후 재시도 (%d/%d)",
                    what, wait, attempt + 1, _OCR_MAX_RETRIES)
        await asyncio.sleep(wait)
        delay = min(delay * 2, _OCR_RETRY_MAX_DELAY)


async def run_ocr(pdf_path: Path, pages_dir: Path) -> None:
    """Submit PDF to Azure, poll until done, write page_NNN.ocr.txt + .ocr.json + .png.
    OCR 파일이 이미 존재하면 Azure 호출을 스킵한다."""
    if pages_dir.exists() and any(pages_dir.glob("page_*.ocr.txt")):
        needs_png  = not any(pages_dir.glob("page_*.png"))
        needs_json = not any(pages_dir.glob("page_*.ocr.json"))
        raw_path   = pages_dir / "_azure_raw.json"
        if (needs_png or needs_json) and raw_path.exists():
            # 저장된 raw 결과로 누락된 파일 재생성
            result_json = json.loads(raw_path.read_text(encoding="utf-8"))
            await asyncio.to_thread(_write_page_files, result_json, pages_dir, pdf_path)
            return
        if needs_png:
            await asyncio.to_thread(_generate_page_images, pdf_path, pages_dir)
        return

    settings = get_settings()
    endpoint = settings.azure_api_endpoint.rstrip("/")
    key = settings.azure_api_key

    analyze_url = (
        f"{endpoint}/documentintelligence/documentModels/{MODEL}"
        f":analyze?api-version={API_VER}"
    )

    pdf_bytes = await asyncio.to_thread(pdf_path.read_bytes)

    async with httpx.AsyncClient(timeout=60) as client:
        # Step 1 — submit (429/5xx/네트워크 일시 장애 backoff)
        resp = await _send_with_retry(
            lambda: client.post(
                analyze_url,
                content=pdf_bytes,
                headers={
                    "Ocp-Apim-Subscription-Key": key,
                    "Content-Type": "application/pdf",
                },
            ),
            what="submit",
        )
        operation_url = resp.headers.get("operation-location")
        if not operation_url:
            raise RuntimeError("operation-location 헤더 없음 — Azure 응답 확인 필요")

        # Step 2 — poll
        result_json: dict = {}
        for _ in range(MAX_POLLS):
            await asyncio.sleep(POLL_INTERVAL)
            poll = await _send_with_retry(
                lambda: client.get(
                    operation_url, headers={"Ocp-Apim-Subscription-Key": key}
                ),
                what="poll",
            )
            result_json = poll.json()
            status = result_json.get("status", "")
            if status == "succeeded":
                break
            if status == "failed":
                raise RuntimeError(f"Azure OCR failed: {result_json}")
        else:
            raise RuntimeError("Azure OCR timeout")

    # raw 결과 저장 — 이후 backfill 시 .ocr.json 재생성에 사용
    pages_dir.mkdir(parents=True, exist_ok=True)
    raw_path = pages_dir / "_azure_raw.json"
    raw_path.write_text(json.dumps(result_json, ensure_ascii=False), encoding="utf-8")

    await asyncio.to_thread(_write_page_files, result_json, pages_dir, pdf_path)


def _write_page_files(result: dict, pages_dir: Path, pdf_path: Path) -> None:
    pages_dir.mkdir(parents=True, exist_ok=True)
    analyze = result.get("analyzeResult", {})
    pages = analyze.get("pages", [])
    tables = analyze.get("tables", [])

    # build per-page table grids
    page_tables: dict[int, list[str]] = {}
    for tbl in tables:
        regions = tbl.get("boundingRegions", [])
        pg = regions[0]["pageNumber"] if regions else None
        if pg is None:
            continue
        cells = tbl.get("cells", [])
        if not cells:
            continue
        max_r = max(c["rowIndex"] for c in cells)
        max_c = max(c["columnIndex"] for c in cells)
        grid = [[""] * (max_c + 1) for _ in range(max_r + 1)]
        for cell in cells:
            grid[cell["rowIndex"]][cell["columnIndex"]] = cell.get("content", "").strip()
        tsv = "\n".join("\t".join(row) for row in grid)
        page_tables.setdefault(pg, []).append(tsv)

    for page in pages:
        pg_num = page["pageNumber"]
        lines = page.get("lines", [])

        # ── .ocr.txt ─────────────────────────────────────────────
        parts = ["\n".join(ln.get("content", "") for ln in lines)]
        if pg_num in page_tables:
            parts += ["", "--- tables ---", ""]
            for tsv in page_tables[pg_num]:
                parts += [tsv, ""]
        out_path = pages_dir / f"page_{pg_num:03d}.ocr.txt"
        out_path.write_text("\n".join(parts), encoding="utf-8")

        # ── .ocr.json (line bbox) ────────────────────────────────
        lines_data = []
        for ln in lines:
            poly = ln.get("polygon", [])
            if len(poly) >= 8:
                xs, ys = poly[0::2], poly[1::2]
                bbox = [min(xs), min(ys), max(xs), max(ys)]
            else:
                bbox = []
            lines_data.append({"text": ln.get("content", ""), "bbox": bbox})

        ocr_json = {
            "page": pg_num,
            "width": page.get("width", 0),
            "height": page.get("height", 0),
            "unit": page.get("unit", "pixel"),
            "lines": lines_data,
        }
        json_path = pages_dir / f"page_{pg_num:03d}.ocr.json"
        json_path.write_text(
            json.dumps(ocr_json, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── .png (PyMuPDF) ───────────────────────────────────────────
    _generate_page_images(pdf_path, pages_dir)


# 레티나(2x) 화면에서 뷰어 폭 ~1000px 기준 2000+ 물리픽셀 필요 → 250 DPI
# (A4 세로 2066px). 150 DPI(1240px)는 업스케일로 흐릿하게 보였음.
PAGE_IMAGE_DPI = 250


def _generate_page_images(pdf_path: Path, pages_dir: Path) -> int:
    """PyMuPDF로 PDF 각 페이지를 PAGE_IMAGE_DPI PNG로 저장. 생성한 페이지 수 반환."""
    try:
        import fitz  # pymupdf
    except ImportError:
        return 0  # requirements.txt에 pymupdf 없으면 조용히 스킵

    pages_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    mat = fitz.Matrix(PAGE_IMAGE_DPI / 72, PAGE_IMAGE_DPI / 72)
    count = 0
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        png_path = pages_dir / f"page_{page.number + 1:03d}.png"
        # 기존 파일을 서빙 중일 수 있으므로 tmp에 쓰고 원자적으로 교체
        tmp_path = png_path.with_name(png_path.name + ".tmp")
        pix.save(str(tmp_path), output="png")  # 확장자가 .tmp라 포맷 명시 필요
        import os as _os
        _os.replace(tmp_path, png_path)
        count += 1
    doc.close()
    return count
