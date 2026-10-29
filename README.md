# GitDrive

A high-performance ( ¯\\_(ツ)_/¯ , I mean it's fast), **cost-free** block (since GitHub doesn't allow you to upload files bigger than 25MB without warnings, we chunk them) **cloud storage system** utilizing FastAPI, MongoDB, and GitHub repositories as an **INFINITE** (lmao, I hope) backend storage provider. This project features chunk-based uploads, client-side parallel streaming downloads, and **two encryption spaces**: client-side zero-knowledge encryption for private files, and server-controlled encryption for public ones.

## Deployment Links

| Resource | Target URL |
| :--- | :--- |
| **Production API Endpoint** | `---` |
| **Frontend Web Interface** | `---` |

## Architectural Highlights

### 1. Producer-Consumer Pipeline (SOTA)
To prevent OOM on resource-constrained virtual machines (e.g., 2 GB RAM instances, or Render's stateless containers), the server employs a producer-consumer upload pipeline. Chunks uploaded by the browser are **immediately written to disk-backed temporary files** and placed into a **queue**. A pool of five worker coroutines drains the queue to upload chunks to GitHub in parallel. This guarantees that the RAM usage remains static at approximately 50 MB regardless of concurrent users or file sizes.

> This approach is better than the previous pipeline as it is a non-blocking system which allows the browser to upload at its maximum speed and, thanks to workers, now the uploads are 5x faster!

### 2. Folder Traversal & Browser Zipping 
When you drag and drop a folder, GitDrive traverses it recursively and appends the relative file path to the record's path metadata, mimicking real directory hierarchies just like Google Drive

> For public files this downloads sequentially, for private ones each chunk is decrypted in the browser on the fly, and `JSZip` compiles the folder into a `.zip` entirely on the client's machine.

### 3. Two Encryption Spaces

**Private space — zero-knowledge.** The password never reaches the server. Your browser runs Argon2id on it, derives a root key, generates a fresh random key for every file, and encrypts every chunk with AES-256-GCM before uploading. The server stores ciphertext and a sealed manifest; it cannot read either. Anyone holding your GitHub repository still cannot read your files without your password.

**Public space — server-controlled.** The browser uploads plaintext over the authenticated connection and the server encrypts it with AES-256-GCM under a dedicated `GITDRIVE_MASTER_KEY` before it ever reaches storage. Downloading the raw object from GitHub gives you nothing; decryption happens only through the GitDrive flow. The master key is independent of `GITHUB_TOKEN`, so rotating the token cannot break stored data.

#### Key hierarchy

```
private:
  password ──Argon2id(64 MiB, t=3)──▶ K_root
                                        ├─ HKDF("auth")  ─▶ K_auth  ─▶ login verifier (sent)
                                        └─ HKDF("kek", salt=file_id) ─▶ KEK
                                                                          ├─ unwraps ─▶ DEK (random per file)
                                                                          │              └─ HKDF("chunk/i/n") ─▶ per-chunk key
                                                                          └─ HKDF("meta") ─▶ encrypts filename, path, chunk list

public:
  GITDRIVE_MASTER_KEY ──HKDF("kek", salt=file_id)──▶ KEK ─▶ (same DEK / chunk-key structure)
```

Every chunk is sealed independently as `header ‖ ciphertext ‖ tag`, where the header carries the magic, mode, chunk index, chunk count, plaintext length and a fresh random 96-bit nonce. That whole header plus the file id is bound in as AES-GCM additional authenticated data, so a chunk cannot be moved between files, reordered, truncated, or swapped for another chunk without the open failing. Each chunk also gets its **own** key, so no AES-GCM key is ever used with two nonces.

#### Portable, server-independent decryption

Every file writes a small manifest to the repository at `manifests/<file_id>/manifest.json`. It contains the format version, algorithm identifiers, the Argon2id parameters and salt, the wrapped file key, and the sealed metadata — but no secrets and no plaintext. That is everything a future CLI needs:

```
gitdrive decrypt --repo dotstore-1 --file-id <id> --password
```

reads the manifest and the chunk objects straight from GitHub, re-derives the key with Argon2id, and reassembles the file. **The website and the API are not part of that trust path for private files.** `api/crypto.py` is the reference implementation and imports nothing from FastAPI, Mongo or GitHub for exactly this reason; `gitdrive-crypto.js` is the browser half of the same format, and `tests/js_vectors.js` proves the two agree byte for byte in both directions.

#### What this does and does not hide

- The server and anyone watching the repository learn the **number of chunks and their sizes** (they are visible in the blob tree regardless), the uploader and the timestamps. Padding would hide sizes but waste real bytes, so it is not done.
- The server does **not** learn filenames, folder paths, MIME types or content.
- A forgotten password means the private files are **unrecoverable**. That is the point; there is no recovery key and no admin override.
- The server stores a password-derived verifier, so a leaked database still permits offline guessing of a weak password. No scheme where a server authenticates a password avoids that; Argon2id at 64 MiB is what makes it expensive.

## API Endpoints Reference

The frontend team's full integration contract (every route, request/response shapes, the browser
crypto format, and the CORS/API-key rules) lives in [`FRONTEND_API_SPEC.md`](FRONTEND_API_SPEC.md).

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Returns the core single-page HTML frontend. |
| `GET` | `/gitdrive-crypto.js` | Shared browser crypto module used by both pages. |
| `GET` | `/auth/kdf?username=` | Argon2id parameters a browser needs before deriving anything. |
| `POST` | `/auth/register`, `/auth/login` | Creates an account or returns a bearer session. Both take a password-derived verifier, never a password. |
| `POST` | `/upload` | Accepts authenticated multipart chunk uploads. Private chunks arrive already encrypted; public chunks arrive as plaintext and are encrypted here. |
| `GET` | `/upload/status/{upload_id}` | Retrieves the sync percentage of the queue workers uploading to GitHub. |
| `GET` | `/download/{file_id}` | Streams a **public** file as plaintext. Private files are refused here. |
| `GET` | `/files/{file_id}/manifest` | Private space: the sealed manifest. |
| `GET` | `/files/{file_id}/chunk/{index}` | Private space: one encrypted chunk, verbatim. |
| `POST` | `/files/{file_id}/share` | Creates a public or access-controlled share link. |
| `GET` | `/share/{token}/download` | Public files download as a normal stream. |
| `GET` | `/files` | Lists files owned by or publicly visible to the signed-in user. |

