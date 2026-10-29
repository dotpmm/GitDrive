"""GitDrive on-disk crypto format, version 1.

This module is the reference implementation of the format and is deliberately free of
FastAPI, Mongo and GitHub imports so that a standalone CLI can `import api.crypto`
and decrypt a private file straight from a GitHub repository.

    password -> Argon2id -> K_root -> HKDF-SHA256 -> KEK -> { DEK, K_meta }
                K_root  -> HKDF-SHA256 -> K_auth -> login verifier
    KEK       -> AES-256-GCM unwrap -> DEK -> HKDF-SHA256 -> per-chunk key -> AES-256-GCM

Public files use the identical format; only the input key material changes from
`K_root` to the server's GITDRIVE_MASTER_KEY, which is why a holder of the GitHub
repository and a user's password still cannot decrypt a public file.
"""

import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT_VERSION = 1

MODE_PRIVATE = 1
MODE_PUBLIC = 2
MODE_NAMES = {MODE_PRIVATE: "private", MODE_PUBLIC: "public"}

KEY_LEN = 32
NONCE_LEN = 12
TAG_LEN = 16

CHUNK_MAGIC = b"GDC1"
CHUNK_AAD_PREFIX = b"GDC1-AAD/1"
CHUNK_HEADER_LEN = 4 + 1 + 4 + 4 + 4 + NONCE_LEN  # magic, mode, index, total, plain_len, nonce

# Argon2id cost. Recorded verbatim in every manifest so any reader can reproduce the
# derivation, and re-checked against these floors so a manifest cannot downgrade us.
ARGON2_M_KIB = 65536
ARGON2_T = 3
ARGON2_P = 1
ARGON2_MIN_M_KIB = 8192
ARGON2_MIN_T = 1

INFO_KEK = b"gitdrive/v1/kek"
INFO_META = b"gitdrive/v1/meta"
INFO_AUTH = b"gitdrive/v1/auth"
AUTH_CONTEXT = b"gitdrive-auth-v1"
CHUNK_INFO = "gitdrive/v1/chunk/{index}/{total}"


class CryptoError(Exception):
    """Raised for every authentication, version or parameter failure. Never leaks plaintext."""


