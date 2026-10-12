"""Revisión documental y de relaciones. Cada decisión guarda actor, momento y motivo."""

from __future__ import annotations

from docia_e.application.handoffs import revoke_handoff_record
from docia_e.application.support import new_id
from docia_e.domain.errors import DomainError, OperationalError
from docia_e.domain.identity import logical_identity, substantial_change, validate_submission
from docia_e.domain.policies import (
    assert_can_review,
    assert_expected_revision,
    has_role,
    hidden_if_out_of_scope,
    transition_document,
)

EDITABLE = {
    "title",
    "subtype",
    "audience",
    "academic_year",
    "issuer",
    "source_reference",
    "source_url",
    "publication_date",
    "consulted_at",
    "authorization_reference",
    "valid_from",
    "valid_to",
    "event_start",
    "event_end",
    "event_timezone",
    "event_status",
    "event_id",
    "edition",
    "location",
    "resource_type",
    "declared_version",
    "general_scope",
    "observations",
    "family",
}


def review_version(gateway, actor, version_id, decision, reason, expected_revision, patch, clock) -> dict:
    if gateway.store.maintenance_enabled():
        raise OperationalError("MAINTENANCE", "Las decisiones están pausadas durante la copia.")
    version = _visible_version(gateway.store, actor, version_id)
    assert_can_review(actor, version["center_id"], version["submitted_by"])
    if decision != "mark_in_review" and not (reason or "").strip():
        raise DomainError("VALIDATION", "La decisión necesita un motivo.", status=422, field_errors={"reason": "indica el motivo"})
    assert_expected_revision(version["metadata_revision"], expected_revision)
    target = transition_document(version["documentary_state"], decision)
    updated = dict(version)
    for field in EDITABLE:
        if field in (patch or {}):
            updated[field] = patch[field]
    errors = validate_submission(updated)
    if errors:
        raise DomainError("VALIDATION", "Los metadatos no superan la validación.", status=422, field_errors=errors)
    resolved, key = logical_identity(updated)
    changed = substantial_change(version, updated) or decision in {"withdraw", "reject"}
    new_revision = version["metadata_revision"] + (1 if changed or decision != "mark_in_review" else 0)
    at = clock()

    def work(tx):
        current = tx.get_version(version_id)
        assert_expected_revision(current["metadata_revision"], expected_revision)
        fields = {field: updated.get(field) for field in EDITABLE}
        fields.update(
            {
                "documentary_state": target,
                "metadata_revision": new_revision,
                "identity_fields_resolved": resolved,
            }
        )
        if not tx.update_version(version_id, expected_revision, fields):
            raise DomainError("REVISION_CONFLICT", "La ficha cambió. Recárgala.", status=409)
        document = tx.get_document(current["document_id"])
        if resolved and key and document.get("logical_key") != key:
            other = tx.find_document_by_logical_key(current["center_id"], current["profile"], key)
            if other and other["id"] != document["id"]:
                tx.update_version(
                    version_id,
                    new_revision,
                    {"relation_state": "pending", "relation_target_id": other["id"], "metadata_revision": new_revision + 1},
                )
            elif document.get("identity_resolved") is not True:
                if not tx.update_document(document["id"], document["revision"], {"logical_key": key, "identity_resolved": True, "title": updated["title"], "revision": document["revision"] + 1}):
                    raise DomainError("REVISION_CONFLICT", "El documento cambió. Recárgalo.", status=409)
        if changed:
            _revoke_if_ready(tx, actor, current, "Cambio de metadatos o de estado documental.", at)
        tx.insert_review(
            {
                "id": new_id(),
                "actor_id": actor["id"],
                "decision": decision,
                "reason": (reason or "").strip(),
                "subject_type": "version",
                "subject_id": version_id,
                "metadata_revision": new_revision,
                "extraction_id": current.get("selected_extraction_id"),
                "at": at,
            }
        )
        tx.insert_audit(
            {
                "id": new_id(),
                "document_id": current["document_id"],
                "at": at,
                "actor_id": actor["id"],
                "action": decision,
                "detail": "decisión documental",
            }
        )
        return {"version_id": version_id, "documentary_state": target, "metadata_revision": new_revision}

    return gateway.transaction(work)


