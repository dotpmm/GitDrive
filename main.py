import asyncio
import hashlib
import logging
import mimetypes
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse

import db
from github import GitHubClient

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-7s %(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)

# this shit just vomits and fills the logs, hate it
logging.getLogger("pymongo").setLevel(logging.WARNING)

log = logging.getLogger("refactored-carnival")

app = FastAPI(title="refactored-carnival", version="2.2.0")

_github = GitHubClient(token=os.environ["GITHUB_TOKEN"])

CHUNK_SIZE  = 10 * 1024 * 1024   # 10 MB, cuz, base64 encoding inflates a bit...anddd smaller the better!
MAX_RETRIES = 3
RETRY_WAIT  = 1

# in this approach , when we click download, instead of downloading all the
# chunks to the server and then combining them and sending it to the client
# this directly streams the chunks from github to the client 
# so that the client can later assemble it , this saves server RAM and 
# is also lighter, thus to keep a track of the received chunk and data, 
# we need this sessions dict
_sessions: dict[str, dict] = {}

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

# now all the files stored end with .chunk extension
def _object_path(sha: str) -> str:
    return f"objects/{sha[:2]}/{sha[2:]}.chunk"

# needed cuz , github throws a bad mac error sometimes on some systems (atleast on mine.. :( )
def _upload_with_retry(repo: str, path: str, content: bytes) -> dict:
    last_exc: Exception = RuntimeError("no attempt made")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = _github.upload_file(repo=repo, path=path, content=content)
            log.debug("GITPUT path=%s attempt=%d", path, attempt)
            return result
        except Exception as exc:
            last_exc = exc
            log.warning("GITPUT FAIL path=%s attempt=%d/%d err=%s", path, attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT)
    raise last_exc


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(content=(Path(__file__).parent / "index.html").read_text(encoding="utf-8"))

# got rid of the old pipeline cuz,
# it was not clear if it was 
# browser to api
# or api to github which was slow / failing

@app.post("/upload")
async def upload_chunk(
    chunk:        UploadFile = File(...),
    upload_id:    str        = Form(...), # the unique id given for every uploaded chunk (temp, not stored anywhere)
    chunk_index:  int        = Form(...),
    total_chunks: int        = Form(...),
    filename:     str        = Form(...),
    mime_type:    str        = Form(""),
    size_bytes:   int        = Form(...),
) -> dict:
    t0        = time.perf_counter()
    data      = await chunk.read()
    chunk_sha = _sha256(data)
    path      = _object_path(chunk_sha)
    is_last   = (chunk_index + 1 == total_chunks)

    log.info("UPLOAD id=%.4s chunk=%d/%d size=%.1fKB sha=%.6s", upload_id, chunk_index + 1, total_chunks, len(data) / 1024, chunk_sha,)

    if upload_id not in _sessions:
        resolved_mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        repo, is_new  = db.get_active_repo() # this checks repo_max_size conditions and then determines
        log.debug("SESSION id=%.8s repo=%s new=%s", upload_id, repo, is_new)

        if is_new:
            try:
                await asyncio.to_thread(_github.create_repo, repo)
                log.info("REPO CREATE name=%s", repo)
            except Exception as exc:
                log.error("REPO FAIL repo=%s  err=%s", repo, exc)
                raise HTTPException(status_code=502, detail=f"Could not create repo: {exc}") from exc

        _sessions[upload_id] = {
            "hasher":     hashlib.sha256(),
            "chunks":     [],
            "filename":   filename,
            "mime_type":  resolved_mime,
            "size_bytes": size_bytes,
            "repo":       repo,
        }

    sess = _sessions[upload_id]
    repo = sess["repo"]

    sess["hasher"].update(data)

    try:
        await asyncio.to_thread(_upload_with_retry, repo, path, data)
    except Exception as exc:
        _sessions.pop(upload_id, None) # cleannnnnn
        log.error("CHUNK-ERR id=%.8s  chunk=%d/%d  err=%s",upload_id, chunk_index + 1, total_chunks, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    sess["chunks"].append({"index": chunk_index, "sha256": chunk_sha, "size": len(data)})
    percent = round((chunk_index + 1) / total_chunks * 100, 1)
    elapsed = (time.perf_counter() - t0) * 1000

    log.debug(
        "CHUNK OK id=%.8s  chunk=%d/%d  percent=%.1f%%  elapsed=%.0fms",
        upload_id, chunk_index + 1, total_chunks, percent, elapsed,
    )

    if is_last:
        file_sha = sess["hasher"].hexdigest()
        log.info("FINISH id=%.8s file_sha=%.12s chunks=%d", upload_id, file_sha, total_chunks)

        # power of hash based naming structure, prevents dups
        if db.find_by_sha256(sha256=file_sha):
            _sessions.pop(upload_id, None)
            log.info("DUP file_sha=%.12s", file_sha)
            return {"status": "duplicate", "sha256": file_sha, "percent": 100.0}

        is_multi = total_chunks > 1
        db.upsert_file(
            sha256=file_sha,
            original_filename=sess["filename"],
            repo=repo,
            mime_type=sess["mime_type"],
            size_bytes=sess["size_bytes"],
            is_chunked=is_multi,
            chunks=sess["chunks"] if is_multi else [],
        )
        db.increment_repo_size(repo, sess["size_bytes"])
        _sessions.pop(upload_id, None)
        log.info("UPLOAD OK file_sha=%.12s file=%r chunks=%d repo=%s", file_sha, sess["filename"], total_chunks, repo)
        return {"status": "done", "sha256": file_sha, "percent": 100.0}

    return {
        "status":       "chunk_ok",
        "chunk":        chunk_index + 1,
        "total_chunks": total_chunks,
        "percent":      percent,
    }


@app.get("/files")
async def list_files() -> list[dict]:
    files = db.list_all_files()
    return files


@app.get("/download/{file_sha256}", response_model=None)
async def download_file(file_sha256: str) -> Response | StreamingResponse:
    record = db.find_by_sha256(sha256=file_sha256)
    if not record:
        log.warning("DOWN 404 sha=%.12s", file_sha256)
        raise HTTPException(status_code=404, detail=f"No file found for SHA-256 {file_sha256!r}")

    repo     = record["repo"]
    filename = record["original_filename"]
    headers  = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length":      str(record["size_bytes"]),
    }
    log.info("DOWN sha=%.12s file=%r chunked=%s", file_sha256, filename, record.get("is_chunked"))

    if record.get("is_chunked"):
        chunks = sorted(record["chunks"], key=lambda c: c["index"])
        log.debug("STREAM %d chunks  repo=%s", len(chunks), repo)

        def _iter_chunks():
            for c in chunks:
                log.debug("FETCHC idx=%d sha=%.12s", c["index"], c["sha256"])
                yield _github.download_file(repo=repo, path=_object_path(c["sha256"]))

        return StreamingResponse(_iter_chunks(), media_type=record["mime_type"], headers=headers)

    try:
        log.debug("FETCHS sha=%.12s", file_sha256)
        data = _github.download_file(repo=repo, path=_object_path(record["sha256"]))
    except Exception as exc:
        log.error("DOWN ERR sha=%.12s err=%s", file_sha256, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    log.info("DOWN OK sha=%.12s size=%dB", file_sha256, len(data))
    return Response(content=data, media_type=record["mime_type"], headers=headers)
