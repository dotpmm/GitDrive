import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Any
import hmac
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from . import crypto, db
from .github import GitHubClient

load_dotenv()

# The public space is encrypted with this key and nothing else. so only the server can decrypt
_MASTER_KEY = crypto.server_master_key()

def _public_kek(file_id: str) -> bytes:
    return crypto.file_kek(_MASTER_KEY, file_id)

# Browser origins allowed to read responses from this API cross-origin (comma separated,
# exact scheme+host+port). Empty means same-origin only. API_KEY, when set, additionally
# requires an X-API-Key header on every route the app itself uses. The full contract the
# frontend codes against lives in FRONTEND_API_SPEC.md.
_FRONTEND_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("FRONTEND_ORIGINS", "").split(",") if o.strip()]
_API_KEY = os.getenv("API_KEY", "")

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
_github   = GitHubClient(token=os.environ["GITHUB_TOKEN"])
_sessions: dict[str, dict] = {}
_queue: asyncio.Queue

CHUNK_SIZE  = 10 * 1024 * 1024   # 10 MB, cuz, base64 encoding inflates a bit...anddd smaller the better!
MAX_RETRIES = 3
RETRY_WAIT  = 1
# workers who take chunks from the temp folder and upload asynchronously 
WORKER_COUNT = 5
# the browser owns the chunker, so the server only sanity-checks the geometry it is given
MAX_FILE_SIZE = 10 * 1024 * 1024 * 1024   # 10 GB ceiling on one upload
MAX_CHUNKS = MAX_FILE_SIZE // CHUNK_SIZE
MAX_CHUNK_LEN = 64 * 1024 * 1024

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

# now all the files stored end with .chunk extension
def _object_path(sha: str) -> str:
    return f"objects/{sha[:2]}/{sha[2:]}.chunk"

# per-file manifest: this is what makes a private file decryptable without GitDrive.
# A future CLI needs nothing but <owner>/<repo>, this path and the password.
def _manifest_path(file_id: str) -> str:
    return f"manifests/{file_id}/manifest.json"

def _write_manifest(repo: str, manifest: dict) -> None:
    # The manifest is a single small file so an offline reader only needs one fetch. It
    # carries no secret and no plaintext: only KDF parameters and two sealed blobs.
    _upload_with_retry(repo, _manifest_path(manifest["file_id"]), crypto.canonical(manifest))

def _validate_private_manifest(manifest: Any, file_id: str, sess: dict, chunks: list[dict]) -> dict:
    """Shape checks only. The server cannot verify or read any of this; the browser does.

    Raises CryptoError rather than HTTPException because this runs in an upload worker,
    where a failed finalize is reported through the session, not to a request.
    """
    if not isinstance(manifest, dict):
        raise crypto.CryptoError("the browser did not send a manifest")
    if manifest.get("version") != crypto.FORMAT_VERSION:
        raise crypto.CryptoError(f"unsupported manifest version {manifest.get('version')!r}")
    if manifest.get("mode") != "private" or manifest.get("file_id") != file_id:
        raise crypto.CryptoError("the manifest does not describe this upload")
    if manifest.get("chunk_count") != sess["total_chunks"] or manifest.get("plain_size") != sess["size_bytes"]:
        raise crypto.CryptoError("the manifest disagrees with the upload geometry")
    if not isinstance(manifest.get("kdf"), dict) or not manifest["kdf"].get("alg"):
        raise crypto.CryptoError("the manifest is missing its KDF description")
    for field in ("key_wrap", "meta"):
        if not isinstance(manifest.get(field), dict) or "nonce" not in manifest[field]:
            raise crypto.CryptoError(f"the manifest is missing {field}")
    # The chunk table is inside the sealed metadata, so this only checks the geometry the
    # browser had to commit to before it encrypted anything.
    if manifest.get("chunk_count") != len(chunks):
        raise crypto.CryptoError("the manifest lists a different number of chunks")
    return manifest

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

