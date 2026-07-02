import asyncio
import hashlib
import mimetypes
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse

import db
from github import GitHubClient

load_dotenv()

app = FastAPI(title="refactored-carnival", version="2.1.0")

_github = GitHubClient(token=os.environ["GITHUB_TOKEN"])

CHUNK_SIZE  = 10 * 1024 * 1024   # 10 MB, cuz, base64 encoding inflates a bit...anddd smaller the better!
MAX_RETRIES = 3
RETRY_WAIT  = 1


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object_path(sha: str, chunked: bool) -> str:
    ext = ".chunk" if chunked else ".bin"
    return f"objects/{sha[:2]}/{sha[2:]}{ext}"


def _split(data: bytes) -> list[bytes]:
    return [data[i : i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]


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


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


async def _upload_stream(content: bytes, original_filename: str, mime_type: str):
    file_sha = _sha256(content)

    existing = db.find_by_sha256(sha256=file_sha)
    if existing:
        yield _sse({"status": "duplicate", "sha256": file_sha})
        return

    repo, is_new = db.get_active_repo()
    if is_new:
        try:
            await asyncio.to_thread(_github.create_repo, repo)
        except Exception as exc:
            yield _sse({"status": "error", "error": str(exc)})
            return

    if len(content) <= CHUNK_SIZE:
        try:
            await asyncio.to_thread(
                _upload_with_retry, repo, _object_path(file_sha, False), content
            )
        except Exception as exc:
            yield _sse({"status": "error", "error": str(exc)})
            return

        db.upsert_file(
            sha256=file_sha,
            original_filename=original_filename,
            repo=repo,
            mime_type=mime_type,
            size_bytes=len(content),
            is_chunked=False,
        )
        db.increment_repo_size(repo, len(content))
        yield _sse({"status": "done", "sha256": file_sha})
        return

    raw_chunks = _split(content)
    total = len(raw_chunks)
    chunk_records: list[dict] = []

    for i, chunk in enumerate(raw_chunks):
        chunk_sha  = _sha256(chunk)
        chunk_path = _object_path(chunk_sha, chunked=True)

        try:
            await asyncio.to_thread(_upload_with_retry, repo, chunk_path, chunk)
        except Exception as exc:
            yield _sse({"status": "error", "chunk": i + 1, "total_chunks": total, "error": str(exc)})
            return

        chunk_records.append({"index": i, "sha256": chunk_sha, "size": len(chunk)})
        yield _sse({
            "status":       "uploading",
            "chunk":        i + 1,
            "total_chunks": total,
            "percent":      round((i + 1) / total * 100, 1),
        })

    db.upsert_file(
        sha256=file_sha,
        original_filename=original_filename,
        repo=repo,
        mime_type=mime_type,
        size_bytes=len(content),
        is_chunked=True,
        chunks=chunk_records,
    )
    db.increment_repo_size(repo, len(content))
    yield _sse({"status": "done", "sha256": file_sha, "total_chunks": total})


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(content=(Path(__file__).parent / "index.html").read_text(encoding="utf-8"))


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)) -> StreamingResponse:
    content = await file.read()
    original_filename = file.filename or "unnamed"
    mime_type = mimetypes.guess_type(original_filename)[0] or "application/octet-stream"
    return StreamingResponse(
        _upload_stream(content, original_filename, mime_type),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
            for chunk in chunks:
                yield _github.download_file(repo=repo, path=_object_path(chunk["sha256"], chunked=True))

        return StreamingResponse(_iter_chunks(), media_type=record["mime_type"], headers=headers)

    try:
        data = _github.download_file(repo=repo, path=_object_path(record["sha256"], chunked=False))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(content=data, media_type=record["mime_type"], headers=headers)
