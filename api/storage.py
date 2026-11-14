"""Chunked, encrypted blob storage on top of GitHub.

A *blob* is one stored file: a list of <=10 MB chunks, each encrypted with the blob's own
data key. Uploads accept chunks in any order / in parallel; downloads fetch and decrypt
chunks in parallel (prefetch) and support HTTP Range.
"""
import hashlib
import io
import threading
import time
import uuid
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Iterator

from bson import Binary, ObjectId

from . import config, db, security
from .github import GitHubClient, object_path

github = GitHubClient(config.GITHUB_TOKEN, config.GITHUB_OWNER, config.GITHUB_PRIVATE)
_blob_slots = threading.BoundedSemaphore(config.UPLOAD_BLOB_CONCURRENCY)
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="dl")


class StorageError(RuntimeError):
    pass


def chunk_count(size: int) -> int:
    return -(-size // config.CHUNK_SIZE)


# ------------------------------------------------------------------- uploading
class UploadSession:
    def __init__(self, owner: str, name: str, size: int, filename_for_commit: str | None = None) -> None:
        self.id = uuid.uuid4().hex
        self.owner = owner
        self.name = name
        self.size = size
        self.total = chunk_count(size)
        self.blob_id = str(ObjectId())
        self.key = security.new_data_key()
        self.chunks: dict[int, dict] = {}
        self.created = time.time()
        self.finished = False
        self.lock = threading.Lock()
        self.commit_name = filename_for_commit
        self.repo, is_new = db.get_active_repo()
        if is_new:
            github.create_repo(self.repo)
        db.add_repo_bytes(self.repo, size)  # reserve space so parallel uploads can't overshoot
        self.meta: dict = {}  # free slot for the caller (space id, parent, ...)

    def expected_len(self, index: int) -> int:
        return min(config.CHUNK_SIZE, self.size - index * config.CHUNK_SIZE)

    def put_chunk(self, index: int, data: bytes) -> None:
        if not 0 <= index < self.total:
            raise StorageError("chunk index out of range")
        if len(data) != self.expected_len(index):
            raise StorageError(f"chunk {index} has {len(data)} bytes, expected {self.expected_len(index)}")
        if index in self.chunks:
            return  # retry of a chunk we already stored
        enc = security.encrypt_chunk(self.key, self.blob_id, index, data)
        sha = hashlib.sha256(enc).hexdigest()
        with _blob_slots:
            gh_sha = github.create_blob(self.repo, enc)
        with self.lock:
            self.chunks[index] = {"i": index, "sha": sha, "gh": gh_sha, "size": len(data)}

    def complete(self) -> dict:
        """Commit all chunks in one git commit and return the blob document."""
        with self.lock:
            if self.finished:
                raise StorageError("upload already finished")
            if len(self.chunks) != self.total:
                raise StorageError(f"missing chunks: have {len(self.chunks)} of {self.total}")
            chunks = [self.chunks[i] for i in range(self.total)]
        if chunks:
            entries = {object_path(c["sha"]): c["gh"] for c in chunks}
            msg = (self.commit_name or "upload") if not config.MASK_COMMITS else chunks[0]["sha"][:8]
            github.commit_blobs(self.repo, entries, msg)
        doc = {
            "_id": ObjectId(self.blob_id), "repo": self.repo, "size": self.size, "chunk_size": config.CHUNK_SIZE,
            "chunks": chunks, "key": Binary(security.wrap_key(self.key)), "created_at": db.now(),
        }
        db.blobs.insert_one(doc)
        self.finished = True
        return doc

    def abort(self) -> None:
        if not self.finished:
            db.add_repo_bytes(self.repo, -self.size)
            self.finished = True


def store_chunks(owner: str, name: str, size: int, chunks: Iterator[bytes]) -> dict:
    """Store an already-streaming file (used by unzip). Returns the blob doc."""
    sess = UploadSession(owner, name, size)
    try:
        for i, data in enumerate(chunks):
            sess.put_chunk(i, data)
        return sess.complete()
    except Exception:
        sess.abort()
        raise


# ----------------------------------------------------------------- downloading
_cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()
_cache_lock = threading.Lock()


def _read_chunk(blob: dict, index: int, key: bytes) -> bytes:
    ck = (str(blob["_id"]), index)
    with _cache_lock:
        if ck in _cache:
            _cache.move_to_end(ck)
            return _cache[ck]
    c = blob["chunks"][index]
    enc = github.read_object(blob["repo"], object_path(c["sha"]), c["gh"])
    if hashlib.sha256(enc).hexdigest() != c["sha"]:
        raise StorageError(f"chunk {index} failed integrity check")
    data = security.decrypt_chunk(key, str(blob["_id"]), index, enc)
    with _cache_lock:
        _cache[ck] = data
        while len(_cache) > config.CHUNK_CACHE_ITEMS:
            _cache.popitem(last=False)
    return data


def blob_key(blob: dict) -> bytes:
    return security.unwrap_key(bytes(blob["key"]))


def iter_range(blob: dict, start: int, end: int) -> Iterator[bytes]:
    """Yield decrypted bytes [start, end] (inclusive), prefetching chunks in parallel."""
    if blob["size"] == 0 or end < start:
        return
    key = blob_key(blob)
    cs = blob["chunk_size"]
    first, last = start // cs, end // cs
    pending: deque[tuple[int, Future]] = deque()
    nxt = first
    try:
        while nxt <= last or pending:
            while nxt <= last and len(pending) < config.DOWNLOAD_PREFETCH:
                pending.append((nxt, _pool.submit(_read_chunk, blob, nxt, key)))
                nxt += 1
            idx, fut = pending.popleft()
            data = fut.result()
            base = idx * cs
            lo = max(start, base) - base
            hi = min(end, base + len(data) - 1) - base
            yield data[lo:hi + 1]
    finally:
        for _, f in pending:
            f.cancel()


class BlobReader(io.RawIOBase):
    """Seekable read-only file object over a blob (lets zipfile read archives in place)."""

    def __init__(self, blob: dict) -> None:
        self._blob = blob
        self._key = blob_key(blob)
        self._pos = 0
        self._size = blob["size"]

    def readable(self) -> bool: return True
    def seekable(self) -> bool: return True
    def tell(self) -> int: return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def readinto(self, b) -> int:
        want = min(len(b), self._size - self._pos)
        if want <= 0:
            return 0
        cs = self._blob["chunk_size"]
        idx = self._pos // cs
        data = _read_chunk(self._blob, idx, self._key)
        off = self._pos - idx * cs
        piece = data[off:off + want]
        b[:len(piece)] = piece
        self._pos += len(piece)
        return len(piece)


# -------------------------------------------------------------------- deleting
def destroy_blobs(blob_ids: list[ObjectId]) -> int:
    """Cryptographic erase: dropping the wrapped data key makes the ciphertext on GitHub
    unreadable. Returns the number of bytes freed."""
    if not blob_ids:
        return 0
    freed: dict[str, int] = {}
    for b in db.blobs.find({"_id": {"$in": blob_ids}}, {"repo": 1, "size": 1}):
        freed[b["repo"]] = freed.get(b["repo"], 0) + b["size"]
    db.blobs.delete_many({"_id": {"$in": blob_ids}})
    for repo, n in freed.items():
        db.add_repo_bytes(repo, -n)
    return sum(freed.values())
