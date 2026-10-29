"""End-to-end smoke test: boots the real ASGI app and drives the same HTTP calls the
browser makes, then decrypts the results with the browser's own code path.

This is the "does the whole thing actually work together" test, on top of the focused
per-feature tests in test_gitdrive.py.
"""

import hashlib
import json
import os
import unittest

import conftest
from api import crypto

TEST_KDF = {"alg": "argon2id", "m_kib": crypto.ARGON2_MIN_M_KIB,
            "t": crypto.ARGON2_MIN_T, "p": 1, "len": 32}
PASSWORD = "smoke test passphrase"


class Smoke(unittest.TestCase):
    def setUp(self):
        self.client = conftest.reset()
        self.db = conftest.db()
        self.gh = self.client.github

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_full_journey_through_the_served_pages(self):
        client = self.client

        # 1. the pages and the shared crypto module are actually served
        self.assertIn(b"GDCrypto", client.get("/").content)
        self.assertEqual(client.get("/gitdrive-crypto.js").status_code, 200)
        self.assertIn(b"buildManifest", client.get("/gitdrive-crypto.js").content)

        # 2. register, the way the browser does
        salt = os.urandom(16).hex()
        root = crypto.derive_kroot(PASSWORD, bytes.fromhex(salt),
                                   TEST_KDF["m_kib"], TEST_KDF["t"], TEST_KDF["p"])
        r = client.post("/auth/register", data={
            "username": "smoketester", "verifier": crypto.auth_verifier(root), "kdf_salt": salt})
        self.assertEqual(r.status_code, 200)
        auth = {"Authorization": f"Bearer {r.json()['token']}"}

        # 3. the KDF endpoint hands the browser what it needs to re-derive later
        kdf = client.get("/auth/kdf?username=smoketester").json()
        self.assertEqual(kdf["salt"], salt)
        self.assertEqual(kdf["alg"], "argon2id")
        unknown = client.get("/auth/kdf?username=nobody-here").json()
        self.assertEqual(unknown["alg"], "argon2id")
        self.assertNotEqual(unknown["salt"], salt, "unknown users must not be distinguishable")

        # 4. sign in again with the same password and the same verifier
        again = client.post("/auth/login", data={
            "username": "smoketester", "verifier": crypto.auth_verifier(root)})
        self.assertEqual(again.status_code, 200)
        bad = client.post("/auth/login", data={
            "username": "smoketester",
            "verifier": crypto.auth_verifier(crypto.derive_kroot(
                "wrong", bytes.fromhex(salt), TEST_KDF["m_kib"], TEST_KDF["t"], TEST_KDF["p"]))})
        self.assertEqual(bad.status_code, 401)

        # 5. upload one private file through /upload exactly as index.html would
        payload = bytes(range(256)) * 400          # ~100 KB, one chunk
        file_id = os.urandom(16).hex()
        dek = os.urandom(crypto.KEY_LEN)
        key = crypto.chunk_key(dek, 0, 1)
        blob = crypto.seal_chunk(file_id, crypto.MODE_PRIVATE, 0, 1, len(payload), key, payload)
        sha = hashlib.sha256(blob).hexdigest()
        manifest, _ = crypto.build_manifest(
            mode=crypto.MODE_PRIVATE, file_id=file_id, chunk_size=10 * 1024 * 1024,
            chunk_count=1, plain_size=len(payload),
            kdf={**TEST_KDF, "salt": salt}, dek=dek, kek=crypto.file_kek(root, file_id),
            meta={"name": "report.pdf", "path": "", "mime": "application/pdf",
                  "chunks": [{"index": 0, "sha256": sha, "size": len(payload)}],
                  "chain": crypto.chain_digest([sha])})
        upload_id = os.urandom(16).hex()
        r = client.post("/upload", files={"chunk": ("f", blob)}, headers=auth, data={
            "upload_id": upload_id, "chunk_index": 0, "total_chunks": 1, "file_id": file_id,
            "size_bytes": len(payload), "visibility": "private", "blob_sha": sha,
            "plain_len": len(payload), "manifest": json.dumps(manifest)})
        self.assertEqual(r.status_code, 200)
        for _ in range(100):
            status = client.get(f"/upload/status/{upload_id}", headers=auth).json()
            if status["status"] in ("done", "duplicate", "error"):
                break
        self.assertEqual(status["status"], "done")

        # 6. GitHub holds the ciphertext and the manifest, and nothing readable
        repo = next(iter(self.gh.repos))
        stored = self.gh.blobs[repo]
        chunk_blob = stored[f"objects/{sha[:2]}/{sha[2:]}.chunk"]
        self.assertEqual(chunk_blob, blob)
        self.assertNotIn(b"report.pdf", stored[f"manifests/{file_id}/manifest.json"])
        self.assertNotIn(payload, chunk_blob)

        # 7. /files hides the name until the browser decrypts it
        listing = client.get("/files", headers=auth).json()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["file_id"], file_id)
        self.assertEqual(listing[0]["mode"], "private")
        self.assertEqual(listing[0]["original_filename"], "")

        # 8. the browser's download path: manifest -> chunk -> open
        got = client.get(f"/files/{file_id}/manifest", headers=auth)
        self.assertNotIn("report.pdf", got.text)
        got_dek, meta = crypto.open_manifest(json.loads(got.text), root)
        self.assertEqual(got_dek, dek)
        self.assertEqual(meta["name"], "report.pdf")
        chunk = client.get(f"/files/{file_id}/chunk/0", headers=auth)
        plain = crypto.open_chunk(file_id, chunk.content, crypto.chunk_key(dek, 0, 1),
                                  expect_index=0, expect_total=1)[1]
        self.assertEqual(plain, payload)

        # 9. a share link, then decrypting through the share page's endpoints
        token = client.post(f"/files/{file_id}/share", headers=auth).json()["token"]
        page = client.get(f"/share/{token}", headers=auth)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("report.pdf", page.text)
        page_text = page.text.replace('"isEncrypted":true', '"isEncrypted": true')
        self.assertIn('"isEncrypted": true', page_text)
        self.assertIn('"fileId"', page_text)
        access = client.post(f"/share/{token}/access", headers=auth).json()
        self.assertEqual(access["mode"], "private")
        ticket = access["url"].rsplit("/", 1)[-1]
        via_ticket = client.get(f"/files/{file_id}/manifest?t={ticket}")
        self.assertEqual(via_ticket.status_code, 200)
        self.assertEqual(crypto.open_manifest(via_ticket.json(), root)[1]["name"], "report.pdf")
        self.assertEqual(client.get(f"/files/{file_id}/chunk/0?t={ticket}").status_code, 200)

        # 10. a ticket for one file must not unlock another
        self.assertEqual(client.get(f"/files/{os.urandom(16).hex()}/manifest?t={ticket}").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)