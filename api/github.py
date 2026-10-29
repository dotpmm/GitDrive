import base64
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.github.com"

# github is ass and occasionally resets TLS mid-transfer (the "bad record mac"
# failures, for which, ive not found any fix yet),
# so every call is bounded and retried instead of being handed to the caller as a dead connection.
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
MAX_ATTEMPTS = 3
RETRY_BACKOFF = 0.5
MAX_CONFLICT_RETRIES = 3

_client: httpx.Client | None = None


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _http() -> httpx.Client:
    # 1 pooled client for the whole process, building a client per call meant a
    # fresh TLS handshake for every chunk was not a good idea
    # and was the reason for some download failures 
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
    return _client


def _request(method: str, url: str, *, headers: dict, **kwargs) -> httpx.Response:
    last: Exception = GitHubError("no attempt made")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = _http().request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            last = exc
        else:
            if response.status_code < 500 and response.status_code != 429:
                return response
            last = GitHubError(f"GitHub returned HTTP {response.status_code}", response.status_code)
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_BACKOFF * attempt)
    raise last


class GitHubClient:
    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._owner = os.getenv("GITHUB_OWNER")
        self._private = os.getenv("GITHUB_PRIVATE", "false").lower() == "true"

    def _commit_message(self, path: str, filename: str | None = None, username: str | None = None) -> str:
        if username and filename:
            return f"{username} uploaded {filename}"
        return path[11:19]
        #           ^^ the starting 8 chars were the same all the time cuz of 'object/ab' lmao

    def create_repo(self, name: str) -> dict:
        r = _request("POST", f"{BASE}/user/repos", headers=self._headers,
                     json={"name": name, "private": self._private, "auto_init": True})
        if r.status_code == 422:
            r = _request("GET", f"{BASE}/repos/{self._owner}/{name}", headers=self._headers)
        return r.json()

    def upload_file(self, repo: str, path: str, content: bytes, filename: str | None = None, username: str | None = None) -> dict:
        encoded = base64.b64encode(content).decode()
        message = self._commit_message(path, filename, username)

        for _ in range(MAX_CONFLICT_RETRIES):
            existing = _request("GET", f"{BASE}/repos/{self._owner}/{repo}/contents/{path}",
                                headers=self._headers)
            sha: str | None = existing.json().get("sha") if existing.status_code == 200 else None

            payload: dict = {"message": message, "content": encoded}
            if sha:
                payload["sha"] = sha

            r = _request("PUT", f"{BASE}/repos/{self._owner}/{repo}/contents/{path}",
                         headers=self._headers, json=payload)

            if r.status_code in (200, 201):
                return r.json()

            # happens when worker overruns over the other one
            # so it kinda treats like reupload
            # now handled well!
            if r.status_code == 409:
                continue

            raise GitHubError(f"upload rejected (HTTP {r.status_code})", r.status_code)

        # falling through used to return None, so the worker counted a chunk as
        # stored while nothing existed in github: the file then 404'ed on download.
        raise GitHubError("upload kept conflicting with a concurrent write")

    def download_file(self, repo: str, path: str) -> bytes:
        r = _request("GET", f"{BASE}/repos/{self._owner}/{repo}/contents/{path}",
                     headers={**self._headers, "Accept": "application/vnd.github.raw+json"})
        if r.status_code == 404:
            raise GitHubError("object is missing from storage", 404)
        if r.status_code >= 400:
            raise GitHubError(f"GitHub refused the object (HTTP {r.status_code})", r.status_code)
        return r.content
