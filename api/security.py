"""Passwords, sessions, signed links, rate limiting and envelope encryption."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import config

# ------------------------------------------------------------------ passwords
_N, _R, _P = 2**15, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=128 * 1024 * 1024, dklen=32)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(h).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              maxmem=128 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:
        return False


# a fixed dummy hash so login takes the same time for unknown users
_DUMMY = hash_password("dummy-password-for-timing")


def burn_password_time(password: str) -> None:
    verify_password(password, _DUMMY)


USERNAME_RE = re.compile(r"^[a-z0-9_]{3,24}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


# ------------------------------------------------------------------- sessions
_SESSION_DAYS = 30


def make_token(username: str, token_version: int) -> str:
    now = int(time.time())
    return jwt.encode({"sub": username, "tv": token_version, "iat": now, "exp": now + _SESSION_DAYS * 86400},
                      config.JWT_SECRET, algorithm="HS256")


def read_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# ---------------------------------------------------------------- signed links
_SIGN_KEY = hashlib.sha256(b"gitdrive-sign|" + config.JWT_SECRET.encode()).digest()


def sign(*parts: str) -> str:
    msg = "|".join(parts).encode()
    return base64.urlsafe_b64encode(hmac.new(_SIGN_KEY, msg, hashlib.sha256).digest()[:24]).decode().rstrip("=")


def verify_sig(sig: str, *parts: str) -> bool:
    return hmac.compare_digest(sig, sign(*parts))


def new_token(nbytes: int = 18) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---------------------------------------------------------------- rate limiter
class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: int) -> bool:
        """Record an attempt; False when the caller is over the limit."""
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 20000:  # keep memory bounded
                for k in [k for k, v in self._hits.items() if not v][:5000]:
                    self._hits.pop(k, None)
            return True


limiter = RateLimiter()

# ------------------------------------------------------------ envelope crypto
# Every blob gets its own random 256-bit data key. The data key is wrapped with a
# master key derived from ENCRYPTION_KEY and stored in MongoDB; the ciphertext lives
# on GitHub. Chunks are bound to (blob, index) via AAD so they cannot be swapped.
_MASTER = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"gitdrive-v4", info=b"master-key").derive(
    config.ENCRYPTION_KEY.encode())


def new_data_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def wrap_key(data_key: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_MASTER).encrypt(nonce, data_key, b"wrap")


def unwrap_key(wrapped: bytes) -> bytes:
    return AESGCM(_MASTER).decrypt(wrapped[:12], wrapped[12:], b"wrap")


def _aad(blob_id: str, index: int) -> bytes:
    return f"{blob_id}:{index}".encode()


def encrypt_chunk(data_key: bytes, blob_id: str, index: int, data: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(data_key).encrypt(nonce, data, _aad(blob_id, index))


def decrypt_chunk(data_key: bytes, blob_id: str, index: int, data: bytes) -> bytes:
    return AESGCM(data_key).decrypt(data[:12], data[12:], _aad(blob_id, index))
