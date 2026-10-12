"""Ejecución de extracciones. El resultado técnico no queda aceptado ni listo para T."""

from __future__ import annotations

import json
from pathlib import Path

from docia_e.adapters.extraction.service import extract_document
from docia_e.application.reviews import _revoke_if_ready
from docia_e.application.support import StoreConflict, new_id, sha256_bytes
from docia_e.domain.contract import config_fingerprint
from docia_e.domain.errors import DomainError, OperationalError
from docia_e.domain.policies import (
    assert_can_review,
    assert_can_upload,
    assert_expected_revision,
    blocking_issues,
    has_role,
    hidden_if_out_of_scope,
    may_execute_extraction,
)
from docia_e.domain.textutil import normalize_technical, split_utf8, text_fingerprint

PERMANENT = {"PASSWORD_REQUIRED", "FILE_CORRUPT", "UNSUPPORTED_FORMAT", "OCR_UNAVAILABLE", "SIZE_LIMIT", "TIME_LIMIT"}


def request_extraction(gateway, actor, version_id, expected_revision, force_pages, clock) -> dict:
    version = gateway.store.get_version(version_id)
    if not version:
        raise DomainError("NOT_FOUND", "No hay una ficha visible con ese identificador.", status=404)
    hidden_if_out_of_scope(actor, version["center_id"])
    if actor["id"] == version["submitted_by"]:
        assert_can_upload(actor, version["center_id"])
    elif has_role(actor, "reviewer"):
        assert_can_review(actor, version["center_id"], version["submitted_by"])
    else:
        raise DomainError("FORBIDDEN", "No puedes solicitar la extracción.", status=403)
    if not may_execute_extraction(version):
        raise DomainError(
            "NOT_APPROVED",
            "El worker solo extrae versiones autorizadas para procesar.",
            status=409,
            next_action="Una cuenta revisora distinta debe aprobar el documento para extraer.",
        )
    assert_expected_revision(version["metadata_revision"], expected_revision)
    attempt = len(gateway.store.list_extractions(version_id)) + 1
    options = {"force_ocr_pages": [int(page) for page in force_pages or []]}
    request_key = f"{version_id}:{attempt}"
    job = {
        "id": new_id(),
        "type": "extract",
        "version_id": version_id,
        "file_id": version["file_id"],
        "attempt": attempt,
        "options": options,
        "state": "queued",
        "tries": 0,
        "lease_owner": None,
        "lease_until": None,
        "fencing_token": 0,
        "request_key": request_key,
        "error_code": None,
        "created_at": clock(),
        "started_at": None,
        "finished_at": None,
    }
    try:
        gateway.store.insert_job(job)
    except StoreConflict:
        existing = gateway.store.get_job_by_key(request_key)
        return {"job_id": existing["id"], "status": existing["state"], "created": False}
    return {"job_id": job["id"], "status": "queued", "created": True}


def execute_job(gateway, blobs, settings, worker_id: str, job: dict, clock, sleep) -> None:
    """Idempotente. Un worker que perdió el arrendamiento no puede confirmar el resultado."""
    started = clock()
    if not gateway.store.renew_job(job["id"], worker_id, job["fencing_token"], _lease(clock)):
        return
    version = gateway.store.get_version(job["version_id"])
    original = gateway.store.get_file(version["file_id"]) if version else None
    if not version or not original or not may_execute_extraction(version):
        gateway.store.finish_job(
            job["id"],
            job["fencing_token"],
            {"state": "cancelled", "finished_at": clock(), "error_code": "NOT_APPROVED"},
        )
        return
    path = _materialize(blobs, original, settings.temp_dir)
    try:
        limits = settings.limits()

        def on_page(page, total):
            if not gateway.store.renew_job(job["id"], worker_id, job["fencing_token"], _lease(clock)):
                raise DomainError("LEASE_LOST", "El arrendamiento ya no es de este worker.", status=409)
            elapsed = _seconds_between(started, clock())
            if elapsed > settings.job_timeout_seconds:
                raise DomainError("TIME_LIMIT", "El trabajo superó el tiempo máximo.", status=422)

        options = {
            "ocr_languages": settings.ocr_languages,
            "ocr_page_timeout_seconds": settings.ocr_page_timeout_seconds,
            "max_pdf_pages": settings.max_pdf_pages,
            "max_image_pixels": settings.max_image_pixels,
            "force_ocr_pages": (job.get("options") or {}).get("force_ocr_pages") or [],
            "on_page": on_page,
        }
        result = extract_document(path, original["kind"], options)
        _persist_result(gateway, blobs, settings, worker_id, job, version, original, result, started, clock)
    except DomainError as exc:
        if exc.code == "LEASE_LOST":
            return
        _fail_job(gateway, job, exc.code, clock, permanent=exc.code in PERMANENT, sleep=sleep)
    finally:
        path.unlink(missing_ok=True)