def _build_meta(*, filename: str, relative_path: str, mime: str, chunks: list[dict]) -> dict:
    return {
        "name": filename,
        "path": relative_path,
        "mime": mime,
        "chunks": chunks,
        "chain": crypto.chain_digest([c["sha256"] for c in chunks]),
    }

# a small function which goes green when the worker upload queue completes
async def _finalize(sess: dict) -> None:
    file_id = sess["file_id"]
    mode = sess["mode"]
    log.info("UPLOAD SUCCESSFUL id=%.8s file=%s mode=%s chunks=%d",
             sess["upload_id"], file_id[:12], mode, sess["total_chunks"])

    if db.find_by_file_id(file_id=file_id):
        sess["status"] = "duplicate"
        log.info("DUPLICATE file=%s", file_id)
        return

    chunks = [
        {"index": i, "sha256": m["sha256"], "size": m["size"]}
        for i, m in sorted(sess["chunk_meta"].items())
    ]
    if len(chunks) != sess["total_chunks"]:
        sess["status"] = "error"
        sess["error"] = "not every chunk arrived"
        return

    if mode == "public":
        declared = sum(m["size"] for m in sess["chunk_meta"].values())
        if declared != sess["size_bytes"]:
            sess["status"] = "error"
            sess["error"] = "sum of chunk sizes does not match size_bytes"
            return

    manifest = sess["manifest"]
    if mode == "public":
        manifest, _ = crypto.build_manifest(
            mode=crypto.MODE_PUBLIC,
            file_id=file_id,
            chunk_size=CHUNK_SIZE,
            chunk_count=sess["total_chunks"],
            plain_size=sess["size_bytes"],
            kdf={"alg": "server", "salt": ""},
            dek=sess["dek"],
            kek=_public_kek(file_id),
            meta=_build_meta(filename=sess["filename"], relative_path=sess.get("relative_path", ""),
                             mime=sess["mime_type"], chunks=chunks),
        )
    else:
        # The browser derived and wrapped the file key. All this server can do is check the
        # manifest's shape; it cannot read, verify, or derive anything from it.
        try:
            manifest = _validate_private_manifest(manifest, file_id, sess, chunks)
        except crypto.CryptoError as exc:
            log.error("MANIFEST REJECTED file=%s err=%s", file_id[:12], exc)
            sess["status"] = "error"
            sess["error"] = str(exc)
            return

    try:
        await asyncio.to_thread(_write_manifest, sess["repo"], manifest)
    except Exception as exc:
        log.error("MANIFEST WRITE FAILED file=%s err=%s", file_id, exc)
        sess["status"] = "error"
        sess["error"] = "the manifest could not be written to storage"
        return

    db.upsert_file(
        file_id=file_id,
        mode=mode,
        original_filename=sess["filename"] if mode == "public" else "",
        repo=sess["repo"],
        relative_path=sess.get("relative_path", "") if mode == "public" else "",
        mime_type=sess["mime_type"] if mode == "public" else "application/octet-stream",
        size_bytes=sess["size_bytes"],
        is_chunked=sess["total_chunks"] > 1,
        chunks=chunks,
        manifest=manifest,
        owner=sess["user"],
        visibility=mode,
    )
    db.increment_repo_size(sess["repo"], sess["size_bytes"])
    sess["status"] = "done"
    shutil.rmtree(sess["dir"], ignore_errors=True)
    log.info("META FILE=%s c=%d repo=%s visible=%s",
             file_id, sess["total_chunks"], sess["repo"], mode == "public")


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
                    if uploaded == total and sess["last_chunk_received"]:
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

