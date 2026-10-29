"""End-to-end tests for both encryption spaces.

Every private-space test asserts against what the fake GitHub actually stored, so "the
server only ever saw ciphertext" is checked rather than assumed.
"""

import hashlib
import json
import os
import unittest

import conftest
from api import crypto

PASSWORD = "a long enough passphrase with spaces"

# The production cost (64 MiB, t=3) is deliberately not used here: the suite derives keys
# dozens of times and would take minutes. This is exactly the documented floor that
# crypto.check_kdf_params enforces, so the tests still exercise the real code path.
TEST_KDF = {"alg": "argon2id", "m_kib": crypto.ARGON2_MIN_M_KIB,
            "t": crypto.ARGON2_MIN_T, "p": 1, "len": 32}


class Base(unittest.TestCase):
    def setUp(self):
        self.client = conftest.reset()
        self.db = conftest.db()
        self.gh = self.client.github

    def tearDown(self):
        self.client.__exit__(None, None, None)

    # --- helpers ----------------------------------------------------------
    def register(self, username="alice", password=PASSWORD):
        """What the browser does: derive locally, send only a verifier."""
        salt = os.urandom(16).hex()
        k_root = crypto.derive_kroot(password, bytes.fromhex(salt),
                                     TEST_KDF["m_kib"], TEST_KDF["t"], TEST_KDF["p"])
        r = self.client.post("/auth/register", data={
            "username": username,
            "verifier": crypto.auth_verifier(k_root),
            "kdf_salt": salt,
        })
        self.assertEqual(r.status_code, 200, r.text)
        return {"headers": {"Authorization": f"Bearer {r.json()['token']}"},
                "k_root": k_root, "salt": salt}

    def upload(self, auth, data, visibility, filename="secret.txt", relative_path="",
               chunk_size=10 * 1024 * 1024):
        total = max(1, -(-len(data) // chunk_size))
        file_id = os.urandom(16).hex()
        upload_id = os.urandom(16).hex()
        dek = os.urandom(crypto.KEY_LEN)
        root = auth["k_root"] if visibility == "private" else crypto.server_master_key()
        kdf = ({**TEST_KDF, "salt": auth["salt"]} if visibility == "private"
               else {"alg": "server", "salt": ""})
        meta_chunks = []

        for i in range(total):
            plain = data[i * chunk_size:(i + 1) * chunk_size]
            fields = {"upload_id": upload_id, "chunk_index": i, "total_chunks": total,
                      "file_id": file_id, "size_bytes": len(data), "visibility": visibility}
            if visibility == "private":
                blob = crypto.seal_chunk(file_id, crypto.MODE_PRIVATE, i, total, len(plain),
                                         crypto.chunk_key(dek, i, total), plain)
                sha = hashlib.sha256(blob).hexdigest()
                meta_chunks.append({"index": i, "sha256": sha, "size": len(plain)})
                fields.update(blob_sha=sha, plain_len=len(plain))
                payload = blob
            else:
                sha = None
                payload = plain
                fields.update(filename=filename, relative_path=relative_path,
                              mime_type="text/plain")
            if i == total - 1 and visibility == "private":
                manifest, _ = crypto.build_manifest(
                    mode=crypto.MODE_PRIVATE, file_id=file_id, chunk_size=chunk_size,
                    chunk_count=total, plain_size=len(data), kdf=kdf, dek=dek,
                    kek=crypto.file_kek(root, file_id),
                    meta={"name": filename, "path": relative_path, "mime": "text/plain",
                          "chunks": meta_chunks,
                          "chain": crypto.chain_digest([c["sha256"] for c in meta_chunks])})
                fields["manifest"] = json.dumps(manifest)
            r = self.client.post("/upload", files={"chunk": ("f", payload)},
                                 data=fields, headers=auth["headers"])
            self.assertEqual(r.status_code, 200, r.text)

        status = {}
        for _ in range(100):
            status = self.client.get(f"/upload/status/{upload_id}", headers=auth["headers"]).json()
            if status["status"] in ("done", "duplicate", "error"):
                break
        self.assertIn(status["status"], ("done", "duplicate"), status)
        return {"file_id": file_id, "dek": dek, "repo": next(iter(self.gh.repos)),
                "total": total, "chunks": meta_chunks}

    def offline_decrypt(self, file_id, password, salt):
        """The future CLI: GitHub's contents plus the password, and nothing else."""
        repo = next(iter(self.gh.repos))
        blobs = self.gh.blobs[repo]
        manifest = json.loads(blobs[f"manifests/{file_id}/manifest.json"])
        dek, meta = crypto.open_manifest(
            manifest, crypto.derive_kroot(password, bytes.fromhex(salt),
                                          TEST_KDF["m_kib"], TEST_KDF["t"], TEST_KDF["p"]))
        ordered = sorted(meta["chunks"], key=lambda c: c["index"])
        plain = b""
        for c in ordered:
            blob = blobs[f"objects/{c['sha256'][:2]}/{c['sha256'][2:]}.chunk"]
            plain += crypto.open_chunk(file_id, blob,
                                       crypto.chunk_key(dek, c["index"], len(ordered)),
                                       expect_index=c["index"], expect_total=len(ordered))[1]
        self.assertEqual(crypto.chain_digest([c["sha256"] for c in ordered]), meta["chain"])
        self.assertEqual(len(plain), manifest["plain_size"])
        return manifest, meta, plain


class PrivateSpace(Base):
    def test_only_ciphertext_reaches_github(self):
        auth = self.register()
        data = b"the quick brown fox" * 5000
        info = self.upload(auth, data, "private", filename="secret.txt")
        blobs = self.gh.objects(info["repo"])

        self.assertEqual(len(blobs), 1)
        blob = next(iter(blobs.values()))
        self.assertNotIn(data, blob)
        self.assertNotIn(b"secret.txt", blob)
        self.assertNotIn(data[:64], blob)
        self.assertNotIn(b"quick brown", blob)
        # neither does the server's own index
        record = self.db.files[info["file_id"]]
        self.assertEqual(record["original_filename"], "")
        self.assertEqual(record["relative_path"], "")
        self.assertEqual(record["mode"], "private")

    def test_password_and_keys_never_reach_the_server(self):
        auth = self.register()
        self.upload(auth, b"x" * 5000, "private")
        dumped = json.dumps(self.db.users, default=str) + json.dumps(self.db.files, default=str)
        self.assertNotIn(PASSWORD, dumped)
        self.assertNotIn(auth["k_root"].hex(), dumped)
        user = self.db.users["alice"]
        self.assertIn("verifier_hash", user)
        for gone in ("password_hash", "encryption_id"):
            self.assertNotIn(gone, user)

    def test_download_round_trip(self):
        auth = self.register()
        data = b"payload bytes " * 40000
        info = self.upload(auth, data, "private", filename="notes.txt")

        r = self.client.get(f"/files/{info['file_id']}/manifest", headers=auth["headers"])
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("notes.txt", r.text)
        manifest = json.loads(r.text)

        dek, meta = crypto.open_manifest(manifest, auth["k_root"])
        self.assertEqual(meta["name"], "notes.txt")
        out = b""
        for c in sorted(meta["chunks"], key=lambda x: x["index"]):
            resp = self.client.get(f"/files/{info['file_id']}/chunk/{c['index']}",
                                   headers=auth["headers"])
            self.assertEqual(resp.status_code, 200)
            out += crypto.open_chunk(info["file_id"], resp.content,
                                     crypto.chunk_key(dek, c["index"], manifest["chunk_count"]),
                                     expect_index=c["index"],
                                     expect_total=manifest["chunk_count"])[1]
        self.assertEqual(out, data)

    def test_wrong_password_fails_cleanly(self):
        auth = self.register()
        info = self.upload(auth, b"secret payload" * 100, "private")
        manifest = json.loads(self.client.get(f"/files/{info['file_id']}/manifest",
                                              headers=auth["headers"]).text)
        with self.assertRaises(crypto.CryptoError):
            crypto.open_manifest(manifest,
                                 crypto.derive_kroot("not it", bytes.fromhex(auth["salt"]),
                                                     TEST_KDF["m_kib"], TEST_KDF["t"],
                                                     TEST_KDF["p"]))

    def test_offline_decrypt_from_github_only(self):
        auth = self.register()
        data = b"offline readable" * 3000
        info = self.upload(auth, data, "private", filename="diary.txt")
        manifest, meta, plain = self.offline_decrypt(info["file_id"], PASSWORD, auth["salt"])
        self.assertEqual(plain, data)
        self.assertEqual(meta["name"], "diary.txt")
        self.assertEqual(manifest["mode"], "private")

    def test_raw_github_object_is_unusable_alone(self):
        auth = self.register()
        info = self.upload(auth, b"top secret contents" * 100, "private", filename="keys.txt")
        blob = next(iter(self.gh.objects(info["repo"]).values()))
        with self.assertRaises(crypto.CryptoError):
            crypto.open_chunk(info["file_id"], blob, crypto.chunk_key(os.urandom(32), 0, 1))
        manifest = self.gh.manifests(info["repo"])[f"manifests/{info['file_id']}/manifest.json"]
        self.assertNotIn(b"keys.txt", manifest)
        self.assertNotIn(b"secret contents", manifest)

    def test_server_refuses_to_stream_private_files(self):
        auth = self.register()
        info = self.upload(auth, b"nope" * 100, "private")
        r = self.client.get(f"/download/{info['file_id']}", headers=auth["headers"])
        self.assertEqual(r.status_code, 400)

    def test_another_user_cannot_fetch_the_ciphertext(self):
        auth = self.register()
        info = self.upload(auth, b"mine" * 100, "private")
        other = self.register("bob")
        self.assertEqual(self.client.get(f"/files/{info['file_id']}/manifest",
                                          headers=other["headers"]).status_code, 403)
        self.assertEqual(self.client.get(f"/files/{info['file_id']}/chunk/0",
                                          headers=other["headers"]).status_code, 403)

    def test_many_chunks_reassemble_in_order(self):
        auth = self.register()
        data = bytes(range(256)) * 20000          # ~5.1 MB
        info = self.upload(auth, data, "private", chunk_size=1_000_000)
        self.assertEqual(info["total"], 6)
        _, _, plain = self.offline_decrypt(info["file_id"], PASSWORD, auth["salt"])
        self.assertEqual(plain, data)

    def test_share_link_plus_password_is_enough(self):
        auth = self.register()
        data = b"shared privately" * 500
        info = self.upload(auth, data, "private", filename="shared.txt")
        token = self.client.post(f"/files/{info['file_id']}/share",
                                 headers=auth["headers"]).json()["token"]
        access = self.client.post(f"/share/{token}/access", headers=auth["headers"]).json()
        ticket = access["url"].rsplit("/", 1)[-1]
        # a visitor with the link but not a GitDrive account
        r = self.client.get(f"/files/{info['file_id']}/manifest?t={ticket}")
        self.assertEqual(r.status_code, 200)
        manifest = json.loads(r.text)
        _, meta = crypto.open_manifest(manifest, auth["k_root"])
        self.assertEqual(meta["name"], "shared.txt")
        self.assertEqual(self.client.get(
            f"/files/{info['file_id']}/chunk/0?t={ticket}").status_code, 200)


class PublicSpace(Base):
    def test_server_encrypts_before_storing(self):
        auth = self.register()
        data = b"public bytes " * 5000
        info = self.upload(auth, data, "public", filename="open.txt")
        blob = next(iter(self.gh.objects(info["repo"]).values()))
        self.assertNotIn(data, blob)
        self.assertNotIn(b"open.txt", blob)
        record = self.db.files[info["file_id"]]
        self.assertEqual(record["original_filename"], "open.txt")
        self.assertEqual(record["mode"], "public")

    def test_download_returns_plaintext(self):
        auth = self.register()
        data = b"public bytes " * 5000
        info = self.upload(auth, data, "public", filename="open.txt")
        r = self.client.get(f"/download/{info['file_id']}", headers=auth["headers"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, data)

    def test_raw_object_is_not_decryptable_by_a_signed_in_user(self):
        auth = self.register()
        info = self.upload(auth, b"public secret" * 500, "public")
        manifest = json.loads(self.gh.manifests(info["repo"])[f"manifests/{info['file_id']}/manifest.json"])
        # The user knows their own password and still gets nowhere: public files descend
        # from GITDRIVE_MASTER_KEY, not from K_root.
        with self.assertRaises(crypto.CryptoError):
            crypto.open_manifest(manifest, auth["k_root"])

    def test_raw_object_is_not_decryptable_offline_with_the_password(self):
        auth = self.register()
        self.upload(auth, b"public secret" * 500, "public")
        with self.assertRaises(crypto.CryptoError):
            self.offline_decrypt(self.db.files and next(f for f in self.db.files),
                                 PASSWORD, auth["salt"])

    def test_rotating_the_master_key_breaks_public_data(self):
        auth = self.register()
        info = self.upload(auth, b"rotate me" * 100, "public")
        manifest = self.db.files[info["file_id"]]["manifest"]
        saved = os.environ["GITDRIVE_MASTER_KEY"]
        try:
            os.environ["GITDRIVE_MASTER_KEY"] = "b" * 64
            with self.assertRaises(crypto.CryptoError):
                crypto.unwrap_dek(crypto.file_kek(crypto.server_master_key(), info["file_id"]),
                                 manifest["key_wrap"], crypto.manifest_aad(manifest))
        finally:
            os.environ["GITDRIVE_MASTER_KEY"] = saved


class Integrity(Base):
    def _tamper_object(self, visibility):
        auth = self.register()
        info = self.upload(auth, b"tamper target " * 1000, visibility)
        sha = next(iter(self.gh.objects(info["repo"])))
        blob = bytearray(self.gh.objects(info["repo"])[sha])
        blob[len(blob) // 2] ^= 0x01
        self.gh.blobs[info["repo"]][sha] = bytes(blob)
        return auth, info

    def test_tampered_public_chunk_fails_the_download(self):
        auth, info = self._tamper_object("public")
        r = self.client.get(f"/download/{info['file_id']}", headers=auth["headers"])
        self.assertEqual(r.status_code, 502)
        self.assertNotIn(b"tamper target", r.content)

    def test_tampered_private_chunk_fails_in_the_browser(self):
        auth, info = self._tamper_object("private")
        with self.assertRaises(crypto.CryptoError):
            self.offline_decrypt(info["file_id"], PASSWORD, auth["salt"])

    def test_tampered_manifest_fails(self):
        auth = self.register()
        info = self.upload(auth, b"hello there" * 100, "private")
        path = f"manifests/{info['file_id']}/manifest.json"
        manifest = json.loads(self.gh.blobs[info["repo"]][path])
        manifest["plain_size"] = 999999
        self.gh.blobs[info["repo"]][path] = json.dumps(manifest).encode()
        with self.assertRaises(crypto.CryptoError):
            self.offline_decrypt(info["file_id"], PASSWORD, auth["salt"])

    def test_downgraded_kdf_is_refused(self):
        auth = self.register()
        info = self.upload(auth, b"downgrade attempt " * 100, "private")
        path = f"manifests/{info['file_id']}/manifest.json"
        manifest = json.loads(self.gh.blobs[info["repo"]][path])
        manifest["kdf"] = {**manifest["kdf"], "m_kib": 8, "t": 1}
        self.gh.blobs[info["repo"]][path] = json.dumps(manifest).encode()
        with self.assertRaises(crypto.CryptoError):
            self.offline_decrypt(info["file_id"], PASSWORD, auth["salt"])

    def test_nonces_are_never_reused(self):
        auth = self.register()
        info = self.upload(auth, b"same" * 5000, "public", chunk_size=1000)
        nonces = [b[17:29] for b in self.gh.objects(info["repo"]).values()]
        self.assertEqual(len(nonces), 20)          # 20000 bytes in 1000-byte chunks
        self.assertEqual(len(set(nonces)), 20)

    def test_identical_uploads_produce_distinct_blobs(self):
        auth = self.register()
        data = b"identical" * 1000
        self.upload(auth, data, "public")
        self.upload(auth, data, "public")
        blobs = list(self.gh.objects("dotstore-1").values())
        self.assertEqual(len(blobs), 2)
        self.assertNotEqual(blobs[0], blobs[1])


class ServerSecrets(Base):
    def test_github_token_never_affects_any_key(self):
        first = crypto.server_master_key()
        saved = os.environ["GITHUB_TOKEN"]
        try:
            os.environ["GITHUB_TOKEN"] = "ghp_a_completely_different_token"
            self.assertEqual(crypto.server_master_key(), first)
        finally:
            os.environ["GITHUB_TOKEN"] = saved

    def test_no_legacy_key_derivation_survives(self):
        main_src = (conftest.ROOT / "api" / "main.py").read_text(encoding="utf-8")
        crypto_src = (conftest.ROOT / "api" / "crypto.py").read_text(encoding="utf-8")
        for gone in ("_aes_key", "_encryption_key", "_decrypt_keys", "encryption_id"):
            self.assertNotIn(gone, main_src + crypto_src)
        self.assertNotIn('os.environ["GITHUB_TOKEN"]', crypto_src)
        self.assertNotIn('os.getenv("GITHUB_TOKEN")', crypto_src)
        # the only remaining use in main.py is handing the token to the GitHub API client
        for line in main_src.splitlines():
            if "GITHUB_TOKEN" in line and "GitHubClient" not in line and not line.lstrip().startswith("#"):
                self.fail(f"GITHUB_TOKEN used outside the API client: {line}")

    def test_public_keys_come_only_from_the_master_key(self):
        import api.main as main
        self.assertEqual(main._MASTER_KEY, crypto.server_master_key())
        self.assertNotEqual(
            main._MASTER_KEY,
            hashlib.sha256(os.environ["GITHUB_TOKEN"].encode()).digest())

    def test_master_key_must_be_configured(self):
        saved = os.environ["GITDRIVE_MASTER_KEY"]
        try:
            os.environ.pop("GITDRIVE_MASTER_KEY")
            with self.assertRaises(crypto.CryptoError):
                crypto.server_master_key()
            os.environ["GITDRIVE_MASTER_KEY"] = "not hex"
            with self.assertRaises(crypto.CryptoError):
                crypto.server_master_key()
        finally:
            os.environ["GITDRIVE_MASTER_KEY"] = saved


class Schema(Base):
    def test_visibility_cannot_change_mid_upload(self):
        """Otherwise a file could start private and land under the server's public keys."""
        auth = self.register()
        file_id = os.urandom(16).hex()
        upload_id = os.urandom(16).hex()
        dek = os.urandom(crypto.KEY_LEN)
        blob = crypto.seal_chunk(file_id, crypto.MODE_PRIVATE, 0, 2, 10,
                                 crypto.chunk_key(dek, 0, 2), b"0123456789")
        fields = {"upload_id": upload_id, "chunk_index": 0, "total_chunks": 2,
                  "file_id": file_id, "size_bytes": 20, "visibility": "private",
                  "blob_sha": hashlib.sha256(blob).hexdigest(), "plain_len": 10}
        self.assertEqual(
            self.client.post("/upload", files={"chunk": ("f", blob)},
                             headers=auth["headers"], data=fields).status_code, 200)
        switch = dict(fields, chunk_index=1, visibility="public", filename="x.txt")
        self.assertEqual(
            self.client.post("/upload", files={"chunk": ("f", b"b" * 10)},
                             headers=auth["headers"], data=switch).status_code, 400)

    def test_private_chunk_must_look_like_a_gitdrive_chunk(self):
        auth = self.register()
        r = self.client.post("/upload", files={"chunk": ("f", b"not a chunk at all")}, headers=auth["headers"], data={
            "upload_id": os.urandom(16).hex(), "chunk_index": 0, "total_chunks": 1,
            "file_id": os.urandom(16).hex(), "size_bytes": 17, "visibility": "private",
            "blob_sha": hashlib.sha256(b"not a chunk at all").hexdigest(), "plain_len": 17})
        self.assertEqual(r.status_code, 400)

    def test_private_chunk_hash_must_match(self):
        auth = self.register()
        dek = os.urandom(crypto.KEY_LEN)
        file_id = os.urandom(16).hex()
        blob = crypto.seal_chunk(file_id, crypto.MODE_PRIVATE, 0, 1, 10,
                                 crypto.chunk_key(dek, 0, 1), b"0123456789")
        r = self.client.post("/upload", files={"chunk": ("f", blob)}, headers=auth["headers"], data={
            "upload_id": os.urandom(16).hex(), "chunk_index": 0, "total_chunks": 1,
            "file_id": file_id, "size_bytes": 10, "visibility": "private",
            "blob_sha": "00" * 32, "plain_len": 10})
        self.assertEqual(r.status_code, 400)

    def test_db_module_has_the_new_shape(self):
        real = (conftest.ROOT / "api" / "db.py").read_text(encoding="utf-8")
        for fn in ("create_user", "find_by_file_id", "upsert_file", "create_share",
                   "create_download_ticket", "list_visible_files"):
            self.assertIn(f"def {fn}(", real)
        for gone in ("encryption_id", "shared_with", "password_hash", "add_collaborator"):
            self.assertNotIn(gone, real)

    def test_fake_db_covers_everything_main_uses(self):
        import inspect
        import api.main as main
        used = {name for name in dir(main.db) if not name.startswith("_")}
        for fn in conftest.DB_METHODS:
            self.assertIn(fn, used, f"main.py no longer calls db.{fn}")
            self.assertTrue(hasattr(self.db, fn))

    def test_collaborators_are_gone(self):
        main_src = (conftest.ROOT / "api" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("collaborator", main_src)


if __name__ == "__main__":
    unittest.main(verbosity=2)