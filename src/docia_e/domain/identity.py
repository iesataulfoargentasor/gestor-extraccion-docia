"""Identidad lógica por perfil. El título no es una clave."""

from __future__ import annotations

from docia_e.domain.errors import DomainError

PROFILES = (
    "actividades_orientacion",
    "programaciones",
    "pec",
    "planes",
    "oferta",
)
FAMILIES = ("activity", "guidance")
EVENT_STATUSES = (
    "planned",
    "confirmed",
    "modified",
    "cancelled",
    "completed",
    "unknown",
)
AUDIENCES = ("students", "families", "teachers", "staff", "community")
RESOURCE_TYPES = ("guide", "procedure")

# Una fecha futura no confirma el evento: el estado lo decide la persona.
SUBSTANTIAL_FIELDS = (
    "audience",
    "source_reference",
    "issuer",
    "source_url",
    "publication_date",
    "valid_from",
    "valid_to",
    "academic_year",
    "event_start",
    "event_end",
    "event_status",
    "location",
    "general_scope",
    "resource_type",
    "declared_version",
    "relation_type",
    "selected_extraction_id",
)


def _clean(value: str | None) -> str:
    return (value or "").strip()


def logical_identity(meta: dict) -> tuple[bool, str | None]:
    """Devuelve (resuelta, clave). Sin datos de identidad no fabrica una clave vacía."""
    profile = meta.get("profile")
    center = _clean(meta.get("center_id"))
    if not center or not profile:
        return False, None
    if profile == "actividades_orientacion":
        return _group5(center, meta)
    if profile == "programaciones":
        module = _clean(meta.get("module_code"))
        year = _clean(meta.get("academic_year"))
        if module and year:
            return True, f"programaciones|{center}|{module}|{year}"
        return False, None
    if profile == "pec":
        family = _clean(meta.get("pec_family"))
        if family:
            return True, f"pec|{center}|{family}"
        return False, None
    if profile == "planes":
        kind = _clean(meta.get("plan_type"))
        period = _clean(meta.get("plan_period"))
        if kind and period:
            return True, f"planes|{center}|{kind}|{period}"
        return False, None
    if profile == "oferta":
        teaching = _clean(meta.get("teaching"))
        shift = _clean(meta.get("shift"))
        year = _clean(meta.get("academic_year"))
        if teaching and shift and year:
            return True, f"oferta|{center}|{teaching}|{shift}|{year}"
        return False, None
    raise DomainError("PROFILE_UNKNOWN", "El perfil no está reconocido.", status=422)


def _group5(center: str, meta: dict) -> tuple[bool, str | None]:
    family = meta.get("family")
    if family == "activity":
        event_id = _clean(meta.get("event_id"))
        edition = _clean(meta.get("edition"))
        if event_id and edition:
            return True, f"activity|{center}|{event_id}|{edition}"
        return False, None
    if family == "guidance":
        resource = _clean(meta.get("resource_type"))
        audience = _audience_key(meta.get("audience") or [])
        version = _clean(meta.get("declared_version"))
        period = _clean(meta.get("academic_year")) or _clean(meta.get("valid_from"))
        if resource and audience and version and period:
            return True, f"guidance|{center}|{resource}|{audience}|{version}|{period}"
        return False, None
    return False, None


def _audience_key(audience: list) -> str:
    values = sorted({str(item).strip() for item in audience if str(item).strip()})
    return ",".join(values)


def metadata_compatible(existing: dict, incoming: dict) -> bool:
    """Mismos bytes con otra ficha de identidad no se tratan como copia automática."""
    if existing.get("center_id") != incoming.get("center_id"):
        return False
    if existing.get("profile") != incoming.get("profile"):
        return False
    if existing.get("family") != incoming.get("family"):
        return False
    for field in ("event_id", "edition", "resource_type", "declared_version"):
        left = _clean(existing.get(field))
        right = _clean(incoming.get(field))
        if left and right and left != right:
            return False
    left_audience = _audience_key(existing.get("audience") or [])
    right_audience = _audience_key(incoming.get("audience") or [])
    if left_audience and right_audience and left_audience != right_audience:
        return False
    return True


def substantial_change(before: dict, after: dict) -> bool:
    for field in SUBSTANTIAL_FIELDS:
        if _normalize_compare(before.get(field)) != _normalize_compare(after.get(field)):
            return True
    return False


def _normalize_compare(value):
    if isinstance(value, list):
        return tuple(sorted(str(item) for item in value))
    if isinstance(value, str):
        return value.strip()
    return value


def validate_submission(meta: dict) -> dict[str, str]:
    """Errores de formulario. Los desconocidos quedan nulos; no se rellenan."""
    errors: dict[str, str] = {}
    if meta.get("profile") not in PROFILES:
        errors["profile"] = "Selecciona un perfil reconocido."
    if not _clean(meta.get("center_id")):
        errors["center_id"] = "Indica el centro."
    if not _clean(meta.get("title")):
        errors["title"] = "Indica un título para localizar la aportación."
    family = meta.get("family")
    if meta.get("profile") == "actividades_orientacion" and family not in FAMILIES:
        errors["family"] = "Elige actividad u orientación general."
    audience = meta.get("audience") or []
    unknown = [item for item in audience if item not in AUDIENCES]
    if unknown:
        errors["audience"] = "Hay un código de audiencia que no está en el catálogo."
    status = _clean(meta.get("event_status"))
    if status and status not in EVENT_STATUSES:
        errors["event_status"] = "El estado del evento no está en el catálogo."
    resource = _clean(meta.get("resource_type"))
    if resource and resource not in RESOURCE_TYPES:
        errors["resource_type"] = "El tipo de recurso no está en el catálogo."
    if family == "guidance" and meta.get("general_scope") is False:
        errors["general_scope"] = (
            "La orientación individualizada queda fuera del corpus. "
            "No la subas como recurso general."
        )
    return errors


def missing_for_handoff(meta: dict) -> list[str]:
    """Lo que impide habilitar T. Registrar y extraer sí está permitido."""
    missing: list[str] = []
    resolved, _key = logical_identity(meta)
    if not resolved:
        missing.append("identidad lógica sin confirmar")
    if not _clean(meta.get("issuer")) or not _clean(meta.get("source_reference")):
        missing.append("fuente identificada")
    if not (meta.get("audience") or []):
        missing.append("audiencia")
    family = meta.get("family")
    if family == "activity" and not _clean(meta.get("event_start")):
        missing.append("fecha de la actividad")
    if family == "guidance":
        if meta.get("general_scope") is not True:
            missing.append("carácter general autorizado")
        if not (_clean(meta.get("academic_year")) or _clean(meta.get("valid_from"))):
            missing.append("referencia temporal")
    if meta.get("contains_individual_data") is True:
        missing.append("datos individuales declarados")
    return missing
