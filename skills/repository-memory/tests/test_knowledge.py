import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from knowledge import KnowledgeClient, _tracked_documents


class KnowledgeClientTests(unittest.TestCase):
    def test_unconfigured_service_is_explicitly_optional(self):
        report = KnowledgeClient({}).health()
        self.assertEqual(report["backend"], "tencentdb-memoryknowledge")
        self.assertFalse(report["configured"])
        self.assertFalse(report["reachable"])
        self.assertEqual(report["status"], "not_configured")

    def test_configured_identity_and_endpoint_are_discovered_without_secret(self):
        client = KnowledgeClient({
            "knowledge": {
                "endpoint": "http://127.0.0.1:8421/v3",
                "service_id": "svc",
                "team_id": "team",
                "user_id": "user",
            }
        })
        self.assertTrue(client.configured)
        self.assertEqual(client.endpoint, "http://127.0.0.1:8421")
        self.assertEqual(client.identity["service_id"], "svc")
        self.assertNotIn("api_key", client.health())

    def test_sync_input_filters_sensitive_and_operational_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "README.md").write_text("safe knowledge\n", encoding="utf-8")
            (root / "notes.md").write_text("safe note\n", encoding="utf-8")
            (root / ".env").write_text("TOKEN=should-not-enter\n", encoding="utf-8")
            (root / "output").mkdir()
            (root / "output" / "report.md").write_text("generated\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            paths = {str(path.relative_to(root)) for path in _tracked_documents(root)}
            self.assertEqual(paths, {"README.md", "notes.md"})


if __name__ == "__main__":
    unittest.main()
