"""Uploads, downloads/streaming, public share links, zip + unzip."""
import html
import io
import mimetypes
import re
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel
from stream_zip import NO_COMPRESSION_64, stream_zip

from . import config, db, security, storage
from .core import (
    add_file_node, add_folder_node, check_parent, check_quota, clean_name, current_user, get_node, get_space,
    is_inside, subtree, unique_name,
)
from .routes_nodes import base_url

log = __import__("logging").getLogger("GitDrive")
router = APIRouter()

# ================================================================== uploads
_uploads: dict[str, storage.UploadSession] = {}


class UploadIn(BaseModel):
    space: str
    parent: str | None = None
    name: str
    size: int
    mime: str = ""


@router.post("/api/uploads")
def start_upload(body: UploadIn, user: dict = Depends(current_user)) -> dict:
    if not 0 <= body.size <= config.MAX_FILE_BYTES:
        raise HTTPException(413, f"Files can be at most {config.MAX_FILE_BYTES // 1024**3} GB")
    get_space(body.space, user, "editor")
    parent = check_parent(body.space, body.parent)
    check_quota(user, body.size)
    sess = storage.UploadSession(user["username"], clean_name(body.name), body.size)
    sess.meta = {"space": body.space, "parent": parent, "mime": body.mime or mimetypes.guess_type(body.name)[0] or "", "user": user["username"]}
    _uploads[sess.id] = sess
    return {"upload_id": sess.id, "chunk_size": config.CHUNK_SIZE, "total_chunks": sess.total}


def _session(upload_id: str, user: dict) -> storage.UploadSession:
    s = _uploads.get(upload_id)
    if not s or s.meta.get("user") != user["username"]:
        raise HTTPException(404, "Upload session not found (it may have expired)")
    return s


@router.put("/api/uploads/{upload_id}/chunks/{index}")
async def put_chunk(upload_id: str, index: int, request: Request, user: dict = Depends(current_user)) -> dict:
    sess = _session(upload_id, user)
    data = await request.body()
    try:
        await run_in_threadpool(sess.put_chunk, index, data)
    except storage.StorageError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        log.error("CHUNK FAIL upload=%.8s idx=%d err=%s", upload_id, index, exc)
        raise HTTPException(502, "Could not store the chunk on GitHub, please retry") from exc
    return {"ok": True, "received": len(sess.chunks), "total": sess.total}


@router.post("/api/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, user: dict = Depends(current_user)) -> dict:
    sess = _session(upload_id, user)
    try:
        blob = sess.complete()
    except storage.StorageError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        log.error("COMPLETE FAIL upload=%.8s err=%s", upload_id, exc)
        sess.abort()
        _uploads.pop(upload_id, None)
        raise HTTPException(502, f"Could not commit to GitHub: {exc}") from exc
    node = add_file_node(sess.meta["space"], sess.meta["parent"], sess.name, user["username"], blob, sess.meta["mime"])
    _uploads.pop(upload_id, None)
    from .core import ser_nodes
    return ser_nodes([node], user)[0]


@router.delete("/api/uploads/{upload_id}")
def abort_upload(upload_id: str, user: dict = Depends(current_user)) -> dict:
    sess = _session(upload_id, user)
    sess.abort()
    _uploads.pop(upload_id, None)
    return {"ok": True}


def reap_uploads() -> None:
    for uid, s in list(_uploads.items()):
        if time.time() - s.created > config.UPLOAD_SESSION_TTL:
            s.abort()
            _uploads.pop(uid, None)


# ================================================================ streaming
_FORCE_TEXT = {"text/html", "application/xhtml+xml", "text/xml", "application/xml", "text/javascript", "application/javascript"}


def _safe_mime(mime: str | None) -> str:
    m = (mime or "application/octet-stream").split(";")[0].strip().lower()
    return "text/plain" if m in _FORCE_TEXT else m


def _disposition(name: str, attachment: bool) -> str:
    safe = re.sub(r'[\r\n"\;]', "_", name).encode("ascii", "replace").decode() or "file"
    return f"{'attachment' if attachment else 'inline'}; filename=\"{safe}\"; filename*=UTF-8''{quote(name, safe='')}"


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header or size == 0:
        return None
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    s, e = m.groups()
    if s == "":
        n = int(e)
        if n == 0:
            raise HTTPException(416, headers={"Content-Range": f"bytes */{size}"})
        return max(0, size - n), size - 1
    start, end = int(s), min(int(e) if e else size - 1, size - 1)
    if start >= size or start > end:
        raise HTTPException(416, headers={"Content-Range": f"bytes */{size}"})
    return start, end


def serve_blob(request: Request, node: dict, *, attachment: bool, cache: str, public: bool) -> Response:
    blob = db.blobs.find_one({"_id": node["blob"]})
    if not blob:
        raise HTTPException(410, "This file's data is gone")
    size = blob["size"]
    mime = _safe_mime(node.get("mime"))
    etag = f'"{blob["_id"]}"'
    headers = {
        "Accept-Ranges": "bytes", "ETag": etag, "Cache-Control": cache, "X-Content-Type-Options": "nosniff",
        "Content-Disposition": _disposition(node["name"], attachment),
        "Cross-Origin-Resource-Policy": "cross-origin",
    }
    if mime != "application/pdf":
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; media-src 'self'"
    if public:
        headers["Access-Control-Allow-Origin"] = "*"
        headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges"
    if request.headers.get("if-none-match") == etag and not request.headers.get("range"):
        return Response(status_code=304, headers=headers)

    rng = _parse_range(request.headers.get("range"), size)
    if rng and request.headers.get("if-range", etag) != etag:
        rng = None
    if rng:
        start, end = rng
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        status = 206
    else:
        start, end, status = 0, size - 1, 200
    headers["Content-Length"] = str(max(0, end - start + 1))
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=mime)
    return StreamingResponse(storage.iter_range(blob, start, end), status_code=status, headers=headers, media_type=mime)


