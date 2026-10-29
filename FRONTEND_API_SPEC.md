# GitDrive API — Frontend Integration Spec

**Audience:** the frontend team building the GitDrive web client.
**Scope:** every HTTP route the frontend may call, the request/response contracts, the browser
crypto contract, access restrictions (CORS + API key) and the behaviours the UI must handle.

GitDrive's backend (this repository) runs as an API; the frontend is a separate application that
talks to it over HTTPS. This document is the contract between the two.

> The current `index.html` in this repository is the reference client. Every flow below is taken
> from code that is exercised by the test suite (`tests/test_gitdrive.py`, `tests/test_smoke.py`).
> When in doubt, read `api/main.py`; it is the source of truth.

---

## 1. What the frontend must know first

1. **Everything is file-id based.** A file is identified by a `file_id`: 32 hex characters chosen
   randomly by the client at upload time. The server never derives it from content, and it is the
   only handle used in URLs.
2. **There are two spaces.**
   - **Public space** — the browser uploads plaintext (over HTTPS, with the session token); the
     server encrypts with its own master key. Downloads come back as plaintext from
     `GET /download/{file_id}`.
   - **Private space** — the browser encrypts everything (chunks and filename) before uploading.
     The server only ever stores and returns ciphertext. The backend **cannot** decrypt private
     files, and neither can the frontend without the user's password. The frontend therefore owns
     the private-space crypto; the shared implementation is served by the backend at
     `GET /gitdrive-crypto.js` and is the recommended dependency (see §6).
3. **Auth is two-layer.** A per-user session token (`Authorization: Bearer …`) proves identity, and
   a deployment-wide `X-API-Key` proves "this is the GitDrive frontend" (see §3.3). Both are
   required on most routes.
4. **Private crypto requires a secure context.** `crypto.subtle` and hash-wasm only run over HTTPS
   (or `localhost`). Plan the dev environment accordingly.

---

## 2. Conventions

- All API paths below are relative to the **API base URL**, e.g. `https://api.gitdrive.example`.
- JSON bodies use `Content-Type: application/json`; form bodies use `multipart/form-data`.
- Timestamps are ISO-8601 UTC strings, e.g. `"2026-01-31T12:00:00.123456+00:00"`.
- Errors are always `{"detail": "<human readable string>"}` with the HTTP status carrying the
  meaning. Do not parse `detail` programmatically except where noted.
- All hex values are lowercase; comparisons are byte-exact.

### Status codes

