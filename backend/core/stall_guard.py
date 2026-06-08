"""분석 중 상태로 굳은 문서를 감지·복구하는 유틸리티.

케이스 1 — 서버 재시작/크래시:
  startup 시 queued/ocr/analyzing 상태 문서를 즉시 error로 리셋.

케이스 2 — 런타임 hang:
  백그라운드 루프가 STALL_TIMEOUT_MINUTES 이상 상태 변화 없는 문서를 error로 처리.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

_STALL_STATES = {"queued", "ocr", "analyzing", "phase1", "phase2", "phase3", "phase4"}
STALL_TIMEOUT_MINUTES = int(os.getenv("STALL_TIMEOUT_MINUTES", "30"))
_WATCH_INTERVAL_SEC = 300  # 5분마다 체크


async def reset_stalled_on_startup() -> None:
    """서버 시작 시 호출. 이전 프로세스가 남긴 진행 중 문서를 error로 리셋."""
    from ..db.queries import list_documents, update_document_error
    docs = await list_documents()
    stalled = [d for d in docs if d.get("status") in _STALL_STATES]
    for doc in stalled:
        await update_document_error(
            doc["doc_id"], "pipeline_stalled", "", "서버 재시작으로 인한 상태 초기화"
        )
    if stalled:
        ids = [d["doc_id"] for d in stalled]
        log.warning("startup: %d건의 중단 문서를 error로 리셋 — %s", len(ids), ids)


async def stall_watcher() -> None:
    """백그라운드 태스크. 런타임 중 hang 문서를 주기적으로 감지."""
    from ..db.queries import list_documents, update_document_error
    log.info("stall_watcher 시작 (타임아웃=%dm, 체크주기=%ds)", STALL_TIMEOUT_MINUTES, _WATCH_INTERVAL_SEC)
    while True:
        await asyncio.sleep(_WATCH_INTERVAL_SEC)
        try:
            docs = await list_documents()
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALL_TIMEOUT_MINUTES)
            stalled = []
            for d in docs:
                if d.get("status") not in _STALL_STATES:
                    continue
                updated = d.get("updated_at")
                if not updated:
                    continue
                try:
                    ts = datetime.fromisoformat(updated)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        stalled.append(d)
                except ValueError:
                    pass
            for doc in stalled:
                await update_document_error(
                    doc["doc_id"], "pipeline_stalled", "", f"{STALL_TIMEOUT_MINUTES}분 이상 상태 변화 없음"
                )
            if stalled:
                ids = [d["doc_id"] for d in stalled]
                log.warning("stall_watcher: %d건 hang 감지 → error 처리 — %s", len(ids), ids)
        except Exception:
            log.exception("stall_watcher 체크 중 오류")
