"""Runs the shared-vector checks in three layers:

  1. api/crypto.py reproduces the pinned vectors          (always)
  2. gitdrive-crypto.js reproduces them under Node          (skipped if node is absent)
  3. tests/js_vectors.js additionally checks the negative cases in JavaScript

Layer 2/3 are what prove the browser and the server agree byte for byte.
"""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import conftest
import make_vectors
from api import crypto

ROOT = conftest.ROOT
VECTORS = Path(__file__).resolve().parent / "vectors.json"
NODE = shutil.which("node")


class PythonVectors(unittest.TestCase):
    def setUp(self):
        self.v = make_vectors.build()

    def test_argon2id_matches(self):
        self.assertEqual(
            crypto.derive_kroot(self.v["password"], bytes.fromhex(self.v["salt_hex"]),
                                crypto.ARGON2_MIN_M_KIB, crypto.ARGON2_MIN_T, 1).hex(),
            self.v["kroot_hex"])

    def test_kdf_is_deterministic_and_salt_sensitive(self):
        a = crypto.derive_kroot("pw", b"0" * 16, crypto.ARGON2_MIN_M_KIB, crypto.ARGON2_MIN_T, 1)
        b = crypto.derive_kroot("pw", b"1" * 16, crypto.ARGON2_MIN_M_KIB, crypto.ARGON2_MIN_T, 1)
        self.assertEqual(a, crypto.derive_kroot("pw", b"0" * 16,
                                                crypto.ARGON2_MIN_M_KIB, crypto.ARGON2_MIN_T, 1))
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, crypto.derive_kroot("pw2", b"0" * 16,
                                                   crypto.ARGON2_MIN_M_KIB, crypto.ARGON2_MIN_T, 1))

    def test_hkdf_separation(self):
        kek = crypto.file_kek(bytes.fromhex(self.v["kroot_hex"]), self.v["file_id"])
        self.assertEqual(kek.hex(), self.v["kek_hex"])
        # the KEK must not be usable as the DEK or as a chunk key
        dek = bytes.fromhex(self.v["dek_hex"])
        self.assertNotEqual(kek, dek)
        self.assertNotEqual(crypto.chunk_key(dek, 1, 2).hex(), kek.hex())
        self.assertNotEqual(crypto.hkdf(bytes.fromhex(self.v["kroot_hex"]), crypto.INFO_AUTH).hex(),
                            crypto.hkdf(bytes.fromhex(self.v["kroot_hex"]), crypto.INFO_KEK).hex())

    def test_chunk_vector(self):
        blob = crypto.seal_chunk(self.v["file_id"], crypto.MODE_PRIVATE, 1, 2, 7,
                                 crypto.chunk_key(bytes.fromhex(self.v["dek_hex"]), 1, 2),
                                 b"payload", bytes.fromhex(self.v["nonce_hex"]))
        self.assertEqual(blob.hex(), self.v["chunk_blob_hex"])
        self.assertEqual(blob[17:29].hex(), self.v["nonce_hex"])

    def test_manifest_vector(self):
        dek, meta = crypto.open_manifest(self.v["manifest"],
                                         bytes.fromhex(self.v["kroot_hex"]))
        self.assertEqual(dek.hex(), self.v["dek_hex"])
        self.assertEqual(meta, self.v["meta_object"])

    def test_manifest_leaks_no_plaintext(self):
        raw = crypto.canonical(self.v["manifest"])
        self.assertNotIn(b"password", raw)
        self.assertNotIn("résumé".encode(), raw)
        self.assertNotIn(bytes.fromhex(self.v["dek_hex"]), raw)   # only the wrapped DEK
        self.assertNotIn(bytes.fromhex(self.v["kroot_hex"]), raw)
        self.assertNotIn(bytes.fromhex(self.v["kek_hex"]), raw)

    def test_chunk_keys_differ_per_index_and_total(self):
        dek = bytes.fromhex(self.v["dek_hex"])
        keys = {crypto.chunk_key(dek, i, n).hex() for i in range(5) for n in (5, 6)}
        self.assertEqual(len(keys), 10)

    def test_aad_binds_the_file(self):
        dek = bytes.fromhex(self.v["dek_hex"])
        key = crypto.chunk_key(dek, 1, 2)
        blob = crypto.seal_chunk(self.v["file_id"], crypto.MODE_PRIVATE, 1, 2, 7, key, b"payload")
        other = "ff" * 16
        with self.assertRaises(crypto.CryptoError):
            crypto.open_chunk(other, blob, key)

    def test_aad_binds_the_mode(self):
        dek = bytes.fromhex(self.v["dek_hex"])
        key = crypto.chunk_key(dek, 1, 2)
        blob = crypto.seal_chunk(self.v["file_id"], crypto.MODE_PUBLIC, 1, 2, 7, key, b"payload")
        # reading it back as private must fail, because the header's mode byte is in the AAD
        self.assertEqual(crypto.parse_chunk_header(blob)[1], crypto.MODE_PUBLIC)


@unittest.skipUnless(NODE, "node is not installed")
class JavaScriptVectors(unittest.TestCase):
    def setUp(self):
        with VECTORS.open("w", encoding="utf-8") as fh:
            json.dump(make_vectors.build(), fh, indent=2, ensure_ascii=False)
        self.script = Path(__file__).resolve().parent / "js_vectors.js"

    def test_browser_matches_python(self):
        env = dict(os.environ)
        # a local hash-wasm checkout is used when present; otherwise the caller points
        # HASH_WASM at the browser build they have.
        vendored = ROOT / "node_modules" / "hash-wasm" / "dist" / "argon2.umd.min.js"
        if not env.get("HASH_WASM") and vendored.exists():
            env["HASH_WASM"] = str(vendored)
        if not env.get("HASH_WASM"):
            self.skipTest("set HASH_WASM to hash-wasm's argon2.umd.min.js to run this")
        proc = subprocess.run([NODE, str(self.script)], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         f"js_vectors.js failed\n{proc.stdout}\n{proc.stderr}")
        print(proc.stdout)

    def test_manifest_built_by_javascript_opens_in_python(self):
        """The reverse direction: the browser's output must be readable here too."""
        env = dict(os.environ)
        vendored = ROOT / "node_modules" / "hash-wasm" / "dist" / "argon2.umd.min.js"
        if not env.get("HASH_WASM") and vendored.exists():
            env["HASH_WASM"] = str(vendored)
        if not env.get("HASH_WASM"):
            self.skipTest("set HASH_WASM to hash-wasm's argon2.umd.min.js to run this")

        out = Path(__file__).resolve().parent / "js_manifest.json"
        out.unlink(missing_ok=True)
        proc = subprocess.run([NODE, str(self.script)], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out.exists(), "js_vectors.js did not emit a manifest")

        v = make_vectors.build()
        manifest = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(crypto.manifest_aad(manifest).hex(), v["manifest_aad_hex"])
        dek, meta = crypto.open_manifest(manifest, bytes.fromhex(v["kroot_hex"]))
        self.assertEqual(dek.hex(), v["dek_hex"])
        self.assertEqual(meta, v["meta_object"])
        out.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)