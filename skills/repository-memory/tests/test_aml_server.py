from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from aml_server import AMLService, Handler


class AMLServerTest(unittest.TestCase):
    def test_add_search_and_user_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as data_home:
            previous = os.environ.get("XDG_DATA_HOME")
            os.environ["XDG_DATA_HOME"] = data_home
            try:
                service = AMLService("secret")
                handler = type("TestAMLHandler", (Handler,), {"service": service})
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    add_payload = {
                        "request_id": "eval:test:add-1",
                        "messages": [{"role": "system", "timestamp": 1704067200000, "content": "I prefer concise technical reports."}],
                        "user_id": "user-a",
                        "session_id": "session-a",
                    }
                    connection.request("POST", "/add", body=json.dumps(add_payload), headers={"Content-Type": "application/json", "X-Api-Key": "secret"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertTrue(json.loads(response.read())["success"])

                    search_payload = {"query": "What report style do I prefer?", "user_id": "user-a", "top_k": 100}
                    connection.request("POST", "/search", body=json.dumps(search_payload), headers={"Content-Type": "application/json", "X-Api-Key": "secret"})
                    response = connection.getresponse()
                    data = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertTrue(data["data"])
                    self.assertIn("concise technical reports", data["data"][0]["content"])
                    self.assertEqual(data["data"][0]["created_at"], "2024-01-01T00:00:00Z")

                    for request_id, timestamp, content in (
                        ("eval:test:add-old", 1704067200000, "The report archive was updated in January."),
                        ("eval:test:add-new", 1735689600000, "The report archive was updated in January and the latest status is green."),
                    ):
                        connection.request(
                            "POST",
                            "/add",
                            body=json.dumps({
                                "request_id": request_id,
                                "messages": [{"role": "tool", "timestamp": timestamp, "content": content}],
                                "user_id": "user-a",
                                "session_id": request_id,
                            }),
                            headers={"Content-Type": "application/json", "X-Api-Key": "secret"},
                        )
                        self.assertEqual(connection.getresponse().status, 200)

                    connection.request(
                        "POST",
                        "/search",
                        body=json.dumps({"query": "What is the latest report archive status?", "user_id": "user-a", "top_k": 1}),
                        headers={"Content-Type": "application/json", "X-Api-Key": "secret"},
                    )
                    response = connection.getresponse()
                    latest = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertIn("latest status is green", latest["data"][0]["content"])

                    other_user = {"query": "What report style do I prefer?", "user_id": "user-b", "top_k": 100}
                    connection.request("POST", "/search", body=json.dumps(other_user), headers={"Content-Type": "application/json", "X-Api-Key": "secret"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())["data"], [])

                    connection.request("POST", "/search", body=json.dumps(search_payload), headers={"Content-Type": "application/json"})
                    self.assertEqual(connection.getresponse().status, 401)
                    connection.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
            finally:
                if previous is None:
                    os.environ.pop("XDG_DATA_HOME", None)
                else:
                    os.environ["XDG_DATA_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