# -------------------------------------------------------- signed in-app links
@router.api_route("/d/zip", methods=["GET"])
def download_zip(space: str, ids: str, exp: str, sig: str, name: str = "GitDrive") -> Response:
    if not exp.isdigit() or int(exp) < time.time() or not security.verify_sig(sig, "zip", space, ids, exp):
        raise HTTPException(403, "This download link has expired")
    roots = [db.oid(i) for i in ids.split(",") if db.oid(i)]
    top = list(db.nodes.find({"_id": {"$in": roots}, "space": space, "trashed_at": None}))
    if not top:
        raise HTTPException(404, "Nothing to download")
    fname = re.sub(r'[\r\n"\;/]', "_", name)[:100] or "GitDrive"
    return StreamingResponse(
        _zip_stream(top), media_type="application/zip",
        headers={"Content-Disposition": _disposition(f"{fname}.zip", True), "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@router.api_route("/d/{node_id}", methods=["GET", "HEAD"])
def download_signed(node_id: str, request: Request, exp: str, sig: str, dl: str = "0") -> Response:
    if not exp.isdigit() or int(exp) < time.time() or not security.verify_sig(sig, "file", node_id, exp, dl):
        raise HTTPException(403, "This link has expired, reopen the file")
    node = db.nodes.find_one({"_id": db.oid(node_id), "type": "file"})
    if not node:
        raise HTTPException(404, "Not found")
    return serve_blob(request, node, attachment=dl == "1", cache="private, max-age=600", public=False)


def _zip_members(top: list[dict]) -> Iterator[tuple]:
    """(name, mtime, mode, method, chunks) tuples for stream_zip, walking folders recursively."""
    def walk(node: dict, prefix: str):
        if node["type"] == "file":
            blob = db.blobs.find_one({"_id": node["blob"]})
            if blob:
                yield (prefix + node["name"], node.get("updated_at") or db.now(), 0o644, NO_COMPRESSION_64,
                       storage.iter_range(blob, 0, blob["size"] - 1))
        else:
            kids = list(db.nodes.find({"parent": node["_id"], "trashed_at": None}))
            if not kids:
                yield (prefix + node["name"] + "/", node.get("updated_at") or db.now(), 0o755, NO_COMPRESSION_64, iter(()))
            for k in kids:
                yield from walk(k, prefix + node["name"] + "/")
    for n in top:
        yield from walk(n, "")


def _zip_stream(top: list[dict]) -> Iterator[bytes]:
    def members():
        for name, mtime, mode, method, chunks in _zip_members(top):
            if mtime.tzinfo is None:
                mtime = mtime.replace(tzinfo=timezone.utc)
            yield name, mtime.replace(tzinfo=None), mode, method, chunks
    yield from stream_zip(members())


# ======================================================== public share links
def _find_share(token: str) -> dict:
    s = db.shares.find_one({"token": token, "revoked": False})
    if s and s.get("expires_at") and s["expires_at"].replace(tzinfo=timezone.utc) < db.now():
        s = None
    node = db.nodes.find_one({"_id": s["node"], "trashed_at": None}) if s else None
    if not s or not node:
        raise HTTPException(404, "This link doesn't exist or has been turned off")
    s["_node"] = node
    return s


def _count_view(request: Request, s: dict) -> None:
    rng = request.headers.get("range", "")
    if request.method == "GET" and (not rng or rng.startswith("bytes=0-")):
        db.shares.update_one({"_id": s["_id"]}, {"$inc": {"views": 1}})


_CACHE_PUBLIC = "public, max-age=3600"


@router.api_route("/s/{token}/n/{node_id}/{name}", methods=["GET", "HEAD"])
def share_child(token: str, node_id: str, name: str, request: Request, dl: int = 0) -> Response:
    s = _find_share(token)
    root = s["_node"]
    node = db.nodes.find_one({"_id": db.oid(node_id), "type": "file", "trashed_at": None})
    if root["type"] != "folder" or not node or not is_inside(node, root["_id"]):
        raise HTTPException(404, "Not found")
    _count_view(request, s)
    return serve_blob(request, node, attachment=bool(dl), cache=_CACHE_PUBLIC, public=True)


@router.api_route("/s/{token}/{name}", methods=["GET", "HEAD"])
def share_file(token: str, name: str, request: Request, dl: int = 0) -> Response:
    s = _find_share(token)
    node = s["_node"]
    if node["type"] != "file":
        raise HTTPException(404, "Not found")
    _count_view(request, s)
    return serve_blob(request, node, attachment=bool(dl), cache=_CACHE_PUBLIC, public=True)


_BOTS = re.compile(r"discord|twitterbot|slackbot|facebookexternalhit|telegrambot|whatsapp|linkedinbot|skypeuripreview|embedly|bsky|mastodon", re.I)


def _fmt(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


@router.get("/s/{token}")
def share_page(token: str, request: Request) -> Response:
    """Link-preview bots get Open Graph tags (Discord/Slack/Telegram embeds); people get the app."""
    s = _find_share(token)
    node = s["_node"]
    app_url = f"{config.FRONTEND_URL}/share/{token}"
    if not _BOTS.search(request.headers.get("user-agent", "")):
        return RedirectResponse(app_url, status_code=302)
    b = base_url(request)
    e = html.escape
    name = node["name"]
    tags = [("og:site_name", "GitDrive"), ("og:title", name), ("og:url", f"{b}/s/{token}")]
    if node["type"] == "file":
        raw = f"{b}/s/{token}/{quote(name)}"
        mime = _safe_mime(node.get("mime"))
        tags.append(("og:description", f"{_fmt(node.get('size', 0))} - shared on GitDrive"))
        if mime.startswith("video/"):
            tags += [("og:type", "video.other"), ("og:video", raw), ("og:video:url", raw), ("og:video:secure_url", raw),
                     ("og:video:type", mime), ("og:video:width", "1280"), ("og:video:height", "720")]
        elif mime.startswith("image/"):
            tags += [("og:type", "website"), ("og:image", raw)]
        elif mime.startswith("audio/"):
            tags += [("og:type", "music.song"), ("og:audio", raw), ("og:audio:type", mime)]
    else:
        tags.append(("og:description", "Shared folder on GitDrive"))
    meta = "\n".join(f'<meta property="{k}" content="{e(v)}">' for k, v in tags)
    twitter = '<meta name="twitter:card" content="summary_large_image">' if node.get("mime", "").startswith("image/") else ""
    page = (f"<!doctype html><html><head><meta charset='utf-8'><title>{e(name)}</title>{meta}{twitter}"
            f"<meta http-equiv='refresh' content='0;url={e(app_url)}'></head><body><a href='{e(app_url)}'>{e(name)}</a></body></html>")
    return HTMLResponse(page, headers={"Cache-Control": "public, max-age=300"})


def _ser_shared(n: dict, token: str, request: Request) -> dict:
    b = base_url(request)
    out = {"id": str(n["_id"]), "name": n["name"], "type": n["type"], "size": n.get("size", 0), "mime": n.get("mime"),
           "created_at": db.iso(n.get("created_at"))}
    if n["type"] == "file":
        out["raw_url"] = f"{b}/s/{token}/{quote(n['name'])}"
    return out


@router.get("/api/shared/{token}")
def shared_info(token: str, request: Request, parent: str | None = None) -> dict:
    s = _find_share(token)
    root = s["_node"]
    out = {"root": {**_ser_shared(root, token, request), "owner": root.get("owner")}, "views": s.get("views", 0), "children": [], "path": []}
    if root["type"] != "folder":
        return out
    cur = root
    if parent and parent != str(root["_id"]):
        cur = db.nodes.find_one({"_id": db.oid(parent), "type": "folder", "trashed_at": None})
        if not cur or not is_inside(cur, root["_id"]):
            raise HTTPException(404, "Not found")
        chain, c = [], cur
        while c and c["_id"] != root["_id"]:
            chain.append({"id": str(c["_id"]), "name": c["name"]})
            c = db.nodes.find_one({"_id": c["parent"]}) if c.get("parent") else None
        out["path"] = list(reversed(chain))
    kids = sorted(db.nodes.find({"parent": cur["_id"], "trashed_at": None}), key=lambda n: (n["type"] != "folder", n["name"].lower()))
    b = base_url(request)
    for k in kids:
        item = _ser_shared(k, token, request)
        if k["type"] == "file":
            item["raw_url"] = f"{b}/s/{token}/n/{k['_id']}/{quote(k['name'])}"
        out["children"].append(item)
    return out


# ================================================================== unzip
_jobs: dict[str, dict] = {}


def _safe_parts(filename: str) -> list[str] | None:
    parts = [p for p in filename.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." or re.search(r"[\x00-\x1f]", p) or len(p) > 255 for p in parts) or filename.startswith("/") or re.match(r"^[A-Za-z]:", filename):
        return None
    return parts


def _file_chunks(f, size: int) -> Iterator[bytes]:
    remaining = size
    while remaining > 0:
        buf = bytearray()
        want = min(config.CHUNK_SIZE, remaining)
        while len(buf) < want:
            piece = f.read(want - len(buf))
            if not piece:
                raise zipfile.BadZipFile("entry shorter than declared")
            buf += piece
        remaining -= len(buf)
        yield bytes(buf)
    if f.read(1):
        raise zipfile.BadZipFile("entry longer than declared")


def _extract(job: dict, node: dict, user: dict) -> None:
    try:
        blob = db.blobs.find_one({"_id": node["blob"]})
        with zipfile.ZipFile(io.BufferedReader(storage.BlobReader(blob), buffer_size=1024 * 1024)) as zf:
            infos = zf.infolist()
            if len(infos) > config.EXTRACT_MAX_FILES:
                raise ValueError(f"The archive has more than {config.EXTRACT_MAX_FILES} entries")
            if sum(i.file_size for i in infos) > config.EXTRACT_MAX_BYTES:
                raise ValueError("The archive is too large to extract")
            if any(i.flag_bits & 1 for i in infos):
                raise ValueError("Password-protected archives aren't supported")
            job["total"] = len(infos)
            stem = node["name"].rsplit(".", 1)[0] if "." in node["name"] else node["name"] + " (extracted)"
            root = add_folder_node(node["space"], node["parent"], stem or "extracted", user["username"])
            folders: dict[tuple, object] = {(): root["_id"]}

            def ensure(parts: tuple):
                for i in range(1, len(parts) + 1):
                    key = parts[:i]
                    if key not in folders:
                        folders[key] = add_folder_node(node["space"], folders[key[:-1]], key[-1], user["username"])["_id"]
                return folders[parts]

            for info in infos:
                parts = _safe_parts(info.filename)
                job["done"] += 1
                if parts is None:
                    job["skipped"] += 1
                    continue
                if info.is_dir():
                    ensure(tuple(parts))
                    continue
                if not parts:
                    continue
                parent_id = ensure(tuple(parts[:-1]))
                fresh = db.users.find_one({"username": user["username"]})
                check_quota(fresh, info.file_size)
                name = clean_name(parts[-1])
                with zf.open(info) as f:
                    b = storage.store_chunks(user["username"], name, info.file_size, _file_chunks(f, info.file_size))
                add_file_node(node["space"], parent_id, name, user["username"], b, mimetypes.guess_type(name)[0] or "")
        job["status"] = "done"
        job["folder"] = str(root["_id"])
    except Exception as exc:
        log.error("EXTRACT FAIL node=%s err=%s", node["_id"], exc)
        job["status"] = "error"
        job["error"] = str(exc) if isinstance(exc, (ValueError, zipfile.BadZipFile, HTTPException)) else "Could not extract this archive"
        if isinstance(exc, HTTPException):
            job["error"] = exc.detail


@router.post("/api/nodes/{node_id}/extract")
def extract(node_id: str, user: dict = Depends(current_user)) -> dict:
    node, space, _ = get_node(node_id, user, "editor")
    if node["type"] != "file" or not node["name"].lower().endswith(".zip"):
        raise HTTPException(400, "Only .zip files can be extracted")
    job = {"id": uuid.uuid4().hex, "user": user["username"], "status": "running", "done": 0, "total": 0, "skipped": 0, "error": None}
    _jobs[job["id"]] = job
    threading.Thread(target=_extract, args=(job, node, user), daemon=True).start()
    return {"job_id": job["id"]}


@router.get("/api/jobs/{job_id}")
def job_status(job_id: str, user: dict = Depends(current_user)) -> dict:
    job = _jobs.get(job_id)
    if not job or job["user"] != user["username"]:
        raise HTTPException(404, "Job not found")
    return {k: v for k, v in job.items() if k != "user"}
