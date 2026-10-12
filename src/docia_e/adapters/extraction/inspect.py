"""Validación real del archivo. El nombre y el Content-Type no bastan."""

from __future__ import annotations

import zipfile
from pathlib import Path

from docia_e.domain.errors import DomainError

PDF_MAGIC = b"%PDF"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
ALLOWED_EXTENSIONS = {".pdf": "pdf", ".docx": "docx", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "png": "image/png",
    "jpeg": "image/jpeg",
}


def extension_of(display_name: str) -> str:
    suffix = Path(display_name or "").suffix.lower()
    return suffix


def inspect_file(path: Path, display_name: str, limits: dict) -> dict:
    size = path.stat().st_size
    max_bytes = int(limits["max_upload_bytes"])
    if size > max_bytes:
        raise DomainError(
            "SIZE_LIMIT",
            f"El archivo supera el límite de {limits['max_upload_mib']} MiB (1 MiB = 1 048 576 bytes).",
            status=413,
            next_action="Reduce el archivo o pide que se revise el límite del aula.",
        )
    if size == 0:
        raise DomainError(
            "FILE_CORRUPT",
            "El archivo está vacío.",
            status=415,
            next_action="Sube una copia que se pueda abrir.",
        )
    ext = extension_of(display_name)
    if ext not in ALLOWED_EXTENSIONS:
        raise DomainError(
            "UNSUPPORTED_FORMAT",
            "Este formato no entra en la entrega. Se admiten PDF, DOCX, PNG y JPEG.",
            status=415,
            field_errors={"file": "formato no admitido"},
        )
    if ext == ".docm" or display_name.lower().endswith(".docm"):
        raise DomainError("UNSUPPORTED_FORMAT", "Los documentos con macros (DOCM) se rechazan.", status=415)
    head = path.read_bytes()[:16]
    kind = _sniff(head)
    expected = ALLOWED_EXTENSIONS[ext]
    if kind == "zip" and expected == "docx":
        kind = "docx"
    if kind != expected:
        raise DomainError(
            "EXTENSION_MISMATCH",
            "La extensión no coincide con el contenido real del archivo.",
            status=415,
            field_errors={"file": "extensión falsa o contenido distinto"},
            next_action="Exporta de nuevo el documento con el formato que declara el nombre.",
        )
    if expected == "docx":
        _inspect_docx_zip(path, limits)
    elif expected == "pdf":
        _inspect_pdf(path, limits)
    else:
        _inspect_image(path, limits)
    return {
        "kind": expected,
        "mime_type": MIME[expected],
        "size_bytes": size,
        "display_name": Path(display_name).name,
    }


def _sniff(head: bytes) -> str:
    if head.startswith(PDF_MAGIC):
        return "pdf"
    if head.startswith(PNG_MAGIC):
        return "png"
    if head.startswith(JPEG_MAGIC):
        return "jpeg"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"MZ") or head.startswith(b"\x7fELF"):
        return "executable"
    return "unknown"


def _inspect_docx_zip(path: Path, limits: dict) -> None:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise DomainError("FILE_CORRUPT", "El DOCX no es un paquete ZIP válido.", status=415) from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > int(limits["docx_max_entries"]):
            raise DomainError(
                "SIZE_LIMIT",
                "El DOCX tiene demasiadas entradas internas.",
                status=413,
            )
        uncompressed = 0
        names = []
        for info in infos:
            names.append(info.filename)
            uncompressed += info.file_size
            if info.file_size < 0 or uncompressed > int(limits["docx_max_uncompressed_bytes"]):
                raise DomainError("SIZE_LIMIT", "El DOCX descomprimido supera el límite del aula.", status=413)
            _reject_zip_name(info.filename)
        name_set = set(names)
        if "[Content_Types].xml" not in name_set or "word/document.xml" not in name_set:
            raise DomainError(
                "UNSUPPORTED_FORMAT",
                "El ZIP no contiene un documento OOXML de Word.",
                status=415,
                next_action="Guarda como DOCX, no como ZIP o como otro contenedor.",
            )
        if "word/vbaProject.bin" in name_set:
            raise DomainError("UNSUPPORTED_FORMAT", "Se rechaza un documento con macros.", status=415)
        try:
            content_types = archive.read("[Content_Types].xml")
        except KeyError as exc:
            raise DomainError("FILE_CORRUPT", "No se puede leer el índice del DOCX.", status=415) from exc
        if b"macroEnabled" in content_types or b"vbaProject" in content_types:
            raise DomainError("UNSUPPORTED_FORMAT", "Se rechaza un documento con macros.", status=415)


def _reject_zip_name(name: str) -> None:
    lowered = name.replace("\\", "/").lower()
    if lowered.startswith("/") or lowered.startswith("../") or "/../" in f"/{lowered}":
        raise DomainError("UNSUPPORTED_FORMAT", "El paquete contiene una ruta interna no permitida.", status=415)
    if lowered.endswith((".exe", ".dll", ".js", ".vbs", ".bat", ".cmd", ".ps1")):
        raise DomainError("UNSUPPORTED_FORMAT", "El paquete contiene un ejecutable.", status=415)


def _inspect_pdf(path: Path, limits: dict) -> None:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            opened = reader.decrypt("")
            if opened == 0:
                raise DomainError(
                    "PASSWORD_REQUIRED",
                    "El PDF pide contraseña de apertura. Esta versión no guarda contraseñas.",
                    status=422,
                    next_action="Pide una copia autorizada que se pueda abrir sin contraseña.",
                )
        pages = len(reader.pages)
    except DomainError:
        raise
    except (PdfReadError, Exception) as exc:
        raise DomainError(
            "FILE_CORRUPT",
            "El PDF no se puede leer. Puede estar dañado.",
            status=415,
            next_action="Exporta de nuevo el PDF.",
        ) from exc
    if pages > int(limits["max_pdf_pages"]):
        raise DomainError(
            "SIZE_LIMIT",
            f"El PDF supera el límite de {limits['max_pdf_pages']} páginas.",
            status=413,
        )
    if pages == 0:
        raise DomainError("FILE_CORRUPT", "El PDF no contiene páginas.", status=415)


def _inspect_image(path: Path, limits: dict) -> None:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise DomainError("FILE_CORRUPT", "La imagen no se puede decodificar.", status=415) from exc
    pixels = width * height
    if pixels > int(limits["max_image_pixels"]):
        raise DomainError(
            "SIZE_LIMIT",
            "La imagen supera el límite de 30 megapíxeles.",
            status=413,
        )
    if width < 1 or height < 1:
        raise DomainError("FILE_CORRUPT", "La imagen no tiene dimensiones útiles.", status=415)