async def _reap_abandoned_sessions() -> None:
    while True:
        await asyncio.sleep(300)
        now = time.time()
        stale = []
        for sid, sess in list(_sessions.items()):
            last = sess.get("last_activity", sess.get("created_at_ts", now))
            if now - last > 3600 and sess.get("status") not in ("done", "duplicate", "error"):
                stale.append(sid)
        for sid in stale:
            sess = _sessions.pop(sid, None)
            if sess:
                shutil.rmtree(sess.get("dir", ""), ignore_errors=True)
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _queue
    _queue = asyncio.Queue()

    if _tmp.exists():
        shutil.rmtree(_tmp)
    _tmp.mkdir(parents=True)

    workers = [asyncio.create_task(_worker(i + 1)) for i in range(WORKER_COUNT)]
    reaper = asyncio.create_task(_reap_abandoned_sessions())
    yield
    reaper.cancel()
    for w in workers:
        w.cancel()

_DOCS_ENABLED = os.getenv("ENABLE_DOCS", "false").lower() == "true" or os.getenv("SERVE_DOCS", "false").lower() == "true"
app = FastAPI(
    title="GitDrive",
    version="3.5.0",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)

# Routes reachable without the API key: the served pages, every share-link path (a
# recipient opens those from an arbitrary browser) and the two private-file read
# endpoints, which are guarded by the session or a ticket instead of the key.
_GATE_EXEMPT_EXACT = {"/", "/health", "/gitdrive-crypto.js"}
_GATE_EXEMPT_PREFIX = "/share/"
_GATE_EXEMPT_PATTERN = re.compile(r"^/files/[^/]+/(?:manifest|chunk/\d+)$")


def _gate_open(path: str, method: str) -> bool:
    return (method == "OPTIONS"
            or path in _GATE_EXEMPT_EXACT
            or path.startswith(_GATE_EXEMPT_PREFIX)
            or _GATE_EXEMPT_PATTERN.match(path) is not None)


