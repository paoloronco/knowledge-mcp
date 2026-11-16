"""Regression coverage for best-effort credential redaction."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "knowledge-mcp"
sys.path.insert(0, str(ROOT / "mcp"))
from redaction import redact_secrets


class RedactionHardeningTest(unittest.TestCase):
    def test_inline_json_and_config_credentials_are_redacted(self):
        for text in (
            '{"api_key": "hunter2supersecretvalue"}',
            "api_key = hunter2supersecretvalue",
            '"client_secret": "hunter2supersecretvalue"',
            "the token=hunter2supersecretvalue here",
        ):
            self.assertNotIn("hunter2supersecretvalue", redact_secrets(text), text)
            self.assertIn("[REDACTED]", redact_secrets(text), text)

    def test_url_userinfo_is_fully_removed_for_common_schemes(self):
        for scheme in (
            "https", "mongodb+srv", "postgresql", "redis", "amqp", "ftp", "sftp", "ldaps"
        ):
            text = f"{scheme}://admin:s3cr3tvalue@db.example.com/data"
            cleaned = redact_secrets(text)
            self.assertNotIn("s3cr3tvalue", cleaned, scheme)
            self.assertNotIn("admin", cleaned, scheme)
            self.assertIn("[REDACTED]@", cleaned, scheme)

    def test_benign_identifiers_and_ports_are_not_over_redacted(self):
        self.assertEqual(redact_secrets("token_counter = 5"), "token_counter = 5")
        self.assertEqual(redact_secrets("port: 8443"), "port: 8443")
        self.assertEqual(redact_secrets("VLAN 42 on port 8443"), "VLAN 42 on port 8443")

    def test_line_anchored_credentials_still_redact_and_are_idempotent(self):
        secret = "synthetic-value-" * 30
        once = redact_secrets("Service configuration\npassword: " + secret + "\nport: 8443")
        self.assertNotIn(secret[:30], once)
        self.assertEqual(redact_secrets(once), once)
        self.assertIn("[REDACTED]", redact_secrets("password: abc"))


if __name__ == "__main__":
    unittest.main()