## Self-Hosting Guide
1. Create a `.env` file in the root directory to store these values:

| Variable | Description | Default / Example |
| :--- | :--- | :--- |
| `GITDRIVE_MASTER_KEY` | **Required.** 64 hex characters encrypting the public space. Back it up: losing it makes every public file unreadable. Generate with `python -c "import secrets;print(secrets.token_hex(32))"` | `81f9f1bb...` |
| `MONGODB_URI` | MongoDB connection URI string | `mongodb+srv://` |
| `DATABASE_NAME` | Name of the MongoDB database | `gitdrive_db` |
| `GITHUB_TOKEN` | GitHub Personal Access Token (Classic) with full `repo` scope. Used **only** to talk to GitHub, never as an encryption key | `ghp_...` |
| `GITHUB_OWNER` | GitHub username that owns the target repos | `your-username` |
| `GITHUB_PRIVATE` | Whether newly created GitHub repos should be private | `false/true` |
| `MASK_COMMITS` | Commit message mode for GitHub uploads: `path8` uses the first 8 chars of the object path, `filename` uses the uploaded file name | `true (to mask) / false` |
| `LOG_MODE` | Application logging verbosity (`prod` or `debug`) | `prod` |
| `FRONTEND_ORIGINS` | Comma-separated browser origins allowed to call the API cross-origin (CORS). Empty = same-origin only. | `https://app.example.com` |
| `API_KEY` | Optional shared secret. When set, all API calls must send `X-API-Key`; served pages and share links stay open. | `a-long-random-string` |

2.  I recommend using [uv](https://github.com/astral-sh/uv), as it's insanely faster than pip, then:
```bash
    uv sync
    uv run uvicorn api.main:app --host 0.0.0.0 --port 10000
```

3.  Run the tests (they stub MongoDB and GitHub, so nothing is written anywhere):
```bash
    uv run python -m unittest discover -s tests -t tests
```
The browser/server agreement check additionally needs Node and a copy of hash-wasm; point `HASH_WASM` at `argon2.umd.min.js` to include it.

> Private mode needs a **secure context**: `crypto.subtle` and `hash-wasm` both require HTTPS (or `localhost`). Serving GitDrive over plain HTTP disables it.

---

### UI Features (as of now) :

- Stuff like `sharable links` and many more are a part of UI, which will be  documented soon!

### Known limitations :

- **Private files cannot have collaborators.** A collaborator holds a different password and therefore a different key, so there is no server-side way to grant access without weakening the model. Share a private file by sending the link *and* the password.
- **Password changes and password recovery are not implemented.** The key hierarchy (KEK wrapping a per-file DEK) was designed to make re-wrapping cheap rather than re-encrypting, but the endpoints do not exist yet. A forgotten password is unrecoverable by design.
- Whole files are buffered in the browser before saving, so very large downloads are bounded by available memory.

### Upcoming Features :
- delete feature (lmao, mb, it not implemented for now)
- maybe some enhanced features like better logging and setup experience
- many more , but after frontend launch
