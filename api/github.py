"""GitHub as an object store.

Uploads use the Git Data API: every encrypted chunk becomes a blob (can run in parallel),
then ONE tree+commit+ref update records all chunks of a file. That is much faster and far
less prone to 409 conflicts / secondary rate limits than one commit per chunk.

Reads from a public repo go through raw.githubusercontent.com (GitHub's CDN); the Git blob
API is the fallback (and the only path for private repos).
"""
import base64
import logging
import threading
import time

import httpx

from . import config

log = logging.getLogger("GitDrive")
BASE = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str, owner: str, private: bool, mask_commits: bool = True) -> None:
        self.owner = owner
        self.private = private
        self.mask_commits = mask_commits
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._http = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=15.0),
            limits=httpx.Limits(max_connections=48, max_keepalive_connections=24),
            headers=self._headers,
        )
        self._branches: dict[str, str] = {}
        self._commit_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- helpers
    def _request(self, method: str, url: str, *, ok=(200, 201), retries: int = 4, **kw) -> httpx.Response:
        last = ""
        for attempt in range(1, retries + 1):
            try:
                r = self._http.request(method, url, **kw)
            except httpx.HTTPError as exc:
                last = f"network error: {exc}"
                time.sleep(attempt)
                continue
            if r.status_code in ok:
                return r
            last = f"{r.status_code} {r.text[:200]}"
            retry_after = r.headers.get("retry-after")
            if r.status_code in (429, 502, 503, 504) or (r.status_code == 403 and ("rate limit" in r.text.lower() or retry_after)):
                time.sleep(min(30, float(retry_after) if retry_after else attempt * 2))
                continue
            break
        raise GitHubError(f"{method} {url.replace(BASE, '')} failed: {last}")

    def _commit_lock(self, repo: str) -> threading.Lock:
        with self._lock:
            return self._commit_locks.setdefault(repo, threading.Lock())

    def _branch(self, repo: str) -> str:
        if repo not in self._branches:
            r = self._request("GET", f"{BASE}/repos/{self.owner}/{repo}")
            self._branches[repo] = r.json().get("default_branch", "main")
        return self._branches[repo]

    # -------------------------------------------------------------------- repos
    def create_repo(self, name: str) -> None:
        r = self._http.post(f"{BASE}/user/repos", json={"name": name, "private": config.GITHUB_PRIVATE or self.private, "auto_init": True})
        if r.status_code not in (201, 422):  # 422 = already exists
            raise GitHubError(f"create_repo {name!r} failed: {r.status_code} {r.text[:200]}")

    # ------------------------------------------------------------------ writing
    def create_blob(self, repo: str, content: bytes) -> str:
        r = self._request(
            "POST", f"{BASE}/repos/{self.owner}/{repo}/git/blobs",
            json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
        )
        return r.json()["sha"]

    def commit_blobs(self, repo: str, entries: dict[str, str], message: str) -> None:
        """Add {path: blob_sha} files to the repo in a single commit."""
        if not entries:
            return
        tree_entries = [{"path": p, "mode": "100644", "type": "blob", "sha": s} for p, s in entries.items()]
        branch = self._branch(repo)
        root = f"{BASE}/repos/{self.owner}/{repo}"
        with self._commit_lock(repo):  # serialise commits per repo (single instance)
            last = ""
            for attempt in range(1, 6):
                try:
                    head = self._request("GET", f"{root}/git/ref/heads/{branch}").json()["object"]["sha"]
                    base_tree = self._request("GET", f"{root}/git/commits/{head}").json()["tree"]["sha"]
                    tree = self._request("POST", f"{root}/git/trees", json={"base_tree": base_tree, "tree": tree_entries}).json()["sha"]
                    commit = self._request("POST", f"{root}/git/commits", json={"message": message, "tree": tree, "parents": [head]}).json()["sha"]
                    self._request("PATCH", f"{root}/git/refs/heads/{branch}", json={"sha": commit})
                    return
                except GitHubError as exc:
                    last = str(exc)
                    time.sleep(attempt)
            raise GitHubError(f"commit failed after retries: {last}")

    # ------------------------------------------------------------------ reading
    def read_object(self, repo: str, path: str, blob_sha: str) -> bytes:
        if not (config.GITHUB_PRIVATE or self.private):
            try:
                r = self._http.get(f"{RAW}/{self.owner}/{repo}/{self._branch(repo)}/{path}", headers={"Authorization": ""})
                if r.status_code == 200:
                    return r.content
            except (httpx.HTTPError, GitHubError):
                pass
        r = self._request("GET", f"{BASE}/repos/{self.owner}/{repo}/git/blobs/{blob_sha}",
                          ok=(200,), headers={"Accept": "application/vnd.github.raw+json"})
        return r.content


def object_path(sha: str) -> str:
    return f"objects/{sha[:2]}/{sha[2:]}.chunk"
