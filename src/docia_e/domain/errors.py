"""Errores de dominio. Los controladores los traducen; no conocen Flask."""

from __future__ import annotations


class DomainError(Exception):
    """Fallo de regla de negocio o de validación, con código público estable."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 422,
        field_errors: dict[str, str] | None = None,
        next_action: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.field_errors = field_errors or {}
        self.next_action = next_action

    def public(self, request_id: str) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "field_errors": self.field_errors,
            "next_action": self.next_action,
            "request_id": request_id,
        }


class OperationalError(DomainError):
    """El sistema no puede completar la operación. No implica que se haya guardado."""

    def __init__(self, code: str, message: str, *, next_action: str = "") -> None:
        super().__init__(code, message, status=503, next_action=next_action)
