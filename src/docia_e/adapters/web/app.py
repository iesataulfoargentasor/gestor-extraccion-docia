"""Aplicación Flask. Las plantillas no consultan MongoDB."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from flask import Flask, g, jsonify, request
from pymongo.errors import PyMongoError
from werkzeug.exceptions import HTTPException

from docia_e.adapters.web.routes import register_routes
from docia_e.adapters.web.security import csrf_token
from docia_e.application.support import StoreConflict
from docia_e.domain.errors import DomainError, OperationalError
from docia_e.domain.timeutil import format_instant_madrid

log = logging.getLogger("docia_e.web")


def create_app(settings, gateway, blobs) -> Flask:
    web_dir = Path(__file__).resolve().parent
    app = Flask(
        __name__,
        template_folder=str(web_dir / "templates"),
        static_folder=str(web_dir / "static"),
    )
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.cookie_secure,
        MAX_CONTENT_LENGTH=settings.max_upload_bytes + (1 * 1024 * 1024),
        TEMPLATES_AUTO_RELOAD=True,
    )
    app.extensions["docia_settings"] = settings
    app.extensions["docia_gateway"] = gateway
    app.extensions["docia_blobs"] = blobs

    @app.before_request
    def prepare():
        g.request_id = uuid.uuid4().hex
        g.started = __import__("time").monotonic()

    @app.after_request
    def access(response):
        duration_ms = int((__import__("time").monotonic() - getattr(g, "started", 0)) * 1000)
        log.info("request_id=%s method=%s path=%s status=%s duration_ms=%s", g.get("request_id"), request.method, request.path, response.status_code, duration_ms)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Request-Id"] = g.get("request_id", "")
        return response

    @app.context_processor
    def inject():
        return {
            "csrf_token": csrf_token,
            "actor": getattr(g, "actor", None),
            "format_instant": format_instant_madrid,
            "request_id": getattr(g, "request_id", ""),
        }

    @app.errorhandler(DomainError)
    def domain_error(exc: DomainError):
        if _wants_json():
            return jsonify(exc.public(g.get("request_id", ""))), exc.status
        from flask import render_template

        return render_template("error.html", error=exc), exc.status

    @app.errorhandler(StoreConflict)
    def store_conflict(_exc: StoreConflict):
        err = DomainError(
            "REVISION_CONFLICT",
            "La operación chocó con otro cambio simultáneo.",
            status=409,
            next_action="Recarga la ficha y repite la decisión.",
        )
        return domain_error(err)

    @app.errorhandler(PyMongoError)
    def mongo_error(_exc: PyMongoError):
        err = OperationalError(
            "MONGO_UNAVAILABLE",
            "MongoDB no ha confirmado la operación. No des por guardado el formulario.",
            next_action="Comprueba que el replica set local está en marcha y repite la acción.",
        )
        return domain_error(err)

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        if exc.code == 413:
            err = DomainError("SIZE_LIMIT", "La petición supera el límite de tamaño.", status=413)
            return domain_error(err)
        if _wants_json():
            return jsonify({"code": "HTTP_ERROR", "message": exc.description, "field_errors": {}, "next_action": "", "request_id": g.get("request_id", "")}), exc.code
        from flask import render_template

        err = DomainError("HTTP_ERROR", exc.description or "Error", status=exc.code or 500)
        return render_template("error.html", error=err), exc.code or 500

    register_routes(app)
    return app


def _wants_json() -> bool:
    if request.path.startswith("/api/") or request.path.startswith("/jobs/"):
        return True
    best = request.accept_mimetypes.best
    return best == "application/json"
