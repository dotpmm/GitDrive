import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
import hmac
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from . import db
from .github import GitHubClient

load_dotenv()

_key_src = os.getenv("GITHUB_TOKEN")
_aes_key = hashlib.sha256(_key_src.encode()).digest()

def _encryption_key(user: dict | None, visibility: str) -> bytes:
    # per-user UUID is stored only in MongoDB
    if visibility == "public":
        # public blobs must be readable by whoever holds the link, so they are
        # scoped by a constant rather than by an uploader, so, maybe a server binded 
        # link
        scope = "public"
    elif user:
        scope = user["encryption_id"]
    else:
        raise ValueError("private files require the uploader's key material")
    return hashlib.sha256(_aes_key + scope.encode()).digest()

def _encrypt(data: bytes, user: dict, visibility: str) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_encryption_key(user, visibility)).encrypt(nonce, data, None)

def _decrypt_keys(user: dict | None, visibility: str) -> list[bytes]:
    keys = []
    if visibility == "public" or user:
        keys.append(_encryption_key(user, visibility))
    # blobs written before keys were scoped per user are keyed by the bare master key, this is 
    # the leftovers of the old version
    keys.append(_aes_key)
    return keys

def _decrypt(data: bytes, user: dict | None, visibility: str, expected_size: int | None) -> bytes:
    # decryption used to fall back to returning the stored bytes, which handed the
    # client an encrypted blob while the response advertised the plaintext size;
    for key in _decrypt_keys(user, visibility):
        try:
            plain = AESGCM(key).decrypt(data[:12], data[12:], None)
        except Exception:
            continue
        if expected_size is None or len(plain) == expected_size:
            return plain
    # compatibility for blobs stored before encryption was enabled at all.
    if expected_size is not None and len(data) == expected_size:
        return data
    raise ValueError("stored object could not be decrypted")

_log_level = logging.DEBUG if os.getenv("LOG_MODE").lower() == "debug" else logging.INFO

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
def _upload_with_retry(repo: str, path: str, content: bytes, filename: str | None = None, username: str | None = None) -> dict:
    last_exc: Exception = RuntimeError("no attempt made")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = _github.upload_file(repo=repo, path=path, content=content, filename=filename, username=username)
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
        owner=sess["user"],
        visibility=sess["visibility"],
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
            await asyncio.to_thread(_upload_with_retry, job["repo"], job["github_path"], data, job.get("filename"), job.get("username"))
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

