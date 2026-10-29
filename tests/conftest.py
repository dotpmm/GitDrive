"""Test doubles for MongoDB and GitHub so the suite can exercise the real api.main
end to end without touching a live database or anybody's repositories.

api.db is replaced before api.main is imported, because the real module opens indexes at
import time. The fake mirrors its signatures; test_schema.py asserts they still match.
"""

import os
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# api.main reads these at import time. Note that GITHUB_TOKEN is set to a throwaway value:
# api.crypto must never depend on it, and tests/test_gitdrive.py asserts exactly that.
os.environ.setdefault("GITHUB_TOKEN", "ghp_test_token_that_must_never_be_a_key")
os.environ["GITDRIVE_MASTER_KEY"] = "a" * 64
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("DATABASE_NAME", "gitdrive_test")
os.environ.setdefault("GITHUB_OWNER", "test-owner")
os.environ.setdefault("LOG_MODE", "debug")
# api.main reads the CORS allowlist at import time, so pin a browser origin for tests.
os.environ.setdefault("FRONTEND_ORIGINS", "https://frontend.test")


class FakeGitHub:
    """An in-memory stand-in for the GitHub contents API."""

    def __init__(self) -> None:
        self.blobs: dict[str, dict[str, bytes]] = {}
        self.repos: list[str] = []
        self.commit_messages: list[str] = []

    def create_repo(self, name: str) -> dict:
        if name not in self.blobs:
            self.repos.append(name)
            self.blobs[name] = {}
        return {"name": name}

    def upload_file(self, repo: str, path: str, content: bytes,
                    filename: str | None = None, username: str | None = None) -> dict:
        self.blobs.setdefault(repo, {})[path] = content
        if filename:
            self.commit_messages.append(filename)
        return {"content": {"path": path}}

    def download_file(self, repo: str, path: str) -> bytes:
        try:
            return self.blobs[repo][path]
        except KeyError as exc:
            raise RuntimeError(f"object is missing from storage: {path}") from exc

    def objects(self, repo: str) -> dict[str, bytes]:
        return {p: b for p, b in self.blobs[repo].items() if p.startswith("objects/")}

    def manifests(self, repo: str) -> dict[str, bytes]:
        return {p: b for p, b in self.blobs[repo].items() if p.startswith("manifests/")}


