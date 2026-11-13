import re
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import config, db, security
from .core import (
    accessible_space_ids, add_folder_node, ancestors, can_modify, check_parent, clean_name, current_user,
    get_node, get_space, is_inside, purge, ser_node, ser_nodes, unique_name, LEVELS,
)

router = APIRouter(prefix="/api")


def base_url(request: Request) -> str:
    return config.API_PUBLIC_URL or str(request.base_url).rstrip("/")


class FolderIn(BaseModel):
    name: str
    parent: str | None = None


class EnsureIn(BaseModel):
    path: str
    parent: str | None = None


class PatchIn(BaseModel):
    name: str | None = None
    parent: str | None = None  # "root" or a folder id
    starred: bool | None = None


class ShareIn(BaseModel):
    expires_days: int | None = None


class UrlIn(BaseModel):
    download: bool = False


class ZipIn(BaseModel):
    space: str
    ids: list[str]
    name: str | None = None


def _sorted(nodes: list[dict]) -> list[dict]:
    return sorted(nodes, key=lambda n: (n["type"] != "folder", n["name"].lower()))


# -------------------------------------------------------------------- listing
@router.get("/spaces/{space_id}/nodes")
def list_nodes(space_id: str, parent: str = "root", trashed: bool = False, user: dict = Depends(current_user)) -> dict:
    space, role = get_space(space_id, user)
    if trashed:
        q = {"space": space_id, "trashed_at": {"$ne": None}}
        if space["type"] == "public" and not user["is_admin"]:
            q["owner"] = user["username"]
        elif LEVELS[role] < LEVELS["editor"]:
            return {"nodes": [], "path": [], "role": role}
        nodes = list(db.nodes.find(q).sort("trashed_at", -1))
        return {"nodes": ser_nodes(nodes, user), "path": [], "role": role}
    pid = check_parent(space_id, parent)
    nodes = list(db.nodes.find({"space": space_id, "parent": pid, "trashed_at": None}))
    path = []
    if pid:
        cur = db.nodes.find_one({"_id": pid})
        path = [{"id": str(a["_id"]), "name": a["name"]} for a in ancestors(cur)] + [{"id": str(cur["_id"]), "name": cur["name"]}]
    return {"nodes": ser_nodes(_sorted(nodes), user), "path": path, "role": role}


@router.get("/nodes/{node_id}")
def node_info(node_id: str, user: dict = Depends(current_user)) -> dict:
    n, _, role = get_node(node_id, user)
    return {**ser_nodes([n], user)[0], "role": role,
            "path": [{"id": str(a["_id"]), "name": a["name"]} for a in ancestors(n)]}


def _accessible_files_query(user: dict) -> dict:
    return {"space": {"$in": accessible_space_ids(user)}, "trashed_at": None}


@router.get("/recent")
def recent(user: dict = Depends(current_user)) -> list[dict]:
    q = {**_accessible_files_query(user), "type": "file"}
    return ser_nodes(list(db.nodes.find(q).sort("created_at", -1).limit(60)), user)


@router.get("/starred")
def starred(user: dict = Depends(current_user)) -> list[dict]:
    q = {**_accessible_files_query(user), "starred_by": user["username"]}
    return ser_nodes(_sorted(list(db.nodes.find(q).limit(300))), user)


@router.get("/search")
def search(q: str, user: dict = Depends(current_user)) -> list[dict]:
    q = q.strip()
    if len(q) < 1:
        return []
    query = {**_accessible_files_query(user), "name": {"$regex": re.escape(q[:100]), "$options": "i"}}
    return ser_nodes(_sorted(list(db.nodes.find(query).limit(80))), user)


# ---------------------------------------------------------------------- folders
@router.post("/spaces/{space_id}/folders")
def create_folder(space_id: str, body: FolderIn, user: dict = Depends(current_user)) -> dict:
    get_space(space_id, user, "editor")
    pid = check_parent(space_id, body.parent)
    n = add_folder_node(space_id, pid, clean_name(body.name), user["username"])
    return ser_nodes([n], user)[0]


