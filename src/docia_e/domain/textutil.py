"""Normalización técnica mínima y partición de unidades. No limpia el significado."""

from __future__ import annotations

import hashlib

# 64 KiB de UTF-8 por unidad de catálogo. No es el troceado semántico de T.
UNIT_TEXT_LIMIT = 64 * 1024


def normalize_technical(text: str) -> str:
    """UTF-8 lógico, finales LF y espacios de cola. Conserva tildes, cifras y «no»."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in cleaned.split("\n")]
    return "\n".join(lines).strip("\n")


def text_fingerprint(text: str) -> str:
    """Huella opcional. No decide vigencia, identidad ni sustitución."""
    normalized = normalize_technical(text)
    payload = f"utf8-lf-trim|{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def split_utf8(text: str, limit: int = UNIT_TEXT_LIMIT) -> list[str]:
    if len(text.encode("utf-8")) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for char in text:
        encoded = len(char.encode("utf-8"))
        if buf and size + encoded > limit:
            parts.append("".join(buf))
            buf = [char]
            size = encoded
        else:
            buf.append(char)
            size += encoded
    if buf:
        parts.append("".join(buf))
    return parts