def _persist_result(gateway, blobs, settings, worker_id, job, version, original, result, started, clock) -> None:
    fingerprint = config_fingerprint(result["configuration"])
    existing = gateway.store.find_extraction(version["id"], fingerprint, job["attempt"])
    if existing and existing.get("run_status") in {"succeeded", "partial", "failed"}:
        gateway.store.finish_job(job["id"], job["fencing_token"], {"state": existing["run_status"], "finished_at": clock()})
        return
    units = _expand_units(result["units"])
    full_text = normalize_technical("\n".join(unit["text"] for unit in units if unit["text"]))
    same_text = gateway.store.find_text_fingerprint(version["center_id"], text_fingerprint(full_text), version["id"])
    if same_text:
        result["issues"].append(
            {
                "code": "SOURCE_CONFLICT",
                "severity": "warning",
                "unit_id": None,
                "locator": None,
                "explanation": "Hay otro archivo del centro con el mismo texto normalizado. No se fusionan.",
                "next_action": "El revisor decide si son equivalentes. La igualdad de texto no sustituye ni cambia la vigencia.",
                "resolved": False,
                "resolution": None,
            }
        )
    canonical = {
        "units": [
            {
                "unit_id": unit["unit_id"],
                "order": unit["order"],
                "kind": unit["kind"],
                "method": unit["method"],
                "text": unit["text"],
                "locator": unit["locator"],
            }
            for unit in units
        ]
    }
    json_bytes = json.dumps(canonical, ensure_ascii=False, indent=2).encode("utf-8")
    text_bytes = full_text.encode("utf-8")
    report = json.dumps({"issues": result["issues"]}, ensure_ascii=False, indent=2).encode("utf-8")
    artifacts = []
    for kind, data, mime, filename in (
        ("extraction_json", json_bytes, "application/json", "extraction.json"),
        ("text", text_bytes, "text/plain; charset=utf-8", "text.txt"),
        ("quality_report", report, "application/json", "incidencias.json"),
    ):
        file_id = new_id()
        gridfs_id, sha, size = blobs.put_bytes(data, file_id, filename, mime)
        artifacts.append(
            {
                "id": file_id,
                "kind": kind,
                "file_id": file_id,
                "mime_type": mime,
                "size_bytes": size,
                "sha256": sha,
                "gridfs_id": gridfs_id,
            }
        )
    extraction_id = new_id()
    finished = clock()
    blockers = any(issue.get("severity") == "blocker" and not issue.get("resolved") for issue in result["issues"])
    run_status = result["run_status"]
    if blockers and run_status == "succeeded":
        run_status = "partial"
    extraction = {
        "id": extraction_id,
        "version_id": version["id"],
        "config_fingerprint": fingerprint,
        "attempt": job["attempt"],
        "job_id": job["id"],
        "run_status": run_status,
        "review_status": "unreviewed",
        "engine": result["engine"],
        "engine_version": result["engine_version"],
        "configuration": result["configuration"],
        "started_at": started,
        "completed_at": finished,
        "pages_expected": result["pages_expected"],
        "pages_processed": result["pages_processed"],
        "pages_failed": result["pages_failed"],
        "coverage_complete": result["coverage_complete"] and not blockers,
        "issues": result["issues"],
        "artifacts": [
            {key: artifact[key] for key in ("kind", "file_id", "mime_type", "size_bytes", "sha256")}
            for artifact in artifacts
        ],
        "reviewed_by": None,
        "reviewed_at": None,
        "reviewed_metadata_revision": None,
        "review_scope": None,
        "text_fingerprint": text_fingerprint(full_text),
    }

    def work(tx):
        if not tx.finish_job(job["id"], job["fencing_token"], {"state": run_status, "finished_at": finished, "error_code": None}):
            return False
        try:
            tx.insert_extraction(extraction)
        except StoreConflict:
            return True
        for artifact in artifacts:
            tx.insert_file(
                {
                    "id": artifact["file_id"],
                    "center_id": version["center_id"],
                    "gridfs_id": artifact["gridfs_id"],
                    "sha256": artifact["sha256"],
                    "size_bytes": artifact["size_bytes"],
                    "mime_type": artifact["mime_type"],
                    "kind": "derivative",
                    "persistence_state": "ready",
                    "purpose": "derivative",
                    "owner_id": worker_id,
                    "revision": 1,
                    "display_name": artifact["kind"],
                    "extraction_id": extraction_id,
                    "artifact_kind": artifact["kind"],
                    "created_at": finished,
                    "restricted": False,
                }
            )
        tx.replace_units(extraction_id, _unit_docs(extraction_id, units))
        current = tx.get_version(version["id"])
        if current and may_execute_extraction(current):
            tx.update_version(
                version["id"],
                current["metadata_revision"],
                {"run_status": run_status, "text_fingerprint": extraction["text_fingerprint"]},
            )
        tx.insert_audit(
            {
                "id": new_id(),
                "document_id": version["document_id"],
                "at": finished,
                "actor_id": worker_id,
                "action": "extraction_finished",
                "detail": run_status,
            }
        )
        return True

    committed = gateway.transaction(work)
    if not committed:
        return


