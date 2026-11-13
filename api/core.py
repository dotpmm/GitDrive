"""Shared dependencies and helpers: auth, space permissions, node utilities."""
import re
from collections import deque

from fastapi import Depends, Header, HTTPException, Request
from pymongo import ReturnDocument

from . import config, db, security, storage

LEVELS = {"viewer": 1, "editor": 2, "owner": 3}


# ------------------------------------------------------------------------ auth
def user_public(u: dict) -> dict:
    return {
        "username": u["username"],
        "email": u.get("email"),
        "is_admin": u["username"] in config.ADMIN_USERNAMES,
        "used_bytes": u.get("used_bytes", 0),
        "quota_bytes": u.get("quota_bytes") if u.get("quota_bytes") is not None else config.DEFAULT_QUOTA_BYTES,
        "created_at": db.iso(u.get("created_at")),
    }


def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Not signed in")
    payload = security.read_token(authorization[7:].strip())
    if not payload:
        raise HTTPException(401, "Session expired, please sign in again")
    user = db.users.find_one({"username": payload["sub"]})
    if not user or user.get("token_version", 0) != payload.get("tv"):
        raise HTTPException(401, "Session expired, please sign in again")
    if user.get("disabled"):
        raise HTTPException(403, "This account has been disabled")
    user["is_admin"] = user["username"] in config.ADMIN_USERNAMES
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    return user


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------- spaces
def space_role(space: dict, username: str) -> str | None:
    t = space["type"]
    if t == "public":
        return "editor"
    if space.get("owner") == username:
        return "owner"
    for m in space.get("members", []):
        if m["username"] == username:
            return m["role"]
    return None


def get_space(space_id: str, user: dict, need: str = "viewer") -> tuple[dict, str]:
    space = db.spaces.find_one({"_id": space_id})
    role = space_role(space, user["username"]) if space else None
    if not role:
        raise HTTPException(404, "Space not found")  # same answer whether it exists or not
    if LEVELS[role] < LEVELS[need]:
        raise HTTPException(403, "You don't have permission to do that here")
    return space, role


def accessible_space_ids(user: dict) -> list[str]:
    q = {"$or": [{"type": "public"}, {"owner": user["username"]}, {"members.username": user["username"]}]}
    return [s["_id"] for s in db.spaces.find(q, {"_id": 1})]


def can_modify(space: dict, role: str, user: dict, node: dict) -> bool:
    """Public space: only the uploader (or an admin) may change a node. Elsewhere editors can."""
    if space["type"] == "public":
        return node.get("owner") == user["username"] or user["is_admin"]
    return LEVELS[role] >= LEVELS["editor"]


# ----------------------------------------------------------------------- nodes
_BAD_NAME = re.compile(r"[\x00-\x1f/\\]")


def clean_name(name: str) -> str:
    n = (name or "").strip()
    if not n or n in (".", "..") or _BAD_NAME.search(n) or len(n) > 255:
        raise HTTPException(400, "Invalid name")
    return n


def get_node(node_id: str, user: dict, need: str = "viewer") -> tuple[dict, dict, str]:
    n = db.nodes.find_one({"_id": db.oid(node_id)}) if db.oid(node_id) else None
    if not n:
        raise HTTPException(404, "Not found")
    space, role = get_space(n["space"], user, need)
    return n, space, role


def unique_name(space_id: str, parent, name: str, exclude_id=None) -> str:
    taken = {n["name"] for n in db.nodes.find(
        {"space": space_id, "parent": parent, "trashed_at": None, **({"_id": {"$ne": exclude_id}} if exclude_id else {})},
        {"name": 1})}
    if name not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    i = 1
    while f"{stem} ({i}){ext}" in taken:
        i += 1
    return f"{stem} ({i}){ext}"


def check_parent(space_id: str, parent_id: str | None):
    """Validate a folder id belongs to the space; returns its ObjectId (or None for root)."""
    if not parent_id or parent_id == "root":
        return None
    p = db.nodes.find_one({"_id": db.oid(parent_id), "space": space_id, "type": "folder", "trashed_at": None})
    if not p:
        raise HTTPException(404, "Folder not found")
    return p["_id"]


