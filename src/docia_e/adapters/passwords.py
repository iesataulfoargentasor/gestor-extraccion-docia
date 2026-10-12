"""Contraseñas y tokens de servicio. El secreto en claro no se guarda."""

from __future__ import annotations

import hashlib
import secrets

from werkzeug.security import check_password_hash, generate_password_hash


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


def new_service_secret() -> tuple[str, str, str]:
    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    presented = f"{token_id}.{secret}"
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return presented, token_id, digest


def parse_bearer(header: str | None) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("bearer "):
        return None
    token = header.split(" ", 1)[1].strip()
    if "." not in token:
        return None
    token_id, secret = token.split(".", 1)
    if not token_id or not secret:
        return None
    return token_id, secret


def secret_matches(secret: str, digest: str) -> bool:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest() == digest
