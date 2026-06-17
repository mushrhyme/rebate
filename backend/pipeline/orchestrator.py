"""파이프라인 오케스트레이터 — Phase 1 → 2 → 3 → (대기) → 4."""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import anthropic

from ..core.config import get_settings, get_drive
from dataclasses import asdict

from ..db.queries import (
    update_document_status,
    update_document_error,
    get_document,
    get_all_mappings,
    clear_mappings,
    save_pending_mappings,
    has_pending_mappings,
    set_current_run_id,
    get_current_run_id,
    set_form_id,
    set_pages_count,
    set_pipeline_hashes,
    record_phase_timing,
    save_phase3_tool_use_stats,
)
from .ocr import run_ocr
from .phase1 import run_phase1
from .phase2 import run_phase2
from .phase2_row_anchor import build_row_anchors, save_row_anchors
from .phase2_verify import run_phase2_verify
from .phase3 import (
    run_phase3,
    _build_issuer_fingerprint,
    _parse_fingerprint_fields,
    get_dist_group_field,
)
from .phase3_fallback import run_phase3_with_tool_use_or_fallback
from ..tools.mapping import confirm_mapping
from .phase4 import run_phase4

log = logging.getLogger(__name__)

# detail 페이지가 이 수를 초과하면 청크 분할 적용
_DETAIL_CHUNK_THRESHOLD = int(os.getenv("PHASE2_CHUNK_THRESHOLD", "4"))
# 청크당 detail 페이지 수
_DETAIL_CHUNK_SIZE = int(os.getenv("PHASE2_CHUNK_SIZE", "2"))
# 청크 앞뒤 overlap 페이지 수 (기본 0: 교차 페이지 블록은 phase2_verify가 처리)
_DETAIL_OVERLAP = int(os.getenv("PHASE2_OVERLAP", "0"))
# 청크당 포함할 summary 페이지 최대 수 (거리 가까운 순)
_MAX_SUMMARY_PER_CHUNK = int(os.getenv("PHASE2_MAX_SUMMARY", "3"))

# 동시 파이프라인 실행 상한 (env: MAX_CONCURRENT_ANALYSES, 기본 5)
# 20명 사용자 기준: 5건 동시 처리, 나머지는 asyncio 큐에서 대기
_pipeline_semaphore: asyncio.Semaphore | None = None


def _get_pipeline_semaphore() -> asyncio.Semaphore:
    global _pipeline_semaphore
    if _pipeline_semaphore is None:
        limit = int(os.getenv("MAX_CONCURRENT_ANALYSES", "5"))
        _pipeline_semaphore = asyncio.Semaphore(limit)
    return _pipeline_semaphore


# Phase 2 청크 동시 실행 상한 (env: MAX_CONCURRENT_PHASE2_CHUNKS, 기본 3)
# 파이프라인 5개 × 청크 3개 = 최대 15 Sonnet 동시 호출
_chunk_semaphore: asyncio.Semaphore | None = None


def _get_chunk_semaphore() -> asyncio.Semaphore:
    global _chunk_semaphore
    if _chunk_semaphore is None:
        limit = int(os.getenv("MAX_CONCURRENT_PHASE2_CHUNKS", "3"))
        _chunk_semaphore = asyncio.Semaphore(limit)
    return _chunk_semaphore


