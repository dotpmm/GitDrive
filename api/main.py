import asyncio
import hashlib
import hmac
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from . import db
from .github import GitHubClient

load_dotenv()

# ---------------------------------------------------------------- config ----
_github_token = os.getenv("GITHUB_TOKEN")
if not _github_token:
    raise RuntimeError("GITHUB_TOKEN is not set (see README -> Self-Hosting Guide)")

# Encryption key. ENCRYPTION_KEY is the recommended source: it lets you rotate the
# GitHub token without losing access to your files. If it is not set we fall back to
# the token (the old behaviour), so already-stored files keep working.
# The token-derived key is always kept as a *decrypt* fallback for the same reason.
_token_key = hashlib.sha256(_github_token.encode()).digest()
_enc_src = os.getenv("ENCRYPTION_KEY")
_aes_key = hashlib.sha256(_enc_src.encode()).digest() if _enc_src else _token_key
_decrypt_keys = [_aes_key] + ([_token_key] if _token_key != _aes_key else [])

# Optional shared secret. When API_KEY is set every API call must send it in the
# X-API-Key header. (Leave it unset for the old, open behaviour.)
API_KEY = os.getenv("API_KEY")
# Comma separated list of allowed browser origins, e.g. "https://my-app.vercel.app"
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]


def _encrypt(data: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_aes_key).encrypt(nonce, data, None)


def _decrypt(data: bytes) -> bytes:
    for key in _decrypt_keys:
        try:
            return AESGCM(key).decrypt(data[:12], data[12:], None)
        except Exception:
            continue
    # Older files were stored unencrypted, so nothing decrypts them - hand them back as is.
    log.debug("DECRYPT fallback: returning blob unchanged (%d bytes)", len(data))
    return data


_log_level = logging.DEBUG if os.getenv("LOG_MODE", "prod").lower() == "debug" else logging.INFO

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)

# all these guys pollute the logs, hence cleaned!
for _lib in ("pymongo", "httpcore", "httpx", "urllib3", "uvicorn.access"):
    logging.getLogger(_lib).setLevel(logging.WARNING)
log = logging.getLogger("GitDrive")

# tmp directory for upload queue
_tmp      = Path(tempfile.gettempdir()) / "GitDrive"
_github   = GitHubClient(token=_github_token)
_sessions: dict[str, dict] = {}
_queue: asyncio.Queue

CHUNK_SIZE  = 10 * 1024 * 1024   # 10 MB, cuz, base64 encoding inflates a bit...anddd smaller the better!
MAX_RETRIES = 3
RETRY_WAIT  = 1
# workers who take chunks from the temp folder and upload asynchronously
WORKER_COUNT = 5
SESSION_TTL  = 60 * 60           # abandoned upload sessions are dropped after 1 hour
_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# now all the files stored end with .chunk extension
def _object_path(sha: str) -> str:
    return f"objects/{sha[:2]}/{sha[2:]}.chunk"


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    if API_KEY and not (x_api_key and hmac.compare_digest(x_api_key, API_KEY)):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")


def _content_disposition(filename: str) -> str:
    # plain filename= must be latin-1 safe and free of quotes / control chars,
    # filename*= carries the real UTF-8 name
    safe = re.sub(r'[\r\n"\\;]', "_", filename).encode("ascii", "replace").decode() or "download"
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(filename, safe='')}"


# needed cuz , github throws a bad mac error sometimes on some systems (atleast on mine.. :( )
def _upload_with_retry(repo: str, path: str, content: bytes, filename: str | None = None) -> dict:
    last_exc: Exception = RuntimeError("no attempt made")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = _github.upload_file(repo=repo, path=path, content=content, filename=filename)
            log.debug("GITPUSH path=%s attempt=%d", path, attempt)
            return result
        except Exception as exc:
            last_exc = exc
            log.warning("GITFAIL path=%s attempt=%d/%d err=%s", path, attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT)
    raise last_exc


def _release_reservation(sess: dict) -> None:
    """Give back the repo space reserved when the session started (once)."""
    if sess.get("reserved"):
        db.increment_repo_size(sess["repo"], -sess["reserved"])
        sess["reserved"] = 0


async def _fail_session(sess: dict, message: str) -> None:
    if sess["status"] == "error":
        return
    sess["status"] = "error"
    sess["error"] = message
    shutil.rmtree(sess["dir"], ignore_errors=True)
    await asyncio.to_thread(_release_reservation, sess)


