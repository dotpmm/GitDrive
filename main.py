import asyncio
import hashlib
import logging
import mimetypes
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse

import db
from github import GitHubClient

load_dotenv()

_key_src = os.getenv["GITHUB_TOKEN"]
_aes_key = hashlib.sha256(_key_src.encode()).digest()

def _encrypt(data: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_aes_key).encrypt(nonce, data, None)
    #              ^^^^ encrypted blob packet (with the key attached)
def _decrypt(data: bytes) -> bytes:
    # this was needed, cuz so of my older
    # files were not encrypted so, while dowloading them
    # u dont get anything by decrypting them lmao
    # empty string resp
    try:
        return AESGCM(_aes_key).decrypt(data[:12], data[12:], None)
        #      ^^^ splitting and decrypting
    except Exception:
        return data

_log_level = logging.DEBUG if os.getenv("LOG_MODE").lower() == "debug" else logging.INFO

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)

# all these guys pollute the logs, hence cleaned!
for _lib in ("pymongo", "httpcore", "httpx", "urllib3", "uvicorn.access"):
    logging.getLogger(_lib).setLevel(logging.WARNING)
log = logging.getLogger("refactored-carnival")

# tmp directory for upload queue
_tmp      = Path(tempfile.gettempdir()) / "refactored-carnival"
_github   = GitHubClient(token=os.environ["GITHUB_TOKEN"])
_sessions: dict[str, dict] = {}
_queue: asyncio.Queue

CHUNK_SIZE  = 10 * 1024 * 1024   # 10 MB, cuz, base64 encoding inflates a bit...anddd smaller the better!
MAX_RETRIES = 3
RETRY_WAIT  = 1
# workers who take chunks from the temp folder and upload asynchronously 
WORKER_COUNT = 5

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

# now all the files stored end with .chunk extension
def _object_path(sha: str) -> str:
    return f"objects/{sha[:2]}/{sha[2:]}.chunk"

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

# a small function which goes green when the worker upload queue completes
async def _finalize(sess: dict) -> None:
    file_sha = sess["file_sha"]
    log.info("UPLOAD SUCCESSFUL id=%.8s file_sha=%.12s chunks=%d",sess["upload_id"], file_sha, sess["total_chunks"])

    if db.find_by_sha256(sha256=file_sha):
        sess["status"] = "duplicate"
        log.info("DUPLICATE file_sha=%.12s", file_sha)
        return

    is_multi = sess["total_chunks"] > 1
    chunks = [
        {"index": i, "sha256": m["sha256"], "size": m["size"]}
        for i, m in sorted(sess["chunk_meta"].items())
    ]
    db.upsert_file(
        sha256=file_sha,
        original_filename=sess["filename"],
        repo=sess["repo"],
        relative_path=sess.get("relative_path", ""),
        mime_type=sess["mime_type"],
        size_bytes=sess["size_bytes"],
        is_chunked=is_multi,
        chunks=chunks,
    )
    db.increment_repo_size(sess["repo"], sess["size_bytes"])
    sess["status"] = "done"
    shutil.rmtree(sess["dir"], ignore_errors=True)
    # the only place where u can see the filename and its associated sha lmao
    log.info("META SHA=%.12s FILE=%r c=%d repo=%s",file_sha, sess["filename"], sess["total_chunks"], sess["repo"])


async def _worker(worker_id: int) -> None:
    # github uploader worker, takes a file from queue and uploads to github
    log.debug("W%d ready", worker_id)
    while True:
        job = await _queue.get()
        upload_id   = job["upload_id"]
        chunk_index = job["chunk_index"]
        disk_path   = job["disk_path"]

        sess = _sessions.get(upload_id)
        try:
            data = await asyncio.to_thread(disk_path.read_bytes)
            await asyncio.to_thread(_upload_with_retry, job["repo"], job["github_path"], data, job.get("filename"))
            disk_path.unlink(missing_ok=True)

            if sess:
                async with sess["lock"]:
                    sess["chunks_uploaded"] += 1
                    uploaded = sess["chunks_uploaded"]
                    total = sess["total_chunks"]
                    log.debug("W%d UPLOADED %d/%d sesh_id=%.8s",worker_id, uploaded, total, upload_id)
                    if uploaded == total and sess["file_sha"] is not None:
                        await _finalize(sess)

        except Exception as exc:
            log.error("W%d FAIL id=%.8s idx=%d err=%s",worker_id, upload_id, chunk_index, exc)
            disk_path.unlink(missing_ok=True)
            if sess:
                sess["status"] = "error"
                sess["error"]  = str(exc)
                shutil.rmtree(sess["dir"], ignore_errors=True)

        finally:
            _queue.task_done()

# whole async manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _queue
    _queue = asyncio.Queue()

    if _tmp.exists():
        shutil.rmtree(_tmp)
    _tmp.mkdir(parents=True)

    workers = [asyncio.create_task(_worker(i + 1)) for i in range(WORKER_COUNT)]
    yield
    for w in workers:
        w.cancel()

