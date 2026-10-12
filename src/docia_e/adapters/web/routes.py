"""Rutas HTTP. Autorizan y traducen errores; las reglas viven en los casos de uso."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from flask import Response, abort, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from docia_e.adapters.extraction.service import ocr_status
from docia_e.adapters.passwords import hash_password, parse_bearer, secret_matches, verify_password
from docia_e.adapters.web.security import allow, csrf_ok
from docia_e.application.extractions import request_extraction, review_extraction
from docia_e.application.handoffs import authorize_handoff, list_changes, list_ready, package_for, revoke_handoff
from docia_e.application.queries import dashboard, search_catalog, version_detail
from docia_e.application.reviews import resolve_relation, review_version
from docia_e.application.uploads import receive_contribution
from docia_e.domain.errors import DomainError, OperationalError
from docia_e.domain.identity import AUDIENCES, EVENT_STATUSES, RESOURCE_TYPES
from docia_e.domain.policies import assert_can_admin, can_see_center, has_role
from docia_e.domain.timeutil import now_utc_z

_DUMMY_HASH = hash_password("dummy-password-not-used")


class LoginRequired(Exception):
    """La página HTML exige sesión. No es un fallo de la API."""


def register_routes(app):
    @app.errorhandler(LoginRequired)
    def login_required(_exc):
        return redirect(url_for("login"))

    @app.before_request
    def load_actor():
        g.actor = None
        if request.endpoint == "static":
            return
        if request.path.startswith("/api/"):
            g.actor = _actor_from_bearer(app)
            return
        user_id = session.get("user_id")
        if not user_id:
            return
        user = _gateway(app).store.get_user(user_id)
        if user and user.get("active", True):
            g.actor = _public_actor(user)

    @app.before_request
    def guard_csrf():
        if request.method != "POST" or request.path.startswith("/api/"):
            return
        if not csrf_ok(request.form.get("csrf_token")):
            raise DomainError("CSRF", "La sesión del formulario no es válida. Recarga la página.", status=400)

    @app.get("/")
    def home():
        if g.actor:
            return redirect(url_for("dashboard_view"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            ip = request.remote_addr or "local"
            login_name = (request.form.get("login") or "").strip().lower()
            if not allow(f"login:{ip}:{login_name}", 5, 60):
                raise DomainError("RATE_LIMIT", "Demasiados intentos. Espera un minuto.", status=429)
            user = _gateway(app).store.get_user_by_login(login_name)
            password = request.form.get("password") or ""
            valid = bool(user and user.get("active", True) and verify_password(password, user.get("password_hash", "")))
            if not valid:
                verify_password(password, _DUMMY_HASH)
                error = "Credenciales no válidas."
            else:
                session.clear()
                session["user_id"] = user["id"]
                return redirect(url_for("dashboard_view"))
        return render_template("login.html", error=error)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/dashboard")
    def dashboard_view():
        _need_session()
        data = dashboard(_gateway(app), g.actor)
        heartbeat = _gateway(app).store.latest_heartbeat()
        return render_template("dashboard.html", counts=data, heartbeat=heartbeat, ocr=ocr_status(), maintenance=_gateway(app).store.maintenance_enabled())

    @app.route("/uploads/new", methods=["GET", "POST"])
    def upload_new():
        _need_session()
        settings = app.extensions["docia_settings"]
        form = _upload_form_defaults(settings)
        if request.method == "POST":
            actor_key = g.actor["id"]
            if not allow(f"upload:{actor_key}", 20, 3600):
                raise DomainError("RATE_LIMIT", "Has superado el límite de cargas de esta hora.", status=429)
            form.update(_posted_meta(settings))
            form["idempotency_key"] = request.form.get("idempotency_key") or form["idempotency_key"]
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                raise DomainError("VALIDATION", "Falta el archivo.", status=422, field_errors={"file": "elige un archivo"})
            try:
                result = receive_contribution(
                    _gateway(app),
                    app.extensions["docia_blobs"],
                    g.actor,
                    form,
                    upload.stream,
                    upload.filename,
                    request.form.get("idempotency_key") or "",
                    settings.limits(),
                    settings.temp_dir,
                    now_utc_z,
                )
            except DomainError as exc:
                if _json():
                    raise
                return render_template(
                    "upload.html",
                    form=form,
                    error=exc,
                    audiences=AUDIENCES,
                    event_statuses=EVENT_STATUSES,
                    resource_types=RESOURCE_TYPES,
                    limits=settings.limits(),
                ), exc.status
            if _json():
                return jsonify(result), 200
            return render_template("result.html", result=result)
        return render_template(
            "upload.html",
            form=form,
            error=None,
            audiences=AUDIENCES,
            event_statuses=EVENT_STATUSES,
            resource_types=RESOURCE_TYPES,
            limits=settings.limits(),
        )

    @app.get("/documents")
    def documents():
        _need_session()
        page = search_catalog(_gateway(app), g.actor, request.args, request.args.get("cursor"), 20)
        return render_template("documents.html", page=page, filters=request.args)

    @app.get("/documents/<document_id>")
    def document_detail(document_id):
        _need_session()
        store = _gateway(app).store
        document = store.get_document(document_id)
        if not document or not can_see_center(g.actor, document["center_id"]):
            raise DomainError("NOT_FOUND", "No hay una ficha visible con ese identificador.", status=404)
        versions = store.list_versions(document_id)
        visible = [version for version in versions if _can_read_version(version)]
        if not visible:
            raise DomainError("NOT_FOUND", "No hay una ficha visible con ese identificador.", status=404)
        selected = request.args.get("version") or visible[-1]["id"]
        detail = version_detail(_gateway(app), g.actor, selected)
        return render_template("detail.html", detail=detail, versions=visible)

    @app.post("/versions/<version_id>/review")
    def version_review(version_id):
        _need_session()
        result = review_version(
            _gateway(app),
            g.actor,
            version_id,
            request.form.get("decision") or "",
            request.form.get("reason") or "",
            int(request.form.get("expected_revision") or 0),
            _meta_patch(request.form),
            now_utc_z,
        )
        return redirect(url_for("document_detail", document_id=_document_of(app, version_id), version=result["version_id"]))

    @app.post("/versions/<version_id>/relations")
    def version_relation(version_id):
        _need_session()
        resolve_relation(
            _gateway(app),
            g.actor,
            version_id,
            request.form.get("relation") or "",
            request.form.get("target_id") or None,
            request.form.get("reason") or "",
            int(request.form.get("expected_revision") or 0),
            now_utc_z,
        )
        return redirect(url_for("document_detail", document_id=_document_of(app, version_id), version=version_id))

    @app.post("/versions/<version_id>/extractions")
    def enqueue_extraction(version_id):
        _need_session()
        pages = [int(item) for item in request.form.getlist("force_ocr_pages") if item.isdigit()]
        result = request_extraction(
            _gateway(app),
            g.actor,
            version_id,
            int(request.form.get("expected_revision") or 0),
            pages,
            now_utc_z,
        )
        if _json():
            return jsonify(result), 202
        return redirect(url_for("job_view", job_id=result["job_id"]))

    @app.get("/jobs/<job_id>")
    def job_view(job_id):
        _need_session()
        job = _gateway(app).store.get_job(job_id)
        if not job:
            raise DomainError("NOT_FOUND", "No hay un trabajo visible.", status=404)
        version = _gateway(app).store.get_version(job["version_id"])
        if not version or not _can_read_version(version):
            raise DomainError("NOT_FOUND", "No hay un trabajo visible.", status=404)
        payload = {"job_id": job["id"], "state": job["state"], "error_code": job.get("error_code"), "attempt": job["attempt"]}
        if _json() or request.args.get("format") == "json":
            return jsonify(payload)
        return render_template("job.html", job=payload, version=version)

    @app.get("/extractions/<extraction_id>")
    def extraction_view(extraction_id):
        _need_session()
        extraction = _gateway(app).store.get_extraction(extraction_id)
        if not extraction:
            raise DomainError("NOT_FOUND", "No hay una extracción visible.", status=404)
        version = _gateway(app).store.get_version(extraction["version_id"])
        if not _can_read_version(version):
            raise DomainError("NOT_FOUND", "No hay una extracción visible.", status=404)
        after = int(request.args.get("after") or 0)
        units = _gateway(app).store.list_units(extraction_id, after, 20)
        return render_template("extraction.html", extraction=extraction, version=version, units=units, after=after)

    @app.post("/extractions/<extraction_id>/review")
    def extraction_review(extraction_id):
        _need_session()
        resolutions = []
        codes = request.form.getlist("issue_code")
        actions = request.form.getlist("issue_resolution")
        for code, action in zip(codes, actions):
            if action:
                resolutions.append({"code": code, "resolution": action})
        review_extraction(
            _gateway(app),
            g.actor,
            extraction_id,
            request.form.get("decision") or "",
            request.form.get("reason") or "",
            int(request.form.get("expected_revision") or 0),
            resolutions,
            request.form.get("review_scope") or "",
            now_utc_z,
        )
        extraction = _gateway(app).store.get_extraction(extraction_id)
        return redirect(url_for("extraction_view", extraction_id=extraction["id"]))

    @app.post("/versions/<version_id>/handoffs")
    def create_handoff(version_id):
        _need_session()
        result = authorize_handoff(
            _gateway(app),
            g.actor,
            version_id,
            int(request.form.get("expected_revision") or 0),
            request.form.get("extraction_id") or "",
            request.form.get("reason") or "",
            now_utc_z,
        )
        return redirect(url_for("document_detail", document_id=_document_of(app, version_id), version=version_id, handoff=result["handoff_id"]))

    @app.post("/handoffs/<handoff_id>/revoke")
    def revoke(handoff_id):
        _need_session()
        revoke_handoff(
            _gateway(app),
            g.actor,
            handoff_id,
            request.form.get("reason") or "",
            request.form.get("reason_code") or "",
            now_utc_z,
        )
        handoff = _gateway(app).store.get_handoff(handoff_id)
        return redirect(url_for("document_detail", document_id=handoff["document_id"], version=handoff["version_id"]))

    @app.get("/files/<file_id>/download")
    def download(file_id):
        actor = g.actor or _actor_from_bearer(app)
        if not actor:
            raise DomainError("AUTH_REQUIRED", "Inicia sesión para descargar.", status=401)
        record = _gateway(app).store.get_file(file_id)
        if not record:
            raise DomainError("NOT_FOUND", "No hay un archivo visible.", status=404)
        _authorize_download(_gateway(app).store, actor, record)
        name = secure_filename(record.get("display_name") or "archivo") or "archivo"
        stream = app.extensions["docia_blobs"].open(record["gridfs_id"])
        response = Response(stream, mimetype=record["mime_type"])
        response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/etl/v1/handoffs")
    def api_handoffs():
        payload = list_ready(_gateway(app), _need_api(), request.args.get("cursor"), _limit(), request.args.get("family"))
        return jsonify(payload)

    @app.get("/api/etl/v1/handoffs/<handoff_id>")
    def api_handoff(handoff_id):
        package, status = package_for(_gateway(app), _need_api(), handoff_id)
        return jsonify(package), status

    @app.get("/api/etl/v1/changes")
    def api_changes():
        raw = request.args.get("cursor")
        cursor = int(raw) if raw not in {None, ""} else None
        if raw not in {None, ""}:
            try:
                cursor = int(raw)
            except ValueError as exc:
                raise DomainError("BAD_REQUEST", "El cursor tiene que ser la secuencia anterior.", status=400) from exc
        return jsonify(list_changes(_gateway(app), _need_api(), cursor, _limit()))

    @app.get("/admin")
    def admin():
        _need_session()
        assert_can_admin(g.actor)
        store = _gateway(app).store
        users = store.list_users()
        heartbeat = store.latest_heartbeat()
        try:
            topology = store.topology()
        except Exception:
            topology = {"set_name": None, "writable_primary": False}
        return render_template("admin.html", users=users, heartbeat=heartbeat, topology=topology, ocr=ocr_status(), maintenance=store.maintenance_enabled())

    @app.get("/health")
    def health():
        body = {"web": "up", "mongo": "down", "topology": None, "worker": "unknown", "ocr": ocr_status()}
        status = 503
        try:
            topology = _gateway(app).store.topology()
            body["mongo"] = "up"
            body["topology"] = topology.get("set_name")
            heartbeat = _gateway(app).store.latest_heartbeat()
            body["worker"] = _worker_state(heartbeat)
            status = 200 if topology.get("set_name") else 503
        except Exception:
            body["mongo"] = "down"
        return jsonify(body), status


def _gateway(app):
    return app.extensions["docia_gateway"]


def _need_session():
    if not g.actor:
        raise LoginRequired()
    return g.actor


def _need_api():
    if not g.actor:
        raise DomainError("AUTH_REQUIRED", "Falta el token de servicio.", status=401)
    return g.actor


def _json() -> bool:
    return request.accept_mimetypes.best == "application/json" or request.args.get("format") == "json"


def _limit() -> int:
    try:
        return max(1, min(int(request.args.get("limit") or 50), 100))
    except ValueError:
        return 50


def _public_actor(user: dict) -> dict:
    return {
        "id": user["id"],
        "login": user.get("login"),
        "roles": list(user.get("roles") or []),
        "center_ids": list(user.get("center_ids") or []),
        "active": user.get("active", True),
    }


def _actor_from_bearer(app):
    parsed = parse_bearer(request.headers.get("Authorization"))
    if not parsed:
        return None
    token_id, secret = parsed
    token = _gateway(app).store.get_token(token_id)
    if not token or not token.get("active") or not secret_matches(secret, token.get("secret_hash", "")):
        return None
    user = _gateway(app).store.get_user(token["user_id"])
    if not user or not user.get("active", True):
        return None
    actor = _public_actor(user)
    if "consumer_t" not in actor["roles"]:
        actor["roles"].append("consumer_t")
    return actor


def _upload_form_defaults(settings) -> dict:
    return {
        "profile": settings.document_profile,
        "family": "activity",
        "center_id": "",
        "title": "",
        "audience": [],
        "issuer": "",
        "source_reference": "",
        "source_url": "",
        "publication_date": "",
        "valid_from": "",
        "valid_to": "",
        "academic_year": "",
        "event_id": "",
        "edition": "",
        "event_start": "",
        "event_end": "",
        "event_timezone": "Europe/Madrid",
        "event_status": "",
        "location": "",
        "resource_type": "",
        "declared_version": "",
        "general_scope": "",
        "observations": "",
        "authorization_reference": "",
        "contains_individual_data": False,
        "idempotency_key": str(uuid.uuid4()),
    }


def _posted_meta(settings) -> dict:
    audience = request.form.getlist("audience")
    return {
        "profile": settings.document_profile,
        "family": request.form.get("family") or "",
        "center_id": (request.form.get("center_id") or "").strip(),
        "title": request.form.get("title") or "",
        "audience": audience,
        "issuer": request.form.get("issuer") or "",
        "source_reference": request.form.get("source_reference") or "",
        "source_url": request.form.get("source_url") or "",
        "publication_date": request.form.get("publication_date") or "",
        "valid_from": request.form.get("valid_from") or "",
        "valid_to": request.form.get("valid_to") or "",
        "valid_to_inclusive": request.form.get("valid_to_inclusive"),
        "academic_year": request.form.get("academic_year") or "",
        "event_id": request.form.get("event_id") or "",
        "edition": request.form.get("edition") or "",
        "event_start": request.form.get("event_start") or "",
        "event_end": request.form.get("event_end") or "",
        "event_timezone": request.form.get("event_timezone") or "Europe/Madrid",
        "event_status": request.form.get("event_status") or "",
        "location": request.form.get("location") or "",
        "resource_type": request.form.get("resource_type") or "",
        "declared_version": request.form.get("declared_version") or "",
        "general_scope": request.form.get("general_scope"),
        "observations": request.form.get("observations") or "",
        "authorization_reference": request.form.get("authorization_reference") or "",
        "contains_individual_data": request.form.get("contains_individual_data") == "on",
    }


def _meta_patch(form) -> dict:
    patch = {}
    for field in ("title", "issuer", "source_reference", "event_status", "event_start", "event_id", "edition", "location", "academic_year", "audience"):
        if field in form:
            patch[field] = form.getlist(field) if field == "audience" else form.get(field)
    return patch


def _can_read_version(version) -> bool:
    actor = g.actor
    if not actor or not can_see_center(actor, version["center_id"]):
        return False
    if has_role(actor, "reviewer"):
        return True
    if version.get("submitted_by") == actor["id"]:
        return True
    return version.get("handoff_status") == "ready"


def _document_of(app, version_id) -> str:
    version = _gateway(app).store.get_version(version_id)
    return version["document_id"] if version else ""


def _authorize_download(store, actor, record) -> None:
    if not can_see_center(actor, record["center_id"]) or record.get("restricted"):
        raise DomainError("NOT_FOUND", "No hay un archivo visible.", status=404)
    if record.get("persistence_state") != "ready":
        raise DomainError("NOT_FOUND", "El archivo todavía no está listo.", status=404)
    if has_role(actor, "reviewer") or has_role(actor, "administrator"):
        return
    versions = []
    if record.get("purpose") == "original":
        versions = store.versions_for_file(record["id"])
    elif record.get("extraction_id"):
        extraction = store.get_extraction(record["extraction_id"])
        if extraction:
            version = store.get_version(extraction["version_id"])
            if version:
                versions = [version]
    for version in versions:
        if version.get("submitted_by") == actor.get("id") and has_role(actor, "collaborator"):
            return
        document = store.get_document(version["document_id"])
        handoff = store.get_handoff(document.get("current_handoff_id")) if document and document.get("current_handoff_id") else None
        if handoff and handoff.get("status") == "ready" and can_see_center(actor, handoff["center_id"]):
            if has_role(actor, "consumer_t") or has_role(actor, "collaborator"):
                return
    raise DomainError("NOT_FOUND", "No hay un archivo visible.", status=404)


def _worker_state(heartbeat) -> str:
    if not heartbeat or not heartbeat.get("seen_at"):
        return "down"
    seen = datetime.fromisoformat(heartbeat["seen_at"].replace("Z", "+00:00"))
    if seen < datetime.now(timezone.utc) - timedelta(seconds=30):
        return "stale"
    return "up"
