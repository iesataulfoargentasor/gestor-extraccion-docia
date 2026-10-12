"""Utilidades de aplicación: identificadores, huellas y copia congelada."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from docia_e.domain.errors import DomainError

SNAPSHOT_FIELDS = (
    "profile",
    "family",
    "center_id",
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
    "resource_status",
    "event_id",
    "edition",
    "location",
    "resource_type",
    "declared_version",
    "general_scope",
    "observations",
)

FINGERPRINT_FIELDS = SNAPSHOT_FIELDS + ("contains_individual_data",)


class StoreConflict(Exception):
    def __init__(self, index: str) -> None:
        super().__init__(index)
        self.index = index


def new_id() -> str:
    return str(uuid.uuid4())


def spool_and_hash(stream, dest: Path, max_bytes: int, max_mib: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with dest.open("wb") as handle:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise DomainError(
                    "SIZE_LIMIT",
                    f"El archivo supera el límite de {max_mib} MiB (1 MiB = 1 048 576 bytes).",
                    status=413,
                )
            digest.update(chunk)
            handle.write(chunk)
    return digest.hexdigest(), total


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def request_fingerprint(meta: dict, sha256: str) -> str:
    body = {field: meta.get(field) for field in FINGERPRINT_FIELDS}
    body["sha256"] = sha256
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def snapshot_from(version: dict, relations: list[dict] | None = None) -> dict:
    frozen = {field: version.get(field) for field in SNAPSHOT_FIELDS}
    frozen["relations"] = relations or []
    return frozen


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