app = FastAPI(title="refactored-carnival", version="3.0.0", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(content=(Path(__file__).parent / "index.html").read_text(encoding="utf-8"))

# BIGGEST PERFORMANCE UPDATE
# with this producer-consumer pipeline,
# ive decreased the overall upload time by almost 40% 
# Old pipeline : entire file buffered in API RAM → 5 users × 10 MB = 50 MB , but it scales with users!!! (dangerous, cuz of OOM)
# New: only workers × 10 MB in RAM at once (3 workers = 30 MB regardless of how many users or how large the files!)
# Chunks spill to disk temp files until workers pick them up
# Total disk at any moment = queued (not-yet-uploaded) chunk data, but bounded and cleaned up immediately after each upload

@app.post("/upload")
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
    t0        = time.perf_counter()
    data      = await chunk.read()
    chunk_sha = _sha256(data)
    is_last   = (chunk_index + 1 == total_chunks)

    log.info("UPLOAD INIT id=%.4s chunk=%d/%d size=%.1fKB sha=%.6s", upload_id, chunk_index + 1, total_chunks, len(data) / 1024, chunk_sha,)

    if upload_id not in _sessions:
        resolved_mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        repo, is_new  = db.get_active_repo() # this checks repo_max_size conditions and then determines
        log.debug("SESSION id=%.4s repo=%s new=%s", upload_id, repo, is_new)

        if is_new:
            await asyncio.to_thread(_github.create_repo, repo)
            log.info("REPO CREATE name=%s", repo)

        sess_dir = _tmp / upload_id
        sess_dir.mkdir(parents=True, exist_ok=True)

        _sessions[upload_id] = {
            "hasher":     hashlib.sha256(),
            "upload_id":  upload_id,
            "relative_path":   relative_path,
            "chunks":     [],
            "total_chunks": total_chunks,
            "filename":   filename,
            "mime_type":  resolved_mime,
            "size_bytes": size_bytes,
            "repo":       repo,
            "file_sha":   None,
            "chunk_meta":      {},
            "chunks_received": 0,
            "chunks_uploaded": 0,
            "status":     "receiving",
            "error":      None,
            "lock":       asyncio.Lock(),
            "dir":        sess_dir,
        }

    sess = _sessions[upload_id]

    sess["hasher"].update(data)
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

    sess["chunks_received"] += 1
    recv_pct = round(sess["chunks_received"] / total_chunks * 100, 1)

    log.debug("QUEUE id=%.8s chunk-idx=%d queue-size=%d",upload_id, chunk_index + 1, _queue.qsize())

    if is_last:
        sess["file_sha"] = sess["hasher"].hexdigest()
        sess["status"]   = "processing"
        log.info("ALL RECEIVED id=%.8s file_sha=%.12s still-in-queue=%d",upload_id, sess["file_sha"], _queue.qsize())
        async with sess["lock"]:
            if sess["chunks_uploaded"] == total_chunks:
                await _finalize(sess)

        return {"status": sess["status"], "upload_id": upload_id, "sha256": sess.get("file_sha")}

    return {"status": "received", "chunk": chunk_index + 1, "percent": recv_pct}

@app.get("/upload/status/{upload_id}")
async def upload_status(upload_id: str) -> dict:
    sess = _sessions.get(upload_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Upload session not found or already complete")

    uploaded = sess["chunks_uploaded"]
    total = sess["total_chunks"]
    gh_pct = round(uploaded / total * 100, 1) if total else 100.0

    log.debug("STATUS id=%.8s status=%s github=%d/%d",upload_id, sess["status"], uploaded, total)

    resp: dict = {"status": sess["status"], "github_percent": gh_pct}

    if sess["status"] in ("done", "duplicate"):
        resp["sha256"] = sess["file_sha"]
        _sessions.pop(upload_id, None)
    elif sess["status"] == "error":
        resp["error"] = sess["error"]
        _sessions.pop(upload_id, None)
    return resp


@app.get("/files")
async def list_files() -> list[dict]:
    return db.list_all_files()


@app.get("/download/{file_sha256}", response_model=None)
async def download_file(file_sha256: str) -> Response | StreamingResponse:
    record = db.find_by_sha256(sha256=file_sha256)
    if not record:
        log.warning("404 sha=%.12s", file_sha256)
        raise HTTPException(status_code=404, detail=f"No file found for SHA-256 {file_sha256!r}")

    repo     = record["repo"]
    filename = record["original_filename"]
    headers  = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length":      str(record["size_bytes"]),
    }
    log.info("DOWNLOAD INIT sha=%.12s file=%r chunked=%s", file_sha256, filename, record.get("is_chunked"))

    if record.get("is_chunked"):
        chunks = sorted(record["chunks"], key=lambda c: c["index"])
        log.debug("STREAM %d chunks  repo=%s", len(chunks), repo)

        def _iter_chunks():
            for c in chunks:
                log.debug("FETCH CHUNK idx=%d sha=%.12s", c["index"], c["sha256"])
                enc_data = _github.download_file(repo=repo, path=_object_path(c["sha256"]))
                yield _decrypt(enc_data)

        return StreamingResponse(_iter_chunks(), media_type=record["mime_type"], headers=headers)

    try:
        log.debug("FETCH SINGLE sha=%.12s", file_sha256)
        storage_sha = record["chunks"][0]["sha256"]
        enc_data = _github.download_file(repo=repo, path=_object_path(storage_sha))
        data = _decrypt(enc_data)
    except Exception as exc:
        log.error("DOWNLOAD ERROR sha=%.12s err=%s", file_sha256, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    log.info("DOWNLOAD SUCCESSFUL sha=%.12s size=%dB", file_sha256, len(data))
    return Response(content=data, media_type=record["mime_type"], headers=headers)
