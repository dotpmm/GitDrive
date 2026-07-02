import asyncio
import hashlib
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

app = FastAPI(title="GDrive", version="2.0.0")

_github = GitHubClient(token=os.environ["GITHUB_TOKEN"])

CHUNK_SIZE  = 10 * 1024 * 1024   # 10 MB, cuz, base64 encoding inflates a bit...anddd smaller the better!
MAX_RETRIES = 3
RETRY_WAIT  = 1

_sessions: dict[str, dict] = {}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object_path(sha: str) -> str:
    return f"objects/{sha[:2]}/{sha[2:]}.chunk"


def _upload_with_retry(repo: str, path: str, content: bytes) -> dict:
    last_exc: Exception = RuntimeError("Upload failed before first attempt.")
    for attempt in range(MAX_RETRIES):
        try:
            return _github.upload_file(repo=repo, path=path, content=content)
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_WAIT)
    raise last_exc


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(content=(Path(__file__).parent / "index.html").read_text(encoding="utf-8"))


@app.post("/upload")
async def upload_chunk(
    chunk:        UploadFile = File(...),
    upload_id:    str        = Form(...),
    chunk_index:  int        = Form(...),
    total_chunks: int        = Form(...),
    filename:     str        = Form(...),
    mime_type:    str        = Form(""),
    size_bytes:   int        = Form(...),
) -> dict:
    data      = await chunk.read()
    chunk_sha = _sha256(data)
    is_multi  = total_chunks > 1
    path      = _object_path(chunk_sha)

    if upload_id not in _sessions:
        resolved_mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        repo, is_new = db.get_active_repo()
        if is_new:
            try:
                await asyncio.to_thread(_github.create_repo, repo)
            except Exception as exc:
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
        _sessions.pop(upload_id, None)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    sess["chunks"].append({"index": chunk_index, "sha256": chunk_sha, "size": len(data)})
    percent = round((chunk_index + 1) / total_chunks * 100, 1)

    if chunk_index + 1 == total_chunks:
        file_sha = sess["hasher"].hexdigest()

        if db.find_by_sha256(sha256=file_sha):
            _sessions.pop(upload_id, None)
            return {"status": "duplicate", "sha256": file_sha, "percent": 100.0}

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
        return {"status": "done", "sha256": file_sha, "percent": 100.0}

    return {
        "status":       "chunk_ok",
        "chunk":        chunk_index + 1,
        "total_chunks": total_chunks,
        "percent":      percent,
    }


@app.get("/files")
async def list_files() -> list[dict]:
    return db.list_all_files()


@app.get("/download/{file_sha256}", response_model=None)
async def download_file(file_sha256: str) -> Response | StreamingResponse:
    record = db.find_by_sha256(sha256=file_sha256)
    if not record:
        raise HTTPException(status_code=404, detail=f"No file found for SHA-256 {file_sha256!r}")

    repo     = record["repo"]
    filename = record["original_filename"]
    headers  = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length":      str(record["size_bytes"]),
    }

    if record.get("is_chunked"):
        chunks = sorted(record["chunks"], key=lambda c: c["index"])

        def _iter_chunks():
            for c in chunks:
                yield _github.download_file(repo=repo, path=_object_path(c["sha256"]))

        return StreamingResponse(_iter_chunks(), media_type=record["mime_type"], headers=headers)

    try:
        data = _github.download_file(repo=repo, path=_object_path(record["sha256"]))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(content=data, media_type=record["mime_type"], headers=headers)