def ser_node(n: dict, user: dict, shared_ids: set | None = None) -> dict:
    return {
        "id": str(n["_id"]), "space": n["space"], "parent": str(n["parent"]) if n.get("parent") else None,
        "type": n["type"], "name": n["name"], "size": n.get("size", 0), "mime": n.get("mime"),
        "owner": n.get("owner"), "created_at": db.iso(n.get("created_at")), "updated_at": db.iso(n.get("updated_at")),
        "trashed_at": db.iso(n.get("trashed_at")), "starred": user["username"] in n.get("starred_by", []),
        "shared": (n["_id"] in shared_ids) if shared_ids is not None else False,
    }


def ser_nodes(nodes: list[dict], user: dict) -> list[dict]:
    ids = [n["_id"] for n in nodes]
    shared = {s["node"] for s in db.shares.find({"node": {"$in": ids}, "revoked": False}, {"node": 1})} if ids else set()
    return [ser_node(n, user, shared) for n in nodes]


def subtree(root_ids: list) -> list[dict]:
    """All nodes at or below the given roots (breadth first)."""
    out, queue = [], deque(root_ids)
    seen = set()
    while queue:
        batch = []
        while queue and len(batch) < 200:
            batch.append(queue.popleft())
        for n in db.nodes.find({"$or": [{"_id": {"$in": batch}}, {"parent": {"$in": batch}}]}):
            if n["_id"] in seen:
                continue
            seen.add(n["_id"])
            out.append(n)
            if n["type"] == "folder" and n["_id"] not in batch:
                queue.append(n["_id"])
    return out


def ancestors(node: dict, limit: int = 64) -> list[dict]:
    chain, cur = [], node
    while cur and cur.get("parent") and len(chain) < limit:
        cur = db.nodes.find_one({"_id": cur["parent"]})
        if cur:
            chain.append(cur)
    return list(reversed(chain))


def is_inside(node: dict, root_id) -> bool:
    cur, hops = node, 0
    while cur and hops < 64:
        if cur["_id"] == root_id:
            return True
        cur = db.nodes.find_one({"_id": cur["parent"]}) if cur.get("parent") else None
        hops += 1
    return False


def add_file_node(space_id: str, parent, name: str, owner: str, blob: dict, mime: str) -> dict:
    doc = {
        "space": space_id, "parent": parent, "type": "file", "name": unique_name(space_id, parent, name),
        "owner": owner, "size": blob["size"], "mime": mime or "application/octet-stream", "blob": blob["_id"],
        "created_at": db.now(), "updated_at": db.now(), "trashed_at": None, "starred_by": [],
    }
    db.nodes.insert_one(doc)
    db.users.update_one({"username": owner}, {"$inc": {"used_bytes": blob["size"]}})
    return doc


def add_folder_node(space_id: str, parent, name: str, owner: str) -> dict:
    doc = {
        "space": space_id, "parent": parent, "type": "folder", "name": unique_name(space_id, parent, name),
        "owner": owner, "created_at": db.now(), "updated_at": db.now(), "trashed_at": None, "starred_by": [],
    }
    db.nodes.insert_one(doc)
    return doc


def purge(root_ids: list) -> None:
    """Permanently delete nodes (and everything below them), their blobs and share links."""
    nodes = subtree(root_ids)
    if not nodes:
        return
    ids = [n["_id"] for n in nodes]
    per_owner: dict[str, int] = {}
    blob_ids = []
    for n in nodes:
        if n["type"] == "file":
            blob_ids.append(n["blob"])
            per_owner[n["owner"]] = per_owner.get(n["owner"], 0) + n.get("size", 0)
    db.shares.delete_many({"node": {"$in": ids}})
    db.nodes.delete_many({"_id": {"$in": ids}})
    storage.destroy_blobs(blob_ids)
    for owner, n in per_owner.items():
        db.users.update_one({"username": owner}, {"$inc": {"used_bytes": -n}})


def check_quota(user: dict, extra: int) -> None:
    quota = user.get("quota_bytes")
    quota = config.DEFAULT_QUOTA_BYTES if quota is None else quota
    if quota and user.get("used_bytes", 0) + extra > quota:
        raise HTTPException(413, "Storage quota exceeded")


def refresh_user(username: str) -> dict:
    return db.users.find_one_and_update({"username": username}, {"$set": {}}, return_document=ReturnDocument.AFTER)
