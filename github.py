import base64
import httpx

BASE = "https://api.github.com"
OWNER = "dot-shad0w"

class GitHubClient:
    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def create_repo(self, name: str) -> dict:
        with httpx.Client() as client:
            r = client.post(
                f"{BASE}/user/repos",
                headers=self._headers,
                json={"name": name, "private": True, "auto_init": True},
            )
        if r.status_code == 422:
            return 
        return r.json()

    def upload_file(self, repo: str, path: str, content: bytes) -> dict:
        encoded = base64.b64encode(content).decode()
        sha: str | None = None

        with httpx.Client() as client:
            existing = client.get(
                f"{BASE}/repos/{OWNER}/{repo}/contents/{path}",
                headers=self._headers,
            )
            if existing.status_code == 200:
                sha = existing.json()["sha"]

            payload: dict = {"message": f"{path}", "content": encoded}
            if sha:
                payload["sha"] = sha

            r = client.put(
                f"{BASE}/repos/{OWNER}/{repo}/contents/{path}",
                headers=self._headers,
                json=payload,
            )
        return r.json()

    def download_file(self, repo: str, path: str) -> bytes:
        with httpx.Client() as client:
            r = client.get(
                f"{BASE}/repos/{OWNER}/{repo}/contents/{path}",
                headers={**self._headers, "Accept": "application/vnd.github.raw+json"},
            )
        return r.content
