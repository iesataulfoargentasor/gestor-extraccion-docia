"""Recepción de aportaciones. GridFS queda fuera de la transacción de catálogo."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from docia_e.adapters.extraction.inspect import inspect_file
from docia_e.application.support import (
    StoreConflict,
    new_id,
    request_fingerprint,
    spool_and_hash,
)
from docia_e.domain.errors import DomainError, OperationalError
from docia_e.domain.identity import logical_identity, metadata_compatible, validate_submission
from docia_e.domain.policies import assert_can_upload, collaborator_may_read
from docia_e.domain.timeutil import exclusive_end, parse_civil_date

CIVIL_FIELDS = (
    "publication_date",
    "consulted_at",
    "valid_from",
    "valid_to",
)


def receive_contribution(
    gateway,
    blobs,
    actor: dict,
    meta: dict,
    stream,
    display_name: str,
    idempotency_key: str,
    limits: dict,
    temp_dir: Path,
    clock,
) -> dict:
    if gateway.store.maintenance_enabled():
        raise OperationalError(
            "MAINTENANCE",
            "Las cargas están pausadas mientras se hace una copia.",
            next_action="Espera a que termine el mantenimiento.",
        )
    if not idempotency_key or len(idempotency_key) > 200:
        raise DomainError("BAD_REQUEST", "Falta la clave de idempotencia.", status=400)
    prepared = _prepare_meta(meta)
    errors = validate_submission(prepared)
    if errors:
        raise DomainError(
            "VALIDATION",
            "Revisa los campos marcados. No se ha guardado la aportación.",
            status=422,
            field_errors=errors,
            next_action="Corrige los campos y reintenta. La clave de idempotencia se conserva.",
        )
    assert_can_upload(actor, prepared["center_id"])
    temp_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(temp_dir, 0o700)
    fd, raw_name = tempfile.mkstemp(prefix="docia-", dir=temp_dir)
    os.close(fd)
    path = Path(raw_name)
    os.chmod(path, 0o600)
    try:
        sha, size = spool_and_hash(stream, path, limits["max_upload_bytes"], limits["max_upload_mib"])
        inspected = inspect_file(path, display_name, limits)
        fingerprint = request_fingerprint(prepared, sha)
        cached = _idempotent_lookup(gateway.store, actor["id"], idempotency_key, fingerprint)
        if cached is not None:
            return cached
        try:
            gateway.store.insert_upload(
                {
                    "actor_id": actor["id"],
                    "idempotency_key": idempotency_key,
                    "fingerprint": fingerprint,
                    "response": None,
                    "created_at": clock(),
                }
            )
        except StoreConflict:
            cached = _idempotent_lookup(gateway.store, actor["id"], idempotency_key, fingerprint)
            if cached is not None:
                return cached
            raise DomainError(
                "IDEMPOTENCY_IN_PROGRESS",
                "Esa clave ya tiene una carga en curso.",
                status=409,
                next_action="Espera y repite la misma petición.",
            )
        try:
            response = _admit(gateway, blobs, actor, prepared, path, inspected, sha, size, clock)
            gateway.store.complete_upload(actor["id"], idempotency_key, response)
            return response
        except Exception:
            gateway.store.delete_upload(actor["id"], idempotency_key)
            raise
    finally:
        path.unlink(missing_ok=True)


def _prepare_meta(meta: dict) -> dict:
    prepared = dict(meta)
    prepared["audience"] = [item for item in (meta.get("audience") or []) if item]
    prepared["contains_individual_data"] = bool(meta.get("contains_individual_data"))
    if prepared.get("general_scope") in {"", None}:
        prepared["general_scope"] = None
    elif prepared.get("general_scope") in {True, "true", "on", "1", "yes"}:
        prepared["general_scope"] = True
    elif prepared.get("general_scope") in {False, "false", "0", "no"}:
        prepared["general_scope"] = False
    field_errors = {}
    for field in CIVIL_FIELDS:
        try:
            prepared[field] = parse_civil_date(prepared.get(field), field)
        except ValueError as exc:
            field_errors[field] = str(exc)
    if prepared.get("valid_to_inclusive") in {True, "true", "on", "1"} and prepared.get("valid_to"):
        prepared["valid_to_declared"] = prepared["valid_to"]
        prepared["valid_to"] = exclusive_end(prepared["valid_to"])
    else:
        prepared["valid_to_declared"] = None
    if field_errors:
        raise DomainError("VALIDATION", "Hay fechas que no son fechas civiles.", status=422, field_errors=field_errors)
    for field in ("event_start", "event_end", "event_timezone", "event_id", "edition", "location", "issuer", "source_reference", "source_url", "title", "subtype", "observations", "authorization_reference", "academic_year", "resource_type", "declared_version", "event_status"):
        value = prepared.get(field)
        if isinstance(value, str):
            prepared[field] = value.strip() or None
    if not prepared.get("event_timezone"):
        prepared["event_timezone"] = "Europe/Madrid" if prepared.get("event_start") else None
    return prepared


def _idempotent_lookup(store, actor_id: str, key: str, fingerprint: str):
    existing = store.get_upload(actor_id, key)
    if existing is None:
        return None
    if existing["fingerprint"] != fingerprint:
        raise DomainError(
            "IDEMPOTENCY_CONFLICT",
            "Esa clave de idempotencia ya se usó con otros datos o con otro archivo.",
            status=409,
            next_action="Recarga el formulario para obtener una clave nueva.",
        )
    if existing.get("response"):
        return existing["response"]
    raise DomainError(
        "IDEMPOTENCY_IN_PROGRESS",
        "Esa clave ya tiene una carga en curso.",
        status=409,
        next_action="Espera y repite la misma petición.",
    )


def _admit(gateway, blobs, actor, meta, path, inspected, sha, size, clock) -> dict:
    store = gateway.store
    at = clock()
    existing = store.find_original(meta["center_id"], sha)
    if existing:
        return _existing_bytes(gateway, actor, meta, existing, at)
    file_id = new_id()
    record = {
        "id": file_id,
        "center_id": meta["center_id"],
        "gridfs_id": None,
        "sha256": sha,
        "size_bytes": size,
        "mime_type": inspected["mime_type"],
        "kind": inspected["kind"],
        "persistence_state": "staging",
        "purpose": "original",
        "owner_id": actor["id"],
        "revision": 0,
        "display_name": inspected["display_name"],
        "extraction_id": None,
        "artifact_kind": None,
        "created_at": at,
        "restricted": False,
    }
    reserved = store.reserve_original(record)
    if reserved["outcome"] == "exists":
        return _existing_bytes(gateway, actor, meta, reserved["file"], at)
    gridfs_id = blobs.put_path(path, file_id, inspected["display_name"], inspected["mime_type"])
    if not blobs.matches(gridfs_id, sha, size):
        blobs.delete(gridfs_id)
        raise OperationalError(
            "BLOB_MISMATCH",
            "El original guardado no coincide con el hash calculado. No queda listo.",
            next_action="Reintenta la carga. Si se repite, revisa el diagnóstico.",
        )
    if not store.update_file(file_id, 0, actor["id"], {"gridfs_id": gridfs_id, "persistence_state": "gridfs_written", "revision": 1}):
        raise OperationalError("BLOB_LINK_FAILED", "No se pudo reservar el original.", next_action="Reintenta la carga.")

    def link(tx):
        response = _link_catalog(tx, actor, meta, file_id, at)
        if not tx.update_file(
            file_id,
            1,
            actor["id"],
            {"persistence_state": "ready", "revision": 2},
        ):
            raise OperationalError(
                "CATALOG_UNCONFIRMED",
                "El archivo se guardó pero el catálogo no lo marcó como listo.",
                next_action="No lo vuelvas a subir todavía. El reconciliador debe comprobar el original.",
            )
        response["file_id"] = file_id
        response["sha256"] = sha
        return response

    return gateway.transaction(link)


def _existing_bytes(gateway, actor, meta, existing, at) -> dict:
    if existing.get("persistence_state") != "ready":
        return {
            "outcome": "processing_pending",
            "message": "Esos mismos bytes ya se están incorporando. No se ha creado otra copia.",
            "next_action": "Espera y consulta el catálogo. No repitas la carga con otra clave.",
            "document_id": None,
            "version_id": None,
            "file_id": None,
            "sha256": existing.get("sha256"),
        }
    versions = gateway.store.versions_for_file(existing["id"])
    visible = [version for version in versions if collaborator_may_read(actor, version)]
    comparable = visible or versions
    compatible = any(metadata_compatible(version, meta) for version in comparable)
    if not compatible:
        discrepancy_id = new_id()
        gateway.store.insert_audit(
            {
                "id": new_id(),
                "document_id": None,
                "at": at,
                "actor_id": actor["id"],
                "action": "metadata_discrepancy",
                "detail": "mismos bytes con metadatos incompatibles",
            }
        )
        gateway.store.insert_discrepancy(
            {
                "id": discrepancy_id,
                "center_id": meta["center_id"],
                "file_id": existing["id"],
                "actor_id": actor["id"],
                "created_at": at,
                "incoming_title": meta.get("title"),
                "status": "open",
            }
        )
        exposed = visible[0] if visible else None
        return {
            "outcome": "metadata_discrepancy",
            "message": "Esos bytes ya están en el centro, pero los metadatos no coinciden. No se ha duplicado el original.",
            "next_action": "Un revisor debe resolver la discrepancia. Subir de nuevo no la autoriza.",
            "document_id": exposed["document_id"] if exposed else None,
            "version_id": exposed["id"] if exposed else None,
            "file_id": existing["id"] if exposed else None,
            "sha256": existing["sha256"] if exposed else None,
        }
    target = next(version for version in comparable if metadata_compatible(version, meta))
    if _source_is_new(gateway.store, target["id"], meta):
        gateway.store.insert_source(_source(target["id"], meta, at))
    gateway.store.insert_audit(
        {
            "id": new_id(),
            "document_id": target["document_id"] if collaborator_may_read(actor, target) else None,
            "at": at,
            "actor_id": actor["id"],
            "action": "same_file",
            "detail": "copia exacta; no se crea versión ni trabajo",
        }
    )
    exposed = target if collaborator_may_read(actor, target) else None
    return {
        "outcome": "exact_duplicate",
        "message": "Copia exacta. No se ha creado otra versión ni otra extracción.",
        "next_action": "Abre la ficha existente si tienes permiso, o espera a que esté en el catálogo autorizado.",
        "document_id": exposed["document_id"] if exposed else None,
        "version_id": exposed["id"] if exposed else None,
        "file_id": existing["id"] if exposed else None,
        "sha256": existing["sha256"] if exposed else None,
    }


def _link_catalog(tx, actor, meta, file_id, at) -> dict:
    resolved, key = logical_identity(meta)
    current = tx.find_document_by_logical_key(meta["center_id"], meta["profile"], key) if resolved else None
    if current:
        ordinal = tx.next_ordinal(current["id"])
        version = _version(actor, meta, current["id"], file_id, ordinal, at, relation_state="pending")
        tx.insert_version(version)
        tx.insert_source(_source(version["id"], meta, at))
        tx.insert_audit(_audit(current["id"], actor["id"], at, "candidate_update", "bytes distintos con la misma identidad; sin reemplazo"))
        return {
            "outcome": "candidate_update",
            "message": "Hay otra versión de la misma identidad. La anterior sigue vigente hasta que un revisor decida.",
            "next_action": "Un revisor debe indicar si sustituye, complementa, es distinta o entra en conflicto.",
            "document_id": current["id"],
            "version_id": version["id"],
        }
    document_id = new_id()
    document = {
        "id": document_id,
        "center_id": meta["center_id"],
        "profile": meta["profile"],
        "logical_key": key if resolved else None,
        "identity_resolved": resolved,
        "title": meta["title"],
        "revision": 0,
        "current_handoff_id": None,
        "created_at": at,
    }
    tx.insert_document(document)
    version = _version(actor, meta, document_id, file_id, 1, at, relation_state="none")
    tx.insert_version(version)
    tx.insert_source(_source(version["id"], meta, at))
    tx.insert_audit(_audit(document_id, actor["id"], at, "received", "alta pendiente; la recepción no aprueba ni publica"))
    return {
        "outcome": "received",
        "message": "Aportación recibida y pendiente de revisión. Subir no la habilita para T.",
        "next_action": "Una cuenta revisora distinta debe comprobar fuente, privacidad e identidad.",
        "document_id": document_id,
        "version_id": version["id"],
    }


def _version(actor, meta, document_id, file_id, ordinal, at, relation_state: str) -> dict:
    version = {
        "id": new_id(),
        "document_id": document_id,
        "ordinal": ordinal,
        "file_id": file_id,
        "metadata_revision": 1,
        "documentary_state": "pending",
        "relation_state": relation_state,
        "relation_type": None,
        "relation_target_id": None,
        "selected_extraction_id": None,
        "handoff_status": "not_ready",
        "run_status": None,
        "submitted_by": actor["id"],
        "received_at": at,
        "text_fingerprint": None,
        "resource_status": None,
    }
    for field in (
        "center_id",
        "profile",
        "family",
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
        "valid_to_declared",
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
        "contains_individual_data",
    ):
        version[field] = meta.get(field)
    return version


def _source(version_id, meta, at) -> dict:
    return {
        "id": new_id(),
        "version_id": version_id,
        "issuer": meta.get("issuer"),
        "reference": meta.get("source_reference"),
        "url": meta.get("source_url"),
        "authorization_reference": meta.get("authorization_reference"),
        "created_at": at,
    }


def _source_is_new(store, version_id, meta) -> bool:
    for source in store.list_sources(version_id):
        if source.get("reference") == meta.get("source_reference") and source.get("issuer") == meta.get("issuer"):
            return False
    return bool(meta.get("source_reference") or meta.get("issuer") or meta.get("source_url"))


def _audit(document_id, actor_id, at, action, detail) -> dict:
    return {"id": new_id(), "document_id": document_id, "at": at, "actor_id": actor_id, "action": action, "detail": detail}
