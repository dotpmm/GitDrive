import os
import secrets
from datetime import datetime, timedelta, timezone
from pymongo import MongoClient, DESCENDING, ReturnDocument

from dotenv import load_dotenv

load_dotenv()

REPO_SIZE_LIMIT = 5 * 1024 * 1024 * 1024
# A browser cannot send an Authorization header on a plain link navigation, so an
# approved share visit trades its session for a short lived, file-scoped ticket.
TICKET_TTL_SECONDS = 600
# im still not sure abt this, they say
# u get a soft warning once hit the 5GB mark
# lets cap it at 5Gb for now

_client = MongoClient(os.getenv("MONGODB_URI"))
_db = _client[os.getenv("DATABASE_NAME")]
files = _db["files"]
repos = _db["repos"]
users = _db["users"]
sessions = _db["sessions"]
shares = _db["shares"]
tickets = _db["tickets"]

repos.create_index([("name", 1)], unique=True, name="repo_name")
files.create_index([("file_id", 1)], unique=True, name="file_id")
files.create_index([("repo", 1)], name="repo_idx")
files.create_index([("owner_id", 1)], name="owner_idx")
users.create_index([("username", 1)], unique=True, name="username")
sessions.create_index([("token", 1)], unique=True, name="session_token")
shares.create_index([("token", 1)], unique=True, name="share_token")
tickets.create_index([("expires_at", 1)], expireAfterSeconds=0, name="ticket_expiry_ttl")


def _serialize(doc: dict) -> dict:
    # mongo wants to hide the id field, so it uses the '_' before it, but i wanna access it through the api
    # thus converted to string
    doc["id"] = str(doc.pop("_id"))
    # raw py datetime objects are not JSON-friendly, so converting them to strings
    for key, val in list(doc.items()):
        if isinstance(val, datetime):
            doc[key] = val.isoformat()
    return doc


from pymongo.errors import DuplicateKeyError

def get_active_repo() -> tuple[str, bool]:
    repo_doc = repos.find_one(sort=[("created_at", DESCENDING)])
    if repo_doc and repo_doc["total_bytes"] < REPO_SIZE_LIMIT:
        return repo_doc["name"], False

    count = repos.count_documents({})
    new_name = f"dotstore-{count + 1}"
    try:
        repos.insert_one({
            "name": new_name,
            "total_bytes": 0,
            "created_at": datetime.now(timezone.utc),
        })
        return new_name, True
    except DuplicateKeyError:
        # race: someone else created it; re-read
        existing = repos.find_one({"name": new_name})
        if existing:
            return existing["name"], False
        # fallback
        repo_doc = repos.find_one(sort=[("created_at", DESCENDING)])
        return repo_doc["name"], False


def increment_repo_size(repo: str, size_bytes: int) -> None:
    repos.update_one({"name": repo}, {"$inc": {"total_bytes": size_bytes}})


def create_user(username: str, verifier_hash: str, kdf_salt: str) -> dict:
    # No password, no password hash and no encryption key material is stored here. The
    # browser derives K_root from the password and only ever sends a keyed verifier.
    doc = {
        "username": username,
        "verifier_hash": verifier_hash,
        "kdf_salt": kdf_salt,
        "created_at": datetime.now(timezone.utc),
    }
    users.insert_one(doc)
    return {"username": username}


def find_user(username: str) -> dict | None:
    return users.find_one({"username": username})


def create_session(user: dict) -> str:
    token = secrets.token_urlsafe(32)
    sessions.insert_one({
        "token": token,
        "user_id": user["_id"],
        "created_at": datetime.now(timezone.utc),
    })
    return token


def find_session_user(token: str) -> dict | None:
    session = sessions.find_one({"token": token})
    return users.find_one({"_id": session["user_id"]}) if session else None


def delete_session(token: str) -> None:
    sessions.delete_one({"token": token})


def _public_user(user: dict) -> dict:
    return {"id": str(user["_id"]), "username": user["username"]}


