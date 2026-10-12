"""Entrega autorizada a T y su revocación. El snapshot no se edita en silencio."""

from __future__ import annotations

from docia_e.application.support import new_id, snapshot_from
from docia_e.domain.contract import build_package, public_change, revoked_view
from docia_e.domain.errors import DomainError, OperationalError
from docia_e.domain.policies import (
    assert_can_review,
    assert_consumer,
    assert_expected_revision,
    can_see_center,
    handoff_blockers,
    hidden_if_out_of_scope,
)

PUBLIC_REASONS = {
    "individual_data": "contenido restringido",
    "withdrawn": "revoked",
    "replaced": "replaced",
    "metadata_change": "revoked",
}


def authorize_handoff(gateway, actor, version_id, expected_revision, extraction_id, reason, clock) -> dict:
    if gateway.store.maintenance_enabled():
        raise OperationalError("MAINTENANCE", "Las entregas están pausadas durante la copia.")
    version = gateway.store.get_version(version_id)
    if not version:
        raise DomainError("NOT_FOUND", "No hay una ficha visible con ese identificador.", status=404)
    hidden_if_out_of_scope(actor, version["center_id"])
    assert_can_review(actor, version["center_id"], version["submitted_by"])
    if not (reason or "").strip():
        raise DomainError("VALIDATION", "Habilitar para T necesita un motivo.", status=422, field_errors={"reason": "indica el motivo"})
    extraction = gateway.store.get_extraction(extraction_id)
    blockers = handoff_blockers({**version, "selected_extraction_id": extraction_id}, extraction)
    if blockers:
        raise DomainError(
            "NOT_READY",
            "La versión todavía no se puede entregar a T.",
            status=422,
            field_errors={"eligibility": "; ".join(blockers)},
            next_action="Completa la revisión. Terminar el parser no habilita la entrega.",
        )
    at = clock()

    def work(tx):
        current = tx.get_version(version_id)
        document = tx.get_document(current["document_id"])
        assert_expected_revision(current["metadata_revision"], expected_revision)
        assert_expected_revision(document["revision"], document["revision"])
        fresh = tx.get_extraction(extraction_id)
        blockers_now = handoff_blockers({**current, "selected_extraction_id": extraction_id}, fresh)
        if blockers_now:
            raise DomainError("NOT_READY", "La elegibilidad cambió. Recarga la ficha.", status=409)
        previous_id = document.get("current_handoff_id")
        event_name = "handoff_ready"
        if previous_id:
            previous = tx.get_handoff(previous_id)
            if previous and previous.get("status") == "ready":
                revoke_handoff_record(tx, actor, previous, reason, at, public_reason="replaced", event_name="handoff_replaced")
        document = tx.get_document(document["id"])
        relations = []
        if current.get("relation_type"):
            relations.append(
                {
                    "type": current["relation_type"],
                    "target_id": current.get("relation_target_id"),
                }
            )
        handoff_id = new_id()
        handoff = {
            "id": handoff_id,
            "document_id": document["id"],
            "version_id": version_id,
            "extraction_id": extraction_id,
            "metadata_revision": current["metadata_revision"],
            "status": "ready",
            "permitted_use": "general_center_information",
            "authorized_by": actor["id"],
            "authorized_at": at,
            "revoked_at": None,
            "public_reason": None,
            "center_id": current["center_id"],
            "snapshot": snapshot_from(current, relations),
            "created_at": at,
        }
        tx.insert_handoff(handoff)
        if not tx.update_document(
            document["id"],
            document["revision"],
            {"current_handoff_id": handoff_id, "revision": document["revision"] + 1, "title": current["title"]},
        ):
            raise DomainError(
                "REVISION_CONFLICT",
                "Otra entrega se activó a la vez para esta identidad.",
                status=409,
                next_action="Recarga la ficha. Solo una entrega principal puede quedar activa.",
            )
        tx.update_version(
            version_id,
            expected_revision,
            {"handoff_status": "ready", "selected_extraction_id": extraction_id},
        )
        _change(tx, current, handoff_id, event_name, at, "")
        tx.insert_review(
            {
                "id": new_id(),
                "actor_id": actor["id"],
                "decision": "handoff_ready",
                "reason": reason.strip(),
                "subject_type": "version",
                "subject_id": version_id,
                "metadata_revision": current["metadata_revision"],
                "extraction_id": extraction_id,
                "at": at,
            }
        )
        tx.insert_audit(
            {
                "id": new_id(),
                "document_id": document["id"],
                "at": at,
                "actor_id": actor["id"],
                "action": "handoff_ready",
                "detail": "entrega autorizada; no actualiza un índice vectorial",
            }
        )
        return {"handoff_id": handoff_id, "status": "ready"}

    return gateway.transaction(work)


def revoke_handoff(gateway, actor, handoff_id, reason, reason_code, clock) -> dict:
    if gateway.store.maintenance_enabled():
        raise OperationalError("MAINTENANCE", "Las retiradas están pausadas durante la copia.")
    handoff = gateway.store.get_handoff(handoff_id)
    if not handoff:
        raise DomainError("NOT_FOUND", "No hay una entrega visible con ese identificador.", status=404)
    hidden_if_out_of_scope(actor, handoff["center_id"])
    version = gateway.store.get_version(handoff["version_id"])
    assert_can_review(actor, handoff["center_id"], version["submitted_by"])
    if not (reason or "").strip():
        raise DomainError("VALIDATION", "La retirada necesita un motivo.", status=422, field_errors={"reason": "indica el motivo"})
    at = clock()
    public = PUBLIC_REASONS.get(reason_code or "", "revoked")

    def work(tx):
        current = tx.get_handoff(handoff_id)
        if not current or current["status"] != "ready":
            raise DomainError("CONFLICT", "La entrega ya no está activa.", status=409)
        revoke_handoff_record(tx, actor, current, reason, at, public_reason=public)
        if reason_code == "individual_data":
            _restrict_tree(tx, current)
        return {"handoff_id": handoff_id, "status": "revoked"}

    return gateway.transaction(work)


