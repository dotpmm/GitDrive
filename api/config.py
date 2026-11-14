"""All configuration comes from environment variables (see README)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Environment variable {name} is required (see README -> Self-Hosting Guide)")
    return v


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


GITHUB_TOKEN = _req("GITHUB_TOKEN")
GITHUB_OWNER = _req("GITHUB_OWNER")
GITHUB_PRIVATE = os.getenv("GITHUB_PRIVATE", "false").lower() == "true"
MASK_COMMITS = os.getenv("MASK_COMMITS", "true").lower() == "true"

MONGODB_URI = _req("MONGODB_URI")
DATABASE_NAME = _req("DATABASE_NAME")

# Secrets. ENCRYPTION_KEY protects the per-file data keys; JWT_SECRET signs sessions
# and download links. Both must be long random strings and must not change.
ENCRYPTION_KEY = _req("ENCRYPTION_KEY")
JWT_SECRET = _req("JWT_SECRET")

ADMIN_USERNAMES = {u.strip().lower() for u in os.getenv("ADMIN_USERNAMES", "").split(",") if u.strip()}
SIGNUPS_ENABLED = os.getenv("SIGNUPS_ENABLED", "true").lower() == "true"

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
CORS_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("CORS_ORIGINS", FRONTEND_URL).split(",") if o.strip()]
# Public base URL of this API (used for share links). Derived from the request when empty.
API_PUBLIC_URL = os.getenv("API_PUBLIC_URL", "").rstrip("/")

LOG_MODE = os.getenv("LOG_MODE", "prod").lower()

# storage
CHUNK_SIZE = 10 * 1024 * 1024
REPO_SIZE_LIMIT = _int("REPO_SIZE_LIMIT_GB", 5) * 1024**3
MAX_FILE_BYTES = _int("MAX_FILE_GB", 2) * 1024**3
DEFAULT_QUOTA_BYTES = _int("DEFAULT_QUOTA_GB", 0) * 1024**3  # 0 = unlimited
UPLOAD_BLOB_CONCURRENCY = _int("UPLOAD_BLOB_CONCURRENCY", 4)
DOWNLOAD_PREFETCH = _int("DOWNLOAD_PREFETCH", 4)
CHUNK_CACHE_ITEMS = _int("CHUNK_CACHE_ITEMS", 4)
UPLOAD_SESSION_TTL = 6 * 3600

# unzip safety limits
EXTRACT_MAX_FILES = _int("EXTRACT_MAX_FILES", 2000)
EXTRACT_MAX_BYTES = _int("EXTRACT_MAX_GB", 2) * 1024**3

# email (password reset). EMAIL_PROVIDER = resend | smtp | none
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "none").lower()
EMAIL_FROM = os.getenv("EMAIL_FROM", "GitDrive <onboarding@resend.dev>")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
