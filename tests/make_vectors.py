"""Generates the shared test vectors that pin the browser and Python implementations
together. Run directly after touching either side of the format:

    python tests/make_vectors.py && node tests/js_vectors.js
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api import crypto  # noqa: E402

SALT = bytes.fromhex("81e7cc338a3b4fc2e6772c7b2a330312")
PASSWORD = "correct horse battery staple"
FILE_ID = "0ca8d5f77444435cf6907d8ee961ad20"
DEK = bytes.fromhex("4e1f9a2b7c3d5e6f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f7")
NONCE = bytes.fromhex("00112233445566778899aabb")


def build():
    # Deliberately weak so the file regenerates in milliseconds; tests/js_vectors.js only
    # checks that both implementations agree, not that the cost is production.
    k_root = crypto.derive_kroot(PASSWORD, SALT, crypto.ARGON2_MIN_M_KIB, crypto.ARGON2_MIN_T, 1)
    kek = crypto.file_kek(k_root, FILE_ID)
    meta = {
        "name": "r\u00e9sum\u00e9 \U0001F4C1.pdf",   # non-ASCII: canonical() must agree too
        "path": "docs/",
        "mime": "application/pdf",
        "created": "2026-01-01T00:00:00Z",
        "chunks": [],
        "chain": "ab" * 32,
    }
    kdf = {"alg": "argon2id", "salt": SALT.hex(), "m_kib": crypto.ARGON2_MIN_M_KIB,
           "t": crypto.ARGON2_MIN_T, "p": 1, "len": crypto.KEY_LEN}
    manifest, _ = crypto.build_manifest(
        mode=crypto.MODE_PRIVATE, file_id=FILE_ID, chunk_size=10485760, chunk_count=2,
        plain_size=20000000, kdf=kdf, dek=DEK, kek=kek, meta=meta)
    blob = crypto.seal_chunk(FILE_ID, crypto.MODE_PRIVATE, 1, 2, 7,
                             crypto.chunk_key(DEK, 1, 2), b"payload", NONCE)
    return {
        "password": PASSWORD,
        "salt_hex": SALT.hex(),
        "kdf": kdf,
        "kroot_hex": k_root.hex(),
        "file_id": FILE_ID,
        "dek_hex": DEK.hex(),
        "kek_hex": kek.hex(),
        "chunk_key_hex": crypto.chunk_key(DEK, 1, 2).hex(),
        "auth_verifier": crypto.auth_verifier(k_root),
        "meta_object": meta,
        "meta_plaintext_hex": crypto.canonical(meta).hex(),
        "manifest": manifest,
        "manifest_aad_hex": crypto.manifest_aad(manifest).hex(),
        "nonce_hex": NONCE.hex(),
        "chunk_blob_hex": blob.hex(),
    }


def main() -> str:
    vectors = build()
    out = Path(__file__).resolve().parent / "vectors.json"
    with out.open("w", encoding="utf-8") as fh:
        json.dump(vectors, fh, indent=2, ensure_ascii=False)
    return str(out)


if __name__ == "__main__":
    print("wrote", main())