def review_extraction(gateway, actor, extraction_id, decision, reason, expected_revision, resolutions, scope, clock) -> dict:
    extraction = gateway.store.get_extraction(extraction_id)
    if not extraction:
        raise DomainError("NOT_FOUND", "No hay una extracción visible.", status=404)
    version = gateway.store.get_version(extraction["version_id"])
    hidden_if_out_of_scope(actor, version["center_id"])
    assert_can_review(actor, version["center_id"], version["submitted_by"])
    assert_expected_revision(version["metadata_revision"], expected_revision)
    if decision not in {"accepted", "rejected"}:
        raise DomainError("VALIDATION", "La revisión de la extracción es accepted o rejected.", status=422)
    if not (reason or "").strip() and decision == "rejected":
        raise DomainError("VALIDATION", "El rechazo necesita un motivo.", status=422, field_errors={"reason": "indica el motivo"})
    issues = [dict(issue) for issue in extraction.get("issues") or []]
    _apply_resolutions(version, issues, resolutions or [])
    if decision == "accepted":
        if extraction.get("run_status") not in {"succeeded", "partial"}:
            raise DomainError("NOT_ACCEPTABLE", "No se puede aceptar una extracción fallida.", status=422)
        if blocking_issues(issues):
            raise DomainError(
                "BLOCKERS_REMAIN",
                "Quedan incidencias bloqueantes. No se aceptan con un botón de ignorar.",
                status=422,
                next_action="Resuelve cada bloqueo: confirma el blanco, marca un falso positivo o pide otra copia.",
            )
        if not extraction.get("pages_processed"):
            raise DomainError("NOT_ACCEPTABLE", "No hay unidades procesadas que aceptar.", status=422)
    at = clock()
    coverage = decision == "accepted" and not blocking_issues(issues)

    def work(tx):
        current = tx.get_version(version["id"])
        assert_expected_revision(current["metadata_revision"], expected_revision)
        tx.update_extraction(
            extraction_id,
            {
                "review_status": decision,
                "issues": issues,
                "coverage_complete": coverage,
                "reviewed_by": actor["id"],
                "reviewed_at": at,
                "reviewed_metadata_revision": expected_revision + 1,
                "review_scope": scope or "",
                "run_status": "succeeded" if coverage else extraction["run_status"],
            },
        )
        fields = {"metadata_revision": expected_revision + 1}
        if decision == "accepted":
            fields["selected_extraction_id"] = extraction_id
            fields["run_status"] = "succeeded" if coverage else extraction["run_status"]
        if not tx.update_version(version["id"], expected_revision, fields):
            raise DomainError("REVISION_CONFLICT", "La ficha cambió. Recárgala.", status=409)
        _revoke_if_ready(tx, actor, current, "Cambió la extracción seleccionada o su revisión.", at)
        tx.insert_review(
            {
                "id": new_id(),
                "actor_id": actor["id"],
                "decision": f"extraction_{decision}",
                "reason": (reason or "").strip(),
                "subject_type": "extraction",
                "subject_id": extraction_id,
                "metadata_revision": expected_revision + 1,
                "extraction_id": extraction_id,
                "at": at,
            }
        )
        return {"extraction_id": extraction_id, "review_status": decision}

    return gateway.transaction(work)


