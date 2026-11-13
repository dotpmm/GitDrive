import base64
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.github.com"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
MAX_ATTEMPTS = 3


class GitHubError(RuntimeError):
    """Raised when a GitHub API call fails for good."""


class GitHubClient:
    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._owner = os.getenv("GITHUB_OWNER")
        self._private = os.getenv("GITHUB_PRIVATE", "false").lower() == "true"
        # MASK_COMMITS=true  -> commit message is a slice of the object path
        # anything else      -> commit message is the original filename
        self._mask_commits = os.getenv("MASK_COMMITS", "false").lower() == "true"

    def _commit_message(self, path: str, filename: str | None = None) -> str:
        if not self._mask_commits and filename:
            return filename
        # path looks like 'objects/ab/cdef....chunk'; the first 11 chars are
        # always the same ('objects/ab/'), so skip them.
        return path[11:19]

    def create_repo(self, name: str) -> dict:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(
                f"{BASE}/user/repos",
                headers=self._headers,
                json={"name": name, "private": self._private, "auto_init": True},
            )
            if r.status_code == 422:  # already exists
                r = client.get(f"{BASE}/repos/{self._owner}/{name}", headers=self._headers)
        if r.status_code not in (200, 201):
            raise GitHubError(f"create_repo {name!r} failed: {r.status_code} {r.text[:200]}")
        return r.json()

    def upload_file(self, repo: str, path: str, content: bytes, filename: str | None = None) -> dict:
        encoded = base64.b64encode(content).decode()
        message = self._commit_message(path, filename)
        url = f"{BASE}/repos/{self._owner}/{repo}/contents/{path}"
        last = "no attempt made"

        with httpx.Client(timeout=TIMEOUT) as client:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                payload: dict = {"message": message, "content": encoded}

                # if the object already exists GitHub needs its blob sha to overwrite it
                existing = client.get(url, headers=self._headers)
                if existing.status_code == 200:
                    payload["sha"] = existing.json()["sha"]

                r = client.put(url, headers=self._headers, json=payload)
                if r.status_code in (200, 201):
                    return r.json()

                last = f"{r.status_code} {r.text[:200]}"
                # 409: concurrent commit on the same branch; 5xx / 429: transient
                if r.status_code == 409 or r.status_code == 429 or r.status_code >= 500:
                    time.sleep(attempt)
                    continue
                break  # 4xx other than the above won't fix itself

        raise GitHubError(f"upload {path} failed: {last}")

    def download_file(self, repo: str, path: str) -> bytes:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(
                f"{BASE}/repos/{self._owner}/{repo}/contents/{path}",
                headers={**self._headers, "Accept": "application/vnd.github.raw+json"},
            )
        if r.status_code != 200:
            raise GitHubError(f"download {path} failed: {r.status_code} {r.text[:200]}")
        return r.content
