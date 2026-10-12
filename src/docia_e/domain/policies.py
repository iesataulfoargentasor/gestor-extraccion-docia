"""Permisos, transiciones y elegibilidad. No consulta MongoDB."""

from __future__ import annotations

from docia_e.domain.errors import DomainError
from docia_e.domain.identity import missing_for_handoff

ROLES = ("collaborator", "reviewer", "administrator", "consumer_t")

DOC_STATES = (
    "pending",
    "in_review",
    "approved_for_processing",
    "returned",
    "rejected",
    "withdrawn",
)
RUN_STATES = ("queued", "running", "succeeded", "partial", "failed", "cancelled")
EXTRACTION_REVIEW = ("unreviewed", "accepted", "rejected")
HANDOFF_STATES = ("not_ready", "ready", "revoked")
RELATIONS = ("replaces", "complements", "distinct", "conflicts", "same_file")

_DOC_TRANSITIONS = {
    "approve_for_processing": {"pending", "in_review"},
    "return": {"pending", "in_review"},
    "reject": {"pending", "in_review", "approved_for_processing"},
    "reopen": {"rejected", "withdrawn", "returned"},
    "withdraw": {"pending", "in_review", "approved_for_processing", "returned"},
    "mark_in_review": {"pending", "returned"},
}

_DECISION_TARGET = {
    "approve_for_processing": "approved_for_processing",
    "return": "returned",
    "reject": "rejected",
    "reopen": "in_review",
    "withdraw": "withdrawn",
    "mark_in_review": "in_review",
}


def has_role(actor: dict, role: str) -> bool:
    return role in set(actor.get("roles") or [])


def centers_of(actor: dict) -> set[str]:
    return set(actor.get("center_ids") or [])


def can_see_center(actor: dict, center_id: str) -> bool:
    if not actor.get("active", True):
        return False
    if has_role(actor, "administrator") and not centers_of(actor):
        return True
    return center_id in centers_of(actor)


def assert_authenticated(actor: dict | None) -> dict:
    if not actor or not actor.get("active", True):
        raise DomainError(
            "AUTH_REQUIRED",
            "Inicia sesión para continuar.",
            status=401,
            next_action="Entra con una cuenta creada por la administración.",
        )
    return actor


def assert_can_upload(actor: dict, center_id: str) -> None:
    assert_authenticated(actor)
    if not has_role(actor, "collaborator"):
        raise DomainError(
            "FORBIDDEN",
            "Tu cuenta no puede crear aportaciones.",
            status=403,
            next_action="Pide una cuenta colaboradora.",
        )
    if center_id not in centers_of(actor):
        raise DomainError(
            "CENTER_FORBIDDEN",
            "No puedes aportar documentos de ese centro.",
            status=403,
        )


def assert_can_review(actor: dict, center_id: str, submitted_by: str) -> None:
    """La aprobación exige rol revisor, otro centro permitido y otra cuenta."""
    assert_authenticated(actor)
    if not has_role(actor, "reviewer"):
        raise DomainError(
            "FORBIDDEN",
            "Tu cuenta no puede revisar ni habilitar entregas.",
            status=403,
            next_action="Esta acción corresponde a una cuenta revisora distinta.",
        )
    if not can_see_center(actor, center_id):
        raise DomainError("NOT_FOUND", "No hay una ficha visible con ese identificador.", status=404)
    if actor.get("id") == submitted_by:
        raise DomainError(
            "SELF_REVIEW_FORBIDDEN",
            "Quien aporta el documento no puede aprobarlo.",
            status=403,
            next_action="Pide la revisión a otra cuenta del centro.",
        )


def assert_can_admin(actor: dict) -> None:
    assert_authenticated(actor)
    if not has_role(actor, "administrator"):
        raise DomainError("FORBIDDEN", "Hace falta una cuenta administradora.", status=403)


