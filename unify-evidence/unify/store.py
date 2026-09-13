"""Storage and retrieval. Source documents are data, never executable instructions."""

import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

MAX_BYTES = 1_000_000
TENANT = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
SOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,119}\Z")


def validate_tenant(value):
    if not isinstance(value, str) or not TENANT.fullmatch(value):
        raise ValueError("Tenant must be 1-48 lowercase letters, digits, underscores or hyphens; start with a letter.")
    return value


def validate_source(value):
    if not isinstance(value, str) or not SOURCE.fullmatch(value) or ".." in value:
        raise ValueError("Source must be a plain filename without traversal or directory separators.")
    if Path(value).suffix.lower() not in {".txt", ".md", ".csv", ".json"}:
        raise ValueError("Supported file types: .txt, .md, .csv, .json.")
    return value


def normalize(source, content):
    """Produce deterministic text. Citation lines refer to this normalized view."""
    validate_source(source)
    if not isinstance(content, str):
        raise ValueError("Content must be a UTF-8 string.")
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Document exceeds the 1 MB limit.")
    if "\x00" in content or not content.strip():
        raise ValueError("Document is empty or contains null bytes.")
    suffix = Path(source).suffix.lower()
    if suffix == ".json":
        try:
            value = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON value")))
        except (ValueError, RecursionError) as exc:
            raise ValueError("Malformed JSON.") from exc
        if not isinstance(value, (dict, list)) or not value:
            raise ValueError("JSON must contain a nonempty object or array.")
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    elif suffix == ".csv":
        try:
            rows = list(csv.reader(io.StringIO(content), strict=True))
        except csv.Error as exc:
            raise ValueError("Malformed CSV.") from exc
        if len(rows) < 2 or not rows[0] or any(not h.strip() for h in rows[0]):
            raise ValueError("CSV requires headers and at least one data row.")
        if len(set(rows[0])) != len(rows[0]) or any(len(row) != len(rows[0]) for row in rows[1:]):
            raise ValueError("CSV headers must be unique and rows must have equal width.")
        content = "\n".join(" | ".join(f"{key.strip()}: {value.strip()}" for key, value in zip(rows[0], row)) for row in rows[1:])
    result = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not result:
        raise ValueError("Document contains no text.")
    return result


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, source TEXT NOT NULL,
    version INTEGER NOT NULL, content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL, UNIQUE(tenant, source)
);
CREATE TABLE IF NOT EXISTS revisions (
    document_id TEXT NOT NULL REFERENCES documents(id), version INTEGER NOT NULL,
    content TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(document_id, version)
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
    tenant TEXT NOT NULL, version INTEGER NOT NULL,
    line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, content TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(content, tokenize='unicode61');
CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS documents_tenant ON documents(tenant);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            with con:
                yield con
        finally:
            con.close()

    def ingest(self, tenant, source, content):
        validate_tenant(tenant)
        text = normalize(source, content)
        document_id = hashlib.sha256(f"{tenant}\0{source}".encode()).hexdigest()[:24]
        digest = hashlib.sha256(text.encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if old and old["content_hash"] == digest:
                return {"document_id": document_id, "version": old["version"], "status": "unchanged"}
            version = old["version"] + 1 if old else 1
            con.execute("""INSERT INTO documents VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET version=excluded.version,
                content_hash=excluded.content_hash, updated_at=excluded.updated_at""",
                (document_id, tenant, source, version, digest, now))
            con.execute("INSERT INTO revisions VALUES (?,?,?,?,?)", (document_id, version, text, digest, now))
            con.execute("DELETE FROM search_index WHERE rowid IN (SELECT id FROM chunks WHERE document_id=?)", (document_id,))
            con.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            # Fixed groups preserve normalized line ranges and bound excerpt size.
            lines = text.splitlines()
            for start in range(0, len(lines), 4):
                block = "\n".join(lines[start:start + 4])
                if not block.strip():
                    continue
                row = con.execute("INSERT INTO chunks(document_id,tenant,version,line_start,line_end,content) VALUES (?,?,?,?,?,?)",
                    (document_id, tenant, version, start + 1, min(start + 4, len(lines)), block))
                con.execute("INSERT INTO search_index(rowid,content) VALUES (?,?)", (row.lastrowid, block))
        return {"document_id": document_id, "version": version, "status": "updated" if old else "created"}

    def search(self, tenant, query, limit=5):
        validate_tenant(tenant)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("Query must contain 1-500 characters.")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Limit must be an integer between 1 and 20.")
        # Quote terms: caller input cannot become an FTS operator or SQL expression.
        terms = list(dict.fromkeys(re.findall(r"[^\W_]+", query.lower(), re.UNICODE)))
        if not terms:
            raise ValueError("Query must contain a word or number.")
        expression = " OR ".join('"' + term + '"' for term in terms)
        with self.connect() as con:
            rows = con.execute("""SELECT d.id AS document_id,d.source,d.content_hash,
                c.version,c.line_start,c.line_end,c.content,bm25(search_index) AS score
                FROM search_index JOIN chunks c ON c.id=search_index.rowid
                JOIN documents d ON d.id=c.document_id
                WHERE search_index MATCH ? AND c.tenant=? AND d.tenant=?
                ORDER BY score,d.source,c.line_start LIMIT ?""", (expression, tenant, tenant, limit)).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["citation"] = f"{item['source']}@v{item['version']}:L{item['line_start']}-L{item['line_end']}"
            results.append(item)
        return {"tenant": tenant, "query": query, "status": "evidence_found" if results else "no_evidence", "results": results}

    def documents(self, tenant):
        validate_tenant(tenant)
        with self.connect() as con:
            return [dict(row) for row in con.execute("SELECT * FROM documents WHERE tenant=? ORDER BY source", (tenant,))]

    def document(self, tenant, document_id, version=None):
        validate_tenant(tenant)
        if not isinstance(document_id, str) or not re.fullmatch(r"[a-f0-9]{24}", document_id):
            raise ValueError("Invalid document ID.")
        if version is not None and (type(version) is not int or version < 1):
            raise ValueError("Version must be a positive integer.")
        with self.connect() as con:
            row = con.execute("""SELECT d.id AS document_id,d.tenant,d.source,r.version,r.content,r.content_hash,r.created_at
                FROM documents d JOIN revisions r ON r.document_id=d.id
                WHERE d.tenant=? AND d.id=? AND r.version=COALESCE(?,d.version)""", (tenant, document_id, version)).fetchone()
        return dict(row) if row else None


def seed(store, fixture_root):
    outcomes = []
    for tenant in ("investment", "public-service"):
        directory = Path(fixture_root) / tenant
        for file in sorted(directory.iterdir()):
            if file.is_file() and file.suffix in {".txt", ".md", ".csv", ".json"}:
                outcomes.append(store.ingest(tenant, file.name, file.read_text(encoding="utf-8")))
    return outcomes
