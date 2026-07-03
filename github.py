import base64
import httpx

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
                f"https://api.github.com/user/repos",
                headers=self._headers,
                json={"name": name, "private": False, "auto_init": True},
            )
        if r.status_code == 422:
            return 
        return r.json()

    def upload_file(self, repo: str, path: str, content: bytes) -> dict:
        encoded = base64.b64encode(content).decode()
        sha: str | None = None

        with httpx.Client() as client:
            existing = client.get(
                f"https://api.github.com/repos/dot-shad0w/{repo}/contents/{path}",
                headers=self._headers,
            )
            if existing.status_code == 200:
                sha = existing.json()["sha"]

            payload: dict = {"message": path[-8:], "content": encoded}
            if sha:
                payload["sha"] = sha

            r = client.put(
                f"https://api.github.com/repos/dot-shad0w/{repo}/contents/{path}",
                headers=self._headers,
                json=payload,
            )
        return r.json()

    def download_file(self, repo: str, path: str) -> bytes:
        with httpx.Client() as client:
            r = client.get(
                f"https://api.github.com/repos/dot-shad0w/{repo}/contents/{path}",
                headers={**self._headers, "Accept": "application/vnd.github.raw+json"},
            )
        return r.content