async def run_pipeline(doc_id: str, pdf_path: Path, hatsu_month: str = "") -> None:
    """업로드 직후 백그라운드에서 실행. 상태를 DB에 계속 업데이트."""
    settings = get_settings()
    pages_dir = settings.samples_dir / f"{doc_id}_pages"
    extracted_dir = settings.extracted_dir / doc_id
    extracted_dir.mkdir(parents=True, exist_ok=True)

    # 이번 분석 실행 ID — 재분석 시 새 ID 생성 (이력 추적용)
    run_id = str(uuid4())
    await set_current_run_id(doc_id, run_id)

    await update_document_status(doc_id, "queued")
    async with _get_pipeline_semaphore():
        try:
            # ── OCR ──────────────────────────────────────────────
            await update_document_status(doc_id, "ocr")
            _t0 = time.monotonic()
            await run_ocr(pdf_path, pages_dir)
            await record_phase_timing(doc_id, "ocr", time.monotonic() - _t0)
            log.info("[%s] OCR 완료", doc_id)

            # pages(PNG + OCR txt/json) → S3 (EC2가 페이지 이미지를 서빙할 수 있도록)
            # 분석 파이프라인과 무관 — 백그라운드 업로드로 Phase 1 대기 제거
            asyncio.create_task(_sync_dir_to_s3(doc_id, pages_dir, f"documents/{doc_id}/pages", "pages"))
            asyncio.create_task(_push_pages_to_drive(doc_id))

            # OCR 완료 후 페이지 수 저장 (다중 번들 경고용)
            pages_count = len(list(pages_dir.glob("page_*.ocr.txt")))
            await set_pages_count(doc_id, pages_count)
            if pages_count > 20:
                log.warning("[%s] 페이지 수 %d — 단일 청구서 세트 초과 가능성", doc_id, pages_count)

            # ── 양식 식별 (OCR 기반 — Phase 1 이전) ─────────────
            form_id, confidence = await _identify_form(doc_id, pages_dir)
            if form_id == "unknown":
                await update_document_error(
                    doc_id,
                    error_type="unknown_form",
                    error_phase="Phase 2",
                    message="양식을 인식할 수 없습니다. form_definitions에 일치하는 양식이 없습니다.",
                )
                return
            await _set_form_id(doc_id, form_id)
            await _record_pipeline_hashes(doc_id, form_id, settings)

            # ── 마스터 가용성 가드 (Sheets 모드) ──────────────────
            # 토큰 만료/네트워크 장애로 마스터(unit_price)가 빈 채로 분석이 진행되면
            # 모든 매핑이 not_found·NET이 None이 되어 '조용히 틀린' 결과가 나온다.
            # 분석을 시작하기 전에 명시적 error로 떨궈 운영자가 즉시 알 수 있게 한다.
            _master_err = await asyncio.to_thread(_check_master_availability)
            if _master_err:
                await update_document_error(
                    doc_id,
                    error_type="sheets_unavailable",
                    error_phase="Phase 3",
                    message=_master_err,
                )
                log.error("[%s] 마스터 가용성 가드 실패 — 분석 중단: %s", doc_id, _master_err)
                return

            # ── Phase 1 ──────────────────────────────────────────
            await update_document_status(doc_id, "phase1")
            _t0 = time.monotonic()
            await run_phase1(doc_id, pages_dir, extracted_dir, run_id=run_id)
            await record_phase_timing(doc_id, "phase1", time.monotonic() - _t0)
            log.info("[%s] Phase 1 완료", doc_id)

            # ── Phase 2 준비: row anchor 생성 (form_types.json row_anchor 설정 기반) ──
            row_anchors_all: list[dict] = []
            _form_types_path = settings.workspace_root / "config" / "form_types.json"
            _form_cfg = (
                json.loads(_form_types_path.read_text(encoding="utf-8")).get(form_id, {})
                if _form_types_path.exists() else {}
            )
            _row_anchor_cfg = _form_cfg.get("row_anchor")
            if _row_anchor_cfg:
                row_anchors_all = await asyncio.to_thread(
                    build_row_anchors, _row_anchor_cfg, extracted_dir
                )
                await asyncio.to_thread(save_row_anchors, extracted_dir, row_anchors_all)
                log.info("[%s] row anchor %d행 생성", doc_id, len(row_anchors_all))

            def _filter_anchors(page_nums: list[int] | None) -> list[dict] | None:
                """청크 페이지 목록에 해당하는 anchor만 필터. None이면 전체 반환."""
                if not row_anchors_all:
                    return None
                if page_nums is None:
                    return row_anchors_all or None
                pages_set = set(page_nums)
                filtered = [a for a in row_anchors_all if a['page'] in pages_set]
                return filtered or None

            # ── Phase 2 (번들 감지 → 청크 분할 → 단일 호출 순 판단) ──────────
            await update_document_status(doc_id, "phase2")
            _t0_phase2 = time.monotonic()
            bundles = await asyncio.to_thread(_detect_bundles, extracted_dir, _form_cfg.get("bundle_detection"))
            if bundles:
                log.warning("[%s] 다중 번들 감지 (%d건): %s", doc_id, len(bundles), bundles)
                phase2_results = []
                for start, end in bundles:
                    bundle_pages = list(range(start, end + 1))
                    detail_chunks = await asyncio.to_thread(
                        _build_detail_chunks, extracted_dir, (start, end)
                    )
                    if detail_chunks:
                        log.info(
                            "[%s] 번들 (%d-%d) 청크 분할 (%d청크) — 병렬 실행",
                            doc_id, start, end, len(detail_chunks),
                        )
                        async def _run_bundle_chunk(chunk_pages: list[int]) -> dict:
                            async with _get_chunk_semaphore():
                                return await run_phase2(
                                    doc_id, form_id, extracted_dir, page_numbers=chunk_pages,
                                    run_id=run_id, row_anchors=_filter_anchors(chunk_pages),
                                    write_output=False,
                                )
                        bundle_results = await asyncio.gather(
                            *[_run_bundle_chunk(c) for c in detail_chunks]
                        )
                        phase2_results.extend(bundle_results)
                    else:
                        r = await run_phase2(
                            doc_id, form_id, extracted_dir, page_range=(start, end),
                            run_id=run_id, row_anchors=_filter_anchors(bundle_pages),
                        )
                        phase2_results.append(r)
                phase2_result = _merge_phase2_results(phase2_results)
                phase2_result["bundles"] = [
                    {"bundle_idx": i, "page_range": [s, e], "cover_page": s}
                    for i, (s, e) in enumerate(bundles)
                ]
                # 각 번들 호출이 phase2_output.json을 덮어쓰므로 머지 결과로 재기록
                await asyncio.to_thread(
                    (extracted_dir / "phase2_output.json").write_text,
                    json.dumps(phase2_result, ensure_ascii=False, indent=2),
                    "utf-8",
                )
            else:
                detail_chunks = await asyncio.to_thread(_build_detail_chunks, extracted_dir)
                if detail_chunks:
                    log.info("[%s] detail 페이지 청크 분할 (%d청크) — 병렬 실행", doc_id, len(detail_chunks))
                    async def _run_chunk(chunk_pages: list[int]) -> dict:
                        async with _get_chunk_semaphore():
                            return await run_phase2(
                                doc_id, form_id, extracted_dir, page_numbers=chunk_pages,
                                run_id=run_id, row_anchors=_filter_anchors(chunk_pages),
                                write_output=False,
                            )
                    phase2_results = await asyncio.gather(
                        *[_run_chunk(c) for c in detail_chunks]
                    )
                    phase2_result = _merge_phase2_results(list(phase2_results))
                    await asyncio.to_thread(
                        (extracted_dir / "phase2_output.json").write_text,
                        json.dumps(phase2_result, ensure_ascii=False, indent=2),
                        "utf-8",
                    )
                else:
                    phase2_result = await run_phase2(
                        doc_id, form_id, extracted_dir,
                        run_id=run_id, row_anchors=_filter_anchors(None),
                    )
            await record_phase_timing(doc_id, "phase2", time.monotonic() - _t0_phase2)
            log.info("[%s] Phase 2 완료 — %d 항목", doc_id, len(phase2_result.get("items", [])))

            # ── Phase 2 역산 검증 (管理No計 불일치 시 핀포인트 재요청) ──
            try:
                phase2_result = await run_phase2_verify(
                    doc_id, form_id, extracted_dir, phase2_result, run_id=run_id
                )
            except Exception as exc:
                log.warning("[%s] Phase 2 역산검증 오류 (무시하고 계속): %s", doc_id, exc)

            # ── Phase 3 ──────────────────────────────────────────
            await update_document_status(doc_id, "phase3")
            _t0 = time.monotonic()
            _, pending = await _call_phase3_by_flag(
                doc_id=doc_id,
                phase2_result=phase2_result,
                extracted_dir=extracted_dir,
                form_id=form_id,
                hatsu_month=hatsu_month,
                run_id=run_id,
                settings=settings,
            )

            await record_phase_timing(doc_id, "phase3", time.monotonic() - _t0)

            if pending:
                await save_pending_mappings(doc_id, pending)
                await update_document_status(doc_id, "pending")
                log.info("[%s] Phase 3 — 확인 필요 %d건, 대기 상태", doc_id, len(pending))
                # pending 시 extracted를 S3에 동기화 (서버 재시작 후 resume_phase4가 복원할 수 있도록)
                await _sync_dir_to_s3(doc_id, extracted_dir, f"documents/{doc_id}/extracted", "extracted(pending)")
                return  # 사용자 확인 후 resume_phase4()가 호출됨

            # 전부 자동 확정된 경우 바로 Phase 4
            await _run_phase4_and_finish(doc_id, run_id=run_id)

        except Exception as exc:
            log.exception("[%s] 파이프라인 오류", doc_id)
            await update_document_error(doc_id, error_type="technical", error_phase="pipeline", message=str(exc))