class _ApiKeyGate:
    """Pure-ASGI middleware answering 401 unless the shared API key is presented.

    Deliberately not BaseHTTPMiddleware: this app streams multi-gigabyte downloads and
    BaseHTTPMiddleware buffers the receive channel. A no-op while API_KEY is unset.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not _API_KEY or _gate_open(scope["path"], scope["method"]):
            return await self.app(scope, receive, send)
        provided = next((value.decode("latin-1")
                         for name, value in scope["headers"] if name == b"x-api-key"), "")
        if provided and hmac.compare_digest(provided, _API_KEY):
            return await self.app(scope, receive, send)
        await JSONResponse(status_code=401,
                           content={"detail": "A valid X-API-Key header is required"})(scope, receive, send)


# CORS is added last so it wraps the gate: preflights are answered even without the key,
# and only the registered frontend origins can read anything cross-origin.
app.add_middleware(_ApiKeyGate)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_FRONTEND_ORIGINS,
    allow_credentials=False,     # sessions ride on Bearer tokens, never cookies
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    expose_headers=["Content-Disposition", "Content-Length", "Content-Range", "Accept-Ranges"],
    max_age=3600,
)


@app.exception_handler(HTTPException)
async def log_http_exception(_: Request, exc: HTTPException) -> JSONResponse:
    # Do not log request URLs: share-link tokens are part of those paths.
    if exc.status_code >= 400:
        log.warning("HTTP ERROR status=%d detail=%s", exc.status_code, exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


# The password itself never reaches this process. The browser runs Argon2id locally and
# sends a keyed verifier; only its SHA-256 is stored. A database leak therefore yields no
# key material -- though note it does still permit offline guessing of a weak password,
# because any scheme where the server authenticates a password has that property.
DUMMY_KDF_SALT = os.urandom(16).hex()
DEFAULT_KDF = {"alg": "argon2id", "m_kib": crypto.ARGON2_M_KIB,
               "t": crypto.ARGON2_T, "p": crypto.ARGON2_P, "len": crypto.KEY_LEN}


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(c in "0123456789abcdefABCDEF" for c in value)


@app.get("/auth/kdf")
async def auth_kdf(username: str = Query(...)) -> dict:
    """The Argon2id parameters a browser needs before it can derive anything."""
    user = db.find_user(username.strip())
    if not user:
        # Answer identically for unknown users so this cannot enumerate accounts.
        return {**DEFAULT_KDF, "salt": DUMMY_KDF_SALT}
    return {**DEFAULT_KDF, "salt": user["kdf_salt"]}


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
    if record.get("visibility") == "public":
        return True
    return bool(user and record.get("owner_id") == str(user["_id"]))


def _verify(verifier: str, stored_hash: str) -> bool:
    if not _is_hex(verifier, 64):
        return False
    return hmac.compare_digest(crypto.hash_verifier(verifier), stored_hash)


@app.post("/auth/register")
async def register(username: str = Form(...), verifier: str = Form(...), kdf_salt: str = Form(...)) -> dict:
    username = username.strip()
    if not 3 <= len(username) <= 32 or not username.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Username must be 3–32 letters, numbers, - or _")
    if not _is_hex(verifier, 64) or not _is_hex(kdf_salt, 32):
        raise HTTPException(status_code=400, detail="Malformed key derivation parameters")
    if db.find_user(username):
        raise HTTPException(status_code=409, detail="Username is already taken")
    try:
        db.create_user(username, crypto.hash_verifier(verifier), kdf_salt)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="Username is already taken") from exc
    user = db.find_user(username)
    return {"token": db.create_session(user), "username": username}


@app.post("/auth/login")
async def login(username: str = Form(...), verifier: str = Form(...)) -> dict:
    user = db.find_user(username.strip())
    if not user or not _verify(verifier, user["verifier_hash"]):
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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": app.version}


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    _serve = os.getenv("SERVE_BUNDLED_UI")
    if _serve is None:
        _serve = "false" if os.getenv("ENV", "").lower() in ("prod", "production") else "true"
    if _serve.lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(status_code=404, detail="Not found")
    return HTMLResponse(content=(Path(__file__).resolve().parent.parent / "index.html").read_text(encoding="utf-8"))

def _asset(name: str) -> str:
    return (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")


@app.get("/gitdrive-crypto.js", response_class=Response)
async def crypto_asset() -> Response:
    """Shared browser crypto module, loaded by both index.html and share.html."""
    return Response(content=_asset("gitdrive-crypto.js"), media_type="text/javascript",
                    headers={"Cache-Control": "public, max-age=300"})

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
    file_id:      str        = Form(...), # random per file; the server never derives it from content
    size_bytes:   int        = Form(...),
    visibility: str          = Form("private"),
    # private space: the browser already encrypted the chunk and hands over the result.
    blob_sha:     str        = Form(""),
    plain_len:    int        = Form(0),
    manifest:     str        = Form(""),   # private space, sent once with the final chunk
    # public space: the browser sends plaintext and this server encrypts it.
    filename:     str        = Form(""),
    mime_type:    str        = Form(""),
    relative_path: str       = Form(""),
    user: dict               = Depends(current_user),
) -> dict:
    if visibility not in ("public", "private"):
        raise HTTPException(status_code=400, detail="Visibility must be public or private")
    if not _is_hex(file_id, 32):
        raise HTTPException(status_code=400, detail="file_id must be 32 hex characters")
    if not 1 <= total_chunks <= MAX_CHUNKS or not 0 <= chunk_index < total_chunks:
        raise HTTPException(status_code=400, detail="Invalid chunk geometry")
    if size_bytes < 0 or size_bytes > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Invalid file size")

    data = await chunk.read()
    is_last = (chunk_index + 1 == total_chunks)

    if upload_id not in _sessions:
        if visibility == "public":
            # The server owns public-space keys, so it mints the file DEK here.
            dek, name, mime = os.urandom(crypto.KEY_LEN), filename, \
                mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        else:
            dek, name, mime = None, "", "application/octet-stream"
        repo, is_new  = db.get_active_repo() # this checks repo_max_size conditions and then determines
        log.debug("SESSION id=%.4s repo=%s new=%s", upload_id, repo, is_new)

        if is_new:
            await asyncio.to_thread(_github.create_repo, repo)
            log.info("REPO CREATE name=%s", repo)

        sess_dir = _tmp / upload_id
        sess_dir.mkdir(parents=True, exist_ok=True)

        _sessions[upload_id] = {
            "upload_id":  upload_id,
            "relative_path":   relative_path,
            "total_chunks": total_chunks,
            "filename":   name,
            "user":       user,
            "mode":       visibility,
            "mime_type":  mime,
            "size_bytes": size_bytes,
            "repo":       repo,
            "file_id":    file_id,
            "dek":        dek,
            "manifest":   None,
            "chunk_meta":      {},
            "chunks_received": 0,
            "chunks_uploaded": 0,
            "last_chunk_received": False,
            "status":     "receiving",
            "error":      None,
            "lock":       asyncio.Lock(),
            "dir":        sess_dir,
        }

    sess = _sessions[upload_id]
    if sess["user"]["_id"] != user["_id"] or sess["file_id"] != file_id:
        raise HTTPException(status_code=403, detail="Upload belongs to another user")
    # The space is decided by the first chunk; a later chunk cannot switch it, or a file
    # could start life private and be stored under public keys.
    if sess["mode"] != visibility or sess["total_chunks"] != total_chunks:
        raise HTTPException(status_code=400, detail="Upload geometry or visibility changed mid-upload")

    if visibility == "private":
        # Already ciphertext: store it byte for byte. This branch cannot read the chunk.
        if not _is_hex(blob_sha, 64):
            raise HTTPException(status_code=400, detail="blob_sha must be the SHA-256 of the chunk")
        if len(data) < crypto.CHUNK_HEADER_LEN + crypto.TAG_LEN or not data.startswith(crypto.CHUNK_MAGIC):
            raise HTTPException(status_code=400, detail="The chunk is not a GitDrive chunk")
        if not 0 <= plain_len <= MAX_CHUNK_LEN or len(data) != plain_len + crypto.CHUNK_HEADER_LEN + crypto.TAG_LEN:
            raise HTTPException(status_code=400, detail="Chunk length disagrees with the declared plaintext length")
        if _sha256(data) != blob_sha:
            raise HTTPException(status_code=400, detail="Chunk does not match the hash the browser sent")
        blob = data
    else:
        # Plaintext arrives here and is encrypted before it ever reaches the disk queue.
        if len(data) > MAX_CHUNK_LEN:
            raise HTTPException(status_code=400, detail="Chunk is larger than the accepted maximum")
        try:
            blob = crypto.seal_chunk(file_id, crypto.MODE_PUBLIC, chunk_index, total_chunks,
                                     len(data), crypto.chunk_key(sess["dek"], chunk_index, total_chunks), data)
        except crypto.CryptoError as exc:
            raise HTTPException(status_code=400, detail="Chunk could not be encrypted") from exc
        blob_sha = _sha256(blob)
        plain_len = len(data)

    sess["chunk_meta"][chunk_index] = {"sha256": blob_sha, "size": plain_len}
    if is_last:
        sess["last_chunk_received"] = True
        sess["last_activity"] = time.time()

    disk_path = sess["dir"] / f"{chunk_index}.part"
    await asyncio.to_thread(disk_path.write_bytes, blob)

    await _queue.put({
        "upload_id":   upload_id,
        "chunk_index": chunk_index,
        "disk_path":   disk_path,
        "repo":        sess["repo"],
        "github_path": _object_path(blob_sha),
        "filename":    sess["filename"],   # blank for private, so commits stay masked too
        "username":    user["username"],
    })

    log.info("UPLOAD INIT id=%.4s chunk=%d/%d plain=%.1fKB blob=%.6s mode=%s",
             upload_id, chunk_index + 1, total_chunks, plain_len / 1024, blob_sha, visibility)

    sess["chunks_received"] += 1
    recv_pct = round(sess["chunks_received"] / total_chunks * 100, 1)

    log.debug("QUEUE id=%.8s chunk-idx=%d queue-size=%d",upload_id, chunk_index + 1, _queue.qsize())

    if is_last:
        if manifest:
            try:
                sess["manifest"] = json.loads(manifest)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Manifest is not valid JSON") from exc
        sess["status"] = "processing"
        log.info("ALL RECEIVED id=%.8s file=%s still-in-queue=%d", upload_id, file_id[:12], _queue.qsize())
        async with sess["lock"]:
            if sess["chunks_uploaded"] == total_chunks:
                await _finalize(sess)

        return {"status": sess["status"], "upload_id": upload_id, "file_id": file_id}

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
        resp["file_id"] = sess["file_id"]
        _sessions.pop(upload_id, None)
    elif sess["status"] == "error":
        resp["error"] = sess["error"]
        _sessions.pop(upload_id, None)
    return resp


@app.get("/files")
async def list_files(user: dict = Depends(current_user)) -> list[dict]:
    return db.list_visible_files(user)


@app.delete("/files/{file_id}")
async def delete_file(file_id: str, permanent: bool = Query(False), user: dict = Depends(current_user)) -> dict:
    record = db.find_by_file_id(file_id)
    if not record or record.get("owner_id") != str(user["_id"]):
        raise HTTPException(status_code=404, detail="File not found")
    if permanent:
        # queue background purge (best-effort sync here)
        db.purge_file(file_id)
        return {"status": "purged"}
    db.mark_deleted(file_id)
    return {"status": "trashed"}


@app.post("/files/{file_id}/restore")
async def restore_file(file_id: str, user: dict = Depends(current_user)) -> dict:
    record = db.find_by_file_id(file_id)
    if not record or record.get("owner_id") != str(user["_id"]):
        raise HTTPException(status_code=404, detail="File not found")
    db.restore_file(file_id)
    return {"status": "restored"}


@app.patch("/files/{file_id}")
async def patch_file(file_id: str, payload: dict, user: dict = Depends(current_user)) -> dict:
    record = db.find_by_file_id(file_id)
    if not record or record.get("owner_id") != str(user["_id"]):
        raise HTTPException(status_code=404, detail="File not found")
    fields = {}
    if "filename" in payload:
        fields["original_filename"] = payload["filename"]
    if "relative_path" in payload:
        fields["relative_path"] = payload["relative_path"]
    db.update_file(file_id, fields)
    return db.find_by_file_id(file_id) or {}


def _session_user(authorization: str | None) -> dict | None:
    return db.find_session_user(authorization.removeprefix("Bearer ")) if authorization and authorization.startswith("Bearer ") else None


def _stream_file(record: dict) -> Response | StreamingResponse:
    """Public space only. The server holds these keys, so it decrypts and streams."""
    if record.get("mode") != "public":
        raise HTTPException(status_code=400, detail="Private files are decrypted in your browser")

    repo     = record["repo"]
    filename = record["original_filename"]
    file_id  = record["file_id"]
    headers  = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length":      str(record["size_bytes"]),
        "X-Content-Type-Options": "nosniff",
    }
    chunks = sorted(record.get("chunks") or [], key=lambda c: c["index"])
    if not chunks:
        raise HTTPException(status_code=502, detail="Stored file metadata is incomplete")
    dek = crypto.unwrap_dek(_public_kek(file_id), record["manifest"]["key_wrap"],
                            crypto.manifest_aad(record["manifest"]))
    if record["manifest"]["chunk_count"] != len(chunks):
        log.error("INDEX MISMATCH file=%s manifest=%d index=%d",
                  file_id[:12], record["manifest"]["chunk_count"], len(chunks))
        raise HTTPException(status_code=502, detail="Stored file metadata is inconsistent")
    log.info("DOWNLOAD INIT file=%s name=%r chunked=%s", file_id[:12], filename, record.get("is_chunked"))

    def _fetch(chunk: dict) -> bytes:
        blob = _github.download_file(repo=repo, path=_object_path(chunk["sha256"]))
        key = crypto.chunk_key(dek, chunk["index"], len(chunks))
        # A failure here is a real authentication failure. There is no plaintext fallback.
        return crypto.open_chunk(file_id, blob, key,
                                 expect_index=chunk["index"], expect_total=len(chunks))[1]

    # The first chunk is fetched here, before any header is sent.  A storage
    # failure inside the streaming generator would otherwise reach the client as
    # a response cut off mid-file, which is what a proxy reports as 502.
    try:
        log.debug("FETCH CHUNK idx=0 sha=%.12s", chunks[0]["sha256"])
        first = _fetch(chunks[0])
    except Exception as exc:
        log.error("DOWNLOAD ERROR file=%s err=%s", file_id[:12], exc)
        raise HTTPException(status_code=502, detail="The file could not be read from storage, please try again") from exc

    if len(chunks) == 1:
        log.info("DOWNLOAD SUCCESSFUL file=%s size=%dB", file_id[:12], len(first))
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
                log.error("DOWNLOAD STREAM FAILED file=%s idx=%d err=%s", record["file_id"][:12], c["index"], exc)
                raise

    return StreamingResponse(_iter_rest(), media_type=record["mime_type"], headers=headers)


@app.get("/download/{file_id}", response_model=None)
async def download_file(file_id: str, authorization: str | None = Header(default=None)) -> Response | StreamingResponse:
    record = db.find_by_file_id(file_id=file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    user = _session_user(authorization)
    if not _can_access(record, user):
        raise HTTPException(status_code=403, detail="You do not have access to this private file")
    return _stream_file(record)


# ---- private space ---------------------------------------------------------
# These two endpoints are the whole server-side of a zero-knowledge download. They hand
# back ciphertext and a manifest; only the password can turn them into a file.

def _private_record(file_id: str, user: dict | None, ticket: str | None) -> dict:
    record = db.find_by_file_id(file_id=file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    if record.get("mode") != "private":
        raise HTTPException(status_code=400, detail="This file is not encrypted end to end")
    if _can_access(record, user):
        return record
    # A share ticket authorises the ciphertext only; the password is still required.
    if ticket:
        grant = db.find_download_ticket(ticket)
        if grant and grant["file_id"] == file_id:
            return record
    raise HTTPException(status_code=403, detail="You do not have access to this private file")


@app.get("/files/{file_id}/manifest", response_model=None)
async def private_manifest(file_id: str, authorization: str | None = Header(default=None),
                           t: str | None = Query(default=None)) -> Response:
    record = _private_record(file_id, _session_user(authorization), t)
    body = crypto.canonical(record["manifest"])
    return Response(content=body, media_type="application/json",
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@app.get("/files/{file_id}/chunk/{index}", response_model=None)
async def private_chunk(file_id: str, index: int, authorization: str | None = Header(default=None),
                        t: str | None = Query(default=None)) -> Response:
    record = _private_record(file_id, _session_user(authorization), t)
    chunks = record.get("chunks") or []
    if not 0 <= index < len(chunks):
        raise HTTPException(status_code=404, detail="No such chunk")
    try:
        blob = _github.download_file(repo=record["repo"], path=_object_path(chunks[index]["sha256"]))
    except Exception as exc:
        log.error("PRIVATE CHUNK MISS file=%s idx=%d", file_id[:12], index)
        raise HTTPException(status_code=502, detail="The chunk could not be read from storage") from exc
    return Response(content=blob, media_type="application/octet-stream",
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@app.post("/files/{file_id}/share")
async def create_share_link(file_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    record = db.find_by_file_id(file_id=file_id)
    if not record or record.get("owner_id") != str(user["_id"]):
        raise HTTPException(status_code=404, detail="Your file was not found")
    expires_in = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            expires_in = body.get("expires_in_seconds")
    except Exception:
        pass
    token = db.create_share(file_id, record.get("visibility", "private"), expires_in)
    return {"token": token, "visibility": record.get("visibility", "private"),
            "mode": record.get("mode"), "expires_in": expires_in}


@app.get("/shares")
async def list_shares(user: dict = Depends(current_user)) -> list[dict]:
    return db.list_shares(user)


@app.delete("/shares/{token}")
async def revoke_share(token: str, user: dict = Depends(current_user)) -> dict:
    share = db.find_share(token)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    rec = db.find_by_file_id(file_id=share["file_id"])
    if not rec or rec.get("owner_id") != str(user["_id"]):
        raise HTTPException(status_code=404, detail="Share not found")
    db.revoke_share(token)
    return {"status": "revoked"}


@app.get("/share/{token}/info")
async def share_info(token: str) -> dict:
    share = db.find_share(token)
    if not share or share.get("revoked"):
        raise HTTPException(status_code=410, detail="Share revoked or expired")
    if share.get("expires_at"):
        # compare if datetime
        exp = share["expires_at"]
        if isinstance(exp, str):
            from datetime import datetime as _dt
            try:
                exp = _dt.fromisoformat(exp)
            except Exception:
                exp = None
        if exp is not None and exp < datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail="Share expired")
    record = db.find_by_file_id(file_id=share["file_id"])
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    info = {
        "mode": record.get("mode"),
        "file_id": record.get("file_id"),
        "size_bytes": record.get("size_bytes"),
        "uploaded_at": record.get("uploaded_at"),
        "owner_username": record.get("owner_username"),
        "expires_at": share.get("expires_at"),
    }
    if record.get("visibility") == "public":
        info["filename"] = record.get("original_filename")
    return info


def _shared_file(token: str) -> dict:
    share = db.find_share(token)
    record = db.find_by_file_id(file_id=share["file_id"]) if share else None
    if not record:
        raise HTTPException(status_code=404, detail="Share link not found")
    return record


@app.get("/me/usage")
async def me_usage(user: dict = Depends(current_user)) -> dict:
    return db.user_usage(user)
    share = db.find_share(token)
    record = db.find_by_file_id(file_id=share["file_id"]) if share else None
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
    record = db.find_by_file_id(file_id=grant["file_id"])
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    return _stream_file(record)


@app.get("/share/{token}", response_class=HTMLResponse)
async def share_page(token: str, request: Request, authorization: str | None = Header(default=None)) -> HTMLResponse:
    record = _shared_file(token)
    user = _session_user(authorization)
    # A private file's name is inside the sealed manifest, so this page cannot name it.
    # The recipient's browser resolves it as soon as the password is entered.
    payload = {
        "token":      token,
        "fileId":     record["file_id"],
        "filename":   record["original_filename"] or "",
        "size":       record["size_bytes"],
        "mime":       record["mime_type"],
        "chunkCount": len(record.get("chunks") or []),
        "isPrivate":  record.get("visibility") == "private",
        "isEncrypted": record.get("mode") == "private",
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
    # A private share link plus the password is the whole authorisation. Demanding a
    # GitDrive account as well would add friction without adding protection, because the
    # bytes stay encrypted either way.
    grant, expires_at = db.create_download_ticket(record["file_id"], token)
    return {"url": f"/share/d/{grant}", "expires_in": db.TICKET_TTL_SECONDS,
            "file_id": record["file_id"], "mode": record.get("mode")}


@app.get("/share/{token}/download", response_model=None)
async def download_shared_file(token: str, request: Request, authorization: str | None = Header(default=None)) -> Response | StreamingResponse:
    record = _shared_file(token)
    user = _session_user(authorization)
    if record.get("mode") != "public" and not _can_access(record, user):
        # The share page owns private files: it mints a ticket and decrypts locally.
        if _wants_html(request):
            return RedirectResponse(url=f"/share/{token}", status_code=302)
        raise HTTPException(status_code=403, detail="Open the share link to decrypt this file in your browser")
    return _stream_file(record)

