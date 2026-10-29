"""Boots the real app on a real socket and runs tests/js_e2e.js against it.

This is the only test that exercises gitdrive-crypto.js over real HTTP through the
production endpoints, rather than calling the crypto module directly.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import conftest

conftest.install()   # before anything imports api.main, or the real api/db.py blocks on Mongo

NODE = shutil.which("node")
ROOT = conftest.ROOT


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(NODE, "node is not installed")
class BrowserEndToEnd(unittest.TestCase):
    def test_browser_code_path_against_a_live_server(self):
        hash_wasm = os.environ.get("HASH_WASM") or (
            str(ROOT / "node_modules" / "hash-wasm" / "dist" / "argon2.umd.min.js")
            if (ROOT / "node_modules" / "hash-wasm" / "dist" / "argon2.umd.min.js").exists()
            else None)
        if not hash_wasm:
            self.skipTest("set HASH_WASM to hash-wasm's argon2.umd.min.js to run this")

        import uvicorn
        import api.main as main
        from conftest import FakeGitHub
        storage = FakeGitHub()
        main._github = storage

        port = free_port()
        config = uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        def stop():
            server.should_exit = True
            thread.join(timeout=10)

        self.addCleanup(stop)

        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if server.started:
                    break
            except Exception:
                pass
            time.sleep(0.05)
        self.assertTrue(server.started, "the app never finished starting")

        env = dict(os.environ, HASH_WASM=hash_wasm)
        proc = subprocess.run(
            [NODE, str(Path(__file__).resolve().parent / "js_e2e.js"), base],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
        print(proc.stdout)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")

        # and the server never saw any of it in the clear
        for blob in storage.blobs[storage.repos[0]].values():
            self.assertNotIn(b"browser-made.bin", blob)
            self.assertNotIn(b"the browser did this", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)