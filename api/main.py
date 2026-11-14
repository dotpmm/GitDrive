import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import config, db
from .routes_admin import router as admin_router
from .routes_auth import router as auth_router
from .routes_files import reap_uploads, router as files_router
from .routes_nodes import router as nodes_router
from .routes_spaces import router as spaces_router

logging.basicConfig(level=logging.DEBUG if config.LOG_MODE == "debug" else logging.INFO,
                    format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
for _lib in ("pymongo", "httpcore", "httpx", "urllib3", "uvicorn.access"):
    logging.getLogger(_lib).setLevel(logging.WARNING)
log = logging.getLogger("GitDrive")


async def _reaper() -> None:
    while True:
        await asyncio.sleep(600)
        await asyncio.to_thread(reap_uploads)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(db.ensure_public_space)
    task = asyncio.create_task(_reaper())
    yield
    task.cancel()


app = FastAPI(title="GitDrive", version="4.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Range"],
    expose_headers=["Content-Disposition", "Content-Length", "Content-Range", "Accept-Ranges"],
)

app.include_router(auth_router)
app.include_router(spaces_router)
app.include_router(nodes_router)
app.include_router(files_router)
app.include_router(admin_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": app.version}
