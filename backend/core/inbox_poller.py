"""Drive rebate-inbox/ 폴링 — 신규 PDF 감지 → 파이프라인 자동 트리거.

폴더 구조:
  rebate-inbox/
    {YYYYMM}/          ← 현업이 청구연월 폴더를 만들고 PDF를 올림
      invoice.pdf
      ...

동작 방식:
  1. rebate-inbox/ 아래 YYYYMM 서브폴더 목록 조회
  2. 각 서브폴더에서 PDF 목록 조회 (서브폴더명 = hatsu_month)
  3. S3 config/drive_inbox_processed.json 에 미기록된 신규 파일 처리
  4. PDF 다운로드 → samples/ 저장 → S3 원본 업로드
  5. 문서 메타 생성 → 파이프라인 트리거 (EC2 백그라운드)
  6. 처리된 file_id를 S3 processed set에 추가 (재처리 방지)

비활성화: DRIVE_INBOX_FOLDER_ID 미설정 시 lifespan에서 태스크 생성 안 함.
"""
import asyncio
import logging
import re
from pathlib import Path
from uuid import uuid4

log = logging.getLogger(__name__)

_PROCESSED_KEY = "config/drive_inbox_processed.json"
_SYSTEM_USER_ID = 0
_SYSTEM_USERNAME = "inbox"
_SYSTEM_NAME_JA = "自動受信"
_FOLDER_MIME = "application/vnd.google-apps.folder"
_YYYYMM_RE = re.compile(r"^20\d{2}(0[1-9]|1[0-2])$")


def _load_processed() -> set[str]:
    from .s3_store import read_json
    data = read_json(_PROCESSED_KEY) or {}
    return set(data.get("processed", []))


def _save_processed(processed: set[str]) -> None:
    from .s3_store import write_json
    write_json(_PROCESSED_KEY, {"processed": sorted(processed)})


async def poll_once() -> int:
    """inbox를 한 번 폴링해 신규 PDF를 처리한다. 처리된 건수 반환."""
    from .config import get_settings, get_drive
    settings = get_settings()

    if not settings.drive_inbox_folder_id:
        return 0

    drive = get_drive()
    if not drive:
        return 0

    # rebate-inbox/ 아래 서브폴더(= 청구연월) 목록
    try:
        entries = await asyncio.to_thread(drive.list_folder, settings.drive_inbox_folder_id)
    except Exception:
        log.exception("inbox 폴더 목록 조회 실패")
        return 0

    month_folders = [
        e for e in entries
        if e["mimeType"] == _FOLDER_MIME and _YYYYMM_RE.match(e["name"])
    ]
    if not month_folders:
        return 0

    processed = await asyncio.to_thread(_load_processed)
    triggered = 0

    for folder in month_folders:
        hatsu_month = folder["name"]   # e.g. "202601"
        folder_id = folder["id"]

        try:
            files = await asyncio.to_thread(drive.list_pdf_files, folder_id)
        except Exception:
            log.exception("inbox/%s PDF 목록 조회 실패", hatsu_month)
            continue

        new_files = [f for f in files if f["id"] not in processed]
        if not new_files:
            continue

        log.info("inbox/%s 신규 PDF %d건", hatsu_month, len(new_files))
        for file_info in new_files:
            try:
                await _process_inbox_file(
                    drive, file_info["id"], file_info["name"], hatsu_month, settings
                )
                processed.add(file_info["id"])
                await asyncio.to_thread(_save_processed, processed)
                triggered += 1
            except Exception:
                log.exception("inbox 파일 처리 실패: %s/%s", hatsu_month, file_info["name"])

    return triggered


async def _process_inbox_file(
    drive, file_id: str, filename: str, hatsu_month: str, settings
) -> None:
    """단일 inbox 파일을 다운로드하고 파이프라인을 트리거한다."""
    from ..db.queries import create_document
    from .s3_store import upload_file

    doc_id = str(uuid4())

    settings.samples_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = settings.samples_dir / filename

    log.info("[%s] inbox/%s/%s 다운로드 중", doc_id, hatsu_month, filename)
    await asyncio.to_thread(drive.download_file, file_id, pdf_path)

    # S3에 원본 업로드
    await asyncio.to_thread(upload_file, pdf_path, f"documents/{doc_id}/original.pdf")

    # 문서 메타 생성
    await create_document(
        doc_id=doc_id,
        pdf_filename=filename,
        hatsu_month=hatsu_month,
        user_id=_SYSTEM_USER_ID,
        uploaded_by_username=_SYSTEM_USERNAME,
        uploaded_by_name_ja=_SYSTEM_NAME_JA,
    )

    _trigger_pipeline(doc_id, hatsu_month, pdf_path, settings)
    log.info("[%s] 파이프라인 트리거 완료 (hatsu_month=%s, file=%s)", doc_id, hatsu_month, filename)


def _trigger_pipeline(doc_id: str, hatsu_month: str, pdf_path: Path, settings) -> None:
    from ..pipeline.orchestrator import run_pipeline
    asyncio.create_task(run_pipeline(doc_id, pdf_path, hatsu_month))
    log.info("[%s] EC2 백그라운드 파이프라인 시작", doc_id)


async def inbox_poller_loop() -> None:
    """메인 lifespan에서 시작되는 무한 폴링 루프."""
    from .config import get_settings
    settings = get_settings()
    interval = settings.drive_inbox_poll_interval
    log.info("Drive inbox 폴러 시작 (간격: %ds, inbox=%s)", interval, settings.drive_inbox_folder_id)
    while True:
        try:
            count = await poll_once()
            if count:
                log.info("inbox 폴링: %d건 처리", count)
        except asyncio.CancelledError:
            log.info("Drive inbox 폴러 종료")
            return
        except Exception:
            log.exception("inbox 폴링 루프 오류 (계속 진행)")
        await asyncio.sleep(interval)
