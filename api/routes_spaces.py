from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .core import accessible_space_ids, current_user, get_space, purge, space_role

router = APIRouter(prefix="/api")
MAX_SHARED_SPACES = 50


class SpaceIn(BaseModel):
    name: str


class InviteIn(BaseModel):
    username: str
    role: str = "editor"


class RoleIn(BaseModel):
    role: str


def _name(raw: str) -> str:
    n = raw.strip()
    if not 1 <= len(n) <= 60:
        raise HTTPException(400, "Space name must be 1-60 characters")
    return n


def _role(raw: str) -> str:
    if raw not in ("viewer", "editor"):
        raise HTTPException(400, "Role must be viewer or editor")
    return raw


def ser_space(s: dict, username: str) -> dict:
    return {
        "id": s["_id"], "name": s["name"], "type": s["type"], "owner": s.get("owner"),
        "role": space_role(s, username), "member_count": len(s.get("members", [])) + (1 if s.get("owner") else 0),
        "created_at": db.iso(s.get("created_at")),
    }


@router.get("/spaces")
def list_spaces(user: dict = Depends(current_user)) -> list[dict]:
    spaces = list(db.spaces.find({"_id": {"$in": accessible_space_ids(user)}}))
    order = {"private": 0, "public": 1, "shared": 2}
    spaces.sort(key=lambda s: (order[s["type"]], s["name"].lower()))
    return [ser_space(s, user["username"]) for s in spaces]


@router.post("/spaces")
def create_space(body: SpaceIn, user: dict = Depends(current_user)) -> dict:
    if db.spaces.count_documents({"owner": user["username"], "type": "shared"}) >= MAX_SHARED_SPACES:
        raise HTTPException(400, "Space limit reached")
    doc = {"name": _name(body.name), "type": "shared", "owner": user["username"], "members": [], "created_at": db.now()}
    res = db.spaces.insert_one({**doc, "_id": str(ObjectId())})
    return ser_space(db.spaces.find_one({"_id": res.inserted_id}), user["username"])


@router.patch("/spaces/{space_id}")
def rename_space(space_id: str, body: SpaceIn, user: dict = Depends(current_user)) -> dict:
    space, _ = get_space(space_id, user, "owner")
    if space["type"] != "shared":
        raise HTTPException(400, "Only shared spaces can be renamed")
    db.spaces.update_one({"_id": space_id}, {"$set": {"name": _name(body.name)}})
    return ser_space(db.spaces.find_one({"_id": space_id}), user["username"])


@router.delete("/spaces/{space_id}")
def delete_space(space_id: str, user: dict = Depends(current_user)) -> dict:
    space, _ = get_space(space_id, user, "owner")
    if space["type"] != "shared":
        raise HTTPException(400, "Only shared spaces can be deleted")
    roots = [n["_id"] for n in db.nodes.find({"space": space_id, "parent": None}, {"_id": 1})]
    purge(roots)
    db.invites.delete_many({"space": space_id})
    db.spaces.delete_one({"_id": space_id})
    return {"ok": True}


# --------------------------------------------------------------------- members
@router.get("/spaces/{space_id}/members")
def members(space_id: str, user: dict = Depends(current_user)) -> dict:
    space, role = get_space(space_id, user)
    if space["type"] != "shared":
        return {"owner": space.get("owner"), "members": [], "invites": [], "role": role}
    pend = [{"id": str(i["_id"]), "to": i["to"], "role": i["role"], "created_at": db.iso(i["created_at"])}
            for i in db.invites.find({"space": space_id, "status": "pending"})] if role == "owner" else []
    return {
        "owner": space["owner"], "role": role,
        "members": [{"username": m["username"], "role": m["role"]} for m in space.get("members", [])],
        "invites": pend,
    }


@router.post("/spaces/{space_id}/invites")
def invite(space_id: str, body: InviteIn, user: dict = Depends(current_user)) -> dict:
    space, _ = get_space(space_id, user, "owner")
    if space["type"] != "shared":
        raise HTTPException(400, "You can only invite people to shared spaces")
    to = body.username.strip().lower().lstrip("@")
    target = db.users.find_one({"username": to})
    if not target or target.get("disabled"):
        raise HTTPException(404, f"No user named {to!r}")
    if to == user["username"] or space_role(space, to):
        raise HTTPException(409, f"{to} is already in this space")
    if db.invites.find_one({"space": space_id, "to": to, "status": "pending"}):
        raise HTTPException(409, f"{to} already has a pending invite")
    doc = {"space": space_id, "space_name": space["name"], "from": user["username"], "to": to,
           "role": _role(body.role), "status": "pending", "created_at": db.now()}
    res = db.invites.insert_one(doc)
    return {"id": str(res.inserted_id), "to": to, "role": doc["role"]}


@router.delete("/spaces/{space_id}/invites/{invite_id}")
def revoke_invite(space_id: str, invite_id: str, user: dict = Depends(current_user)) -> dict:
    get_space(space_id, user, "owner")
    db.invites.delete_one({"_id": db.oid(invite_id), "space": space_id, "status": "pending"})
    return {"ok": True}


@router.patch("/spaces/{space_id}/members/{username}")
def set_role(space_id: str, username: str, body: RoleIn, user: dict = Depends(current_user)) -> dict:
    get_space(space_id, user, "owner")
    res = db.spaces.update_one({"_id": space_id, "members.username": username}, {"$set": {"members.$.role": _role(body.role)}})
    if not res.matched_count:
        raise HTTPException(404, "Member not found")
    return {"ok": True}


@router.delete("/spaces/{space_id}/members/{username}")
def remove_member(space_id: str, username: str, user: dict = Depends(current_user)) -> dict:
    space, role = get_space(space_id, user)
    if space["type"] != "shared":
        raise HTTPException(400, "Not a shared space")
    if username != user["username"] and role != "owner":
        raise HTTPException(403, "Only the owner can remove other people")
    if username == space["owner"]:
        raise HTTPException(400, "The owner can't leave; delete the space instead")
    db.spaces.update_one({"_id": space_id}, {"$pull": {"members": {"username": username}}})
    return {"ok": True}


# --------------------------------------------------------------------- invites
@router.get("/invites")
def my_invites(user: dict = Depends(current_user)) -> list[dict]:
    return [{"id": str(i["_id"]), "space": i["space"], "space_name": i["space_name"], "from": i["from"],
             "role": i["role"], "created_at": db.iso(i["created_at"])}
            for i in db.invites.find({"to": user["username"], "status": "pending"}).sort("created_at", -1)]


def _own_invite(invite_id: str, user: dict) -> dict:
    inv = db.invites.find_one({"_id": db.oid(invite_id), "to": user["username"], "status": "pending"})
    if not inv:
        raise HTTPException(404, "Invite not found")
    return inv


@router.post("/invites/{invite_id}/accept")
def accept(invite_id: str, user: dict = Depends(current_user)) -> dict:
    inv = _own_invite(invite_id, user)
    space = db.spaces.find_one({"_id": inv["space"]})
    if space and not space_role(space, user["username"]):
        db.spaces.update_one({"_id": space["_id"]}, {"$push": {"members": {
            "username": user["username"], "role": inv["role"], "added_at": db.now()}}})
    db.invites.update_one({"_id": inv["_id"]}, {"$set": {"status": "accepted"}})
    return {"ok": True, "space": inv["space"]}


@router.post("/invites/{invite_id}/decline")
def decline(invite_id: str, user: dict = Depends(current_user)) -> dict:
    inv = _own_invite(invite_id, user)
    db.invites.update_one({"_id": inv["_id"]}, {"$set": {"status": "declined"}})
    return {"ok": True}
