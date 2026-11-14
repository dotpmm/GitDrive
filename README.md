# GitDrive

A free, encrypted, Google-Drive-style cloud storage that keeps your files in GitHub repositories.

- **Accounts** with username + password (+ email for resets)
- **Spaces**: a *Private* drive per user, one *Public* space everybody can see, and any number of *Shared* spaces where you invite people by username
- **Folders, trash, starring, search, rename/move**, folder uploads, **zip download** of folders and **unzip** on the server
- **Share links** with a direct URL (`/s/<token>/<file>`) that streams with HTTP Range, so videos play and embed in Discord / Slack / Telegram without downloading
- **Fast transfers**: parallel chunk uploads, one git commit per file, parallel prefetched downloads over a pooled connection, reads from GitHub's CDN
- **Encryption**: every file has its own random AES-256-GCM key, wrapped with a master key (`ENCRYPTION_KEY`) stored in MongoDB. Chunks are bound to their file and position. Deleting a file destroys its key (cryptographic erase)
- **Admin panel**: users, storage per user, spaces, repositories

```
frontend/   React + TypeScript (Material 3 Expressive), deploy on Vercel
api/        FastAPI backend, deploy on Render (Dockerfile included)
tests/      end-to-end API tests (mongomock + fake GitHub)
```

## Self-hosting the API

1. Copy `.env.example` to `.env` and fill it in. Generate secrets with `python -c "import secrets;print(secrets.token_urlsafe(48))"`.
2. `uv sync && uv run uvicorn api.main:app --port 10000`
3. On Render: create a *Web Service* from this repo (Docker), add the same variables.

Free-tier notes: Render's free instances sleep after ~15 min idle (first request is slow - ping `/health` with a free uptime monitor), have 512 MB RAM and no persistent disk (nothing here needs one). Chunks are 10 MB and at most 4 are processed at once to stay inside that memory.

## API overview

| Area | Endpoints |
| :-- | :-- |
| Auth | `POST /api/auth/signup`, `/login`, `/forgot`, `/reset`, `GET /api/me`, `POST /api/me/password` |
| Spaces | `GET/POST /api/spaces`, `PATCH/DELETE /api/spaces/{id}`, members, `POST /api/spaces/{id}/invites`, `GET /api/invites`, `POST /api/invites/{id}/accept\|decline` |
| Files | `GET /api/spaces/{id}/nodes`, folders, `PATCH /api/nodes/{id}`, trash/restore/delete, `/api/recent`, `/api/starred`, `/api/search` |
| Upload | `POST /api/uploads` -> `PUT /api/uploads/{id}/chunks/{i}` (any order, parallel) -> `POST /api/uploads/{id}/complete` |
| Download | `POST /api/nodes/{id}/url` -> `GET /d/{id}?...` (Range supported), `POST /api/zip-url` |
| Sharing | `POST/DELETE /api/nodes/{id}/share`, public: `GET /s/{token}[/{name}]`, `GET /api/shared/{token}` |
| Zip | `POST /api/nodes/{id}/extract`, `GET /api/jobs/{id}` |
| Admin | `/api/admin/overview`, `/users`, `/spaces`, per-user disable / quota / reset link / delete |

## Tests

```
uv run pytest
```

## Honest limits

GitHub's terms are not meant for bulk file hosting; keep this for personal / friends use. Git history keeps old ciphertext forever, but deleted files are unreadable because their keys are destroyed. Back up `ENCRYPTION_KEY` - without it nothing can be decrypted.
