"""Read-only local HTTP interface. Ingestion is deliberately a CLI operation."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def handler_for(store):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, body, content_type="application/json; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # Limit DNS rebinding exposure. This is not authentication.
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") not in {expected, f"localhost:{self.server.server_port}"}:
                self.respond(403, {"error": "Use the local 127.0.0.1 or localhost URL."})
                return
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            tenant = params.get("tenant", [""])[0]
            try:
                if parsed.path in {"/", "/app.js", "/style.css"}:
                    name, mime = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}[parsed.path]
                    self.respond(200, (Path(__file__).parent / name).read_text(), mime + "; charset=utf-8")
                elif parsed.path == "/api/search":
                    self.respond(200, store.search(tenant, params.get("q", [""])[0]))
                elif parsed.path == "/api/documents":
                    self.respond(200, store.documents(tenant))
                elif parsed.path == "/api/document":
                    version = int(params["version"][0]) if "version" in params else None
                    result = store.document(tenant, params.get("id", [""])[0], version)
                    self.respond(200 if result else 404, result or {"error": "Document not found."})
                else:
                    self.respond(404, {"error": "Not found."})
            except (ValueError, UnicodeError) as exc:
                self.respond(400, {"error": str(exc)})

        def log_message(self, format, *args):
            pass  # Do not put search terms in logs.

    return Handler


def serve(store, port):
    with ThreadingHTTPServer(("127.0.0.1", port), handler_for(store)) as server:
        print(f"Unify Evidence: http://127.0.0.1:{server.server_port} (local synthetic demo)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