class FakeDB:
    """Mirrors api.db's public surface with plain dicts."""

    TICKET_TTL_SECONDS = 600

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.files: dict[str, dict] = {}
        self.shares: dict[str, dict] = {}
        self.tickets: dict[str, dict] = {}
        self.repos: dict[str, dict] = {}

    # --- users ------------------------------------------------------------
    def create_user(self, username: str, verifier_hash: str, kdf_salt: str) -> dict:
        if username in self.users:
            raise RuntimeError("duplicate username")
        self.users[username] = {
            "_id": uuid.uuid4().hex, "username": username,
            "verifier_hash": verifier_hash, "kdf_salt": kdf_salt,
            "created_at": datetime.now(timezone.utc),
        }
        return {"username": username}

    def find_user(self, username: str) -> dict | None:
        return self.users.get(username)

    # --- sessions ---------------------------------------------------------
    def create_session(self, user: dict) -> str:
        token = uuid.uuid4().hex
        self.sessions[token] = {"token": token, "user_id": user["_id"]}
        return token

    def find_session_user(self, token: str) -> dict | None:
        s = self.sessions.get(token)
        if not s:
            return None
        return next((u for u in self.users.values() if u["_id"] == s["user_id"]), None)

    def delete_session(self, token: str) -> None:
        self.sessions.pop(token, None)

    # --- repos ------------------------------------------------------------
    def get_active_repo(self) -> tuple[str, bool]:
        if not self.repos:
            name = f"dotstore-{len(self.repos) + 1}"
            self.repos[name] = {"name": name, "total_bytes": 0}
            return name, True
        return next(iter(self.repos)), False

    def increment_repo_size(self, repo: str, size_bytes: int) -> None:
        self.repos.setdefault(repo, {"name": repo, "total_bytes": 0})
        self.repos[repo]["total_bytes"] += size_bytes

    # --- files ------------------------------------------------------------
    def upsert_file(self, **kwargs) -> dict:
        doc = dict(kwargs)
        doc["owner_id"] = str(kwargs["owner"]["_id"])
        doc["owner_username"] = kwargs["owner"]["username"]
        doc["uploaded_at"] = datetime.now(timezone.utc)
        doc["chunk_count"] = len(doc["chunks"])
        self.files[doc["file_id"]] = doc
        return dict(doc)

    def find_by_file_id(self, file_id: str) -> dict | None:
        doc = self.files.get(file_id)
        return dict(doc) if doc else None

    def mark_deleted(self, file_id: str) -> None:
        if file_id in self.files:
            self.files[file_id]["deleted_at"] = datetime.now(timezone.utc)

    def restore_file(self, file_id: str) -> None:
        if file_id in self.files:
            self.files[file_id]["deleted_at"] = None

    def purge_file(self, file_id: str) -> dict | None:
        return self.files.pop(file_id, None)

    def update_file(self, file_id: str, fields: dict) -> dict | None:
        if file_id in self.files:
            self.files[file_id].update(fields or {})
        doc = self.files.get(file_id)
        return dict(doc) if doc else None
        if file_id in self.files:
            self.files[file_id]["deleted_at"] = datetime.now(timezone.utc)

    def restore_file(self, file_id: str) -> None:
        if file_id in self.files:
            self.files[file_id]["deleted_at"] = None

    def purge_file(self, file_id: str) -> dict | None:
        return self.files.pop(file_id, None)

    def update_file(self, file_id: str, fields: dict) -> dict | None:
        if file_id in self.files:
            self.files[file_id].update(fields or {})
        return self.files.get(file_id) and dict(self.files[file_id])

    def list_visible_files(self, user: dict) -> list[dict]:
        return [dict(f) for f in self.files.values()
                if f["owner_id"] == str(user["_id"]) or f["visibility"] == "public"]

    # --- shares -----------------------------------------------------------
    def create_share(self, file_id: str, visibility: str, expires_in_seconds=None) -> str:
        token = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=expires_in_seconds) if expires_in_seconds else None
        self.shares[token] = {"token": token, "file_id": file_id, "visibility": visibility,
                              "created_at": now, "expires_at": expires, "revoked": False}
        return token

    def list_shares(self, user: dict) -> list[dict]:
        out = []
        for s in self.shares.values():
            # find file owner
            rec = None
            for f in self.files.values():
                if f.get("file_id") == s["file_id"]:
                    rec = f
            if not rec or str(rec.get("owner_id")) != str(user["_id"]):
                continue
            out.append({"token": s["token"], "file_id": s["file_id"], "mode": s.get("visibility"),
                        "created_at": s.get("created_at"), "expires_at": s.get("expires_at"),
                        "revoked": bool(s.get("revoked", False))})
        return out

    def revoke_share(self, token: str) -> None:
        if token in self.shares:
            self.shares[token]["revoked"] = True

    def user_usage(self, user: dict) -> dict:
        used = 0; count = 0
        for f in self.files.values():
            if str(f.get("owner_id")) != str(user["_id"]):
                continue
            if f.get("deleted_at"):
                continue
            used += int(f.get("size_bytes", 0))
            count += 1
        quota = int(os.getenv("DEFAULT_USER_QUOTA_BYTES", str(5*1024*1024*1024)))
        return {"used_bytes": used, "file_count": count, "quota_bytes": quota}

    def find_share(self, token: str) -> dict | None:
        return self.shares.get(token)

    def create_download_ticket(self, file_id: str, share_token: str) -> tuple[str, datetime]:
        token = uuid.uuid4().hex
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.TICKET_TTL_SECONDS)
        self.tickets[token] = {"token": token, "file_id": file_id,
                               "share_token": share_token, "expires_at": expires}
        return token, expires

    def find_download_ticket(self, token: str) -> dict | None:
        t = self.tickets.get(token)
        return t if t and t["expires_at"] > datetime.now(timezone.utc) else None

    def find_ticket_share(self, token: str) -> str | None:
        t = self.tickets.get(token)
        return t["share_token"] if t else None


DB_METHODS = ("create_user", "find_user", "create_session", "find_session_user",
              "delete_session", "get_active_repo", "increment_repo_size", "upsert_file",
              "find_by_file_id", "list_visible_files", "mark_deleted", "restore_file",
              "purge_file", "update_file", "create_share", "list_shares", "revoke_share",
              "find_share", "create_download_ticket", "find_download_ticket", "find_ticket_share",
              "user_usage")

_module = None


def _install() -> types.ModuleType:
    """Install a delegating fake as api.db, then import api.main for real."""
    import api  # noqa: F401

    module = types.ModuleType("api.db")
    module.TICKET_TTL_SECONDS = FakeDB.TICKET_TTL_SECONDS

    # Wrappers rather than bound methods, so every test can swap in a brand new FakeDB
    # without rebuilding the module or reloading api.main.
    for name in DB_METHODS:
        impl = getattr(FakeDB, name)
        def make(impl=impl, name=name):
            def wrapper(*args, **kwargs):
                return impl(module._fake, *args, **kwargs)
            wrapper.__name__ = name
            return wrapper
        setattr(module, name, make())

    module._fake = FakeDB()
    sys.modules["api.db"] = module
    setattr(api, "db", module)

    import api.main  # noqa: F401
    return module


def install() -> types.ModuleType:
    global _module
    if _module is None:
        _module = _install()
    return _module


def db():
    return install()._fake


def reset():
    """A clean database, a clean fake GitHub and a client with a live lifespan."""
    from fastapi.testclient import TestClient

    # install() must run before api.main is imported: the real api/db.py opens indexes at
    # import time and would block for the whole MongoDB server-selection timeout.
    module = install()
    import api.main as main

    module._fake = FakeDB()
    main._github = FakeGitHub()
    main._sessions.clear()

    client = TestClient(main.app)
    client.__enter__()      # runs lifespan: creates the queue and the upload workers
    client.github = main._github
    return client