"""Local opaque credentials with server-controlled identities and roles.

Tokens contain 256 random bits. Configuration stores SHA-256 digests only.
This is a loopback prototype, not an identity provider or deployment guide.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

ROLES = frozenset({"operator", "reviewer", "auditor"})
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}")


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str
    role: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value)
               for value in (self.subject, self.tenant)):
            raise ValueError("Subject and tenant must be valid identifiers.")
        if not isinstance(self.role, str) or self.role not in ROLES:
            raise ValueError("Role must be operator, reviewer, or auditor.")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_credentials(principals: list[Principal]) -> tuple[dict, dict[str, str]]:
    """Return (hash-only config, one-time plaintext tokens keyed by subject).

    Caller must protect token delivery and write config to a local mode-0600
    file. Never commit either output. Each identity has exactly one role.
    """
    if not principals or any(not isinstance(p, Principal) for p in principals):
        raise ValueError("Provide at least one Principal.")
    if len({p.subject for p in principals}) != len(principals):
        raise ValueError("Subjects must be unique; one identity has one role.")
    config, tokens = {"version": 1, "tokens": {}}, {}
    for principal in principals:
        token = secrets.token_urlsafe(32)
        config["tokens"][token_digest(token)] = asdict(principal)
        tokens[principal.subject] = token
    return config, tokens


def _validate(config: dict) -> dict[str, Principal]:
    if not isinstance(config, dict) or set(config) != {"version", "tokens"} or type(config["version"]) is not int or config["version"] != 1:
        raise ValueError("Invalid credential configuration.")
    if not isinstance(config["tokens"], dict):
        raise ValueError("Invalid credential mapping.")
    result, subjects = {}, set()
    for digest, record in config["tokens"].items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Credentials must be stored as SHA-256 digests.")
        if not isinstance(record, dict) or set(record) != {"subject", "tenant", "role"}:
            raise ValueError("Invalid principal record.")
        principal = Principal(**record)
        if principal.subject in subjects:
            raise ValueError("Duplicate subject in credential configuration.")
        subjects.add(principal.subject)
        result[digest] = principal
    return result


class Auth:
    def __init__(self, config: dict):
        self._principals = _validate(config)
        self._path = None

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        instance = cls(cls._read(path))
        instance._path = path
        return instance

    @staticmethod
    def _read(path: Path):
        with path.open("rb") as handle:
            data = handle.read(65_537)
        if len(data) > 65_536:
            raise ValueError("Credential configuration exceeds size limit.")
        return json.loads(data)

    def authenticate(self, token: str) -> Principal:
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise AuthError("A valid access token is required.")
        if self._path is not None:
            try:
                principals = _validate(self._read(self._path))
            except (ValueError, OSError, TypeError) as exc:
                raise AuthError("Access configuration is unavailable.") from exc
        else:
            principals = self._principals
        principal = principals.get(token_digest(token))
        if principal is None:
            raise AuthError("A valid access token is required.")
        return principal

    def revoke(self, token: str) -> None:
        """Revoke an in-memory token; file-backed revocation edits local config."""
        if self._path is not None:
            raise ValueError("Remove the token digest from the local config file.")
        self._principals.pop(token_digest(token), None)