@router.post("/spaces/{space_id}/folders/ensure")
def ensure_folders(space_id: str, body: EnsureIn, user: dict = Depends(current_user)) -> dict:
    """Create (or reuse) a nested folder path like 'photos/2024/trip'. Used by folder uploads."""
    get_space(space_id, user, "editor")
    pid = check_parent(space_id, body.parent)
    parts = [p for p in body.path.split("/") if p]
    if len(parts) > 32:
        raise HTTPException(400, "Path too deep")
    node = None
    for part in parts:
        name = clean_name(part)
        node = db.nodes.find_one({"space": space_id, "parent": pid, "type": "folder", "name": name, "trashed_at": None})
        if not node:
            node = add_folder_node(space_id, pid, name, user["username"])
        pid = node["_id"]
    return {"id": str(pid) if pid else None}


# ------------------------------------------------------------- rename/move/star
@router.patch("/nodes/{node_id}")
def patch_node(node_id: str, body: PatchIn, user: dict = Depends(current_user)) -> dict:
    n, space, role = get_node(node_id, user)
    updates: dict = {}
    if body.starred is not None:
        op = "$addToSet" if body.starred else "$pull"
        db.nodes.update_one({"_id": n["_id"]}, {op: {"starred_by": user["username"]}})
    if body.name is not None or body.parent is not None:
        if not can_modify(space, role, user, n):
            raise HTTPException(403, "You can't change this item")
        if n.get("trashed_at"):
            raise HTTPException(400, "Restore the item first")
        parent = n["parent"]
        if body.parent is not None:
            parent = check_parent(n["space"], body.parent)
            if parent and (parent == n["_id"] or is_inside(db.nodes.find_one({"_id": parent}), n["_id"])):
                raise HTTPException(400, "You can't move a folder into itself")
        name = clean_name(body.name) if body.name is not None else n["name"]
        if name != n["name"] or parent != n["parent"]:
            updates = {"name": unique_name(n["space"], parent, name, exclude_id=n["_id"]), "parent": parent, "updated_at": db.now()}
            db.nodes.update_one({"_id": n["_id"]}, {"$set": updates})
    return ser_nodes([db.nodes.find_one({"_id": n["_id"]})], user)[0]


@router.post("/nodes/{node_id}/trash")
def trash(node_id: str, user: dict = Depends(current_user)) -> dict:
    n, space, role = get_node(node_id, user)
    if not can_modify(space, role, user, n):
        raise HTTPException(403, "You can't delete this item")
    db.nodes.update_one({"_id": n["_id"]}, {"$set": {"trashed_at": db.now()}})
    return {"ok": True}


@router.post("/nodes/{node_id}/restore")
def restore(node_id: str, user: dict = Depends(current_user)) -> dict:
    n, space, role = get_node(node_id, user)
    if not can_modify(space, role, user, n):
        raise HTTPException(403, "You can't restore this item")
    parent = n["parent"]
    if parent and not db.nodes.find_one({"_id": parent, "trashed_at": None}):
        parent = None  # its folder is gone, put it back at the top
    db.nodes.update_one({"_id": n["_id"]}, {"$set": {
        "trashed_at": None, "parent": parent, "name": unique_name(n["space"], parent, n["name"], exclude_id=n["_id"])}})
    return {"ok": True}


@router.delete("/nodes/{node_id}")
def delete_forever(node_id: str, user: dict = Depends(current_user)) -> dict:
    n, space, role = get_node(node_id, user)
    if not can_modify(space, role, user, n):
        raise HTTPException(403, "You can't delete this item")
    if not n.get("trashed_at"):
        raise HTTPException(400, "Move it to the trash first")
    purge([n["_id"]])
    return {"ok": True}


@router.post("/spaces/{space_id}/trash/empty")
def empty_trash(space_id: str, user: dict = Depends(current_user)) -> dict:
    space, role = get_space(space_id, user, "editor")
    q = {"space": space_id, "trashed_at": {"$ne": None}}
    mine = [n["_id"] for n in db.nodes.find(q) if can_modify(space, role, user, n)]
    purge(mine)
    return {"ok": True, "deleted": len(mine)}


