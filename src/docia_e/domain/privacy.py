"""Alertas de privacidad. Un patrón no certifica que el texto sea anónimo."""

from __future__ import annotations

import re

# Patrones orientativos. El revisor decide; el sistema solo bloquea la habilitación.
_PATTERNS = (
    ("dni_like", re.compile(r"\b\d{8}[A-Za-z]\b")),
    ("nie_like", re.compile(r"\b[XYZxyz]\d{7}[A-Za-z]\b")),
    ("expediente", re.compile(r"expediente", re.IGNORECASE)),
    ("entrevista", re.compile(r"entrevista", re.IGNORECASE)),
    ("diagnostico", re.compile(r"diagn[oó]stico", re.IGNORECASE)),
    ("lista_nominativa", re.compile(r"lista de (participantes|alumnado)", re.IGNORECASE)),
    ("recomendacion_individual", re.compile(r"recomendaci[oó]n individual", re.IGNORECASE)),
)


def privacy_hits(text: str) -> list[str]:
    if not text:
        return []
    found = []
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            found.append(name)
    return found


def privacy_issue(hits: list[str], *, unit_id: str | None, locator: dict | None) -> dict:
    return {
        "code": "PRIVACY_REVIEW_REQUIRED",
        "severity": "blocker",
        "unit_id": unit_id,
        "locator": locator,
        "explanation": (
            "Hay señales que pueden corresponder a información individual "
            f"({', '.join(hits)}). Esto no demuestra que existan datos personales "
            "ni que el texto esté anonimizado."
        ),
        "next_action": (
            "Un revisor debe leer el original. Si es un falso positivo de un "
            "recurso general, puede resolver la alerta con motivo. Si hay datos "
            "de alumnado, hay que retirar el acceso y no entregar a T."
        ),
        "resolved": False,
        "resolution": None,
    }
