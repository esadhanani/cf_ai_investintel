import json
import tempfile
import unittest
from pathlib import Path

from casework.auth import Auth, AuthError, Principal, create_credentials, token_digest


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.principal = Principal("alice", "demo-shop", "operator")
        self.config, self.tokens = create_credentials([self.principal])
        self.token = self.tokens["alice"]

    def test_tokens_are_random_and_config_contains_hashes_only(self):
        _, second = create_credentials([self.principal])
        self.assertNotEqual(self.token, second["alice"])
        self.assertEqual(len(self.token), 43)
        self.assertNotIn(self.token, json.dumps(self.config))
        self.assertIn(token_digest(self.token), self.config["tokens"])
        self.assertEqual(Auth(self.config).authenticate(self.token), self.principal)

    def test_invalid_and_tampered_tokens_rejected(self):
        auth = Auth(self.config)
        for token in ["", "é", "x" * 43, self.token + "x", self.token[:-1]]:
            with self.subTest(token_length=len(token)), self.assertRaises(AuthError):
                auth.authenticate(token)

    def test_in_memory_revocation(self):
        auth = Auth(self.config)
        auth.revoke(self.token)
        with self.assertRaises(AuthError):
            auth.authenticate(self.token)

    def test_file_config_revocation_applies_without_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps(self.config))
            auth = Auth.from_file(path)
            self.assertEqual(auth.authenticate(self.token), self.principal)
            path.write_text(json.dumps({"version": 1, "tokens": {}}))
            with self.assertRaises(AuthError):
                auth.authenticate(self.token)

    def test_missing_or_corrupt_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps(self.config))
            auth = Auth.from_file(path)
            path.write_text("{")
            with self.assertRaises(AuthError):
                auth.authenticate(self.token)
            path.unlink()
            with self.assertRaises(AuthError):
                auth.authenticate(self.token)

    def test_plaintext_or_invalid_principal_config_rejected(self):
        with self.assertRaises(ValueError):
            Auth({"version": 1, "tokens": {self.token: {"subject": "a", "tenant": "demo-shop", "role": "operator"}}})
        for subject, tenant, role in [("", "t", "operator"), ("a", "../t", "reviewer"), ("a", "t", "admin"), ("a", "t", [])]:
            with self.assertRaises(ValueError):
                Principal(subject, tenant, role)

    def test_same_identity_cannot_receive_two_roles(self):
        with self.assertRaises(ValueError):
            create_credentials([self.principal, Principal("alice", "demo-shop", "reviewer")])
        config = json.loads(json.dumps(self.config))
        config["tokens"]["0" * 64] = {"subject": "alice", "tenant": "demo-shop", "role": "reviewer"}
        with self.assertRaises(ValueError):
            Auth(config)


if __name__ == "__main__":
    unittest.main()
