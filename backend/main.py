import asyncio
import logging
import socket

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .core.stall_guard import reset_stalled_on_startup, stall_watcher
from .api.routes import auth, documents, mappings, forms, search, form_manage, reviews, sap, admin_retail, usage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


class _SuppressPolling(logging.Filter):
    _PATHS = {"/api/v3/documents", "/health"}

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(p in record.getMessage() for p in self._PATHS)


logging.getLogger("uvicorn.access").addFilter(_SuppressPolling())


def _local_origins() -> list[str]:
    """로컬 IP 기반 CORS origin 목록 자동 생성."""
    origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
        if local_ip and not local_ip.startswith("127."):
            origins.append(f"http://{local_ip}:5173")
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            origins.append(f"http://{ip}:5173")
    except Exception:
        pass
    import os
    extra = os.environ.get("EXTRA_CORS_ORIGINS", "")
    if extra:
        origins.extend(o.strip() for o in extra.split(",") if o.strip())
    return list(dict.fromkeys(origins))


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 기본 풀은 cpu+4 (EC2 2 vCPU → 6개) — 파이프라인의 S3·Drive·파일 작업이
    # 점유하면 API 요청의 S3 읽기까지 줄을 서서 프론트 전체가 멈춘 듯 보인다.
    # I/O 대기 스레드이므로 넉넉히 확장.
    from concurrent.futures import ThreadPoolExecutor
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=32, thread_name_prefix="io")
    )
    await reset_stalled_on_startup()
    watcher = asyncio.create_task(stall_watcher())
    from .core.config import get_settings
    settings = get_settings()
    poller = None
    if settings.drive_inbox_folder_id:
        from .core.inbox_poller import inbox_poller_loop
        poller = asyncio.create_task(inbox_poller_loop())
    yield
    watcher.cancel()
    if poller:
        poller.cancel()


app = FastAPI(title="Rebate Lecture API", version="3.0.0", lifespan=lifespan)

origins = _local_origins()
logging.getLogger(__name__).info("CORS origins: %s", origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins + [o.replace(":5173", ":5174") for o in origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(mappings.router)
app.include_router(forms.router)
app.include_router(form_manage.router)
app.include_router(search.router)
app.include_router(reviews.router)
app.include_router(sap.router)
app.include_router(admin_retail.router)
app.include_router(usage.router)


@app.get("/health")
async def health():
    """가벼운 헬스 — API 왕복 없이 캐시된 Sheets 상태 요약 포함.

    sheets.status: disabled(미설정) / ok / degraded(최근 fetch 실패) / uninitialized / error.
    토큰 만료를 적극 감지하려면 /health/sheets(deep probe)를 폴링한다.
    """
    from .core.sheets_store import get_sheets_health
    return {"ok": True, "sheets": get_sheets_health(deep=False)}


@app.get("/health/sheets")
async def health_sheets():
    """Sheets 토큰·연결 deep probe — 모니터링/운영자 폴링용.

    토큰 만료·refresh_token 철회를 분석 실패 전에 잡기 위한 조기 경보 엔드포인트.
    상태가 error면 503으로 응답해 외부 모니터가 알람을 띄울 수 있게 한다.
    """
    from fastapi.responses import JSONResponse
    from .core.sheets_store import get_sheets_health
    h = await asyncio.to_thread(get_sheets_health, True)
    code = 503 if h.get("status") == "error" else 200
    return JSONResponse(status_code=code, content={"ok": code == 200, "sheets": h})