async def _call_phase3_by_flag(
    doc_id: str,
    phase2_result: dict,
    extracted_dir,
    form_id: str,
    hatsu_month: str,
    run_id: str,
    settings,
) -> tuple:
    """PHASE3_TOOL_USE_ENABLED flag에 따라 적절한 Phase 3 경로를 실행한다.

    flag OFF (기본값): legacy run_phase3() 직접 호출
    flag ON:           run_phase3_with_tool_use_or_fallback() 호출
                       Tool Use 실패 시 자동 fallback → legacy 결과 반환

    Returns:
        (phase3_result, pending)  — flag OFF: 2-tuple
        (phase3_result, pending)  — flag ON: 내부적으로 3-tuple을 받아 2-tuple로 반환
    """
    if settings.phase3_tool_use_enabled:
        # Tool Use 경로 (experimental) — 실패 시 legacy로 자동 fallback
        # settings를 명시적으로 전달 → wrapper 내부 get_settings() 호출 불필요
        _, pending, _fb_stats = await run_phase3_with_tool_use_or_fallback(
            doc_id, phase2_result, extracted_dir,
            form_id=form_id, hatsu_month=hatsu_month, run_id=run_id,
            enable_tool_use=True,
            settings=settings,           # ← 명시적 주입
        )
        if _fb_stats.fallback_triggered:
            log.warning(
                "[%s] Phase 3 Tool Use fallback 발생: [%s] %s",
                doc_id, _fb_stats.fallback_class, _fb_stats.fallback_reason,
            )
        await save_phase3_tool_use_stats(doc_id, asdict(_fb_stats))
        return _, pending
    else:
        # Legacy 경로 (기본값, flag OFF)
        return await run_phase3(
            doc_id, phase2_result, extracted_dir,
            form_id=form_id, hatsu_month=hatsu_month, run_id=run_id,
        )


async def resume_phase4(doc_id: str) -> None:
    """Phase 3 매핑 확인 완료 후 사용자가 호출."""
    if await has_pending_mappings(doc_id):
        raise ValueError("아직 확인되지 않은 매핑이 있습니다")
    await _ensure_extracted_from_s3(doc_id)
    await _merge_confirmed_mappings(doc_id)
    run_id = await get_current_run_id(doc_id)
    await _run_phase4_and_finish(doc_id, run_id=run_id)


async def resume_phase3_with_cache(doc_id: str) -> None:
    """Phase 2 결과를 재사용하여 Phase 3(매핑)만 재실행.
    캐시 미스 항목은 Claude 판단으로 처리한다.
    해소된 항목은 Phase 4까지 자동 진행, 여전히 미매핑인 항목은 pending으로 남는다."""
    settings = get_settings()
    await _ensure_extracted_from_s3(doc_id)

    extracted_dir = settings.extracted_dir / doc_id
    p2_path = extracted_dir / "phase2_output.json"
    if not p2_path.exists():
        await update_document_error(
            doc_id, error_type="technical", error_phase="phase3",
            message="phase2_output.json 없음 — Phase 2 결과가 필요합니다",
        )
        return

    phase2_result = json.loads(p2_path.read_text(encoding="utf-8"))
    doc = await get_document(doc_id)
    if not doc:
        return

    form_id = doc.get("form_id") or ""
    hatsu_month = doc.get("hatsu_month") or ""
    run_id = await get_current_run_id(doc_id)

    await clear_mappings(doc_id)
    await update_document_status(doc_id, "phase3")

    try:
        _, pending = await _call_phase3_by_flag(
            doc_id, phase2_result, extracted_dir,
            form_id=form_id, hatsu_month=hatsu_month, run_id=run_id,
            settings=settings,
        )

        if pending:
            await save_pending_mappings(doc_id, pending)
            await update_document_status(doc_id, "pending")
            await _sync_dir_to_s3(doc_id, extracted_dir, f"documents/{doc_id}/extracted", "extracted(pending)")
            log.info("[%s] Phase 3 재실행 — 여전히 미매핑 %d건, 대기 상태", doc_id, len(pending))
            return

        await _run_phase4_and_finish(doc_id, run_id=run_id)

    except Exception as exc:
        log.exception("[%s] Phase 3 재실행 오류", doc_id)
        await update_document_error(doc_id, error_type="technical", error_phase="phase3", message=str(exc))


