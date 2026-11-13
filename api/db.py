"""MongoDB access + small shared helpers."""
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

from . import config

_client = MongoClient(config.MONGODB_URI)
_db = _client[config.DATABASE_NAME]

users = _db["users"]
spaces = _db["spaces"]
nodes = _db["nodes"]
blobs = _db["blobs"]
shares = _db["shares"]
invites = _db["invites"]
repos = _db["repos"]
resets = _db["resets"]

users.create_index("username", unique=True)
users.create_index("email", unique=True)
spaces.create_index("owner")
spaces.create_index("members.username")
nodes.create_index([("space", 1), ("parent", 1), ("trashed_at", 1)])
nodes.create_index("owner")
nodes.create_index("starred_by")
shares.create_index("token", unique=True)
shares.create_index("node")
invites.create_index([("to", 1), ("status", 1)])
invites.create_index([("space", 1), ("status", 1)])
resets.create_index("token_hash", unique=True)
resets.create_index("expires_at", expireAfterSeconds=0)
repos.create_index("name", unique=True)

PUBLIC_SPACE_ID = "public"


def now() -> datetime:
    return datetime.now(timezone.utc)


def oid(value: str | None) -> ObjectId | None:
    try:
        return ObjectId(value) if value else None
    except (InvalidId, TypeError):
        return None


def iso(v):
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.isoformat()
    return v


def ensure_public_space() -> None:
    spaces.update_one(
        {"_id": PUBLIC_SPACE_ID},
        {"$setOnInsert": {"name": "Public", "type": "public", "owner": None, "members": [], "created_at": now()}},
        upsert=True,
    )


def private_space_id(username: str) -> str:
    return f"private:{username}"


# ----------------------------------------------------------------- repo choice
def get_active_repo() -> tuple[str, bool]:
    """(repo_name, is_new). A new dotstore-N is started when the newest one is full."""
    doc = repos.find_one(sort=[("created_at", DESCENDING)])
    if doc and doc["total_bytes"] < config.REPO_SIZE_LIMIT:
        return doc["name"], False
    name = f"dotstore-{repos.count_documents({}) + 1}"
    try:
        repos.insert_one({"name": name, "total_bytes": 0, "created_at": now()})
    except DuplicateKeyError:
        doc = repos.find_one(sort=[("created_at", DESCENDING)])
        return doc["name"], False
    return name, True


def add_repo_bytes(repo: str, delta: int) -> None:
    repos.update_one({"name": repo}, {"$inc": {"total_bytes": delta}})
