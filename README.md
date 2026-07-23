# GitDrive

A high-performance ( ¯\\_(ツ)_/¯ , I mean it's fast), **cost-free** block (since GitHub doesn't allow you to upload files bigger than 25MB without warnings, we chunk them) **cloud storage system** utilizing FastAPI, MongoDB, and GitHub repositories as an **INFINITE** (lmao, I hope) backend storage provider. This project features chunk-based uploads, client-side parallel streaming downloads, and transparent AES-256-GCM encryption.

## Deployment Links

| Resource | Target URL |
| :--- | :--- |
| **Production API Endpoint** | `---` |
| **Frontend Web Interface** | `---` |

## Architectural Highlights

### 1. Producer-Consumer Pipeline (SOTA)
To prevent OOM on resource-constrained virtual machines (e.g., 2 GB RAM instances, or Render's stateless containers), the server employs a producer-consumer upload pipeline. Chunks uploaded by the browser are **immediately written to disk-backed temporary files** and placed into a **queue**. A pool of five worker coroutines drains the queue to upload chunks to GitHub in parallel. This guarantees that the RAM usage remains static at approximately 50 MB regardless of concurrent users or file sizes.

> This approach is better than the previous pipeline as this is a non-blocking system which allows the browser to upload at its maximum speed and, thanks to workers, now the uploads are 5x faster!

### 2. Folder Traversal & Browser Zipping 
When you drag and drop a folder, GitDrive traverses it recursively and appends the relative file path to the record's path metadata, mimicking real directory hierarchies just like Google Drive

> This approach downloads the files sequentially to the client's browser, decrypts them on-the-fly, and uses `JSZip` to compile the folder into a `.zip` archive entirely on the client's machine. Zero server overhead, and scalable

### 3. AES-256-GCM Encryption (Not reallu SOTA :P)
All raw payloads are encrypted using AES-256 in Galois/Counter Mode (GCM) with a cryptographically secure random 12-byte nonce prepended to each chunk. This ensures that no one other than the server can decrypt the files, making this a highly secure storage solution.

> P.S. Do we need a stronger encryption? Not necessarily, because first of all the files are stored as chunks and since you don't know the order of the chunks you cannot reassemble them by yourself, so it's already hard. This encryption does its job well (this enc ain't a joke gng).

## API Endpoints Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Returns the core single-page HTML frontend. |
| `POST` | `/auth/register`, `/auth/login` | Creates an account or returns a bearer session. |
| `POST` | `/upload` | Accepts authenticated multipart chunk form uploads. |
| `GET` | `/upload/status/{upload_id}` | Retrieves the sync percentage of the queue workers uploading to GitHub. | 
| `GET` | `/download/{file_sha256}` | Downloads public files or authenticated private files. |
| `POST` | `/files/{sha}/collaborators` | Adds a GitDrive user to a private file. |
| `POST` | `/files/{sha}/share` | Creates a public or access-controlled share link. |
| `GET` | `/share/{token}/download` | Downloads from a share link; private links still require a collaborator account. |
| `GET` | `/files` | Lists files owned by, shared with, or publicly visible to the signed-in user. |

## Self-Hosting Guide
1. Create a `.env` file in the root directory to store these values:

| Variable | Description | Default / Example |
| :--- | :--- | :--- |
| `MONGODB_URI` | MongoDB connection URI string | `mongodb+srv://` |
| `DATABASE_NAME` | Name of the MongoDB database | `gitdrive_db` |
| `GITHUB_TOKEN` | GitHub Personal Access Token (Classic) with full `repo` scope | `ghp_...` |
| `GITHUB_OWNER` | GitHub username that owns the target repos | `your-username` |
| `GITHUB_PRIVATE` | Whether newly created GitHub repos should be private | `false/true` |
| `MASK_COMMITS` | Commit message mode for GitHub uploads: `path8` uses the first 8 chars of the object path, `filename` uses the uploaded file name | `true (to mask) / false` |
| `LOG_MODE` | Application logging verbosity (`prod` or `debug`) | `prod` |

2.  I recommend using [uv](https://github.com/astral-sh/uv), as it's insanely faster than pip, then:
```bash
    uv sync
    uv run uvicorn api.main:app --host 0.0.0.0 --port 10000
```
---

### UI Features (as of now) :

- Stuff like `sharable links` and many more are a part of UI, which will be  documented soon!

### Upcoming Features :
- delete feature (lmao, mb, it not implemented for now)
- maybe some enhanced features like better logging and setup experience
- many more , but after frontend launch