# a small function which goes green when the worker upload queue completes
async def _finalize(sess: dict) -> None:
    file_sha = sess["file_sha"]
    log.info("UPLOAD SUCCESSFUL id=%.8s file_sha=%.12s chunks=%d", sess["upload_id"], file_sha, sess["total_chunks"])

    try:
        if await asyncio.to_thread(db.find_by_sha256, file_sha):
            sess["status"] = "duplicate"
            await asyncio.to_thread(_release_reservation, sess)
            shutil.rmtree(sess["dir"], ignore_errors=True)
            log.info("DUPLICATE file_sha=%.12s", file_sha)
            return

        chunks = [
            {"index": i, "sha256": m["sha256"], "size": m["size"]}
            for i, m in sorted(sess["chunk_meta"].items())
        ]
        await asyncio.to_thread(
            db.upsert_file,
            sha256=file_sha,
            original_filename=sess["filename"],
            repo=sess["repo"],
            relative_path=sess.get("relative_path", ""),
            mime_type=sess["mime_type"],
            size_bytes=sess["received_bytes"],
            is_chunked=sess["total_chunks"] > 1,
            chunks=chunks,
        )
        # the declared size was reserved when the session started; settle the difference
        diff = sess["received_bytes"] - sess["reserved"]
        if diff:
            await asyncio.to_thread(db.increment_repo_size, sess["repo"], diff)
        sess["reserved"] = 0
        sess["status"] = "done"
        shutil.rmtree(sess["dir"], ignore_errors=True)
        # the only place where u can see the filename and its associated sha lmao
        log.info("META SHA=%.12s FILE=%r c=%d repo=%s", file_sha, sess["filename"], sess["total_chunks"], sess["repo"])
    except Exception as exc:
        log.error("FINALIZE FAIL id=%.8s err=%s", sess["upload_id"], exc)
        await _fail_session(sess, f"could not save metadata: {exc}")


async def _process(worker_id: int, job: dict) -> None:
    upload_id = job["upload_id"]
    disk_path = job["disk_path"]
    sess = _sessions.get(upload_id)

    # session failed / was reaped while this chunk was waiting in the queue
    if sess is None or sess["status"] == "error":
        disk_path.unlink(missing_ok=True)
        return

    try:
        data = await asyncio.to_thread(disk_path.read_bytes)
        await asyncio.to_thread(_upload_with_retry, job["repo"], job["github_path"], data, job.get("filename"))
        disk_path.unlink(missing_ok=True)
    except Exception as exc:
        log.error("W%d FAIL id=%.8s idx=%d err=%s", worker_id, upload_id, job["chunk_index"], exc)
        disk_path.unlink(missing_ok=True)
        await _fail_session(sess, str(exc))
        return

    async with sess["lock"]:
        if sess["status"] == "error":
            return
        sess["chunks_uploaded"] += 1
        uploaded, total = sess["chunks_uploaded"], sess["total_chunks"]
        log.debug("W%d UPLOADED %d/%d sesh_id=%.8s", worker_id, uploaded, total, upload_id)
        if uploaded == total and sess["file_sha"] is not None:
            await _finalize(sess)


async def _worker(worker_id: int) -> None:
    # github uploader worker, takes a file from queue and uploads to github
    log.debug("W%d ready", worker_id)
    while True:
        job = await _queue.get()
        try:
            await _process(worker_id, job)
        except Exception:
            log.exception("W%d unexpected error", worker_id)
        finally:
            _queue.task_done()


async def _reaper() -> None:
    """Drop upload sessions nobody ever finished / polled, so they can't leak forever."""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for uid, sess in list(_sessions.items()):
            if now - sess["created"] > SESSION_TTL:
                log.info("REAP id=%.8s status=%s", uid, sess["status"])
                _sessions.pop(uid, None)
                shutil.rmtree(sess["dir"], ignore_errors=True)
                if sess["status"] != "done":
                    await asyncio.to_thread(_release_reservation, sess)


# whole async manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _queue
    _queue = asyncio.Queue()

    if _tmp.exists():
        shutil.rmtree(_tmp)
    _tmp.mkdir(parents=True)

    tasks = [asyncio.create_task(_worker(i + 1)) for i in range(WORKER_COUNT)]
    tasks.append(asyncio.create_task(_reaper()))
    yield
    for t in tasks:
        t.cancel()

app = FastAPI(title="GitDrive", version="3.6.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length"],
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse, response_model=None)
async def root():
    index = Path(__file__).resolve().parent.parent / "index.html"
    if not index.exists():
        return JSONResponse({"name": "GitDrive", "status": "ok"})
    return HTMLResponse(content=index.read_text(encoding="utf-8"))

