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
    await reset_stalled_on_startup()
    watcher = asyncio.create_task(stall_watcher())
    yield
    watcher.cancel()


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
    return {"ok": True}
