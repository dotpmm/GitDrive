import os
from datetime import datetime, timezone
from pymongo import MongoClient, DESCENDING
from pymongo.collection import Collection

REPO_SIZE_LIMIT = 2 * 1024 * 1024 * 1024

_client: MongoClient = MongoClient(os.environ["MONGODB_URI"]) 
_db = _client[os.environ.get("DATABASE_NAME")]
files: Collection = _db["files"] 
repos: Collection = _db["repos"]

files.create_index([("sha256", 1)], unique=True, name="hash")
files.create_index([("repo", 1)], name="repo_idx")


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
    new_name = f".store-{count + 1}"
    repos.insert_one({
        "name": new_name,
        "total_bytes": 0,
        "created_at": datetime.now(timezone.utc),
    })
    return new_name, True


def increment_repo_size(repo: str, size_bytes: int) -> None:
    repos.update_one({"name": repo}, {"$inc": {"total_bytes": size_bytes}})


def get_repo_stats() -> list[dict]:
    cursor = repos.find({}).sort("created_at", 1)
    return [_serialize(doc) for doc in cursor]


def upsert_file(
    *,
    sha256: str,
    original_filename: str,
    repo: str,
    mime_type: str,
    size_bytes: int,
    is_chunked: bool = False,
    chunks: list[dict] | None = None,
) -> dict:
    doc = {
        "sha256": sha256,
        "original_filename": original_filename,
        "repo": repo,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "is_chunked": is_chunked,
        "chunk_count": len(chunks) if chunks else 0,
        "chunks": chunks or [],
        "uploaded_at": datetime.now(timezone.utc),
    }
    result = files.find_one_and_update(
        {"sha256": sha256},
        {"$set": doc}, # this thing is crazy, it only updates the changes, instead of rewriting it!
        upsert=True,
        return_document=True,
    )
    return _serialize(result)


def find_by_sha256(sha256: str) -> dict | None:
    doc = files.find_one({"sha256": sha256})
    return _serialize(doc) if doc else None


def list_all_files() -> list[dict]:
    cursor = files.find({}).sort("uploaded_at", DESCENDING)
    return [_serialize(doc) for doc in cursor]