def resolve_relation(gateway, actor, version_id, relation, target_id, reason, expected_revision, clock) -> dict:
    if relation not in {"replaces", "complements", "distinct", "conflicts"}:
        raise DomainError("VALIDATION", "La relación no está reconocida.", status=422)
    if not (reason or "").strip():
        raise DomainError("VALIDATION", "La relación necesita un motivo.", status=422, field_errors={"reason": "indica el motivo"})
    version = _visible_version(gateway.store, actor, version_id)
    assert_can_review(actor, version["center_id"], version["submitted_by"])
    assert_expected_revision(version["metadata_revision"], expected_revision)
    at = clock()

    def work(tx):
        current = tx.get_version(version_id)
        assert_expected_revision(current["metadata_revision"], expected_revision)
        fields = {
            "relation_type": relation,
            "relation_state": "resolved",
            "relation_target_id": target_id,
            "metadata_revision": current["metadata_revision"] + 1,
        }
        if relation == "conflicts":
            fields["relation_state"] = "resolved"
        if not tx.update_version(version_id, expected_revision, fields):
            raise DomainError("REVISION_CONFLICT", "La ficha cambió. Recárgala.", status=409)
        if relation in {"replaces", "conflicts"}:
            _revoke_if_ready(tx, actor, current, f"Relación {relation}.", at)
        tx.insert_review(
            {
                "id": new_id(),
                "actor_id": actor["id"],
                "decision": f"relation_{relation}",
                "reason": reason.strip(),
                "subject_type": "version",
                "subject_id": version_id,
                "metadata_revision": fields["metadata_revision"],
                "extraction_id": current.get("selected_extraction_id"),
                "at": at,
            }
        )
        tx.insert_audit(
            {
                "id": new_id(),
                "document_id": current["document_id"],
                "at": at,
                "actor_id": actor["id"],
                "action": f"relation_{relation}",
                "detail": "relación resuelta; no sustituye solo por haberse subido",
            }
        )
        return {"version_id": version_id, "relation_type": relation, "metadata_revision": fields["metadata_revision"]}

    return gateway.transaction(work)


def link_existing_file(gateway, actor, version_id, file_id, reason, expected_revision, clock) -> dict:
    """El revisor puede respaldar otro documento con un original ya guardado."""
    if not (reason or "").strip():
        raise DomainError("VALIDATION", "Enlazar un original existente exige un motivo.", status=422)
    version = _visible_version(gateway.store, actor, version_id)
    assert_can_review(actor, version["center_id"], version["submitted_by"])
    existing = gateway.store.get_file(file_id)
    if not existing or existing["center_id"] != version["center_id"] or existing["purpose"] != "original":
        raise DomainError("NOT_FOUND", "No hay un original visible con ese identificador.", status=404)
    if gateway.store.find_version_by_file(version["document_id"], file_id):
        raise DomainError("CONFLICT", "Ese original ya está enlazado a este documento.", status=409)
    at = clock()

    def work(tx):
        if not tx.update_version(
            version_id,
            expected_revision,
            {
                "file_id": file_id,
                "metadata_revision": version["metadata_revision"] + 1,
                "relation_type": "same_file",
                "relation_state": "resolved",
            },
        ):
            raise DomainError("REVISION_CONFLICT", "La ficha cambió. Recárgala.", status=409)
        tx.insert_audit(
            {
                "id": new_id(),
                "document_id": version["document_id"],
                "at": at,
                "actor_id": actor["id"],
                "action": "link_existing_file",
                "detail": "enlace motivado; no es una autorización automática",
            }
        )
        return {"version_id": version_id, "file_id": file_id}

    return gateway.transaction(work)


def _revoke_if_ready(tx, actor, version, reason, at) -> None:
    document = tx.get_document(version["document_id"])
    handoff_id = document.get("current_handoff_id")
    if not handoff_id:
        return
    handoff = tx.get_handoff(handoff_id)
    if handoff and handoff.get("status") == "ready":
        revoke_handoff_record(tx, actor, handoff, reason, at, public_reason="revoked")


def _visible_version(store, actor, version_id) -> dict:
    version = store.get_version(version_id)
    if not version:
        raise DomainError("NOT_FOUND", "No hay una ficha visible con ese identificador.", status=404)
    hidden_if_out_of_scope(actor, version["center_id"])
    if has_role(actor, "collaborator") and not has_role(actor, "reviewer"):
        raise DomainError("FORBIDDEN", "Tu cuenta no puede revisar ni habilitar entregas.", status=403)
    return version