# BIGGEST PERFORMANCE UPDATE
# with this producer-consumer pipeline,
# ive decreased the overall upload time by almost 40%
# Old pipeline : entire file buffered in API RAM -> 5 users x 10 MB = 50 MB , but it scales with users!!! (dangerous, cuz of OOM)
# New: only workers x 10 MB in RAM at once (5 workers = 50 MB regardless of how many users or how large the files!)
# Chunks spill to disk temp files until workers pick them up
# Total disk at any moment = queued (not-yet-uploaded) chunk data, but bounded and cleaned up immediately after each upload

@app.post("/upload", dependencies=[Depends(require_key)])
async def receive_chunk(
    chunk:        UploadFile = File(...),
    upload_id:    str        = Form(...), # the unique id given for every uploaded chunk (temp, not stored anywhere)
    chunk_index:  int        = Form(...),
    total_chunks: int        = Form(...),
    filename:     str        = Form(...),
    mime_type:    str        = Form(""),
    size_bytes:   int        = Form(...),
    relative_path: str       = Form(""),
) -> dict:
    if not _UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(status_code=400, detail="upload_id must be 8-64 chars of [A-Za-z0-9_-]")
    if total_chunks < 1 or not (0 <= chunk_index < total_chunks) or size_bytes < 0:
        raise HTTPException(status_code=400, detail="invalid chunk_index / total_chunks / size_bytes")

    data      = await chunk.read()
    chunk_sha = _sha256(data)

    log.info("UPLOAD INIT id=%.4s chunk=%d/%d size=%.1fKB sha=%.6s", upload_id, chunk_index + 1, total_chunks, len(data) / 1024, chunk_sha)

    sess = _sessions.get(upload_id)
    if sess is None:
        if chunk_index != 0:
            raise HTTPException(status_code=409, detail="Unknown upload session; start again from chunk 0")

        resolved_mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        repo, is_new  = await asyncio.to_thread(db.get_active_repo) # this checks repo_max_size conditions and then determines
        log.debug("SESSION id=%.4s repo=%s new=%s", upload_id, repo, is_new)

        if is_new:
            try:
                await asyncio.to_thread(_github.create_repo, repo)
            except Exception as exc:
                log.error("REPO CREATE FAIL name=%s err=%s", repo, exc)
                raise HTTPException(status_code=502, detail=f"Could not create GitHub repo: {exc}") from exc
            log.info("REPO CREATE name=%s", repo)

        # reserve the space now so concurrent uploads can't all squeeze into a nearly-full repo
        await asyncio.to_thread(db.increment_repo_size, repo, size_bytes)

        sess_dir = _tmp / upload_id
        sess_dir.mkdir(parents=True, exist_ok=True)

        sess = _sessions[upload_id] = {
            "hasher":     hashlib.sha256(),
            "upload_id":  upload_id,
            "relative_path":   relative_path,
            "total_chunks": total_chunks,
            "filename":   filename,
            "mime_type":  resolved_mime,
            "size_bytes": size_bytes,
            "reserved":   size_bytes,
            "received_bytes": 0,
            "repo":       repo,
            "file_sha":   None,
            "chunk_meta":      {},
            "next_index":      0,
            "chunks_uploaded": 0,
            "status":     "receiving",
            "error":      None,
            "lock":       asyncio.Lock(),
            "dir":        sess_dir,
            "created":    time.time(),
        }

    if sess["status"] == "error":
        _sessions.pop(upload_id, None)
        raise HTTPException(status_code=500, detail=sess["error"] or "upload failed")
    if sess["total_chunks"] != total_chunks:
        raise HTTPException(status_code=400, detail="total_chunks changed mid-upload")
    if chunk_index < sess["next_index"]:
        # client re-sent a chunk we already have; nothing to do
        return {"status": "received", "chunk": chunk_index + 1, "percent": round(sess["next_index"] / total_chunks * 100, 1)}
    if chunk_index != sess["next_index"] or sess["status"] != "receiving":
        # chunks must arrive in order: the whole-file hash is computed incrementally
        raise HTTPException(status_code=409, detail=f"Expected chunk {sess['next_index']}, got {chunk_index}")

    sess["hasher"].update(data)
    sess["received_bytes"] += len(data)
    enc_data = _encrypt(data)
    enc_sha  = _sha256(enc_data)
    sess["chunk_meta"][chunk_index] = {"sha256": enc_sha, "size": len(data)}

    disk_path = sess["dir"] / f"{chunk_index}.part"
    await asyncio.to_thread(disk_path.write_bytes, enc_data)

    await _queue.put({
        "upload_id":   upload_id,
        "chunk_index": chunk_index,
        "chunk_sha":   enc_sha,
        "disk_path":   disk_path,
        "repo":        sess["repo"],
        "github_path": _object_path(enc_sha),
        "filename":    sess["filename"],
    })

    sess["next_index"] += 1
    recv_pct = round(sess["next_index"] / total_chunks * 100, 1)

    log.debug("QUEUE id=%.8s chunk-idx=%d queue-size=%d", upload_id, chunk_index + 1, _queue.qsize())

    if sess["next_index"] == total_chunks:
        sess["file_sha"] = sess["hasher"].hexdigest()
        async with sess["lock"]:
            if sess["status"] == "receiving":
                sess["status"] = "processing"
            log.info("ALL RECEIVED id=%.8s file_sha=%.12s still-in-queue=%d", upload_id, sess["file_sha"], _queue.qsize())
            if sess["status"] == "processing" and sess["chunks_uploaded"] == total_chunks:
                await _finalize(sess)

        return {"status": sess["status"], "upload_id": upload_id, "sha256": sess.get("file_sha")}

    return {"status": "received", "chunk": chunk_index + 1, "percent": recv_pct}

