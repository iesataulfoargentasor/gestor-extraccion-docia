"""Contrato docia-e/1.0. Las unidades no son chunks ni llevan vectores."""

from __future__ import annotations

import hashlib
import json

SCHEMA_VERSION = "docia-e/1.0"


def config_fingerprint(configuration: dict) -> str:
    payload = json.dumps(configuration, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_package(
    *,
    handoff: dict,
    version: dict,
    extraction: dict,
    original: dict,
    units: list[dict],
) -> dict:
    """Arma el manifiesto con tipos JSON. No incluye ObjectId ni datetime."""
    frozen = handoff["snapshot"]
    package = {
        "schema_version": SCHEMA_VERSION,
        "handoff_id": handoff["id"],
        "document_id": handoff["document_id"],
        "version_id": handoff["version_id"],
        "extraction_id": handoff["extraction_id"],
        "metadata_revision": int(handoff["metadata_revision"]),
        "profile": frozen["profile"],
        "family": frozen["family"],
        "center_id": frozen["center_id"],
        "title": frozen["title"],
        "audience": list(frozen.get("audience") or []),
        "academic_year": frozen.get("academic_year"),
        "source": {
            "issuer": frozen.get("issuer"),
            "reference": frozen.get("source_reference"),
            "url": frozen.get("source_url"),
            "publication_date": frozen.get("publication_date"),
            "consulted_at": frozen.get("consulted_at"),
            "authorization_reference": frozen.get("authorization_reference"),
        },
        "temporal": {
            "valid_from": frozen.get("valid_from"),
            "valid_to": frozen.get("valid_to"),
            "event_start": frozen.get("event_start"),
            "event_end": frozen.get("event_end"),
            "event_timezone": frozen.get("event_timezone"),
            "event_status": frozen.get("event_status"),
            "resource_status": frozen.get("resource_status"),
            "event_id": frozen.get("event_id"),
            "edition": frozen.get("edition"),
            "location": frozen.get("location"),
        },
        "relations": list(frozen.get("relations") or []),
        "original": {
            "file_id": original["id"],
            "display_name": original.get("display_name") or "original",
            "mime_type": original["mime_type"],
            "size_bytes": int(original["size_bytes"]),
            "sha256": original["sha256"],
            "download_path": f"/files/{original['id']}/download",
        },
        "extraction": {
            "engine": extraction["engine"],
            "engine_version": extraction["engine_version"],
            "config_fingerprint": extraction["config_fingerprint"],
            "configuration": extraction["configuration"],
            "started_at": extraction["started_at"],
            "completed_at": extraction["completed_at"],
            "status": extraction["run_status"],
            "artifacts": [
                {
                    "kind": artifact["kind"],
                    "file_id": artifact["file_id"],
                    "mime_type": artifact["mime_type"],
                    "size_bytes": int(artifact["size_bytes"]),
                    "sha256": artifact["sha256"],
                    "download_path": f"/files/{artifact['file_id']}/download",
                }
                for artifact in extraction.get("artifacts") or []
            ],
        },
        "quality": {
            "pages_expected": extraction.get("pages_expected"),
            "pages_processed": extraction.get("pages_processed"),
            "pages_failed": extraction.get("pages_failed"),
            "coverage_complete": bool(extraction.get("coverage_complete")),
            "unresolved_blockers": sum(
                1
                for issue in extraction.get("issues") or []
                if issue.get("severity") == "blocker" and not issue.get("resolved")
            ),
            "issues": [_public_issue(issue) for issue in extraction.get("issues") or []],
            "review_status": extraction.get("review_status"),
            "reviewed_by": extraction.get("reviewed_by"),
            "reviewed_at": extraction.get("reviewed_at"),
            "reviewed_metadata_revision": extraction.get("reviewed_metadata_revision"),
            "reviewed_extraction_id": extraction["id"],
            "review_scope": extraction.get("review_scope"),
        },
        "units": [_public_unit(unit) for unit in units],
        "eligibility": {
            "status": handoff["status"],
            "permitted_use": handoff.get("permitted_use"),
            "authorized_by": handoff.get("authorized_by"),
            "authorized_at": handoff.get("authorized_at"),
            "status_path": f"/api/etl/v1/handoffs/{handoff['id']}",
            "changes_path": "/api/etl/v1/changes",
        },
    }
    _reject_bson(package)
    return package


def revoked_view(handoff: dict) -> dict:
    """Una revocación no reenvía el contenido ni el motivo interno completo."""
    return {
        "schema_version": SCHEMA_VERSION,
        "handoff_id": handoff["id"],
        "document_id": handoff["document_id"],
        "version_id": handoff["version_id"],
        "extraction_id": handoff["extraction_id"],
        "eligibility": {
            "status": "revoked",
            "permitted_use": None,
            "authorized_by": None,
            "authorized_at": handoff.get("revoked_at"),
            "status_path": f"/api/etl/v1/handoffs/{handoff['id']}",
            "changes_path": "/api/etl/v1/changes",
            "public_reason": handoff.get("public_reason") or "revoked",
        },
    }


def _public_issue(issue: dict) -> dict:
    return {
        "code": issue.get("code"),
        "severity": issue.get("severity"),
        "unit_id": issue.get("unit_id"),
        "locator": issue.get("locator"),
        "explanation": issue.get("explanation"),
        "next_action": issue.get("next_action"),
        "resolved": bool(issue.get("resolved")),
    }


def _public_unit(unit: dict) -> dict:
    locator = dict(unit.get("locator") or {})
    if unit.get("part_index") is not None:
        locator["part_index"] = unit["part_index"]
        locator["part_count"] = unit["part_count"]
    return {
        "unit_id": unit["unit_id"],
        "order": int(unit["order"]),
        "kind": unit["kind"],
        "method": unit["method"],
        "text": unit["text"],
        "locator": locator,
    }


def _reject_bson(value) -> None:
    if type(value).__name__ in {"ObjectId", "datetime"}:
        raise TypeError("El contrato no puede serializar ObjectId ni datetime")
    if isinstance(value, dict):
        for item in value.values():
            _reject_bson(item)
    elif isinstance(value, list):
        for item in value:
            _reject_bson(item)


def public_change(event: dict) -> dict:
    return {
        "sequence": int(event["sequence"]),
        "event": event["event"],
        "handoff_id": event["handoff_id"],
        "document_id": event["document_id"],
        "version_id": event["version_id"],
        "metadata_revision": int(event["metadata_revision"]),
        "occurred_at": event["occurred_at"],
        "public_reason": event.get("public_reason") or "",
    }