def canonical(obj: Any) -> bytes:
    """Byte-exact serialization two independent implementations must agree on."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def hkdf(ikm: bytes, info: bytes, salt: bytes = b"", length: int = KEY_LEN) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def derive_kroot(password: str, salt: bytes, m_kib: int = ARGON2_M_KIB,
                 t: int = ARGON2_T, p: int = ARGON2_P) -> bytes:
    """Argon2id(password, salt) -> K_root. Never leaves the client."""
    if len(salt) < 16:
        raise CryptoError("kdf salt is too short")
    if m_kib < ARGON2_MIN_M_KIB or t < ARGON2_MIN_T or p < 1:
        raise CryptoError("kdf parameters are below the enforced floor")
    if m_kib > 1 << 20 or t > 16 or p > 16:
        raise CryptoError("kdf parameters are above the accepted maximum")
    return Argon2id(
        salt=salt, length=KEY_LEN, iterations=t, lanes=p, memory_cost=m_kib,
    ).derive(password.encode("utf-8"))


def check_kdf_params(params: dict) -> None:
    """Refuse manifests that ask for a weaker KDF than this build accepts."""
    if params.get("alg") == "server":
        return  # public space: no password derivation happens at all
    if params.get("alg") != "argon2id":
        raise CryptoError(f"unsupported kdf: {params.get('alg')!r}")
    if len(bytes.fromhex(params["salt"])) < 16:
        raise CryptoError("kdf salt is too short")
    if int(params["m_kib"]) < ARGON2_MIN_M_KIB or int(params["t"]) < ARGON2_MIN_T:
        raise CryptoError("manifest kdf parameters are below the enforced floor")
    if int(params["m_kib"]) > 1 << 20 or int(params["t"]) > 16 or int(params["p"]) > 16:
        raise CryptoError("manifest kdf parameters are above the accepted maximum")


def auth_verifier(k_root: bytes) -> str:
    """Keyed value the server stores instead of the password."""
    k_auth = hkdf(k_root, INFO_AUTH)
    return hmac.new(k_auth, AUTH_CONTEXT, hashlib.sha256).hexdigest()


def hash_verifier(verifier: str) -> str:
    """What the database actually holds: a password is never stored, and neither is this key."""
    return hashlib.sha256(verifier.encode("utf-8")).hexdigest()


def server_master_key() -> bytes:
    """GITDRIVE_MASTER_KEY: the public-space root secret. Independent of GITHUB_TOKEN."""
    raw = os.environ.get("GITDRIVE_MASTER_KEY", "")
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise CryptoError("GITDRIVE_MASTER_KEY must be 64 hex characters") from exc
    if len(key) != KEY_LEN:
        raise CryptoError("GITDRIVE_MASTER_KEY must be 64 hex characters")
    return key


def file_kek(root_key: bytes, file_id: str) -> bytes:
    """Per-file key-encryption key. root_key is K_root (private) or the master key (public)."""
    return hkdf(root_key, INFO_KEK, salt=bytes.fromhex(file_id))


def chunk_key(dek: bytes, index: int, total: int) -> bytes:
    """A distinct key per chunk, so every AES-GCM key is used with exactly one nonce."""
    return hkdf(dek, CHUNK_INFO.format(index=index, total=total).encode("utf-8"))


def chunk_aad(file_id: str, header: bytes) -> bytes:
    return CHUNK_AAD_PREFIX + file_id.encode("ascii") + header


def chunk_header(mode: int, index: int, total: int, plain_len: int, nonce: bytes) -> bytes:
    if not 0 <= index < total:
        raise CryptoError("chunk index out of range")
    if not 0 <= plain_len < 1 << 32 or total > 1 << 20:
        raise CryptoError("chunk geometry out of range")
    if len(nonce) != NONCE_LEN:
        raise CryptoError("nonce must be 12 bytes")
    return (CHUNK_MAGIC + bytes([mode])
            + index.to_bytes(4, "big") + total.to_bytes(4, "big") + plain_len.to_bytes(4, "big")
            + nonce)


def parse_chunk_header(blob: bytes) -> tuple[bytes, int, int, int, int, bytes]:
    if len(blob) < CHUNK_HEADER_LEN + TAG_LEN or not blob.startswith(CHUNK_MAGIC):
        raise CryptoError("not a GitDrive chunk")
    header, body = blob[:CHUNK_HEADER_LEN], blob[CHUNK_HEADER_LEN:]
    mode = header[4]
    index = int.from_bytes(header[5:9], "big")
    total = int.from_bytes(header[9:13], "big")
    plain_len = int.from_bytes(header[13:17], "big")
    nonce = header[17:CHUNK_HEADER_LEN]
    if len(body) != plain_len + TAG_LEN:
        raise CryptoError("chunk length does not match its header")
    if not 0 <= index < total:
        raise CryptoError("chunk index out of range")
    return header, mode, index, total, plain_len, nonce


def seal_chunk(file_id: str, mode: int, index: int, total: int, plain_len: int,
               key: bytes, plaintext: bytes, nonce: bytes | None = None) -> bytes:
    """header || ciphertext || tag. Self-contained: a reader needs only the file key."""
    if len(plaintext) != plain_len:
        raise CryptoError("plaintext length does not match the declared length")
    nonce = nonce if nonce is not None else os.urandom(NONCE_LEN)
    header = chunk_header(mode, index, total, plain_len, nonce)
    try:
        body = AESGCM(key).encrypt(nonce, plaintext, chunk_aad(file_id, header))
    except Exception as exc:
        raise CryptoError("chunk encryption failed") from exc
    return header + body


def open_chunk(file_id: str, blob: bytes, key: bytes, *, expect_index: int | None = None,
               expect_total: int | None = None) -> tuple[int, bytes]:
    """Authenticates and decrypts one chunk. Raises CryptoError on any tampering."""
    header, _mode, index, total, _plain_len, nonce = parse_chunk_header(blob)
    if expect_index is not None and index != expect_index:
        raise CryptoError("chunk is out of order")
    if expect_total is not None and total != expect_total:
        raise CryptoError("chunk count does not match the manifest")
    try:
        plaintext = AESGCM(key).decrypt(nonce, blob[CHUNK_HEADER_LEN:], chunk_aad(file_id, header))
    except InvalidTag as exc:
        raise CryptoError("chunk failed authentication") from exc
    except Exception as exc:
        raise CryptoError("chunk could not be decrypted") from exc
    return index, plaintext


def chain_digest(shas: list[str]) -> str:
    """Folds the per-chunk blob hashes so reordering, duplication or truncation is detectable."""
    digest = hashlib.sha256()
    for sha in shas:
        digest.update(digest.digest() + bytes.fromhex(sha))
    return digest.hexdigest()


def manifest_aad(manifest: dict) -> bytes:
    """Authenticates every field that is not itself a ciphertext.

    `key_wrap` and `meta` are excluded because each is sealed *under* this AAD; they are
    still authenticated, since altering either one breaks its own tag.
    """
    return canonical({k: v for k, v in manifest.items() if k not in ("meta", "key_wrap")})


def wrap_dek(kek: bytes, dek: bytes, aad: bytes) -> dict:
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(kek).encrypt(nonce, dek, aad)
    return {"alg": "aes-256-gcm", "nonce": nonce.hex(), "wrapped_dek": ct.hex()}


def unwrap_dek(kek: bytes, key_wrap: dict, aad: bytes) -> bytes:
    if key_wrap.get("alg") != "aes-256-gcm":
        raise CryptoError("unsupported key wrapping algorithm")
    try:
        return AESGCM(kek).decrypt(bytes.fromhex(key_wrap["nonce"]),
                                   bytes.fromhex(key_wrap["wrapped_dek"]), aad)
    except InvalidTag as exc:
        raise CryptoError("wrong password, or the manifest was altered") from exc


def seal_meta(kek: bytes, meta: dict, aad: bytes) -> dict:
    nonce = os.urandom(NONCE_LEN)
    return {"alg": "aes-256-gcm", "nonce": nonce.hex(),
            "ct": AESGCM(kek).encrypt(nonce, canonical(meta), aad).hex()}


def open_meta(kek: bytes, meta: dict, aad: bytes) -> dict:
    if meta.get("alg") != "aes-256-gcm":
        raise CryptoError("unsupported metadata algorithm")
    try:
        return json.loads(AESGCM(kek).decrypt(bytes.fromhex(meta["nonce"]),
                                              bytes.fromhex(meta["ct"]), aad).decode("utf-8"))
    except InvalidTag as exc:
        raise CryptoError("metadata failed authentication") from exc
    except (ValueError, KeyError) as exc:
        raise CryptoError("metadata is malformed") from exc


def build_manifest(*, mode: int, file_id: str, chunk_size: int, chunk_count: int,
                   plain_size: int, kdf: dict, dek: bytes, kek: bytes, meta: dict) -> tuple[dict, bytes]:
    """Assembles the manifest. `dek` is only ever stored wrapped."""
    manifest = {
        "version": FORMAT_VERSION,
        "mode": MODE_NAMES[mode],
        "cipher": {"alg": "aes-256-gcm", "nonce": NONCE_LEN, "tag": TAG_LEN},
        "kdf": kdf,
        "hkdf": {"alg": "hkdf-sha256"},
        "file_id": file_id,
        "chunk_size": chunk_size,
        "chunk_count": chunk_count,
        "plain_size": plain_size,
    }
    aad = manifest_aad(manifest)
    manifest["key_wrap"] = wrap_dek(kek, dek, aad)
    manifest["meta"] = seal_meta(hkdf(kek, INFO_META), meta, aad)
    return manifest, aad


def open_manifest(manifest: dict, root_key: bytes) -> tuple[bytes, dict]:
    """root_key -> KEK -> { DEK, metadata }. The CLI's entry point."""
    if manifest.get("version") != FORMAT_VERSION:
        raise CryptoError(f"unsupported manifest version: {manifest.get('version')!r}")
    if manifest.get("cipher", {}).get("alg") != "aes-256-gcm":
        raise CryptoError("unsupported cipher")
    check_kdf_params(manifest.get("kdf", {}))
    aad = manifest_aad(manifest)
    kek = file_kek(root_key, manifest["file_id"])
    dek = unwrap_dek(kek, manifest["key_wrap"], aad)
    return dek, open_meta(hkdf(kek, INFO_META), manifest["meta"], aad)