def upsert_file(
    *,
    file_id: str,
    mode: str,
    repo: str,
    size_bytes: int,
    is_chunked: bool = False,
    chunks: list[dict],
    manifest: dict,
    original_filename: str,
    relative_path: str = "",
    mime_type: str,
    owner: dict,
    visibility: str,
) -> dict:
    doc = {
        "file_id": file_id,
        "mode": mode,
        "repo": repo,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "is_chunked": is_chunked,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "manifest": manifest,
        # Blank for private files: the filename only exists inside the sealed manifest.
        "original_filename": original_filename,
        "relative_path": relative_path,
        "owner_id": str(owner["_id"]),
        "owner_username": owner["username"],
        "visibility": visibility,
        "uploaded_at": datetime.now(timezone.utc),
        "deleted_at": None,
    }
    result = files.find_one_and_update(
        {"file_id": file_id},
        {"$set": doc}, # this thing is crazy, it only updates the changes, instead of rewriting it!
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return _serialize(result)


def find_by_file_id(file_id: str) -> dict | None:
    doc = files.find_one({"file_id": file_id})
    return _serialize(doc) if doc else None


def mark_deleted(file_id: str) -> None:
    files.update_one({"file_id": file_id}, {"$set": {"deleted_at": datetime.now(timezone.utc)}})


def restore_file(file_id: str) -> None:
    files.update_one({"file_id": file_id}, {"$unset": {"deleted_at": ""}})


def purge_file(file_id: str) -> dict | None:
    doc = files.find_one_and_delete({"file_id": file_id})
    return _serialize(doc) if doc else None


def update_file(file_id: str, fields: dict) -> dict | None:
    files.update_one({"file_id": file_id}, {"$set": fields})
    return find_by_file_id(file_id)


def list_visible_files(user: dict) -> list[dict]:
    cursor = files.find({
        "$or": [
            {"owner_id": str(user["_id"])},
            {"visibility": "public"},
        ]
    }).sort("uploaded_at", DESCENDING)
    return [_serialize(doc) for doc in cursor]


def create_share(file_id: str, visibility: str, expires_in_seconds: int | None = None) -> str:
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_in_seconds) if expires_in_seconds else None
    shares.insert_one({
        "token": token,
        "file_id": file_id,
        "visibility": visibility,
        "created_at": now,
        "expires_at": expires_at,
        "revoked": False,
    })
    return token


def list_shares(user: dict) -> list[dict]:
    out = []
    for s in shares.find({}):
        rec = files.find_one({"file_id": s["file_id"]})
        if not rec or str(rec.get("owner_id")) != str(user["_id"]):
            continue
        out.append({
            "token": s["token"],
            "file_id": s["file_id"],
            "mode": s.get("visibility"),
            "created_at": s["created_at"].isoformat() if isinstance(s.get("created_at"), datetime) else s.get("created_at"),
            "expires_at": s["expires_at"].isoformat() if isinstance(s.get("expires_at"), datetime) else s.get("expires_at"),
            "revoked": bool(s.get("revoked", False)),
        })
    return out


def revoke_share(token: str) -> None:
    shares.update_one({"token": token}, {"$set": {"revoked": True}})


def find_share(token: str) -> dict | None:
    return shares.find_one({"token": token})


def user_usage(user: dict) -> dict:
    used = 0
    count = 0
    for f in files.find({"owner_id": str(user["_id"])}):
        if f.get("deleted_at"):
            continue
        used += int(f.get("size_bytes", 0))
        count += 1
    quota = int(os.getenv("DEFAULT_USER_QUOTA_BYTES", str(5 * 1024 * 1024 * 1024)))
    return {"used_bytes": used, "file_count": count, "quota_bytes": quota}


# link ticketing system, so no more link copy and user binding issues
def create_download_ticket(file_id: str, share_token: str) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=TICKET_TTL_SECONDS)
    tickets.insert_one({
        "token": token,
        "file_id": file_id,
        "share_token": share_token,
        "expires_at": expires_at,
        "created_at": datetime.now(timezone.utc),
    })
    return token, expires_at


def find_download_ticket(token: str) -> dict | None:
    return tickets.find_one({"token": token, "expires_at": {"$gt": datetime.now(timezone.utc)}})


def find_ticket_share(token: str) -> str | None:
    doc = tickets.find_one({"token": token})
    return doc["share_token"] if doc else None
