"""Copias, restauración y reconciliación. La copia no es un volumen en caliente."""

from __future__ import annotations

from docia_e.application.support import new_id
from docia_e.domain.errors import DomainError
from docia_e.domain.policies import assert_can_admin

COLLECTIONS = (
    "users",
    "service_tokens",
    "documents",
    "versions",
    "sources",
    "files",
    "extractions",
    "extraction_units",
    "jobs",
    "reviews",
    "audit_events",
    "handoffs",
    "change_events",
    "upload_requests",
    "discrepancies",
    "counters",
    "runtime_flags",
    "worker_heartbeats",
)


def set_maintenance(gateway, actor, enabled: bool, reason: str, clock) -> None:
    assert_can_admin(actor)
    gateway.store.set_maintenance(enabled, reason, actor["id"], clock())


def reconcile(gateway, blobs, actor, older_than: str, apply: bool) -> dict:
    assert_can_admin(actor)
    stale = gateway.store.list_stale_files(older_than)
    report = []
    for record in stale:
        referenced = gateway.store.file_referenced(record["id"])
        active = gateway.store.file_has_active_job(record["id"])
        action = "keep"
        if not referenced and not active and record.get("persistence_state") != "ready":
            action = "delete" if apply else "would_delete"
            if apply and record.get("gridfs_id"):
                blobs.delete(record["gridfs_id"])
                gateway.store.delete_file_record(record["id"])
        report.append(
            {
                "file_id": record["id"],
                "state": record.get("persistence_state"),
                "referenced": referenced,
                "active_job": active,
                "action": action,
            }
        )
    return {"apply": apply, "files": report}


def copy_database(client, source_name: str, target_name: str, bucket: str) -> dict:
    if source_name == target_name:
        raise DomainError("VALIDATION", "La restauración tiene que usar otra base.", status=422)
    source = client[source_name]
    target = client[target_name]
    counts = {}
    for name in COLLECTIONS:
        target.drop_collection(name)
        docs = list(source[name].find({}))
        if docs:
            target[name].insert_many(docs)
        counts[name] = len(docs)
    for suffix in ("files", "chunks"):
        origin = f"{bucket}.{suffix}"
        destination = f"{bucket}.{suffix}"
        target.drop_collection(destination)
        blobs = list(source[origin].find({}))
        if blobs:
            target[destination].insert_many(blobs)
        counts[origin] = len(blobs)
    return counts


def verify_copy(client, source_name: str, target_name: str, bucket: str) -> dict:
    mismatches = []
    for name in COLLECTIONS:
        left = client[source_name][name].count_documents({})
        right = client[target_name][name].count_documents({})
        if left != right:
            mismatches.append({"collection": name, "source": left, "target": right})
    for suffix in ("files", "chunks"):
        label = f"{bucket}.{suffix}"
        left = client[source_name][label].count_documents({})
        right = client[target_name][label].count_documents({})
        if left != right:
            mismatches.append({"collection": label, "source": left, "target": right})
    sample = client[target_name].files.find_one({"purpose": "original", "persistence_state": "ready"})
    sample_ok = sample is None or bool(sample.get("sha256") and sample.get("gridfs_id"))
    return {"ok": not mismatches and sample_ok, "mismatches": mismatches, "sample_checked": sample is not None}


def audit_maintenance(store, actor_id: str, action: str, at: str) -> None:
    store.insert_audit(
        {
            "id": new_id(),
            "document_id": None,
            "at": at,
            "actor_id": actor_id,
            "action": action,
            "detail": "operación de copia",
        }
    )
