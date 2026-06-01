"""분석 중 상태로 굳은 문서를 감지·복구하는 유틸리티.

케이스 1 — 서버 재시작/크래시:
  startup 시 queued/ocr/analyzing 상태 문서를 즉시 error로 리셋.

케이스 2 — 런타임 hang:
  백그라운드 루프가 STALL_TIMEOUT_MINUTES 이상 상태 변화 없는 문서를 error로 처리.
"""
import asyncio
import logging
import os

import asyncpg

log = logging.getLogger(__name__)

_STALL_STATES = ("queued", "ocr", "analyzing", "phase1", "phase2", "phase3", "phase4")
STALL_TIMEOUT_MINUTES = int(os.getenv("STALL_TIMEOUT_MINUTES", "30"))
_WATCH_INTERVAL_SEC = 300  # 5분마다 체크


async def reset_stalled_on_startup(pool: asyncpg.Pool) -> None:
    """서버 시작 시 호출. 이전 프로세스가 남긴 진행 중 문서를 error로 리셋."""
    rows = await pool.fetch(
        """
        UPDATE v3_documents
           SET status     = 'error',
               error_type = 'pipeline_stalled',
               updated_at = NOW()
         WHERE status = ANY($1::text[])
        RETURNING doc_id, status
        """,
        list(_STALL_STATES),
    )
    if rows:
        ids = [r["doc_id"] for r in rows]
        log.warning("startup: %d건의 중단 문서를 error로 리셋 — %s", len(ids), ids)


async def stall_watcher(pool: asyncpg.Pool) -> None:
    """백그라운드 태스크. 런타임 중 hang 문서를 주기적으로 감지."""
    log.info("stall_watcher 시작 (타임아웃=%dm, 체크주기=%ds)", STALL_TIMEOUT_MINUTES, _WATCH_INTERVAL_SEC)
    while True:
        await asyncio.sleep(_WATCH_INTERVAL_SEC)
        try:
            rows = await pool.fetch(
                """
                UPDATE v3_documents
                   SET status     = 'error',
                       error_type = 'pipeline_stalled',
                       updated_at = NOW()
                 WHERE status = ANY($1::text[])
                   AND updated_at < NOW() - ($2 || ' minutes')::interval
                RETURNING doc_id, status, updated_at
                """,
                list(_STALL_STATES),
                str(STALL_TIMEOUT_MINUTES),
            )
            if rows:
                ids = [r["doc_id"] for r in rows]
                log.warning("stall_watcher: %d건 hang 감지 → error 처리 — %s", len(ids), ids)
        except Exception:
            log.exception("stall_watcher 체크 중 오류")
