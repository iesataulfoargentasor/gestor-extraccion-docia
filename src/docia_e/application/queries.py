"""Consultas de catálogo y panel. Los filtros se construyen aquí, no llegan como operadores."""

from __future__ import annotations

from docia_e.domain.errors import DomainError
from docia_e.domain.identity import AUDIENCES, EVENT_STATUSES, FAMILIES
from docia_e.domain.policies import assert_authenticated, can_see_center, has_role

DOC_STATES = {"pending", "in_review", "approved_for_processing", "returned", "rejected", "withdrawn"}
RUN_STATES = {"queued", "running", "succeeded", "partial", "failed", "cancelled"}
HANDOFF_STATES = {"not_ready", "ready", "revoked"}


def search_catalog(gateway, actor, raw: dict, cursor: str | None, limit: int) -> dict:
    assert_authenticated(actor)
    centers = [center for center in (actor.get("center_ids") or []) if center]
    if not centers:
        return {"items": [], "next_cursor": None}
    filters = {
        "center_ids": centers,
        "actor_id": actor["id"],
        "reviewer": has_role(actor, "reviewer"),
        "family": _choice(raw.get("family"), FAMILIES),
        "academic_year": _text(raw.get("academic_year")),
        "audience": _choice(raw.get("audience"), AUDIENCES),
        "documentary_state": _choice(raw.get("documentary_state"), DOC_STATES),
        "run_status": _choice(raw.get("run_status"), RUN_STATES),
        "handoff_status": _choice(raw.get("handoff_status"), HANDOFF_STATES),
        "event_status": _choice(raw.get("event_status"), EVENT_STATUSES),
        "received_from": _text(raw.get("received_from")),
        "received_to": _text(raw.get("received_to")),
    }
    items, next_cursor = gateway.store.search_versions(filters, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


def dashboard(gateway, actor) -> dict:
    assert_authenticated(actor)
    centers = list(actor.get("center_ids") or [])
    return gateway.store.counts(centers, actor["id"], has_role(actor, "reviewer"))


def version_detail(gateway, actor, version_id: str) -> dict:
    version = gateway.store.get_version(version_id)
    if not version or not _readable(gateway, actor, version):
        raise DomainError("NOT_FOUND", "No hay una ficha visible con ese identificador.", status=404)
    document = gateway.store.get_document(version["document_id"])
    return {
        "version": version,
        "document": document,
        "sources": gateway.store.list_sources(version_id),
        "reviews": gateway.store.list_reviews(version_id),
        "extractions": gateway.store.list_extractions(version_id),
        "audit": gateway.store.list_audit(version["document_id"], 50),
        "versions": gateway.store.list_versions(version["document_id"]),
    }


def _readable(gateway, actor, version) -> bool:
    if not can_see_center(actor, version["center_id"]):
        return False
    if has_role(actor, "reviewer"):
        return True
    if version.get("submitted_by") == actor.get("id"):
        return True
    return version.get("handoff_status") == "ready"


def _choice(value, allowed) -> str | None:
    if value in allowed:
        return value
    return None


def _text(value) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 80 or any(char in cleaned for char in "$."):
        return None
    return cleaned
