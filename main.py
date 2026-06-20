import mimetypes
import os

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response

import db
from github import GitHubClient

load_dotenv()

app = FastAPI(title="GDrive", version="1.1.0")

_github = GitHubClient(token=os.environ["GITHUB_TOKEN"])


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

    try:
        result = _github.upload_file(repo=repo, filename=filename, content=content)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    file_info = result.get("content", {})

    record = db.upsert_file(
        filename=filename,
        repo=repo,
        mime_type=mime_type or "application/octet-stream",
        size_bytes=file_info.get("size") or len(content),
        github_sha=file_info.get("sha", ""),
    )

    return record

@app.get("/repo/{repo}/files")
async def list_files(repo: str) -> list[dict]:
    return db.list_files(repo=repo)


@app.get("/repo/{repo}/download/{filename}")
async def download_file(repo: str, filename: str) -> Response:
    record = db.get_file(repo=repo, filename=filename)
    if not record:
        raise HTTPException(status_code=404, detail=f"{filename!r} not found in repo {repo!r}")

    try:
        data = _github.download_file(repo=repo, filename=filename)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(
        content=data,
        media_type=record["mime_type"],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