async def resume_phase4_for_remap(doc_id: str) -> None:
    """결과 화면에서 remap 후 Phase 4 재실행 — pending 체크 없이.
    remap은 이미 phase4가 완료된 문서에서 개별 매핑을 수정하는 용도이므로
    다른 미확정 항목이 있어도 재실행을 허용한다.
    교차검증(Claude 호출)은 건너뛰고 NET 재계산만 수행해 응답 속도를 높인다."""
    await _ensure_extracted_from_s3(doc_id)
    await _merge_confirmed_mappings(doc_id)
    run_id = await get_current_run_id(doc_id)
    await _run_phase4_and_finish(doc_id, run_id=run_id, skip_xv=True)


async def _merge_confirmed_mappings(doc_id: str) -> None:
    """DB 확정 매핑을 phase3_output.json의 confirmed dicts와 items 배열에 반영.
    사용자 확인 매핑은 동시에 CSV 캐시(ocr_retailer / ocr_product / ocr_dist)에도 기록한다.
    """
    settings = get_settings()
    out_path = settings.extracted_dir / doc_id / "phase3_output.json"
    if not out_path.exists():
        return

    result = json.loads(out_path.read_text(encoding="utf-8"))
    rows = await get_all_mappings(doc_id)

    confirmed_retailers = result.setdefault("confirmed_retailers", {})
    confirmed_products  = result.setdefault("confirmed_products", {})

    for row in rows:
        if row["mapping_type"] == "retailer":
            existing = confirmed_retailers.get(row["ocr_name"], {})
            confirmed_retailers[row["ocr_name"]] = {
                **existing,
                "retailer_code": row["confirmed_code"],
                "basis": "user_confirmed",
            }
        elif row["mapping_type"] == "dist":
            existing = confirmed_retailers.get(row["ocr_name"], {})
            confirmed_retailers[row["ocr_name"]] = {
                **existing,
                "dist_code": row["confirmed_code"],
                "basis": existing.get("basis", "user_confirmed"),
            }
        elif row["mapping_type"] == "product":
            confirmed_products[row["ocr_name"]] = {
                "code":  row["confirmed_code"],
                "name":  row["confirmed_name"],
                "basis": "user_confirmed",
            }

    # 사용자가 판매처(dist)를 직접 확정한 (소매처, jisho) → 해당 item에만 적용.
    # jisho가 빈 행("")은 그 소매처의 모든 jisho에 적용 (jisho 미사용 양식 / 레거시 교정).
    _dist_group_field = get_dist_group_field(result.get("form_id", ""))
    user_dist_specific: dict[tuple[str, str], str] = {}
    user_dist_all: dict[str, str] = {}
    for row in rows:
        if row["mapping_type"] != "dist" or not row.get("confirmed_code"):
            continue
        _rj = row.get("jisho", "")
        if _rj:
            user_dist_specific[(row["ocr_name"], _rj)] = row["confirmed_code"]
        else:
            user_dist_all[row["ocr_name"]] = row["confirmed_code"]

    # items 배열 codes 업데이트
    for item in result.get("items", []):
        ocr_customer = item.get("customer_ocr", "") or item.get("customer", "")
        ocr_product  = item.get("product_ocr",  "") or item.get("product",  "")

        rc = confirmed_retailers.get(ocr_customer)
        if rc:
            item["retailer_code"] = rc.get("retailer_code", "")
            item["unconfirmed"]   = False

        # dist_code: 사용자 확정이 있으면 덮어쓰고, 없으면 phase3가 넣은 (소매처×jisho) 값 유지
        # (retailer 단위로 무조건 덮어쓰면 jisho별 판매처가 뭉개지므로 보존한다)
        _item_jisho = item.get(_dist_group_field, "") if _dist_group_field else ""
        if (ocr_customer, _item_jisho) in user_dist_specific:
            item["dist_code"] = user_dist_specific[(ocr_customer, _item_jisho)]
        elif ocr_customer in user_dist_all:
            item["dist_code"] = user_dist_all[ocr_customer]

        pc = confirmed_products.get(ocr_product)
        if pc:
            item["product_code"] = pc.get("code")

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 사용자 확정 매핑을 CSV 캐시에 upsert (중복 방지)
    md = settings.mappings_dir
    form_id = result.get("form_id", "")
    issuer = result.get("issuer", {})
    form_path = settings.form_definitions_dir / f"{form_id}.md"
    form_md = form_path.read_text(encoding="utf-8") if form_path.exists() else ""
    issuer_fp = _build_issuer_fingerprint(issuer, _parse_fingerprint_fields(form_md))

    for row in rows:
        code = row.get("confirmed_code")
        name = row.get("confirmed_name") or ""
        if not code:
            continue
        if row["mapping_type"] == "retailer":
            await confirm_mapping(
                mapping_type="retailer",
                ocr_name=row["ocr_name"],
                confirmed_code=code,
                context={"retailer_name": name},
                mappings_dir=md,
            )
        elif row["mapping_type"] == "product":
            await confirm_mapping(
                mapping_type="product",
                ocr_name=row["ocr_name"],
                confirmed_code=code,
                context={"product_name": name},
                mappings_dir=md,
            )
        elif row["mapping_type"] == "dist":
            rc = confirmed_retailers.get(row["ocr_name"], {}).get("retailer_code", "")
            if rc:
                await confirm_mapping(
                    mapping_type="dist",
                    ocr_name=row["ocr_name"],
                    confirmed_code=code,
                    context={
                        "form_id": form_id,
                        "issuer_fingerprint": issuer_fp,
                        "retailer_code": rc,
                        "jisho": row.get("jisho", ""),
                        "dist_name": name,
                    },
                    mappings_dir=md,
                )


