import asyncio
import logging
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .core.database import close_pool, init_pool, get_pool
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
    # en0 / en1 방식으로도 시도
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            origins.append(f"http://{ip}:5173")
    except Exception:
        pass
    # 환경변수로 추가 origin 지정 가능 (사내망 다른 IP 접근 시 사용)
    import os
    extra = os.environ.get("EXTRA_CORS_ORIGINS", "")
    if extra:
        origins.extend(o.strip() for o in extra.split(",") if o.strip())
    return list(dict.fromkeys(origins))  # 중복 제거


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    await reset_stalled_on_startup(get_pool())
    watcher = asyncio.create_task(stall_watcher(get_pool()))
    yield
    watcher.cancel()
    await close_pool()


app = FastAPI(title="Rebate Lecture API", version="3.0.0", lifespan=lifespan)

origins = _local_origins()
logging.getLogger(__name__).info("CORS origins: %s", origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins + [o.replace(":5173", ":5174") for o in origins],  # Vite 포트 여유
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
    return {"ok": True}
