"""Read-only views of persisted imports and the refund ledger.

These helpers never create import tables. Import history is empty until an
actual import creates it; looking at history cannot initialize import state.
"""

import json

from .core import fail, identifier


def _has_imports(connection):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='import_batches'"
    ).fetchone() is not None


def list_imports(store, tenant):
    identifier(tenant, "tenant")
    with store.connection() as connection:
        if not _has_imports(connection):
            return []
        rows = connection.execute(
            "SELECT batch_key,batch_hash,created_at,response FROM import_batches "
            "WHERE tenant=? ORDER BY created_at DESC,batch_key DESC LIMIT 25", (tenant,)
        ).fetchall()
        batches = []
        for row in rows:
            result = json.loads(row["response"])
            batches.append({"batch_key": row["batch_key"], "batch_hash": row["batch_hash"],
                            "created_at": row["created_at"],
                            **{key: result[key] for key in ("accepted", "updated", "unchanged", "quarantined")}})
        return batches


def get_import(store, tenant, batch_key):
    identifier(tenant, "tenant")
    identifier(batch_key, "batch key")
    with store.connection() as connection:
        row = None
        if _has_imports(connection):
            row = connection.execute(
                "SELECT response FROM import_batches WHERE tenant=? AND batch_key=?",
                (tenant, batch_key)
            ).fetchone()
        if row is None:
            fail("not_found", "Import batch not found in this workspace.", 404)
        return json.loads(row["response"])


def list_refunds(store, tenant):
    identifier(tenant, "tenant")
    with store.connection() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT id,order_id,amount_pence FROM refunds WHERE tenant=? ORDER BY id", (tenant,)
        )]
