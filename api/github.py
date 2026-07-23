import base64
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.github.com"

class GitHubClient:
    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._owner = os.getenv("GITHUB_OWNER")
        self._private = os.getenv("GITHUB_PRIVATE", "false").lower() == "true"
        self._commit_name_mode = os.getenv("MASK_COMMITS", "false").lower()

    def _commit_message(self, path: str, filename: str | None = None, username: str | None = None) -> str:
        if username and filename:
            return f"{username} uploaded {filename}"
        if self._commit_name_mode == "false" and filename:
            return filename
        return path[11:19]
        #           ^^ the starting 8 chars were the same all the time cuz of 'object/ab' lmao

    def create_repo(self, name: str) -> dict:
        with httpx.Client() as client:
            r = client.post(
                f"{BASE}/user/repos",
                headers=self._headers,
                json={"name": name, "private": self._private, "auto_init": True},
            )
        if r.status_code == 422:
            with httpx.Client() as client:
                r = client.get(
                    f"{BASE}/repos/{self._owner}/{name}",
                    headers=self._headers,
                )
        return r.json()

    def upload_file(self, repo: str, path: str, content: bytes, filename: str | None = None, username: str | None = None) -> dict:
        encoded = base64.b64encode(content).decode()
        message = self._commit_message(path, filename, username)

        for _ in range(3):
            sha: str | None = None
            with httpx.Client() as client:
                existing = client.get(
                    f"{BASE}/repos/{self._owner}/{repo}/contents/{path}",
                    headers=self._headers,
                )
                if existing.status_code == 200:
                    sha = existing.json()["sha"]
                
                payload: dict = {"message": message, "content": encoded}
                if sha:
                    payload["sha"] = sha

                with httpx.Client() as client:
                    r = client.put(f"{BASE}/repos/{self._owner}/{repo}/contents/{path}", headers=self._headers, json=payload)

                if r.status_code in (200, 201):
                    return r.json()
                
                # happens when worker overruns over the other one
                # so it kinda treats like reupload 
                # now handled well!
                if r.status_code == 409:
                    continue

    def download_file(self, repo: str, path: str) -> bytes:
        with httpx.Client() as client:
            r = client.get(
                f"{BASE}/repos/{self._owner}/{repo}/contents/{path}",
                headers={**self._headers, "Accept": "application/vnd.github.raw+json"},
            )
        # ahh, that didnt do much, lemme add some debug statements
        # r.raise_for_status()
        # FIXEDD!!
        return r.content
