import io
import mimetypes
import os

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response

import db
from github import GitHubClient

load_dotenv()

app = FastAPI(title="GDrive", version="1.2.0")

_github = GitHubClient(token=os.environ["GITHUB_TOKEN"])

CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB which happens to be ~13 MB base64 payload per chunk

def _split(data: bytes) -> list[bytes]:
    return [data[i : i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]

@app.get("/")
async def root() -> dict:
    return {"status": "ok"}


@app.post("/repo/create")
async def create_repo(name: str) -> dict:
    try:
        result = _github.create_repo(name=name)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"repo": result["full_name"], "url": result["html_url"]}


@app.post("/repo/{repo}/upload")
async def upload_file(repo: str, file: UploadFile = File(...)) -> dict:
    content = await file.read()
    filename = file.filename
    mime_type, _ = mimetypes.guess_type(filename or "")

    if len(content) <= CHUNK_SIZE:
        try:
            result = _github.upload_file(repo=repo, filename=filename, content=content)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        file_info = result.get("content", {})
        return db.upsert_file(
            filename=filename,
            repo=repo,
            mime_type=mime_type,
            size_bytes=len(content),
            github_sha=file_info.get("sha"),
            is_chunked=False,
        )

    raw_chunks = _split(content)
    total = len(raw_chunks)
    chunk_records: list[dict] = []

    for i, chunk in enumerate(raw_chunks):
        chunk_path = f"_chunks/{filename}/chunk_{i:04d}"

        try:
            result = _github.upload_file(repo=repo, filename=chunk_path, content=chunk)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed on chunk {i + 1}/{total} ({chunk_path}): {exc}",
            ) from exc

        file_info = result.get("content", {})
        chunk_records.append({
            "index": i,
            "path": chunk_path,
            "sha": file_info.get("sha", ""),
            "size": len(chunk),
        })

    return db.upsert_file(
        filename=filename,
        repo=repo,
        mime_type=mime_type,
        size_bytes=len(content),
        github_sha=None,
        is_chunked=True,
        chunks=chunk_records,
    )


@app.get("/repo/{repo}/files")
async def list_files(repo: str) -> list[dict]:
    return db.list_files(repo=repo)


@app.get("/repo/{repo}/download/{file_path:path}")
async def download_file(repo: str, file_path: str) -> Response:
    record = db.get_file(repo=repo, filename=file_path)
    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"{file_path!r} not found in repo {repo!r}",
        )

    if record.get("is_chunked"):
        buffer = io.BytesIO()
        chunks = sorted(record["chunks"], key=lambda c: c["index"])
        for chunk in chunks:
            try:
                buffer.write(_github.download_file(repo=repo, filename=chunk["path"]))
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed fetching chunk {chunk['index']} ({chunk['path']}): {exc}",
                ) from exc
        data = buffer.getvalue()

    else:
        try:
            data = _github.download_file(repo=repo, filename=file_path)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    filename = file_path.rsplit("/", 1)[-1]
    return Response(
        content=data,
        media_type=record["mime_type"],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