app = FastAPI(title="GitDrive", version="3.5.0", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def log_http_exception(_: Request, exc: HTTPException) -> JSONResponse:
    # Do not log request URLs: share-link tokens are part of those paths.
    if exc.status_code >= 400:
        log.warning("HTTP ERROR status=%d detail=%s", exc.status_code, exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"{salt.hex()}${digest.hex()}"


def _password_matches(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        return hmac.compare_digest(_password_hash(password, bytes.fromhex(salt_hex)).split("$", 1)[1], digest_hex)
    except (ValueError, TypeError):
        return False


async def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        log.warning("AUTH REJECT reason=missing_bearer_token")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
    user = db.find_session_user(authorization.removeprefix("Bearer "))
    if not user:
        log.warning("AUTH REJECT reason=invalid_or_expired_session")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid")
    return user


def _can_access(record: dict, user: dict | None) -> bool:
    # Old records have no owner metadata, so remain readable after the upgrade.
    if not record.get("owner_id") or record.get("visibility") == "public":
        return True
    return bool(user and (record["owner_id"] == str(user["_id"]) or user["username"] in record.get("shared_with", [])))


@app.post("/auth/register")
async def register(username: str = Form(...), password: str = Form(...)) -> dict:
    username = username.strip()
    if not 3 <= len(username) <= 32 or not username.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Username must be 3–32 letters, numbers, - or _")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if db.find_user(username):
        raise HTTPException(status_code=409, detail="Username is already taken")
    try:
        db.create_user(username, _password_hash(password))
    except Exception as exc:
        raise HTTPException(status_code=409, detail="Username is already taken") from exc
    user = db.find_user(username)
    return {"token": db.create_session(user), "username": username}


@app.post("/auth/login")
async def login(username: str = Form(...), password: str = Form(...)) -> dict:
    user = db.find_user(username.strip())
    if not user or not _password_matches(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {"token": db.create_session(user), "username": user["username"]}


@app.get("/auth/me")
async def me(user: dict = Depends(current_user)) -> dict:
    return {"username": user["username"]}


@app.post("/auth/logout")
async def logout(authorization: str | None = Header(default=None)) -> dict:
    if authorization and authorization.startswith("Bearer "):
        db.delete_session(authorization.removeprefix("Bearer "))
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(content=(Path(__file__).resolve().parent.parent / "index.html").read_text(encoding="utf-8"))

def _asset(name: str) -> str:
    return (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")

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
    visibility: str          = Form("private"),
    user: dict               = Depends(current_user),
) -> dict:
    if visibility not in ("public", "private"):
        raise HTTPException(status_code=400, detail="Visibility must be public or private")
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
            "user":       user,
            "visibility": visibility,
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
    if sess["user"]["_id"] != user["_id"]:
        raise HTTPException(status_code=403, detail="Upload belongs to another user")
    enc_data = _encrypt(data, user, sess["visibility"])
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
        "username":    user["username"],
    })

    sess["chunks_received"] += 1
    recv_pct = round(sess["chunks_received"] / total_chunks * 100, 1)

    log.debug("QUEUE id=%.8s chunk-idx=%d queue-size=%d",upload_id, chunk_index + 1, _queue.qsize())

    if is_last:
        # Same contents may belong to different users; this public identifier is scoped to its owner.
        sess["file_sha"] = _sha256(f"{user['encryption_id']}:{sess['hasher'].hexdigest()}".encode())
        sess["status"]   = "processing"
        log.info("ALL RECEIVED id=%.8s file_sha=%.12s still-in-queue=%d",upload_id, sess["file_sha"], _queue.qsize())
        async with sess["lock"]:
            if sess["chunks_uploaded"] == total_chunks:
                await _finalize(sess)

        return {"status": sess["status"], "upload_id": upload_id, "sha256": sess.get("file_sha")}

    return {"status": "received", "chunk": chunk_index + 1, "percent": recv_pct}

@app.get("/upload/status/{upload_id}")
async def upload_status(upload_id: str, user: dict = Depends(current_user)) -> dict:
    sess = _sessions.get(upload_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Upload session not found or already complete")
    if sess["user"]["_id"] != user["_id"]:
        raise HTTPException(status_code=403, detail="Upload belongs to another user")

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
async def list_files(user: dict = Depends(current_user)) -> list[dict]:
    return db.list_visible_files(user)


def _session_user(authorization: str | None) -> dict | None:
    return db.find_session_user(authorization.removeprefix("Bearer ")) if authorization and authorization.startswith("Bearer ") else None


def _stream_file(record: dict) -> Response | StreamingResponse:

    repo     = record["repo"]
    filename = record["original_filename"]
    visibility = record.get("visibility", "public")
    # Private chunks are keyed to the uploader.  Access is still checked before this
    # point; collaborators never receive the uploader's UUID or encryption key.
    key_user = db.find_user(record["owner_username"]) if visibility == "private" else None
    headers  = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length":      str(record["size_bytes"]),
        "X-Content-Type-Options": "nosniff",
    }
    chunks = sorted(record.get("chunks") or [], key=lambda c: c["index"])
    if not chunks:
        raise HTTPException(status_code=502, detail="Stored file metadata is incomplete")
    log.info("DOWNLOAD INIT sha=%.12s file=%r chunked=%s", record["sha256"], filename, record.get("is_chunked"))

    def _fetch(chunk: dict) -> bytes:
        enc_data = _github.download_file(repo=repo, path=_object_path(chunk["sha256"]))
        expected = chunk.get("size")
        if expected is None and len(chunks) == 1:
            expected = record["size_bytes"]
        return _decrypt(enc_data, key_user, visibility, expected)

    # The first chunk is fetched here, before any header is sent.  A storage
    # failure inside the streaming generator would otherwise reach the client as
    # a response cut off mid-file, which is what a proxy reports as 502.
    try:
        log.debug("FETCH CHUNK idx=0 sha=%.12s", chunks[0]["sha256"])
        first = _fetch(chunks[0])
    except Exception as exc:
        log.error("DOWNLOAD ERROR sha=%.12s err=%s", record["sha256"], exc)
        raise HTTPException(status_code=502, detail="The file could not be read from storage, please try again") from exc

    if len(chunks) == 1:
        log.info("DOWNLOAD SUCCESSFUL sha=%.12s size=%dB", record["sha256"], len(first))
        return Response(content=first, media_type=record["mime_type"], headers=headers)

    log.debug("STREAM %d chunks  repo=%s", len(chunks), repo)

    def _iter_rest():
        yield first
        for c in chunks[1:]:
            try:
                log.debug("FETCH CHUNK idx=%d sha=%.12s", c["index"], c["sha256"])
                yield _fetch(c)
            except Exception as exc:
                # The status line is long gone, so the only honest signal left is to
                # abort the transfer rather than serve a silently short file.
                log.error("DOWNLOAD STREAM FAILED sha=%.12s idx=%d err=%s", record["sha256"], c["index"], exc)
                raise

    return StreamingResponse(_iter_rest(), media_type=record["mime_type"], headers=headers)


@app.get("/download/{file_sha256}", response_model=None)
async def download_file(file_sha256: str, authorization: str | None = Header(default=None)) -> Response | StreamingResponse:
    record = db.find_by_sha256(sha256=file_sha256)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    user = _session_user(authorization)
    if not _can_access(record, user):
        raise HTTPException(status_code=403, detail="You do not have access to this private file")
    return _stream_file(record)


@app.post("/files/{file_sha256}/collaborators")
async def add_collaborator(file_sha256: str, username: str = Form(...), user: dict = Depends(current_user)) -> dict:
    record = db.find_by_sha256(file_sha256)
    if not record or record.get("owner_id") != str(user["_id"]):
        raise HTTPException(status_code=404, detail="Your file was not found")
    if record.get("visibility") != "private":
        raise HTTPException(status_code=400, detail="Public files do not need collaborators")
    if not db.find_user(username.strip()):
        raise HTTPException(status_code=404, detail="That username does not exist")
    updated = db.add_collaborator(file_sha256, username.strip())
    return {"shared_with": updated["shared_with"]}


@app.post("/files/{file_sha256}/share")
async def create_share_link(file_sha256: str, user: dict = Depends(current_user)) -> dict:
    record = db.find_by_sha256(file_sha256)
    if not record or record.get("owner_id") != str(user["_id"]):
        raise HTTPException(status_code=404, detail="Your file was not found")
    token = db.create_share(file_sha256, record.get("visibility", "private"))
    return {"token": token, "visibility": record.get("visibility", "private")}


def _shared_file(token: str) -> dict:
    share = db.find_share(token)
    record = db.find_by_sha256(share["file_sha256"]) if share else None
    if not record:
        raise HTTPException(status_code=404, detail="Share link not found")
    return record


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


@app.get("/share/d/{ticket}", response_model=None)
async def download_by_ticket(ticket: str, request: Request) -> Response | StreamingResponse:
    grant = db.find_download_ticket(ticket)
    if not grant:
        # it shld never show a JSON error inside a download, so send browsers back to
        # the share link to mint a fresh ticket.
        if _wants_html(request):
            share_token = db.find_ticket_share(ticket)
            if share_token:
                return RedirectResponse(url=f"/share/{share_token}", status_code=302)
        raise HTTPException(status_code=404, detail="This download link has expired, open the share link again")
    record = db.find_by_sha256(sha256=grant["file_sha256"])
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    return _stream_file(record)


@app.get("/share/{token}", response_class=HTMLResponse)
async def share_page(token: str, request: Request, authorization: str | None = Header(default=None)) -> HTMLResponse:
    record = _shared_file(token)
    user = _session_user(authorization)
    payload = {
        "token":      token,
        "filename":   record["original_filename"],
        "size":       record["size_bytes"],
        "mime":       record["mime_type"],
        "chunkCount": len(record.get("chunks") or []),
        "isPrivate":  record.get("visibility") == "private",
        "authorized": _can_access(record, user),
        "username":   user["username"] if user else None,
        "expired":    request.query_params.get("expired") == "1",
    }
    page = _asset("share.html").replace("__SHARE_DATA__", json.dumps(payload).replace("</", "<\\/"))
    return HTMLResponse(
        content=page,
        # NO SHARED CACHE: page carries the share token, so it must never sit in a shared cache.
        headers={"Cache-Control": "private, no-store"},
    )


@app.post("/share/{token}/access")
async def share_access(token: str, authorization: str | None = Header(default=None)) -> dict:
    record = _shared_file(token)
    user = _session_user(authorization)
    if record.get("visibility") == "private" and not _can_access(record, user):
        raise HTTPException(status_code=401, detail="Sign in with an account this file is shared with")
    grant, expires_at = db.create_download_ticket(record["sha256"], token)
    return {"url": f"/share/d/{grant}", "expires_in": db.TICKET_TTL_SECONDS}


@app.get("/share/{token}/download", response_model=None)
async def download_shared_file(token: str, request: Request, authorization: str | None = Header(default=None)) -> Response | StreamingResponse:
    record = _shared_file(token)
    user = _session_user(authorization)
    if record.get("visibility") == "private" and not _can_access(record, user):
        # got rid of that JSON body saying "sign in to download"
        # now it directly opens the sign in page
        if not authorization and _wants_html(request):
            return RedirectResponse(url=f"/share/{token}", status_code=302)
        raise HTTPException(status_code=403, detail="Sign in with an account this file is shared with")
    return _stream_file(record)

