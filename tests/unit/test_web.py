"""La interfaz escapa el texto extraído y el login no guarda nada si falta Mongo."""

from docia_e.adapters.web.app import create_app
from docia_e.config import Settings
from pathlib import Path


class _Store:
    def maintenance_enabled(self):
        return False

    def get_user(self, _user_id):
        return None

    def latest_heartbeat(self):
        return None

    def topology(self):
        raise RuntimeError("sin mongo")


class _Gateway:
    store = _Store()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        host="127.0.0.1",
        port=5051,
        secret_key="x" * 40,
        mongo_uri="mongodb://127.0.0.1:27017/?replicaSet=rs0",
        mongo_db="docia_etl_e",
        gridfs_bucket="docia_blobs",
        document_profile="actividades_orientacion",
        ocr_languages="spa",
        max_upload_mib=25,
        max_pdf_pages=100,
        max_image_megapixels=30,
        ocr_page_timeout_seconds=60,
        job_timeout_seconds=900,
        worker_concurrency=1,
        docx_max_uncompressed_mib=200,
        docx_max_entries=5000,
        temp_dir=tmp_path,
        cookie_secure=False,
        root=tmp_path,
    )


def test_login_se_muestra_sin_mongo(tmp_path: Path):
    app = create_app(_settings(tmp_path), _Gateway(), object())
    client = app.test_client()
    response = client.get("/login")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Entrar" in body
    assert "csrf_token" in body
    assert 'name="login"' in body


def test_titulo_con_script_se_escapa(tmp_path: Path):
    app = create_app(_settings(tmp_path), _Gateway(), object())
    with app.test_request_context("/"):
        html = app.jinja_env.from_string("<td>{{ title }}</td>").render(title="<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_salud_informa_mongo_caido(tmp_path: Path):
    app = create_app(_settings(tmp_path), _Gateway(), object())
    response = app.test_client().get("/health")
    assert response.status_code == 503
    assert response.get_json()["mongo"] == "down"
    assert response.get_json()["web"] == "up"


def test_las_plantillas_compilan(tmp_path: Path):
    app = create_app(_settings(tmp_path), _Gateway(), object())
    for name in ("login.html", "dashboard.html", "upload.html", "detail.html", "extraction.html", "documents.html"):
        app.jinja_env.get_template(name)