async def _run_phase4_and_finish(doc_id: str, run_id: str = "", skip_xv: bool = False) -> None:
    await update_document_status(doc_id, "phase4")
    _t0 = time.monotonic()
    phase4_data = await run_phase4(doc_id, run_id=run_id, skip_xv=skip_xv)
    await record_phase_timing(doc_id, "phase4", time.monotonic() - _t0)

    await update_document_status(doc_id, "done")
    log.info("[%s] Phase 4 완료 — done", doc_id)

    # S3·Drive·Sheets 동기화는 사용자 응답과 무관 — 백그라운드 처리
    settings = get_settings()
    extracted_dir = settings.extracted_dir / doc_id
    asyncio.create_task(_sync_dir_to_s3(doc_id, extracted_dir, f"documents/{doc_id}/extracted", "extracted(done)"))
    asyncio.create_task(_push_to_drive(doc_id))
    asyncio.create_task(_write_results_to_sheets(doc_id))


async def _sync_dir_to_s3(doc_id: str, local_dir: Path, prefix: str, label: str) -> None:
    """local_dir → S3 prefix 업로드. 실패해도 파이프라인 계속 진행."""
    if not local_dir.exists():
        return
    try:
        from ..core.s3_store import upload_dir
        count = await asyncio.to_thread(upload_dir, local_dir, prefix)
        log.info("[%s] %s → S3 %d 파일 업로드", doc_id, label, count)
    except Exception:
        log.exception("[%s] %s S3 업로드 실패 (무시)", doc_id, label)


async def _ensure_extracted_from_s3(doc_id: str) -> None:
    """extracted/{doc_id}/ 가 로컬에 없으면 S3에서 복원 (서버 재시작 후 resume 지원)."""
    settings = get_settings()
    extracted_dir = settings.extracted_dir / doc_id
    if extracted_dir.exists() and any(extracted_dir.iterdir()):
        return
    log.info("[%s] extracted 로컬 없음 → S3에서 복원 시도", doc_id)
    try:
        from ..core.s3_store import download_dir
        count = await asyncio.to_thread(
            download_dir, f"documents/{doc_id}/extracted", extracted_dir
        )
        log.info("[%s] S3에서 extracted %d 파일 복원", doc_id, count)
    except Exception:
        log.exception("[%s] S3 extracted 복원 실패", doc_id)
        return

    # 복원된 산출물이 현재 실행분인지 검증 — run_id 불일치는 오래된 결과 가능성
    try:
        p3 = extracted_dir / "phase3_output.json"
        if p3.exists():
            restored_run = json.loads(p3.read_text(encoding="utf-8")).get("run_id", "")
            current_run = await get_current_run_id(doc_id)
            if restored_run and current_run and restored_run != current_run:
                log.warning(
                    "[%s] S3 복원 산출물 run_id 불일치 (restored=%s, current=%s) "
                    "— 이전 실행 결과일 수 있음. 재분석 권장",
                    doc_id, restored_run, current_run,
                )
    except Exception:
        log.warning("[%s] 복원 산출물 run_id 검증 실패 (무시)", doc_id, exc_info=True)


async def _push_pages_to_drive(doc_id: str) -> None:
    """OCR 완료 후 — pages 디렉토리를 Drive에 업로드 (Drive 미설정 시 no-op)."""
    drive = get_drive()
    if not drive:
        return
    settings = get_settings()
    doc = await get_document(doc_id)
    if not doc:
        return
    hatsu_month = doc.get("hatsu_month") or ""
    if not hatsu_month:
        return
    pages_dir = settings.samples_dir / f"{doc_id}_pages"
    try:
        await asyncio.to_thread(
            drive.push_pages,
            settings.drive_root_folder_id,
            hatsu_month,
            pages_dir,
            doc_id,
        )
        log.info("[%s] Drive pages 업로드 완료", doc_id)
    except Exception:
        log.exception("[%s] Drive pages 업로드 실패 (무시)", doc_id)


async def _push_to_drive(doc_id: str) -> None:
    """Phase 4 완료 후 — extracted/ 결과물을 Drive에 업로드 (Drive 미설정 시 no-op)."""
    drive = get_drive()
    if not drive:
        return
    settings = get_settings()
    doc = await get_document(doc_id)
    if not doc:
        return
    hatsu_month = doc.get("hatsu_month") or ""
    if not hatsu_month:
        return
    try:
        await asyncio.to_thread(
            drive.push_extracted,
            settings.drive_root_folder_id,
            hatsu_month,
            settings.extracted_dir,
            doc_id,
        )
        log.info("[%s] Drive extracted 업로드 완료", doc_id)
    except Exception:
        log.exception("[%s] Drive 동기화 실패 (로컬 파일 유지)", doc_id)