def _apply_resolutions(version, issues, resolutions) -> None:
    allowed = {"blank_confirmed", "false_positive", "needs_better_copy"}
    for item in resolutions:
        code = item.get("code")
        action = item.get("resolution")
        if action not in allowed:
            raise DomainError("VALIDATION", "La resolución de la incidencia no está reconocida.", status=422)
        if action == "false_positive" and version.get("contains_individual_data"):
            raise DomainError(
                "PRIVACY_BLOCKED",
                "La aportación declara datos individuales. No se puede marcar la alerta como falso positivo.",
                status=422,
            )
        matched = False
        for issue in issues:
            if issue.get("code") == code and not issue.get("resolved"):
                if action == "blank_confirmed" and code != "EMPTY_TEXT":
                    continue
                issue["resolved"] = True
                issue["resolution"] = action
                if action == "needs_better_copy":
                    issue["resolved"] = False
                matched = True
                break
        if not matched and action != "needs_better_copy":
            raise DomainError("VALIDATION", "No hay una incidencia abierta con ese código.", status=422)


def _expand_units(units: list[dict]) -> list[dict]:
    expanded = []
    order = 1
    for unit in units:
        parts = split_utf8(unit["text"])
        for index, part in enumerate(parts, start=1):
            copy = dict(unit)
            copy["text"] = part
            copy["order"] = order
            copy["unit_id"] = f"u-{order:04d}"
            if len(parts) > 1:
                copy["part_index"] = index
                copy["part_count"] = len(parts)
            expanded.append(copy)
            order += 1
    return expanded


def _unit_docs(extraction_id: str, units: list[dict]) -> list[dict]:
    docs = []
    for unit in units:
        docs.append(
            {
                "extraction_id": extraction_id,
                "unit_id": unit["unit_id"],
                "order": unit["order"],
                "kind": unit["kind"],
                "method": unit["method"],
                "locator": unit["locator"],
                "text": unit["text"],
                "part_index": unit.get("part_index"),
                "part_count": unit.get("part_count"),
            }
        )
    return docs


def _materialize(blobs, original, temp_dir: Path) -> Path:
    temp_dir.mkdir(parents=True, exist_ok=True)
    target = temp_dir / f"job-{original['id']}"
    blobs.write_to(original["gridfs_id"], target)
    if not blobs.matches(original["gridfs_id"], original["sha256"], original["size_bytes"]):
        target.unlink(missing_ok=True)
        raise OperationalError("BLOB_MISMATCH", "El original descargado no coincide con su hash.")
    return target


def _fail_job(gateway, job, code, clock, permanent: bool, sleep) -> None:
    tries = int(job.get("tries") or 0) + 1
    if permanent or tries >= 3:
        gateway.store.finish_job(
            job["id"],
            job["fencing_token"],
            {"state": "failed", "finished_at": clock(), "error_code": code, "tries": tries},
        )
        return
    gateway.store.release_job(job["id"], job["fencing_token"], tries, code)
    sleep(min(30, 2**tries))


def _lease(clock) -> str:
    from datetime import datetime, timedelta, timezone

    moment = datetime.fromisoformat(clock().replace("Z", "+00:00"))
    return (moment + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds_between(start: str, end: str) -> int:
    from datetime import datetime

    left = datetime.fromisoformat(start.replace("Z", "+00:00"))
    right = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return int((right - left).total_seconds())