def revoke_handoff_record(tx, actor, handoff, reason, at, public_reason: str, event_name: str = "handoff_revoked") -> None:
    tx.update_handoff(
        handoff["id"],
        {"status": "revoked", "revoked_at": at, "public_reason": public_reason},
    )
    document = tx.get_document(handoff["document_id"])
    if document and document.get("current_handoff_id") == handoff["id"]:
        tx.update_document(
            document["id"],
            document["revision"],
            {"current_handoff_id": None, "revision": document["revision"] + 1},
        )
    version = tx.get_version(handoff["version_id"])
    if version and version.get("handoff_status") == "ready":
        tx.update_version(version["id"], version["metadata_revision"], {"handoff_status": "revoked"})
    _change(tx, handoff, handoff["id"], event_name, at, public_reason)
    tx.insert_audit(
        {
            "id": new_id(),
            "document_id": handoff["document_id"],
            "at": at,
            "actor_id": actor["id"],
            "action": event_name,
            "detail": public_reason,
        }
    )
    _ = reason


def package_for(gateway, actor, handoff_id: str) -> tuple[dict, int]:
    handoff = gateway.store.get_handoff(handoff_id)
    if not handoff or not can_see_center(actor, handoff["center_id"]):
        raise DomainError("NOT_FOUND", "No hay una entrega visible con ese identificador.", status=404)
    if handoff["status"] != "ready":
        if _may_see_revocation(actor, handoff):
            return revoked_view(handoff), 410
        raise DomainError("NOT_FOUND", "No hay una entrega visible con ese identificador.", status=404)
    if not _may_read_ready(actor, handoff):
        raise DomainError("NOT_FOUND", "No hay una entrega visible con ese identificador.", status=404)
    version = gateway.store.get_version(handoff["version_id"])
    extraction = gateway.store.get_extraction(handoff["extraction_id"])
    original = gateway.store.get_file(version["file_id"])
    units = gateway.store.list_all_units(extraction["id"])
    package = build_package(handoff=handoff, version=version, extraction=extraction, original=original, units=units)
    return package, 200


def list_changes(gateway, actor, cursor: int | None, limit: int) -> dict:
    assert_consumer(actor)
    centers = list(actor.get("center_ids") or [])
    minimum = gateway.store.min_sequence(centers)
    if cursor is not None and minimum is not None and cursor + 1 < minimum:
        return {"resync_required": True, "changes": [], "next_cursor": None}
    events = gateway.store.list_changes(centers, cursor, limit)
    maximum = gateway.store.max_sequence(centers)
    next_cursor = events[-1]["sequence"] if events else cursor
    return {
        "resync_required": False,
        "changes": [public_change(event) for event in events],
        "next_cursor": next_cursor,
        "up_to_date": maximum is None or next_cursor == maximum or (not events and cursor is not None and maximum is not None and cursor >= maximum),
    }


def list_ready(gateway, actor, cursor: str | None, limit: int, family: str | None) -> dict:
    assert_consumer(actor)
    centers = list(actor.get("center_ids") or [])
    rows, next_cursor = gateway.store.list_ready_handoffs(centers, cursor, limit, family)
    return {
        "handoffs": [
            {
                "handoff_id": row["id"],
                "document_id": row["document_id"],
                "version_id": row["version_id"],
                "center_id": row["center_id"],
                "family": row["snapshot"].get("family"),
                "title": row["snapshot"].get("title"),
                "status": "ready",
                "created_at": row["created_at"],
            }
            for row in rows
        ],
        "next_cursor": next_cursor,
    }


def _change(tx, owner, handoff_id, event_name, at, public_reason) -> None:
    sequence = tx.next_sequence()
    tx.insert_change(
        {
            "sequence": sequence,
            "event": event_name,
            "handoff_id": handoff_id,
            "document_id": owner["document_id"],
            "version_id": owner.get("version_id") or owner.get("id"),
            "metadata_revision": int(owner.get("metadata_revision") or 0),
            "occurred_at": at,
            "public_reason": public_reason,
            "center_id": owner["center_id"],
        }
    )


def _restrict_tree(tx, handoff) -> None:
    version = tx.get_version(handoff["version_id"])
    if version:
        tx.update_file_unconditional(version["file_id"], {"restricted": True})
    extraction = tx.get_extraction(handoff["extraction_id"])
    for artifact in (extraction or {}).get("artifacts") or []:
        tx.update_file_unconditional(artifact["file_id"], {"restricted": True})


def _may_read_ready(actor, handoff) -> bool:
    if "consumer_t" in set(actor.get("roles") or []):
        return can_see_center(actor, handoff["center_id"])
    if "reviewer" in set(actor.get("roles") or []):
        return can_see_center(actor, handoff["center_id"])
    return False


def _may_see_revocation(actor, handoff) -> bool:
    return _may_read_ready(actor, handoff)