async def _write_results_to_sheets(doc_id: str) -> None:
    """Phase 4 완료 후 Sheets results 탭에 소매처별 NET 결과를 기록한다."""
    from ..core.sheets_store import get_sheets_store
    sheets = get_sheets_store()
    if not sheets:
        return

    settings = get_settings()
    p3_path = settings.extracted_dir / doc_id / "phase3_output.json"
    p4_path = settings.extracted_dir / doc_id / "phase4_output.json"
    if not p3_path.exists() or not p4_path.exists():
        log.warning("[%s] results 기록 건너뜀: phase3/4 출력 파일 없음", doc_id)
        return

    try:
        p3 = json.loads(p3_path.read_text(encoding="utf-8"))
        p4 = json.loads(p4_path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("[%s] results 기록: 출력 파일 파싱 실패", doc_id)
        return

    issuer_name = (p3.get("issuer") or {}).get("name", "")
    hatsu_month = p3.get("hatsu_month", "")

    # dist_code → NET 합산 (受注先コード 컬럼)
    dist_net: dict[str, float] = {}
    for row in p4.get("rows", []):
        dc = str(row.get("受注先コード", "") or "")
        try:
            net = float(row.get("NET") or row.get("net") or 0)
        except (ValueError, TypeError):
            net = 0.0
        if dc:
            dist_net[dc] = dist_net.get(dc, 0.0) + net

    # retailer_code → 소매처명 (retail_user.csv: 소매처코드, 소매처명)
    retail_map: dict[str, str] = {}
    try:
        for r in sheets.read_csv("retail_user.csv"):
            rc = str(r.get("소매처코드", "") or "")
            rn = str(r.get("소매처명", "") or "")
            if rc:
                retail_map[rc] = rn
    except Exception:
        log.warning("[%s] retail_user.csv 로드 실패 (소매처명 공백으로 기록)", doc_id)

    confirmed = p3.get("confirmed_retailers", {})
    seen: set[str] = set()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    written = 0

    for mapping in confirmed.values():
        rc = str(mapping.get("retailer_code", "") or "")
        dc = str(mapping.get("dist_code", "") or "")
        if not rc or rc in seen:
            continue
        seen.add(rc)
        rn = retail_map.get(rc, "")
        net_val = dist_net.get(dc, 0.0)
        net_str = str(int(net_val)) if net_val == int(net_val) else f"{net_val:.2f}"
        await asyncio.to_thread(
            sheets.append_to_tab,
            "results",
            [doc_id, issuer_name, hatsu_month, rc, rn, net_str, now_str],
        )
        written += 1

    if written:
        log.info("[%s] Sheets results 기록: %d 소매처", doc_id, written)
    else:
        log.warning("[%s] results 기록: confirmed_retailers 없음 — 건너뜀", doc_id)


def _extract_table_headers(md_text: str) -> str:
    """page MD에서 테이블 헤더 행만 추출. 데이터 행은 제거해 context를 줄인다."""
    # 코드 펜스 제거
    if md_text.startswith("```"):
        lines = md_text.splitlines()
        # 첫 펜스 제거
        lines = lines[1:]
        # 마지막 ``` 제거
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == "```":
                lines = lines[:i]
                break
        md_text = "\n".join(lines)

    result_lines = []
    prev_was_header = False
    for line in md_text.splitlines():
        stripped = line.strip()
        # 테이블 행 판별
        if stripped.startswith("|") and stripped.endswith("|"):
            cols = [c.strip() for c in stripped.split("|")[1:-1]]
            # 구분 행(---|---) 은 건너뜀
            if all(set(c) <= set("-: ") for c in cols):
                prev_was_header = False
                continue
            # 첫 번째 헤더 행만 포함 (데이터 행 제외)
            if not prev_was_header:
                result_lines.append(line)
                prev_was_header = True
            # else: 데이터 행 — 생략
        else:
            prev_was_header = False
            result_lines.append(line)
    return "\n".join(result_lines)


async def _identify_form(doc_id: str, pages_dir: Path) -> tuple[str, float]:
    """OCR txt(결정적) → form_*.md 식별 패턴과 문자열 매칭.
    미인식 시 ('unknown', 0.0).
    """
    settings = get_settings()

    ocr_files = sorted(pages_dir.glob("page_*.ocr.txt"))[:3]
    if not ocr_files:
        # 로컬 파일 없으면 S3에서 fallback
        log.warning("[%s] 로컬 OCR txt 없음 — S3 fallback 시도", doc_id)
        try:
            from ..core.s3_store import list_keys, read_text as s3_read_text
            s3_prefix = f"documents/{doc_id}/pages/"
            all_keys = list_keys(s3_prefix)
            ocr_keys = sorted(k for k in all_keys if k.endswith(".ocr.txt"))[:3]
            if not ocr_keys:
                log.warning("[%s] S3 OCR txt 없음 — 양식 식별 불가", doc_id)
                return "unknown", 0.0
            texts = [s3_read_text(k) or "" for k in ocr_keys]
            ocr_text = "\n".join(texts)
            log.info("[%s] S3 fallback — %d개 OCR txt 로드", doc_id, len(ocr_keys))
        except Exception:
            log.exception("[%s] S3 OCR fallback 실패 — 양식 식별 불가", doc_id)
            return "unknown", 0.0
    else:
        ocr_text = "\n".join(f.read_text(encoding="utf-8") for f in ocr_files)

    form_dir = settings.form_definitions_dir
    for form_path in sorted(form_dir.glob("form_[0-9]*.md")):
        form_id = form_path.stem
        text = form_path.read_text(encoding="utf-8")
        m = re.search(r"##\s*식별\s*패턴\s*\n+(.+)", text)
        if not m:
            continue
        patterns = re.findall(r"`([^`]+)`", m.group(1))
        if not patterns:
            continue
        if all(p in ocr_text for p in patterns):
            log.info("[%s] 양식 식별: %s (패턴: %s)", doc_id, form_id, patterns)
            return form_id, 1.0
        missing = [p for p in patterns if p not in ocr_text]
        log.warning("[%s] %s 불일치 — 없는 패턴: %s", doc_id, form_id, missing)

    log.warning("[%s] 양식 식별 실패 — 매칭되는 form 없음 (OCR 파일: %d개, 텍스트 앞100자: %s)",
                doc_id, len(ocr_files), repr(ocr_text[:100]))
    return "unknown", 0.0


async def _set_form_id(doc_id: str, form_id: str) -> None:
    await set_form_id(doc_id, form_id)


def _check_master_availability() -> str | None:
    """Sheets 모드일 때 마스터(unit_price) 가용성 확인. 문제 있으면 사유 문자열, 없으면 None.

    로컬 CSV 모드(Sheets 미설정)는 검사하지 않는다 — 파일 기반은 가드 대상이 아님.
    빈 unit_price는 운영상 정상이 아니므로(=토큰/네트워크 장애) 분석을 막는다.
    """
    from ..core.sheets_store import get_sheets_store, SheetsUnavailableError
    store = get_sheets_store()
    if store is None:
        from ..core.config import get_settings
        # Sheets가 설정돼 있는데 인스턴스 생성 실패 = 토큰/네트워크 장애
        if get_settings().google_sheets_mappings_id:
            from ..core.sheets_store import _init_error_msg
            return f"Sheets 초기화 실패 (토큰 만료/네트워크 가능성): {_init_error_msg or 'unknown'}"
        return None  # 로컬 CSV 모드 — 가드 비대상
    ok, reason = store.probe()
    if not ok:
        return f"Sheets 연결 probe 실패: {reason}"
    try:
        if not store.read_csv("unit_price.csv", required=True):
            return "unit_price 마스터가 비어 있음 (탭 비정상 또는 권한 문제)"
    except SheetsUnavailableError as e:
        return str(e)
    return None


def _compute_pipeline_hashes(form_id: str, settings) -> dict:
    """이번 실행이 어떤 규칙 버전으로 계산되는지 식별하는 해시 묶음.
    form 정의 MD·form_types.json 항목·phase 프롬프트가 대상."""
    def _h(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    hashes: dict[str, str] = {}
    form_path = settings.form_definitions_dir / f"{form_id}.md"
    if form_path.exists():
        hashes["form_definition_hash"] = _h(form_path.read_text(encoding="utf-8"))
    ft_path = settings.workspace_root / "config" / "form_types.json"
    if ft_path.exists():
        entry = json.loads(ft_path.read_text(encoding="utf-8")).get(form_id, {})
        hashes["form_types_hash"] = _h(json.dumps(entry, ensure_ascii=False, sort_keys=True))
    for name in ("phase1-prompt.md", "phase2-prompt.md", "phase3-prompt.md"):
        p = settings.workspace_root / "docs" / name
        if p.exists():
            hashes[f"{name.split('-')[0]}_prompt_hash"] = _h(p.read_text(encoding="utf-8"))
    return hashes


async def _record_pipeline_hashes(doc_id: str, form_id: str, settings) -> None:
    """해시 기록 실패가 파이프라인을 막지 않도록 분리."""
    try:
        hashes = await asyncio.to_thread(_compute_pipeline_hashes, form_id, settings)
        await set_pipeline_hashes(doc_id, hashes)
    except Exception:
        log.warning("[%s] pipeline 해시 기록 실패 (무시)", doc_id, exc_info=True)


def _get_page_roles(extracted_dir: Path) -> dict[int, str]:
    """page MD frontmatter의 page_type_hint를 읽어 {page_num: role} 반환."""
    roles: dict[int, str] = {}
    for f in extracted_dir.glob("page_*.md"):
        m = re.search(r"page_(\d+)\.md", f.name)
        if not m:
            continue
        page_num = int(m.group(1))
        content = f.read_text(encoding="utf-8")
        hint = re.search(r"^page_type_hint:\s*(\w+)", content, re.MULTILINE | re.IGNORECASE)
        roles[page_num] = hint.group(1).lower() if hint else "unknown"
    return roles


def _build_detail_chunks(
    extracted_dir: Path,
    page_range: tuple[int, int] | None = None,
) -> list[list[int]] | None:
    """detail 페이지 수가 임계값 초과 시 청크 목록 반환. 불필요하면 None.

    page_range: (start, end) 지정 시 해당 범위 내 페이지만 대상으로 삼는다 (번들 모드용).
    각 청크 = cover 전체 + 가장 가까운 summary 최대 N개 + detail 페이지 N개.
    PHASE2_OVERLAP=0(기본값): 각 페이지는 정확히 하나의 청크에만 속한다.
    교차 페이지 블록이 발생해도 phase2_verify가 역산 검증으로 복구한다.
    """
    roles = _get_page_roles(extracted_dir)
    if page_range:
        start_p, end_p = page_range
        roles = {p: r for p, r in roles.items() if start_p <= p <= end_p}
    cover_pages   = sorted(p for p, r in roles.items() if r == "cover")
    summary_pages = sorted(p for p, r in roles.items() if r == "summary")
    detail_pages  = sorted(p for p, r in roles.items() if r not in ("cover", "summary"))

    if len(detail_pages) <= _DETAIL_CHUNK_THRESHOLD:
        return None

    chunks: list[list[int]] = []
    i = 0
    while i < len(detail_pages):
        end = i + _DETAIL_CHUNK_SIZE
        is_last = end >= len(detail_pages)
        main_detail = detail_pages[i:] if is_last else detail_pages[i:end]

        # 앞 overlap: 이전 청크 마지막 페이지
        back = max(0, i - _DETAIL_OVERLAP)
        # 뒤 overlap: 다음 청크 첫 페이지
        fwd_end = len(detail_pages) if is_last else end + _DETAIL_OVERLAP
        chunk_detail = detail_pages[back:fwd_end]

        chunk_min, chunk_max = min(main_detail), max(main_detail)
        nearest_summary = sorted(
            summary_pages,
            key=lambda p: min(abs(p - chunk_min), abs(p - chunk_max)),
        )[:_MAX_SUMMARY_PER_CHUNK]
        chunks.append(sorted(set(cover_pages + nearest_summary + chunk_detail)))
        i += _DETAIL_CHUNK_SIZE

    return chunks


def _dedup_items(items: list[dict]) -> list[dict]:
    """invoice_no 또는 내용 해시 기준으로 중복 항목 제거.

    overlap 경계에서 같은 항목이 여러 청크에서 추출될 수 있어 필요.
    kanri_no를 hash에 포함해 다른 管理No의 동일 제품·금액 항목을 별개로 유지한다.
    """
    seen: set[str] = set()
    result: list[dict] = []
    for item in items:
        inv = (item.get("invoice_no") or "").strip()
        if inv:
            key = inv
        else:
            key = hashlib.md5(
                json.dumps(
                    {
                        "kanri_no": item.get("kanri_no", ""),
                        "customer": item.get("customer", ""),
                        "product":  item.get("product", ""),
                        "columns":  item.get("columns", {}),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _is_skip_page(content: str, cfg: dict) -> bool:
    """bundle_detection.skip_markers 기준으로 번들 기점에서 제외할 페이지를 감지한다.

    skip_markers 중 하나라도 있고, skip_excluded 중 어느 것도 없으면 제외 대상.
    설정이 없으면 항상 False.
    """
    skip_markers = cfg.get("skip_markers", [])
    skip_excluded = cfg.get("skip_excluded", [])
    if not skip_markers:
        return False
    has_skip = any(m in content for m in skip_markers)
    has_excluded = any(m in content for m in skip_excluded)
    return has_skip and not has_excluded


def _is_extra_cover_page(content: str, cfg: dict) -> bool:
    """bundle_detection 설정 기준으로 Phase 1이 놓친 cover 페이지를 감지한다.

    cover_required:     전부 존재해야 함 (AND)
    cover_required_any: 하나 이상 존재해야 함 (OR)
    cover_excluded:     어느 것도 없어야 함 (NOT ANY)
    cover_required/cover_required_any 모두 없으면 항상 False.
    """
    required = cfg.get("cover_required", [])
    required_any = cfg.get("cover_required_any", [])
    excluded = cfg.get("cover_excluded", [])
    if not required and not required_any:
        return False
    if required and not all(m in content for m in required):
        return False
    if required_any and not any(m in content for m in required_any):
        return False
    if any(m in content for m in excluded):
        return False
    return True


def _detect_bundles(extracted_dir: Path, bundle_cfg: dict | None = None) -> list[tuple[int, int]]:
    """page MD의 page_type_hint: cover 를 감지해 번들 경계를 반환.

    bundle_cfg: form_types.json의 bundle_detection 설정. None이면 Phase 1 hint만 사용.
    Phase 1 오분류 보완 및 skip 페이지 제외는 bundle_cfg가 있을 때만 동작한다.
    Returns: [(start_page, end_page), ...] 1-indexed. 단일 번들이면 빈 리스트.
    """
    cfg = bundle_cfg or {}
    md_files = sorted(extracted_dir.glob("page_*.md"))
    if not md_files:
        return []

    cover_pages: list[int] = []
    for f in md_files:
        m = re.search(r"page_(\d+)\.md", f.name)
        if not m:
            continue
        page_num = int(m.group(1))
        content = f.read_text(encoding="utf-8")
        if _is_skip_page(content, cfg):
            continue
        is_hint_cover = bool(re.search(r"^page_type_hint:\s*cover", content, re.MULTILINE | re.IGNORECASE))
        if is_hint_cover and (cfg.get("cover_required") or cfg.get("cover_required_any") or cfg.get("cover_excluded")):
            is_hint_cover = _is_extra_cover_page(content, cfg)
        if is_hint_cover or _is_extra_cover_page(content, cfg):
            cover_pages.append(page_num)

    if len(cover_pages) <= 1:
        return []  # 단일 번들 또는 감지 불가

    total = max(
        int(re.search(r"page_(\d+)\.md", f.name).group(1))
        for f in md_files
    )
    bundles = []
    for i, start in enumerate(cover_pages):
        end = cover_pages[i + 1] - 1 if i + 1 < len(cover_pages) else total
        bundles.append((start, end))
    return bundles


def _merge_phase2_results(results: list[dict]) -> dict:
    """여러 청크/번들의 Phase 2 출력을 하나로 합침.

    - pages[]: page 번호 기준 dedup (cover가 여러 청크에 반복 등장)
    - items[]: invoice_no 또는 내용 해시 기준 dedup (overlap 경계 중복 제거)
    - cover_totals: 첫 번째 결과에서만 가져옴
    - issuer: pages[]에 포함되므로 별도 반환 불필요 — phase3.py가 pages[]를 순회해 추출함
    """
    seen_pages: set[int] = set()
    pages: list[dict] = []
    for r in results:
        for p in r.get("pages", []):
            pn = p.get("page")
            if pn not in seen_pages:
                seen_pages.add(pn)
                pages.append(p)
    pages.sort(key=lambda p: p.get("page", 0))

    all_items: list[dict] = []
    for r in results:
        all_items.extend(r.get("items", []))

    first = results[0] if results else {}
    return {
        "pages":        pages,
        "items":        _dedup_items(all_items),
        "bundle_count": len(results),
        "cover_totals": first.get("cover_totals", {}),
    }