| Status | Meaning |
| :--- | :--- |
| `200` | Success. |
| `400` | Malformed request, bad geometry, wrong visibility, expired ticket navigation. |
| `401` | Missing/expired session token, wrong password (login), or missing `X-API-Key`. |
| `403` | Authenticated but not allowed (another user's upload, private file without access). |
| `404` | Unknown file, share link, upload session or chunk index. |
| `409` | Username already taken. |
| `502` | Storage read failed (GitHub). Retry later; not a client error. |

---

## 3. Connecting to the API

### 3.1 CORS (browser access control)

The backend only lets registered origins read API responses. The deployment sets
`FRONTEND_ORIGINS` (comma-separated exact origins, e.g. `https://app.gitdrive.example`).
With an empty list, only same-origin pages (the bundled UI) can call the API.

- Allowed methods: `GET`, `POST`, `OPTIONS`.
- Allowed request headers: `Authorization`, `Content-Type`, `X-API-Key`.
- Exposed response headers: `Content-Disposition`, `Content-Length`.
- Credentials (cookies) are **not** used: sessions are Bearer tokens, so calls must NOT set
  `credentials: 'include'`; the default `same-origin`/`omit` is correct.

If your dev origin is not allowlisted you will see a CORS error before the request even reaches
the API. Ask the backend owner to add the exact scheme+host+port you develop on.

### 3.2 Base URL configuration

The frontend needs one build-time value: the API base URL. All example paths here are relative to
it. Store it in the frontend's environment config (e.g. `VITE_API_URL`).

### 3.3 API key (`X-API-Key`)

When the deployment sets `API_KEY`, every request to the app's API routes must carry:

```
X-API-Key: <the shared key>
```

Rules:

- Missing or wrong key → `401 {"detail": "A valid X-API-Key header is required"}`.
- Requested with the **same CORS machinery**, so browsers send a preflight for it; the header is
  already allowlisted server-side.
- Exempt (no key needed): `GET /`, `GET /gitdrive-crypto.js`, everything under `/share/`
  (recipients open share links from arbitrary browsers), and
  `GET /files/{file_id}/manifest` / `GET /files/{file_id}/chunk/{n}` (guarded by a session or a
  short-lived ticket instead).
- The key is a deployment secret shared with the frontend team; put it in the frontend build env
  (e.g. `VITE_API_KEY`) and send it on every fetch. It is not a substitute for per-user auth.

### 3.4 Sessions

`POST /auth/register` and `POST /auth/login` return `{"token": "…", "username": "…"}`. Send the
token as `Authorization: Bearer <token>` on every authenticated call. Tokens are opaque; store
them where you would store a login session (localStorage in the reference client). **There is no
server-side expiry yet**; `POST /auth/logout` deletes the session immediately.

### 3.5 Uploads need XHR (or `fetch` with stream body)

`POST /upload` is used with `XMLHttpRequest` in the reference client purely for progress events.
`fetch` works too if per-chunk progress is not required.

---

## 4. Endpoint reference

### 4.1 `GET /` — bundled UI (optional)

Returns the reference SPA. Not needed by a separately built frontend; ignore it.

### 4.2 `GET /gitdrive-crypto.js` — shared crypto module

Returns the browser crypto module (`window.GDCrypto`, plain JavaScript, cacheable for 5 minutes).
Load it via `<script src="${API_BASE}/gitdrive-crypto.js" defer>` (or fetch + evaluate) and use it
for all private-space operations. See §6 for its exact API.

### 4.3 `GET /auth/kdf?username=<name>`

No auth required. Returns the Argon2id parameters the browser needs to derive keys for that user.

```json
{ "alg": "argon2id", "m_kib": 65536, "t": 3, "p": 1, "len": 32, "salt": "9f8c04…32 hex…" }
```

- Unknown users get a well-formed but useless salt (anti-enumeration); login simply fails later.
- Cache the response per username for the tab session; do not hardcode the params, read them.

### 4.4 `POST /auth/register`

`multipart/form-data` fields:

| Field | Type | Rules |
| :--- | :--- | :--- |
| `username` | string | 3–32 chars, letters/numbers/`-`/`_` |
| `verifier` | string | 64 hex chars, derived in the browser (§6.2) — never the password |
| `kdf_salt` | string | 32 hex chars, freshly random per account |

Flow: generate a 16-byte random salt, derive `K_root` with Argon2id, compute the verifier,
POST the form.

Response `200`: `{"token": "…", "username": "…"}`
Errors: `400` username/key-format violations, `409` username taken.

### 4.5 `POST /auth/login`

`multipart/form-data`: `username`, `verifier` (the same verifier formula; fetch `/auth/kdf`
first to get the account's salt/params).

Response `200`: `{"token": "…", "username": "…"}` — Errors: `401 Invalid username or password`.

### 4.6 `GET /auth/me`

Bearer required. Returns `{"username": "…"}` or `401` if the session is gone. Useful on app boot.

### 4.7 `POST /auth/logout`

Bearer optional (idempotent). Deletes the session server-side. Returns `{"status": "ok"}`.

---

### 4.8 `GET /files`

Bearer required. Returns the user's files plus every public file, newest first, as a JSON array.
Each item:

| Field | Meaning |
| :--- | :--- |
| `file_id` | 32-hex id. |
| `mode` / `visibility` | `"public"` or `"private"` (always equal today). |
| `mime_type` | Public: real MIME. Private: `"application/octet-stream"`. |
| `size_bytes` | Plaintext size in bytes. |
| `is_chunked`, `chunk_count` | Geometry. |
| `chunks` | `[{"index", "sha256", "size"}]` — blob hashes; for private these are integrity anchors. |
| `manifest` | The full manifest object (for private files it is sealed/opaque; needed for download). |
| `original_filename` | Public: real name. Private: `""` (the name lives inside the sealed metadata). |
| `relative_path` | Public: folder path prefix or `""`. Private: `""`. |
| `owner_id`, `owner_username` | Uploader. |
| `uploaded_at` | ISO timestamp. |
| `id`, `repo` | Internal; ignore in the UI. |

For private files the UI cannot render a filename until it decrypts the manifest metadata with the
user's `K_root` (§6.6). Remember that only *this* user's password can do that.

### 4.9 `POST /upload` — upload one chunk

Bearer required. `multipart/form-data`. One request per chunk; the upload is a session keyed by
`upload_id`, and the **first chunk binds** user, `file_id`, `visibility`, `total_chunks` and
`size_bytes`; any later chunk that disagrees is rejected (`400`/`403`).

Common fields (every chunk):

| Field | Type | Notes |
| :--- | :--- | :--- |
| `chunk` | file | The chunk body — filename part irrelevant. |
| `upload_id` | string | Client-chosen, unique per upload (UUID recommended). Not persisted by the server. |
| `chunk_index` | int | `0`-based, `0 <= index < total_chunks`. |
| `total_chunks` | int | `1 … 1024`. Always send at least one chunk (empty file ⇒ 1 empty chunk). |
| `file_id` | string | 32 hex chars, random per file (§6.4). |
| `size_bytes` | int | **Total plaintext file size**, sent with every chunk (0 … 10 GiB). |
| `visibility` | string | `"public"` or `"private"`. |

Public-space extra fields:

| Field | Type | Notes |
| :--- | :--- | :--- |
| `filename` | string | Original filename (goes into listings and commit messages). |
| `mime_type` | string | May be empty; server guesses from `filename`. |
| `relative_path` | string | Folder prefix for folder uploads, e.g. `"docs/reports/"` or `""`. |

Private-space extra fields:

| Field | Type | Notes |
| :--- | :--- | :--- |
| `blob_sha` | string | SHA-256 (64 hex) **of the blob exactly as uploaded**. |
| `plain_len` | int | Plaintext byte length of this chunk. |
| `manifest` | string | JSON string. **Only on the final chunk** (`chunk_index == total_chunks - 1`). |

Do **not** send `filename`, `mime_type` or `relative_path` for private chunks; the name travels
inside the sealed manifest metadata instead.

Recommended chunk size: **10 MiB (`10 * 1024 * 1024`)** — the same value the reference client and
the server's public-space path use. The server accepts any geometry whose totals match, but stick
to 10 MiB unless you have a specific reason. A private chunk body on the wire is
`plain_len + 45` bytes (29-byte header + 16-byte GCM tag).

Responses per chunk:

- Non-final chunk → `200 {"status": "received", "chunk": <n>, "percent": <received %>}`
- Final chunk → `200 {"status": "processing" | "done" | "duplicate", "upload_id": "…", "file_id": "…"}`
  - `processing`: background workers are still pushing chunks to storage — start polling status.
  - `done`: everything already settled synchronously.
  - `duplicate`: a record with this `file_id` already exists (client-side collision; treat as success
    for the file, or regenerate the `file_id` and re-upload if the user expects a new copy).

Selected errors: `400` for geometry/hash/magic/length violations (see §7), `403` if the upload
belongs to another user, `401` without session.

### 4.10 `GET /upload/status/{upload_id}`

Bearer required, same user only. Poll roughly every 1–2 s after the final chunk returns
`processing`.

- In progress → `200 {"status": "processing", "github_percent": 0–100}`
- Finished → `200 {"status": "done" | "duplicate", "github_percent": 100, "file_id": "…"}`
  *(the session is dropped after this response, so the next poll 404s — treat a `404` as
  "finished", exactly like the reference client does)*
- Failed → `200 {"status": "error", "github_percent": …, "error": "<message>"}`
- `404` → session gone: already finished.

---

### 4.11 `GET /download/{file_id}` — public download (plaintext)

Bearer optional (public files are readable by any caller allowed through the API key — send the
token anyway). Server decrypts and streams the plaintext.

- Response: binary, `Content-Disposition: attachment; filename="<name>"`.
- Private file → `400 {"detail": "Private files are decrypted in your browser"}` — never call this
  for private files.
- `404` unknown id; `502` storage read failure.
- For progress display, read `Content-Length` (same-origin) or use the exposed headers (**CORS
  exposes `Content-Length`** for cross-origin).

### 4.12 Private download — `manifest` + `chunk/{index}`

`GET /files/{file_id}/manifest`

- Access: valid `Authorization` (owner) **or** `?t=<ticket>` from a share link.
- Response: the sealed manifest JSON; `Cache-Control: private, no-store`.
- Errors: `404` unknown, `400` if the file is not private, `403` no access.

`GET /files/{file_id}/chunk/{index}`

- Same access rules as the manifest. `index` must be `0 <= index < chunk_count`.
- Response: the raw encrypted blob, `application/octet-stream`; `404` for bad index; `502` storage
  failure.

The frontend then decrypts locally (§6.6). The server never sees plaintext for these.

### 4.13 `POST /files/{file_id}/share`

Bearer required, owner only. No body. Creates a share link token.

Response `200`: `{"token": "…", "visibility": "public" | "private", "mode": "public" | "private"}`

The canonical share URL is `<API_BASE>/share/<token>` (the backend serves a ready-made landing
page for it — link to it directly, or rebuild the recipient flow per §5).

Errors: `404 {"detail": "Your file was not found"}` if the file does not exist or is not yours.

### 4.14 Share-link endpoints (recipient side)

These are the routes the served share page itself uses; rebuild them if you want a
frontend-owned share experience. All of them are **exempt from the API key** so links work in any
browser.

`GET /share/{token}` → HTML landing page (not JSON). The page payload is embedded in the HTML for
the bundled client; a custom frontend does not have to use this route at all.

`POST /share/{token}/access` → mints a short-lived ticket.

```json
{ "url": "/share/d/<ticket>", "expires_in": 600, "file_id": "…", "mode": "private" }
```

- No auth required. `url` is relative to the API base.
- `404 {"detail": "Share link not found"}` for unknown tokens.

`GET /share/d/{ticket}` → **public files only**: streams the plaintext attachment
(`Content-Disposition`). For private files it fails (`400`); use the ticket as the `?t=` auth on
the manifest/chunk endpoints instead. If the ticket is missing/expired and the caller is a
browser (`Accept: text/html`), the server `302`s to `/share/{share_token}` to mint a fresh one;
API calls get `404 {"detail": "This download link has expired, open the share link again"}`.

`GET /share/{token}/download` → convenience route. Public: streams like `/download/{id}`.
Private: `403` JSON for API callers; browsers are redirected to the share page.

**Recipient flow for a private share** (what the share page does, rebuild or link to it):

1. `POST /share/{token}/access` → ticket (the part after `/share/d/` in `url`).
2. `GET /files/{file_id}/manifest?t={ticket}` → sealed manifest.
3. Ask the recipient for the password; derive `K_root` from `manifest.kdf` (salt and params come
   from the manifest, not from an account) and open the manifest (§6.6).
4. For each chunk: `GET /files/{file_id}/chunk/{index}?t={ticket}`, decrypt with the chunk key.
5. Verify the chain digest (§6.7), concatenate, save under the decrypted name.

The ticket is file-scoped and valid for 600 s.

---

## 5. Quick reference

| Method | Path | Caller | Key required | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| GET | `/` | browser | no | Bundled UI (legacy). |
| GET | `/gitdrive-crypto.js` | browser | no | Shared crypto module. |
| GET | `/docs`, `/openapi.json` | devs | yes | FastAPI docs (when enabled). |
| GET | `/auth/kdf` | anyone | yes | Argon2id params for a username. |
| POST | `/auth/register` | anyone | yes | Create account. |
| POST | `/auth/login` | anyone | yes | Get session token. |
| GET | `/auth/me` | session | yes | Check session. |
| POST | `/auth/logout` | session | yes | Drop session. |
| GET | `/files` | session | yes | List visible files. |
| POST | `/upload` | session | yes | Upload one chunk. |
| GET | `/upload/status/{upload_id}` | session | yes | Poll storage sync. |
| GET | `/download/{file_id}` | session (public: any) | yes | Public plaintext download. |
| GET | `/files/{file_id}/manifest` | session or ticket | no | Sealed manifest. |
| GET | `/files/{file_id}/chunk/{index}` | session or ticket | no | Encrypted chunk. |
| POST | `/files/{file_id}/share` | session (owner) | yes | Mint share token. |
| POST | `/share/{token}/access` | anyone | no | Mint ticket. |
| GET | `/share/d/{ticket}` | anyone | no | Public download by ticket. |
| GET | `/share/{token}/download` | anyone | no | Public download by share token. |
| GET | `/share/{token}` | browser | no | Share landing page (HTML). |

---

## 6. Crypto contract (format v1) — for private space

Use `window.GDCrypto` from `/gitdrive-crypto.js`. It exposes:
`deriveKRoot(password, saltHex, params)`, `authVerifier(kRoot)`, `fileKek(rootKey, fileId)`,
`chunkKey(dek, index, total)`, `sealChunk(fileId, mode, index, total, keyHex, plain, nonceHex?)`,
`openChunk(fileId, blob, keyHex, expect?)`, `chainDigest(shaHexes)`, `buildManifest(opts)`,
`openManifest(manifest, rootKey)`, plus hex/random helpers and the constants
(`ARGON2_M_KIB = 65536`, `ARGON2_T = 3`, `ARGON2_P = 1`, `KEY_LEN = 32`,
`NONCE_LEN = 12`, `TAG_LEN = 16`, `CHUNK_HEADER_LEN = 29`).

This section is the byte-level contract it implements; follow it exactly if you ever reimplement
it. `tests/js_vectors.js` pins the JS and Python implementations together.

### 6.1 Canonical JSON

All authenticated JSON is serialized as: object keys sorted lexicographically, `,`/`:` separators,
no whitespace, UTF-8, non-ASCII preserved (no `\u` escaping). Hex strings stay lowercase.

### 6.2 Password → verification

```
K_root  = Argon2id(password, salt, m=65536 KiB, t=3, p=1, out=32 bytes)
K_auth  = HKDF-SHA256(ikm=K_root, salt=empty, info="gitdrive/v1/auth")
verifier = hex(HMAC-SHA256(key=K_auth, msg="gitdrive-auth-v1"))     # 64 hex chars, sent to server
```

The server stores only `SHA-256(verifier)`. The password and `K_root` never leave the browser.

### 6.3 File keys

```
KEK     = HKDF-SHA256(ikm=K_root (private) or server master key (public),
                      salt=<file_id bytes: hex-decoded, 16 bytes>,
                      info="gitdrive/v1/kek")
K_meta  = HKDF-SHA256(ikm=KEK, salt=empty, info="gitdrive/v1/meta")
DEK     = 32 random bytes, generated per file, stored only wrapped
chunkKey(i, n) = HKDF-SHA256(ikm=DEK, salt=empty, info="gitdrive/v1/chunk/{i}/{n}")
```

### 6.4 Chunk envelope

```
offset  size  field
0       4     magic "GDC1" (47 44 43 31)
4       1     mode: 1 = private, 2 = public
5       4     chunk index      (uint32, big-endian)
9       4     total chunks     (uint32, big-endian)
13      4     chunk plaintext length (uint32, big-endian)
17      12    AES-GCM nonce (fresh random per chunk)
29      n+16  ciphertext + 16-byte tag

AAD = "GDC1-AAD/1" (ASCII) + file_id (ASCII, exactly the 32 hex chars) + bytes[0..29)
```

`sealChunk` = header ‖ AES-256-GCM(chunkKey, nonce, plaintext, AAD). One key per chunk index, so
no (key, nonce) pair is ever reused. AAD binding means a chunk cannot be moved across files,
reordered, or relabeled.

### 6.5 Manifest schema

```json
{
  "version": 1,
  "mode": "private",
  "cipher": { "alg": "aes-256-gcm", "nonce": 12, "tag": 16 },
  "kdf":    { "alg": "argon2id", "salt": "…", "m_kib": 65536, "t": 3, "p": 1, "len": 32 },
  "hkdf":   { "alg": "hkdf-sha256" },
  "file_id": "…",
  "chunk_size": 10485760,
  "chunk_count": 3,
  "plain_size": 22020126,
  "key_wrap": { "alg": "aes-256-gcm", "nonce": "<24 hex>", "wrapped_dek": "<hex>" },
  "meta":     { "alg": "aes-256-gcm", "nonce": "<24 hex>", "ct": "<hex>" }
}
```

- `manifest_aad = canonical(manifest minus "meta" and "key_wrap")`
- `key_wrap` = AES-GCM(KEK, aad=manifest_aad) sealing the DEK.
- `meta` plaintext = canonical `{"name", "path", "mime", "chunks": [{"index", "sha256", "size"}], "chain"}`
  sealed with `K_meta` and the same AAD. This is the only place private filenames/paths exist.
- `sha256` per chunk = SHA-256 hex of the uploaded blob (header+ciphertext+tag) — identical to the
  `blob_sha` you sent.

### 6.6 Private upload / download with GDCrypto

Upload (per file):

1. `fileId = GD.randomHex(16)`, `dek = GD.randomBytes(32)`.
2. For each chunk: `blob = await GD.sealChunk(fileId, GD.MODE_PRIVATE, i, total, GD.hex(dek), plain)`.
3. Last chunk additionally carries the manifest:
   `manifest = await GD.buildManifest({ mode: GD.MODE_PRIVATE, fileId, chunkSize: 10*1024*1024, chunkCount: total, plainSize: file.size, kRoot: K_ROOT, dekHex: GD.hex(dek), kdf: <from /auth/kdf>, meta: { name, path, mime, chunks: [{index, sha256, size}], chain: await GD.chainDigest(shas) } })`.

Download: fetch manifest → `{ dekHex, meta } = await GD.openManifest(manifest, K_ROOT)` → per chunk
`await GD.openChunk(fileId, blob, GD.hex(await GD.chunkKey(GD.unhex(dekHex), i, total)), { index, total })`
→ verify `await GD.chainDigest(shas) === meta.chain` → concatenate `plain` parts in index order.

### 6.7 Chain digest

```
h_0 = SHA-256("")                       (digest of the empty byte string)
h_i = SHA-256(h_{i-1} bytes ‖ sha256_bytes(chunk_i blob))
chain = hex(h_n)
```

Detects reordering, duplication and truncation of chunks.

### 6.8 KDF validation floors (enforced on both sides)

Reject manifests/params with `m_kib < 8192`, `t < 1`, `salt < 16 bytes`, or anything above
`m_kib > 1048576`, `t > 16`, `p > 16`.

---

## 7. Limits and server behaviours

| Limit | Value | Notes |
| :--- | :--- | :--- |
| Recommended chunk size | 10 MiB | `10 * 1024 * 1024`; keep consistent across an upload. |
| Max file size | 10 GiB | `total_chunks <= 1024`. |
| Max single plaintext chunk (public) | 64 MiB | Server-side guard. |
| Empty files | allowed | Send one empty chunk (`total_chunks = 1`). |
| `file_id` | exactly 32 hex chars | Client random; must be unique per file. |
| Username | 3–32 chars, `A-Za-z0-9-_` | |
| Ticket TTL | 600 s | Mint a fresh one when expired. |
| Share token / session token | opaque `urlsafe` strings | Treat as secrets; never log them. |
| API key header | `X-API-Key` | See §3.3 for exemptions. |

Upload-session semantics the UI must handle:

- Chunks of one `upload_id` may be re-sent only before the session is finalized; the server does
  **not** support resume-after-restart. On any upload failure, restart with a fresh `upload_id`.
- The final chunk can return `done`/`duplicate`/`processing`; poll `/upload/status` only for
  `processing`. A `404` while polling means finished.
- `github_percent` measures storage sync, not network progress; network progress comes from XHR
  upload events.

Private-space validation the frontend must satisfy (or the server rejects with a precise `400`):

- `blob_sha` must equal the SHA-256 of the exact bytes uploaded.
- The blob must start with `GDC1` and its length must be exactly `plain_len + 45`.
- The manifest must be version 1, `mode: "private"`, the same `file_id`, `chunk_count == total_chunks`,
  `plain_size == size_bytes`, with `kdf`, `key_wrap` and `meta` objects present.
- `visibility` and the upload geometry are fixed by the first chunk.

---

## 8. Not implemented yet — do not build against

The following exist in neither the API nor the data model: **delete/rename/move files, password
change or recovery, collaborators, file search, album/folder objects, usage quotas per user,
session expiry, resumable uploads, thumbnails/previews**. If a UI feature needs any of these,
schedule backend work first.

---

## 9. Handoff checklist

1. Backend owner sets `FRONTEND_ORIGINS` to the deployed frontend origin(s) and generates a
   strong `API_KEY`; both are shared out-of-band.
2. Frontend consumes `X-API-Key` + `Authorization` on every call, from build-time env config.
3. Frontend loads `/gitdrive-crypto.js` and implements private flows per §6 (or reuses the
   reference client's code).
4. Smoke-test in the browser console before building UI:

```bash
# health / CORS / key check (expect a JSON KDF response)
curl -s "$API_BASE/auth/kdf?username=smokecheck" -H "X-API-Key: $API_KEY"

# cross-origin preflight check from a browser console on the frontend origin
fetch("$API_BASE/files", { headers: { "X-API-Key": KEY, "Authorization": "Bearer "+token } })
```

If the API key or origin is not yet provisioned, ask the backend owner; do not disable the gate.
