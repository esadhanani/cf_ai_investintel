"""Loopback-only server with explicit demo or optional role-enforced access."""

from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .core import DomainError
from .auth import AuthError
from .planner import PlannerError, plan_case
from .ingestion import import_orders, reconcile_refunds
from .operations import get_import, list_imports, list_refunds

MAX_BODY = 16_384
MAX_CSV_BODY = 3 * 1024 * 1024
CSV_ROUTES = frozenset({"/api/import-orders", "/api/reconcile-refunds"})
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


def make_server(store, port: int = 8765, auth=None) -> ThreadingHTTPServer:
    """Return a bound server; caller starts serve_forever and closes it."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.store = store
    server.auth = auth
    server.csrf_token = secrets.token_urlsafe(32)
    return server


class Handler(BaseHTTPRequestHandler):
    server_version = "Casework"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Do not emit case IDs, customer text, or request bodies to access logs.
        return

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, allow_nan=False).encode(), "application/json; charset=utf-8")

    def _error(self, status: int, message: str, code: str = "invalid_request") -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def _safe_request(self) -> bool:
        port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed_hosts:
            self._error(403, "This demonstration accepts local requests only.", "invalid_host")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in allowed_hosts}:
            self._error(403, "The request came from another website.", "invalid_origin")
            return False
        return True

    def _principal(self):
        if self.server.auth is None:
            return None
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            self._error(401, "Sign in with your access token.", "auth_required")
            return False
        try:
            return self.server.auth.authenticate(value[7:])
        except AuthError:
            self._error(401, "The access token is invalid or no longer active.", "invalid_token")
            return False

    def _tenant(self, supplied, principal):
        if principal is not None:
            if supplied not in (None, "", principal.tenant):
                self._error(403, "Your access is limited to your assigned workspace.", "tenant_forbidden")
                return None
            return principal.tenant
        if not isinstance(supplied, str) or not supplied:
            self._error(400, "Choose a demo workspace.")
            return None
        return supplied

    def do_GET(self) -> None:
        if not self._safe_request():
            return
        parsed = urlsplit(self.path)
        if parsed.path in ASSETS:
            filename, content_type = ASSETS[parsed.path]
            self._send(200, Path(__file__).with_name(filename).read_bytes(), content_type)
            return
        if parsed.path == "/api/bootstrap":
            if self.server.auth is not None and not self.headers.get("Authorization"):
                self._json(200, {"auth_required": True, "auth_mode": "roles"})
                return
            principal = self._principal()
            if principal is False:
                return
            if principal is not None:
                self._json(200, {"auth_required": False, "auth_mode": "roles",
                    "csrf_token": self.server.csrf_token,
                    "principal": {"subject": principal.subject, "tenant": principal.tenant, "role": principal.role},
                    "tenants": [{"id": principal.tenant, "name": principal.tenant}]})
                return
            self._json(200, {"csrf_token": self.server.csrf_token,
                             "auth_mode": "demo", "auth_required": False,
                             "tenants": [{"id": "demo-shop", "name": "Fern & Field"},
                                         {"id": "other-shop", "name": "North House"}]})
            return
        query = parse_qs(parsed.query)
        principal = self._principal()
        if principal is False:
            return
        tenant = self._tenant(query.get("tenant", [None])[0], principal)
        if tenant is None:
            return
        try:
            if parsed.path == "/api/cases":
                self._json(200, {"cases": self.server.store.list_cases(tenant)})
            elif parsed.path == "/api/imports":
                self._json(200, {"batches": list_imports(self.server.store, tenant)})
            elif parsed.path.startswith("/api/imports/"):
                batch_key = unquote(parsed.path[len("/api/imports/"):])
                self._json(200, get_import(self.server.store, tenant, batch_key))
            elif parsed.path == "/api/refunds":
                self._json(200, {"refunds": list_refunds(self.server.store, tenant)})
            elif parsed.path.startswith("/api/cases/"):
                case_id = unquote(parsed.path[len("/api/cases/"):])
                case = self.server.store.get_case(tenant, case_id)
                audit = self.server.store.trace(tenant, case_id)
                policy = self.server.store.get_policy(tenant) if hasattr(self.server.store, "get_policy") else {}
                self._json(200, {"case": case, "audit": audit, "policy": policy})
            else:
                self._error(404, "This page does not exist.", "not_found")
        except DomainError as exc:
            self._error(exc.status, exc.message, exc.code)
        except Exception:
            self._error(500, "The record could not be loaded.", "server_error")

    def do_POST(self) -> None:
        if not self._safe_request():
            return
        path = urlsplit(self.path).path
        principal = self._principal()
        if principal is False:
            return
        if principal is not None:
            permissions = {"/api/suggest": "operator", "/api/propose": "operator",
                           "/api/execute": "operator", "/api/approve": "reviewer",
                           "/api/import-orders": "operator"}
            required_role = permissions.get(path)
            if required_role is not None and principal.role != required_role:
                self._error(403, "Your role cannot perform this action.", "role_forbidden")
                return
        token = self.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(token.encode("utf-8"), self.server.csrf_token.encode("ascii")):
            self._error(403, "Reload the workspace before taking an action.", "missing_csrf")
            return
        if self.headers.get("Transfer-Encoding"):
            self._error(400, "Chunked request bodies are not accepted.")
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            self._error(415, "Actions require a JSON request.")
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(411, "The request length is required.")
            return
        body_limit = MAX_CSV_BODY if path in CSV_ROUTES else MAX_BODY
        if not 0 < size <= body_limit:
            self._error(413, "The action request is too large or empty.")
            return
        self.connection.settimeout(5)
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("incomplete body")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("object required")
            case_id = payload.get("case_id")
            if path not in CSV_ROUTES and (not isinstance(case_id, str) or not case_id):
                raise ValueError("case required")
        except (ValueError, UnicodeDecodeError, TimeoutError):
            self._error(400, "The action request is incomplete or malformed.")
            return
        if principal is not None and {"actor", "role", "subject", "principal"}.intersection(payload):
            self._error(403, "Identity and permissions come from your access token.", "identity_override")
            return
        tenant = self._tenant(payload.get("tenant"), principal)
        if tenant is None:
            return
        try:
            if path in CSV_ROUTES:
                required = {"csv_text", "batch_key"} if path == "/api/import-orders" else {"csv_text"}
                if set(payload) not in (required, required | {"tenant"}):
                    self._error(400, "Provide only the CSV text, workspace and import batch key where required.")
                    return
                if path == "/api/import-orders":
                    result = import_orders(self.server.store, tenant, payload["csv_text"], payload["batch_key"])
                else:
                    result = reconcile_refunds(self.server.store, tenant, payload["csv_text"])
            elif path == "/api/suggest":
                if set(payload) not in ({"tenant", "case_id"}, {"case_id"}):
                    self._error(400, "A suggestion needs only the selected case and workspace.")
                    return
                result = plan_case(self.server.store, tenant, case_id, model="mistral:latest", timeout=45)
            elif path == "/api/propose":
                request = {k: v for k, v in payload.items() if k not in ("tenant", "case_id")}
                result = self.server.store.propose(tenant, case_id, request)
            elif path == "/api/approve":
                result = self.server.store.approve(tenant, case_id, payload.get("proposal_id"),
                                                  actor=principal.subject if principal is not None else "demo-reviewer")
            elif path == "/api/execute":
                result = self.server.store.execute(tenant, case_id, payload.get("proposal_id"), payload.get("idempotency_key"))
            else:
                self._error(404, "This action does not exist.", "not_found")
                return
            self._json(200, result)
        except DomainError as exc:
            self._error(exc.status, exc.message, exc.code)
        except PlannerError as exc:
            self._error(422, str(exc), "suggestion_unavailable")
        except Exception:
            self._error(500, "The action could not be completed.", "server_error")
