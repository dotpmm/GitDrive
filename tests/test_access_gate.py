"""The API-key gate and the CORS allowlist: enforcement, exemptions and preflights.

The gate reads api.main._API_KEY per request, so one booted app covers both the open
and the locked configuration; the tests patch the module attribute for the latter.
"""

import unittest
from unittest import mock

import conftest

GATE_DETAIL = "A valid X-API-Key header is required"
TEST_ORIGIN = "https://frontend.test"


class AccessGate(unittest.TestCase):
    def setUp(self):
        # conftest.reset() must run first: it installs the api.db fake before api.main
        # is imported for real.
        self.client = conftest.reset()
        import api.main
        self.main = api.main

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_open_when_no_api_key_is_configured(self):
        r = self.client.get("/files")
        self.assertEqual(r.status_code, 401)
        self.assertNotEqual(r.json()["detail"], GATE_DETAIL)   # rejected by auth, not the gate

    def test_api_routes_reject_missing_and_wrong_keys(self):
        with mock.patch.object(self.main, "_API_KEY", "s3cret"):
            missing = self.client.get("/files")
            wrong = self.client.get("/files", headers={"X-API-Key": "nope"})
        for r in (missing, wrong):
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.json()["detail"], GATE_DETAIL)

    def test_correct_key_passes_the_gate(self):
        with mock.patch.object(self.main, "_API_KEY", "s3cret"):
            r = self.client.get("/auth/kdf?username=nobody", headers={"X-API-Key": "s3cret"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["alg"], "argon2id")

    def test_pages_share_links_and_ticket_reads_are_exempt(self):
        with mock.patch.object(self.main, "_API_KEY", "s3cret"):
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.get("/gitdrive-crypto.js").status_code, 200)
            # both reach their routes (404) instead of being stopped at the gate (401)
            self.assertEqual(self.client.post("/share/unknown-token/access").status_code, 404)
            self.assertEqual(self.client.get("/files/deadbeef/manifest").status_code, 404)
            self.assertEqual(self.client.get("/files/deadbeef/chunk/0").status_code, 404)

    def test_share_creation_is_still_gated(self):
        with mock.patch.object(self.main, "_API_KEY", "s3cret"):
            r = self.client.post("/files/deadbeef/share")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["detail"], GATE_DETAIL)

    def test_cors_preflight_only_for_registered_origins(self):
        preflight = {"Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "content-type, x-api-key"}
        allowed = self.client.options("/auth/login", headers={"Origin": TEST_ORIGIN, **preflight})
        refused = self.client.options("/auth/login", headers={"Origin": "https://evil.test", **preflight})
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers.get("access-control-allow-origin"), TEST_ORIGIN)
        self.assertIn("x-api-key", allowed.headers.get("access-control-allow-headers", "").lower())
        self.assertEqual(refused.status_code, 400)
        self.assertIsNone(refused.headers.get("access-control-allow-origin"))

    def test_cross_origin_reads_carry_no_allow_header_for_other_origins(self):
        r = self.client.get("/gitdrive-crypto.js", headers={"Origin": "https://evil.test"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.headers.get("access-control-allow-origin"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
