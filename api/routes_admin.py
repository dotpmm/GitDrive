from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import config, db
from .core import admin_user, purge
from .routes_auth import create_reset_link

router = APIRouter(prefix="/api/admin")


class UserPatch(BaseModel):
    disabled: bool | None = None
    quota_bytes: int | None = None
    clear_quota: bool = False


@router.get("/overview")
def overview(_: dict = Depends(admin_user)) -> dict:
    by_type = {t: db.spaces.count_documents({"type": t}) for t in ("public", "private", "shared")}
    agg = list(db.nodes.aggregate([{"$match": {"type": "file"}}, {"$group": {"_id": None, "n": {"$sum": 1}, "bytes": {"$sum": "$size"}}}]))
    files, total = (agg[0]["n"], agg[0]["bytes"]) if agg else (0, 0)
    return {
        "users": db.users.count_documents({}), "disabled_users": db.users.count_documents({"disabled": True}),
        "spaces": by_type, "files": files, "bytes": total,
        "folders": db.nodes.count_documents({"type": "folder"}), "shares": db.shares.count_documents({"revoked": False}),
        "repos": [{"name": r["name"], "bytes": r["total_bytes"], "limit": config.REPO_SIZE_LIMIT} for r in db.repos.find().sort("created_at", 1)],
    }


@router.get("/users")
def users(_: dict = Depends(admin_user)) -> list[dict]:
    usage = {r["_id"]: r for r in db.nodes.aggregate([
        {"$match": {"type": "file"}}, {"$group": {"_id": "$owner", "files": {"$sum": 1}, "bytes": {"$sum": "$size"}}}])}
    owned = {r["_id"]: r["n"] for r in db.spaces.aggregate([
        {"$match": {"type": "shared"}}, {"$group": {"_id": "$owner", "n": {"$sum": 1}}}])}
    member = {}
    for s in db.spaces.find({"type": "shared"}, {"members": 1}):
        for m in s.get("members", []):
            member[m["username"]] = member.get(m["username"], 0) + 1
    out = []
    for u in db.users.find().sort("created_at", 1):
        us = usage.get(u["username"], {})
        out.append({
            "username": u["username"], "email": u.get("email"), "created_at": db.iso(u.get("created_at")),
            "last_login_at": db.iso(u.get("last_login_at")), "disabled": bool(u.get("disabled")),
            "is_admin": u["username"] in config.ADMIN_USERNAMES, "files": us.get("files", 0), "bytes": us.get("bytes", 0),
            "spaces_owned": owned.get(u["username"], 0), "spaces_joined": member.get(u["username"], 0),
            "quota_bytes": u.get("quota_bytes"),
        })
    return out


@router.patch("/users/{username}")
def patch_user(username: str, body: UserPatch, me: dict = Depends(admin_user)) -> dict:
    u = db.users.find_one({"username": username})
    if not u:
        raise HTTPException(404, "No such user")
    upd: dict = {}
    if body.disabled is not None:
        if username == me["username"]:
            raise HTTPException(400, "You can't disable yourself")
        upd["disabled"] = body.disabled
    if body.clear_quota:
        db.users.update_one({"_id": u["_id"]}, {"$unset": {"quota_bytes": ""}})
    elif body.quota_bytes is not None:
        upd["quota_bytes"] = max(0, body.quota_bytes)
    if upd:
        db.users.update_one({"_id": u["_id"]}, {"$set": upd, **({"$inc": {"token_version": 1}} if upd.get("disabled") else {})})
    return {"ok": True}


@router.post("/users/{username}/reset-link")
def reset_link(username: str, _: dict = Depends(admin_user)) -> dict:
    if not db.users.find_one({"username": username}):
        raise HTTPException(404, "No such user")
    return {"link": create_reset_link(username)}


@router.delete("/users/{username}")
def delete_user(username: str, me: dict = Depends(admin_user)) -> dict:
    if username == me["username"] or username in config.ADMIN_USERNAMES:
        raise HTTPException(400, "Admins can't be deleted here")
    if not db.users.find_one({"username": username}):
        raise HTTPException(404, "No such user")
    purge([n["_id"] for n in db.nodes.find({"owner": username}, {"_id": 1})])
    for s in list(db.spaces.find({"owner": username})):
        purge([n["_id"] for n in db.nodes.find({"space": s["_id"], "parent": None}, {"_id": 1})])
        db.invites.delete_many({"space": s["_id"]})
        db.spaces.delete_one({"_id": s["_id"]})
    db.spaces.update_many({}, {"$pull": {"members": {"username": username}}})
    db.invites.delete_many({"$or": [{"to": username}, {"from": username}]})
    db.shares.update_many({"created_by": username}, {"$set": {"revoked": True}})
    db.resets.delete_many({"username": username})
    db.users.delete_one({"username": username})
    return {"ok": True}


@router.get("/spaces")
def spaces(_: dict = Depends(admin_user)) -> list[dict]:
    usage = {r["_id"]: r for r in db.nodes.aggregate([
        {"$match": {"type": "file"}}, {"$group": {"_id": "$space", "files": {"$sum": 1}, "bytes": {"$sum": "$size"}}}])}
    out = []
    for s in db.spaces.find().sort("created_at", 1):
        us = usage.get(s["_id"], {})
        out.append({"id": s["_id"], "name": s["name"], "type": s["type"], "owner": s.get("owner"),
                    "members": len(s.get("members", [])), "files": us.get("files", 0), "bytes": us.get("bytes", 0),
                    "created_at": db.iso(s.get("created_at"))})
    return out