def assert_consumer(actor: dict) -> None:
    assert_authenticated(actor)
    if not has_role(actor, "consumer_t"):
        raise DomainError("FORBIDDEN", "Esta API es de solo lectura para el consumidor T.", status=403)


def hidden_if_out_of_scope(actor: dict, center_id: str) -> None:
    assert_authenticated(actor)
    if has_role(actor, "consumer_t") and not has_role(actor, "reviewer"):
        raise DomainError("NOT_FOUND", "No hay una ficha visible con ese identificador.", status=404)
    if not can_see_center(actor, center_id):
        raise DomainError("NOT_FOUND", "No hay una ficha visible con ese identificador.", status=404)


def collaborator_may_read(actor: dict, version: dict) -> bool:
    if has_role(actor, "reviewer") and can_see_center(actor, version["center_id"]):
        return True
    if has_role(actor, "administrator") and can_see_center(actor, version["center_id"]):
        return version.get("handoff_status") == "ready" or version.get("submitted_by") == actor.get("id")
    if not has_role(actor, "collaborator"):
        return False
    if not can_see_center(actor, version["center_id"]):
        return False
    if version.get("submitted_by") == actor.get("id"):
        return True
    return version.get("handoff_status") == "ready"


def transition_document(current: str, decision: str) -> str:
    allowed = _DOC_TRANSITIONS.get(decision)
    if allowed is None or current not in allowed:
        raise DomainError(
            "INVALID_TRANSITION",
            "Esa decisión no corresponde al estado documental actual.",
            status=409,
            next_action="Recarga la ficha y elige una acción posible.",
        )
    return _DECISION_TARGET[decision]


def assert_expected_revision(current: int, expected: int) -> None:
    if int(current) != int(expected):
        raise DomainError(
            "REVISION_CONFLICT",
            "La ficha cambió mientras la revisabas.",
            status=409,
            next_action="Recarga la ficha y repite la decisión sobre la revisión actual.",
        )


def blocking_issues(issues: list[dict]) -> list[dict]:
    return [issue for issue in issues if issue.get("severity") == "blocker" and not issue.get("resolved")]


def extraction_acceptable(extraction: dict) -> list[str]:
    reasons: list[str] = []
    if extraction.get("run_status") not in {"succeeded"}:
        reasons.append("la extracción no terminó en succeeded")
    if extraction.get("review_status") == "rejected":
        reasons.append("la extracción fue rechazada")
    blockers = blocking_issues(extraction.get("issues") or [])
    if blockers:
        reasons.append("quedan incidencias bloqueantes sin resolver")
    if not extraction.get("coverage_complete"):
        reasons.append("la cobertura no está completa o los blancos no están justificados")
    return reasons


def handoff_blockers(version: dict, extraction: dict | None) -> list[str]:
    """Listo para T no se deduce de haber terminado el parser."""
    reasons = list(missing_for_handoff(version))
    if version.get("documentary_state") != "approved_for_processing":
        reasons.append("la versión no está autorizada para procesar")
    if version.get("relation_state") == "pending":
        reasons.append("hay una relación pendiente de decisión")
    if version.get("relation_type") == "conflicts":
        reasons.append("hay un conflicto de fuentes sin resolver")
    if extraction is None:
        reasons.append("no hay una extracción seleccionada")
        return reasons
    if version.get("selected_extraction_id") != extraction.get("id"):
        reasons.append("la extracción seleccionada no es la revisada")
    if extraction.get("review_status") != "accepted":
        reasons.append("la extracción no tiene aceptación humana")
    reasons.extend(extraction_acceptable(extraction))
    if any(issue.get("code") == "PRIVACY_REVIEW_REQUIRED" and not issue.get("resolved") for issue in extraction.get("issues") or []):
        reasons.append("alerta de privacidad sin revisión")
    return reasons


def may_execute_extraction(version: dict) -> bool:
    return version.get("documentary_state") == "approved_for_processing"
