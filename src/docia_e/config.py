"""Configuración. Los secretos llegan por el entorno, nunca por el repositorio."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024


def load_env_file(path: Path) -> None:
    """Lee KEY=VAL sin pisar variables que ya existan. No imprime valores."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    app_env: str
    host: str
    port: int
    secret_key: str
    mongo_uri: str
    mongo_db: str
    gridfs_bucket: str
    document_profile: str
    ocr_languages: str
    max_upload_mib: int
    max_pdf_pages: int
    max_image_megapixels: int
    ocr_page_timeout_seconds: int
    job_timeout_seconds: int
    worker_concurrency: int
    docx_max_uncompressed_mib: int
    docx_max_entries: int
    temp_dir: Path
    cookie_secure: bool
    root: Path

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mib * MIB

    @property
    def max_image_pixels(self) -> int:
        return self.max_image_megapixels * 1_000_000

    @property
    def docx_max_uncompressed_bytes(self) -> int:
        return self.docx_max_uncompressed_mib * MIB

    def limits(self) -> dict:
        return {
            "max_upload_bytes": self.max_upload_bytes,
            "max_upload_mib": self.max_upload_mib,
            "max_pdf_pages": self.max_pdf_pages,
            "max_image_pixels": self.max_image_pixels,
            "docx_max_uncompressed_bytes": self.docx_max_uncompressed_bytes,
            "docx_max_entries": self.docx_max_entries,
            "ocr_page_timeout_seconds": self.ocr_page_timeout_seconds,
            "job_timeout_seconds": self.job_timeout_seconds,
            "ocr_languages": self.ocr_languages,
        }


def load_settings(root: Path | None = None) -> Settings:
    if root is None:
        cwd = Path.cwd()
        packaged = Path(__file__).resolve().parents[2]
        root = cwd if (cwd / "pyproject.toml").is_file() else packaged
    load_env_file(root / ".env")
    secret = os.environ.get("SECRET_KEY", "")
    temp = Path(os.environ.get("TEMP_DIR", root / "var" / "tmp"))
    return Settings(
        app_env=os.environ.get("APP_ENV", "development"),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5051")),
        secret_key=secret,
        mongo_uri=os.environ.get(
            "MONGO_URI",
            "mongodb://127.0.0.1:27017/?replicaSet=rs0",
        ),
        mongo_db=os.environ.get("MONGO_DB", "docia_etl_e"),
        gridfs_bucket=os.environ.get("GRIDFS_BUCKET", "docia_blobs"),
        document_profile=os.environ.get("DOCUMENT_PROFILE", "actividades_orientacion"),
        ocr_languages=os.environ.get("OCR_LANGUAGES", "spa"),
        max_upload_mib=int(os.environ.get("MAX_UPLOAD_MIB", "25")),
        max_pdf_pages=int(os.environ.get("MAX_PDF_PAGES", "100")),
        max_image_megapixels=int(os.environ.get("MAX_IMAGE_MEGAPIXELS", "30")),
        ocr_page_timeout_seconds=int(os.environ.get("OCR_PAGE_TIMEOUT_SECONDS", "60")),
        job_timeout_seconds=int(os.environ.get("JOB_TIMEOUT_SECONDS", "900")),
        worker_concurrency=int(os.environ.get("WORKER_CONCURRENCY", "1")),
        docx_max_uncompressed_mib=int(os.environ.get("DOCX_MAX_UNCOMPRESSED_MIB", "200")),
        docx_max_entries=int(os.environ.get("DOCX_MAX_ENTRIES", "5000")),
        temp_dir=temp,
        cookie_secure=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
        root=root,
    )


def require_secret(settings: Settings) -> None:
    if len(settings.secret_key) < 32:
        raise SystemExit(
            "SECRET_KEY no está definida o es corta. Genérala en local y ponla en .env, "
            "que no se versiona. El ejemplo .env.example no lleva secretos."
        )
