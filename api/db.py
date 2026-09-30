import os
import secrets
import uuid
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

files.create_index([("sha256", 1)], unique=True, name="hash")
files.create_index([("repo", 1)], name="repo_idx")
users.create_index([("username", 1)], unique=True, name="username")
sessions.create_index([("token", 1)], unique=True, name="session_token")
shares.create_index([("token", 1)], unique=True, name="share_token")
tickets.create_index([("expires_at", 1)], name="ticket_expiry")


def _serialize(doc: dict) -> dict:
    # mongo wants to hide the id field, so it uses the '_' before it, but i wanna access it through the api
    # thus converted to string
    doc["id"] = str(doc.pop("_id"))
    # raw py datetime objects are not JSON-friendly, so converting them to strings
    for key, val in list(doc.items()):
        if isinstance(val, datetime):
            doc[key] = val.isoformat()
    return doc


def get_active_repo() -> tuple[str, bool]:
    repo_doc = repos.find_one(sort=[("created_at", DESCENDING)])
    if repo_doc and repo_doc["total_bytes"] < REPO_SIZE_LIMIT:
        return repo_doc["name"], False

    count = repos.count_documents({})
    new_name = f"dotstore-{count + 1}"
    repos.insert_one({
        "name": new_name,
        "total_bytes": 0,
        "created_at": datetime.now(timezone.utc),
    })
    return new_name, True


def increment_repo_size(repo: str, size_bytes: int) -> None:
    repos.update_one({"name": repo}, {"$inc": {"total_bytes": size_bytes}})


def create_user(username: str, password_hash: str) -> dict:
    """The encryption UUID is intentionally never returned by this module's public serializers."""
    doc = {
        "username": username,
        "password_hash": password_hash,
        "encryption_id": str(uuid.uuid4()),
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


def get_repo_stats() -> list[dict]:
    cursor = repos.find({}).sort("created_at", 1)
    return [_serialize(doc) for doc in cursor]


def upsert_file(
    *,
    sha256: str,
    original_filename: str,
    repo: str,
    relative_path: str = "",
    mime_type: str,
    size_bytes: int,
    is_chunked: bool = False,
    chunks: list[dict],
    owner: dict,
    visibility: str,
) -> dict:
    doc = {
        "sha256": sha256,
        "original_filename": original_filename,
        "relative_path": relative_path,
        "repo": repo,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "is_chunked": is_chunked,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "owner_id": str(owner["_id"]),
        "owner_username": owner["username"],
        "visibility": visibility,
        "shared_with": [],
        "uploaded_at": datetime.now(timezone.utc),
    }
    result = files.find_one_and_update(
        {"sha256": sha256},
        {"$set": doc}, # this thing is crazy, it only updates the changes, instead of rewriting it!
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return _serialize(result)


def find_by_sha256(sha256: str) -> dict | None:
    doc = files.find_one({"sha256": sha256})
    return _serialize(doc) if doc else None


def list_visible_files(user: dict) -> list[dict]:
    cursor = files.find({
        "$or": [
            {"owner_id": str(user["_id"])},
            {"visibility": "public"},
            {"shared_with": user["username"]},
        ]
    }).sort("uploaded_at", DESCENDING)
    return [_serialize(doc) for doc in cursor]


def add_collaborator(sha256: str, username: str) -> dict | None:
    doc = files.find_one_and_update(
        {"sha256": sha256}, {"$addToSet": {"shared_with": username}},
        return_document=ReturnDocument.AFTER,
    )
    return _serialize(doc) if doc else None


def create_share(file_sha256: str, visibility: str) -> str:
    token = secrets.token_urlsafe(24)
    shares.insert_one({
        "token": token,
        "file_sha256": file_sha256,
        "visibility": visibility,
        "created_at": datetime.now(timezone.utc),
    })
    return token


def find_share(token: str) -> dict | None:
    return shares.find_one({"token": token})

# link ticketing system, so no more link copy and user binding issues
def create_download_ticket(file_sha256: str, share_token: str) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=TICKET_TTL_SECONDS)
    tickets.insert_one({
        "token": token,
        "file_sha256": file_sha256,
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
