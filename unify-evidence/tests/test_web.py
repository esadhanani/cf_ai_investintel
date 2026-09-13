import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from unify.store import Store
from unify.web import handler_for


class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "test.db")
        self.document = self.store.ingest("alpha", "note.txt", "Transformer <script>alert(1)</script>")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, path, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def test_search_api_and_security_headers(self):
        code, headers, body = self.request("/api/search?tenant=alpha&q=transformer")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["results"][0]["source"], "note.txt")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_document_api_cannot_cross_workspace(self):
        code, _, _ = self.request("/api/document?tenant=beta&id=" + self.document["document_id"])
        self.assertEqual(code, 404)

    def test_bad_query_returns_400(self):
        self.assertEqual(self.request("/api/search?tenant=../alpha&q=test")[0], 400)

    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(self.request("/api/documents?tenant=alpha", headers={"Host": "untrusted.example"})[0], 403)

    def test_arbitrary_paths_are_not_served(self):
        self.assertEqual(self.request("/../unify.db")[0], 404)

    def test_write_methods_not_supported(self):
        self.assertEqual(self.request("/api/documents", method="POST")[0], 501)

    def test_ui_uses_text_nodes_for_sources(self):
        code, _, html = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("Unify Evidence", html)
        _, _, script = self.request("/app.js")
        self.assertIn("excerpt.textContent = result.content", script)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()
