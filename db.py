import os
from datetime import datetime, timezone
from pymongo import MongoClient, DESCENDING
from pymongo.collection import Collection


_client: MongoClient = MongoClient(os.environ["MONGODB_URI"]) 
_db = _client[os.environ.get("DATABASE_NAME")]
files: Collection = _db["files"]  

files.create_index([("repo", 1), ("filename", 1)], unique=True)

def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc

def upsert_file(
    *, 
    filename: str,
    repo: str,
    mime_type: str,
    size_bytes: int,
    github_sha: str | None,
    is_chunked: bool = False,
    chunks: list[dict] | None = None,
) -> dict:
    doc = {
        "filename": filename,
        "repo": repo,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "github_sha": github_sha,
        "is_chunked": is_chunked,
        "chunk_count": len(chunks) if chunks else 0,
        "chunks": chunks or [],
        "uploaded_at": datetime.now(timezone.utc),
    }
    result = files.find_one_and_update(
        {"repo": repo, "filename": filename},
        {"$set": doc},
        upsert=True,
        return_document=True,
    )
    return _serialize(result)

def list_files(repo: str) -> list[dict]:
    cursor = files.find({"repo": repo}).sort("uploaded_at", DESCENDING)
    return [_serialize(doc) for doc in cursor]


def get_file(repo: str, filename: str) -> dict | None:
    doc = files.find_one({"repo": repo, "filename": filename})
    return _serialize(doc) if doc else None
