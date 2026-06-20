import mimetypes
import os

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from github import GitHubClient

load_dotenv()

app = FastAPI(title="GDrive", version="1.0.0")

_github = GitHubClient(token=os.environ["GITHUB_TOKEN"])


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

    try:
        result = _github.upload_file(repo=repo, filename=filename, content=content)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    file_info = result.get("content", {})
    return {
        "name": file_info.get("name"),
        "size": file_info.get("size"),
        "sha": file_info.get("sha"),
        "html_url": file_info.get("html_url"),
    }

@app.get("/repo/{repo}/files")
async def list_files(repo: str) -> list[dict]:
    try:
        items = _github.list_files(repo=repo)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return [
        {
            "name": item["name"],
            "type": item["type"],
            "size": item.get("size"),
            "sha": item["sha"],
            "download_url": item.get("download_url"),
        }
        for item in items
    ]


@app.get("/repo/{repo}/download/{filename}")
async def download_file(repo: str, filename: str) -> Response:
    try:
        data = _github.download_file(repo=repo, filename=filename)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    mime, _ = mimetypes.guess_type(filename)

    return Response(
        content=data,
        media_type=mime or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
