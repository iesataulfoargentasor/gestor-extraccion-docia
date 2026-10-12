"""Reglas de dominio que no necesitan MongoDB, Flask ni Tesseract."""

import ast
from pathlib import Path

import pytest

from docia_e.domain.contract import build_package
from docia_e.domain.errors import DomainError
from docia_e.domain.identity import logical_identity, metadata_compatible, missing_for_handoff
from docia_e.domain.policies import (
    assert_can_review,
    handoff_blockers,
    transition_document,
)
from docia_e.domain.privacy import privacy_hits
from docia_e.domain.textutil import normalize_technical, split_utf8, text_fingerprint
from docia_e.domain.timeutil import exclusive_end


def test_domain_no_importa_adaptadores():
    root = Path(__file__).resolve().parents[2] / "src" / "docia_e" / "domain"
    forbidden = ("flask", "pymongo", "gridfs", "pytesseract", "pypdf", "docx")
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.Import):
                module = node.names[0].name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
            assert not module.startswith(forbidden)


def test_identidad_de_actividad_no_usa_la_fecha():
    base = {
        "profile": "actividades_orientacion",
        "center_id": "centro-demo",
        "family": "activity",
        "event_id": "evento-1",
        "edition": "2026-2027",
        "event_start": "2026-11-05T17:00:00+01:00",
    }
    resolved, key = logical_identity(base)
    moved = dict(base, event_start="2026-12-01T10:00:00+01:00")
    assert resolved
    assert logical_identity(moved)[1] == key
    other_year = dict(base, edition="2027-2028")
    assert logical_identity(other_year)[1] != key


def test_sin_identidad_no_fabrica_clave_vacia():
    resolved, key = logical_identity(
        {"profile": "actividades_orientacion", "center_id": "centro-demo", "family": "activity"}
    )
    assert resolved is False
    assert key is None


def test_mismo_nombre_no_hace_incompatibles_los_bytes():
    existing = {"center_id": "c", "profile": "actividades_orientacion", "family": "activity", "title": "A", "event_id": "e", "edition": "2026"}
    incoming = dict(existing, title="B")
    assert metadata_compatible(existing, incoming)
    assert not metadata_compatible(existing, dict(incoming, edition="2027"))


def test_fecha_de_actividad_bloquea_t_sin_inventarla():
    version = {
        "profile": "actividades_orientacion",
        "center_id": "c",
        "family": "activity",
        "event_id": "e",
        "edition": "2026-2027",
        "issuer": "Centro",
        "source_reference": "circular",
        "audience": ["students"],
        "documentary_state": "approved_for_processing",
        "relation_state": "none",
        "selected_extraction_id": "ext",
        "contains_individual_data": False,
    }
    missing = missing_for_handoff(version)
    assert "fecha de la actividad" in missing
    blockers = handoff_blockers(version, None)
    assert blockers


def test_revisor_no_aprueba_su_propia_aportacion():
    actor = {"id": "misma", "roles": ["reviewer"], "center_ids": ["c"], "active": True}
    with pytest.raises(DomainError) as caught:
        assert_can_review(actor, "c", "misma")
    assert caught.value.code == "SELF_REVIEW_FORBIDDEN"


def test_colaborador_no_revisa():
    actor = {"id": "col", "roles": ["collaborator"], "center_ids": ["c"], "active": True}
    with pytest.raises(DomainError) as caught:
        assert_can_review(actor, "c", "otra")
    assert caught.value.code == "FORBIDDEN"


def test_reabrir_un_rechazo_vuelve_a_revision():
    assert transition_document("rejected", "reopen") == "in_review"
    with pytest.raises(DomainError):
        transition_document("rejected", "approve_for_processing")


def test_normalizacion_conserva_no_tildes_y_porcentaje():
    text = "No\r\norientación 40 %  "
    normalized = normalize_technical(text)
    assert "No" in normalized
    assert "orientación" in normalized
    assert "40 %" in normalized
    assert text_fingerprint(normalized) == text_fingerprint("No\norientación 40 %")


def test_particion_respeta_el_limite_utf8():
    parts = split_utf8("á" * 100, limit=4)
    assert all(len(part.encode("utf-8")) <= 4 for part in parts)
    assert "".join(parts) == "á" * 100


def test_validez_inclusive_pasa_a_limite_exclusivo():
    assert exclusive_end("2026-06-30") == "2026-07-01"


def test_privacidad_no_certifica_anonimato():
    hits = privacy_hits("Informe de entrevista y DNI 12345678Z")
    assert "entrevista" in hits
    assert "dni_like" in hits


def test_paquete_json_con_nulos_y_localizador_docx():
    package = build_package(
        handoff={
            "id": "h1",
            "document_id": "d1",
            "version_id": "v1",
            "extraction_id": "e1",
            "metadata_revision": 2,
            "status": "ready",
            "permitted_use": "general_center_information",
            "authorized_by": "revisor",
            "authorized_at": "2026-10-12T10:11:00Z",
            "snapshot": {
                "profile": "actividades_orientacion",
                "family": "guidance",
                "center_id": "centro-demo",
                "title": "Guía con ñ y <script>",
                "audience": ["students"],
                "academic_year": None,
                "issuer": "Centro",
                "source_reference": "nota",
                "source_url": None,
                "publication_date": None,
                "consulted_at": None,
                "authorization_reference": None,
                "valid_from": None,
                "valid_to": None,
                "event_start": None,
                "event_end": None,
                "event_timezone": None,
                "event_status": None,
                "resource_status": None,
                "event_id": None,
                "edition": None,
                "location": None,
                "relations": [],
            },
        },
        version={},
        extraction={
            "id": "e1",
            "engine": "python-docx",
            "engine_version": "test",
            "config_fingerprint": "abc",
            "configuration": {"mode": "body-order"},
            "started_at": "2026-10-12T10:00:00Z",
            "completed_at": "2026-10-12T10:00:01Z",
            "run_status": "succeeded",
            "artifacts": [],
            "pages_expected": 1,
            "pages_processed": 1,
            "pages_failed": 0,
            "coverage_complete": True,
            "issues": [],
            "review_status": "accepted",
            "reviewed_by": "revisor",
            "reviewed_at": "2026-10-12T10:10:00Z",
            "reviewed_metadata_revision": 2,
            "review_scope": "bloque",
        },
        original={
            "id": "f1",
            "display_name": "guia.docx",
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "size_bytes": 10,
            "sha256": "0" * 64,
        },
        units=[
            {
                "unit_id": "u-0001",
                "order": 1,
                "kind": "paragraph",
                "method": "digital",
                "text": "No es un 40 %.",
                "locator": {"type": "docx_block", "block_index": 1, "page": None},
            }
        ],
    )
    import json

    encoded = json.dumps(package, ensure_ascii=False)
    assert "ñ" in encoded or "\\u00f1" in encoded or "script" in encoded
    assert package["units"][0]["locator"]["page"] is None
    assert package["academic_year"] is None
    assert "chunk_id" not in encoded
    assert "ObjectId" not in encoded
    schema_path = Path(__file__).resolve().parents[2] / "src" / "docia_e" / "schema" / "handoff-1.0.json"
    import jsonschema

    jsonschema.validate(package, json.loads(schema_path.read_text(encoding="utf-8")))