# ------------------------------------------------------------------ share links
def ser_share(s: dict, node: dict, request: Request) -> dict:
    b = base_url(request)
    return {
        "token": s["token"], "page_url": f"{b}/s/{s['token']}",
        "raw_url": f"{b}/s/{s['token']}/{quote(node['name'])}" if node["type"] == "file" else None,
        "app_url": f"{config.FRONTEND_URL}/share/{s['token']}",
        "expires_at": db.iso(s.get("expires_at")), "views": s.get("views", 0), "created_at": db.iso(s["created_at"]),
    }


def _active_share(node_id) -> dict | None:
    s = db.shares.find_one({"node": node_id, "revoked": False})
    if s and s.get("expires_at") and s["expires_at"].replace(tzinfo=db.now().tzinfo) < db.now():
        db.shares.update_one({"_id": s["_id"]}, {"$set": {"revoked": True}})
        return None
    return s


def _share_perm(space, role, user, node) -> None:
    if not can_modify(space, role, user, node):
        raise HTTPException(403, "You can't share this item")


@router.get("/nodes/{node_id}/share")
def get_share(node_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    n, *_ = get_node(node_id, user)
    s = _active_share(n["_id"])
    return {"share": ser_share(s, n, request) if s else None}


@router.post("/nodes/{node_id}/share")
def create_share(node_id: str, body: ShareIn, request: Request, user: dict = Depends(current_user)) -> dict:
    from datetime import timedelta

    n, space, role = get_node(node_id, user)
    _share_perm(space, role, user, n)
    if n.get("trashed_at"):
        raise HTTPException(400, "Restore the item first")
    expires = db.now() + timedelta(days=body.expires_days) if body.expires_days and 0 < body.expires_days <= 3650 else None
    s = _active_share(n["_id"])
    if s:
        db.shares.update_one({"_id": s["_id"]}, {"$set": {"expires_at": expires}})
        s["expires_at"] = expires
    else:
        s = {"token": security.new_token(16), "node": n["_id"], "space": n["space"], "created_by": user["username"],
             "created_at": db.now(), "expires_at": expires, "revoked": False, "views": 0}
        db.shares.insert_one(s)
    return {"share": ser_share(s, n, request)}


@router.delete("/nodes/{node_id}/share")
def revoke_share(node_id: str, user: dict = Depends(current_user)) -> dict:
    n, space, role = get_node(node_id, user)
    _share_perm(space, role, user, n)
    db.shares.update_many({"node": n["_id"]}, {"$set": {"revoked": True}})
    return {"ok": True}


# ------------------------------------------------------------- signed app links
LINK_TTL = 6 * 3600


@router.post("/nodes/{node_id}/url")
def signed_url(node_id: str, body: UrlIn, request: Request, user: dict = Depends(current_user)) -> dict:
    n, *_ = get_node(node_id, user)
    if n["type"] != "file":
        raise HTTPException(400, "Folders download as a zip")
    exp = str(int(time.time()) + LINK_TTL)
    dl = "1" if body.download else "0"
    sig = security.sign("file", str(n["_id"]), exp, dl)
    return {"url": f"{base_url(request)}/d/{n['_id']}?exp={exp}&dl={dl}&sig={sig}"}


@router.post("/zip-url")
def zip_url(body: ZipIn, request: Request, user: dict = Depends(current_user)) -> dict:
    get_space(body.space, user)
    ids = [i for i in body.ids if db.oid(i)][:500]
    if not ids:
        raise HTTPException(400, "Nothing selected")
    found = list(db.nodes.find({"_id": {"$in": [db.oid(i) for i in ids]}, "space": body.space, "trashed_at": None}, {"_id": 1}))
    if not found:
        raise HTTPException(404, "Nothing found")
    joined = ",".join(str(f["_id"]) for f in found)
    exp = str(int(time.time()) + LINK_TTL)
    sig = security.sign("zip", body.space, joined, exp)
    name = quote(clean_name(body.name) if body.name else "GitDrive")
    return {"url": f"{base_url(request)}/d/zip?space={quote(body.space)}&ids={joined}&exp={exp}&name={name}&sig={sig}"}