@app.get("/upload/status/{upload_id}", dependencies=[Depends(require_key)])
async def upload_status(upload_id: str) -> dict:
    sess = _sessions.get(upload_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Upload session not found or already complete")

    uploaded = sess["chunks_uploaded"]
    total = sess["total_chunks"]
    gh_pct = round(uploaded / total * 100, 1) if total else 100.0

    log.debug("STATUS id=%.8s status=%s github=%d/%d", upload_id, sess["status"], uploaded, total)

    resp: dict = {"status": sess["status"], "github_percent": gh_pct}

    if sess["status"] in ("done", "duplicate"):
        resp["sha256"] = sess["file_sha"]
        _sessions.pop(upload_id, None)
    elif sess["status"] == "error":
        resp["error"] = sess["error"]
        _sessions.pop(upload_id, None)
    return resp


@app.get("/files", dependencies=[Depends(require_key)])
async def list_files() -> list[dict]:
    return await asyncio.to_thread(db.list_all_files)


def _fetch_chunk(repo: str, chunk: dict) -> bytes:
    log.debug("FETCH CHUNK idx=%d sha=%.12s", chunk["index"], chunk["sha256"])
    return _decrypt(_github.download_file(repo=repo, path=_object_path(chunk["sha256"])))


@app.get("/download/{file_sha256}", response_model=None, dependencies=[Depends(require_key)])
async def download_file(file_sha256: str) -> Response | StreamingResponse:
    record = await asyncio.to_thread(db.find_by_sha256, file_sha256)
    if not record:
        log.warning("404 sha=%.12s", file_sha256)
        raise HTTPException(status_code=404, detail=f"No file found for SHA-256 {file_sha256!r}")

    repo     = record["repo"]
    filename = record["original_filename"]
    headers  = {
        "Content-Disposition": _content_disposition(filename),
        "Content-Length":      str(record["size_bytes"]),
    }
    chunks = sorted(record["chunks"], key=lambda c: c["index"])
    log.info("DOWNLOAD INIT sha=%.12s file=%r chunked=%s", file_sha256, filename, record.get("is_chunked"))

    # Fetch the first chunk before answering, so a GitHub failure becomes a real
    # 502 instead of a broken 200 response.
    try:
        first = await asyncio.to_thread(_fetch_chunk, repo, chunks[0]) if chunks else b""
    except Exception as exc:
        log.error("DOWNLOAD ERROR sha=%.12s err=%s", file_sha256, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if len(chunks) <= 1:
        log.info("DOWNLOAD SUCCESSFUL sha=%.12s size=%dB", file_sha256, len(first))
        headers["Content-Length"] = str(len(first))
        return Response(content=first, media_type=record["mime_type"], headers=headers)

    log.debug("STREAM %d chunks  repo=%s", len(chunks), repo)

    def _iter_chunks():
        yield first
        for c in chunks[1:]:
            try:
                yield _fetch_chunk(repo, c)
            except Exception as exc:
                # headers are already sent: abort the connection so the client sees
                # a failed download instead of a silently truncated file
                log.error("STREAM ABORT sha=%.12s idx=%d err=%s", file_sha256, c["index"], exc)
                raise

    return StreamingResponse(_iter_chunks(), media_type=record["mime_type"], headers=headers)
