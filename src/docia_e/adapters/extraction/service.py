"""Extractores locales. No cambian estados documentales ni autorizan T.

Heurística de cobertura, documentada y no infalible:
- Una página PDF con al menos 40 caracteres no blancos se considera texto digital útil.
- Por debajo de ese umbral, y si hay imagen o no hay texto, se intenta OCR.
- Si conviven texto digital útil y un OCR completo distinto, se elige una sola
  representación (la digital si sigue siendo útil) y se marca MIXED_PAGE_REVIEW_REQUIRED.
- Nunca se concatenan las dos lecturas.
- Una página sin texto y sin imagen queda como posible blanco, con incidencia,
  hasta que un revisor la confirme. El sistema no la declara en blanco solo.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

from docia_e.domain.privacy import privacy_hits, privacy_issue
from docia_e.domain.textutil import normalize_technical

USEFUL_CHARS = 40
CONTRACT_NOTE = "docia-e/1.0"


def package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "desconocida"


def extract_document(path: Path, kind: str, options: dict) -> dict:
    if kind == "pdf":
        result = _extract_pdf(path, options)
    elif kind == "docx":
        result = _extract_docx(path, options)
    elif kind in {"png", "jpeg"}:
        result = _extract_image(path, options)
    else:
        result = _failed("unsupported", "UNSUPPORTED_FORMAT", "El extractor no admite este tipo.")
    _attach_privacy(result)
    result["coverage_complete"] = _coverage(result)
    if any(issue["severity"] == "blocker" and not issue.get("resolved") for issue in result["issues"]):
        if result["run_status"] == "succeeded":
            result["run_status"] = "partial"
    return result


def ocr_status() -> dict:
    """Disponibilidad del binario y de los idiomas. No ejecuta un reconocimiento."""
    try:
        import pytesseract
        from pytesseract.pytesseract import TesseractNotFoundError
    except ImportError:
        return {"available": False, "languages": [], "reason": "OCR_UNAVAILABLE", "version": None}
    try:
        version = str(pytesseract.get_tesseract_version())
        languages = list(pytesseract.get_languages(config=""))
    except TesseractNotFoundError:
        return {"available": False, "languages": [], "reason": "OCR_UNAVAILABLE", "version": None}
    except Exception as exc:
        return {"available": False, "languages": [], "reason": "OCR_UNAVAILABLE", "version": None, "detail": type(exc).__name__}
    return {"available": True, "languages": languages, "reason": None, "version": version}


def _extract_pdf(path: Path, options: dict) -> dict:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    engine_version = package_version("pypdf")
    configuration = {
        "engine": "pypdf",
        "engine_version": engine_version,
        "mode": "digital-then-ocr",
        "page_number_base": 1,
        "technical_normalization": "utf8-lf",
        "ocr_languages": options.get("ocr_languages") or "spa",
        "render_dpi": 300,
        "renderer": "pypdfium2",
        "renderer_version": package_version("pypdfium2"),
        "contract": CONTRACT_NOTE,
        "useful_char_threshold": USEFUL_CHARS,
        "force_ocr_pages": list(options.get("force_ocr_pages") or []),
    }
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and reader.decrypt("") == 0:
            return _failed("pypdf", "PASSWORD_REQUIRED", "El PDF pide contraseña.", engine_version, configuration)
        labels = _page_labels(reader)
        pages = reader.pages
    except PdfReadError:
        return _failed("pypdf", "FILE_CORRUPT", "El PDF no se puede leer.", engine_version, configuration)
    expected = len(pages)
    max_pages = int(options.get("max_pdf_pages") or 100)
    if expected > max_pages:
        return _failed("pypdf", "SIZE_LIMIT", "El PDF supera el máximo de páginas.", engine_version, configuration)
    units = []
    issues = []
    failed = 0
    for index, page in enumerate(pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception:
            raw = ""
            failed += 1
            issues.append(_issue("PAGE_FAILED", "blocker", None, {"type": "pdf_page", "page": index, "page_label": labels.get(index)}, "No se pudo leer el texto de la página.", "Reintenta o aporta otra copia."))
            continue
        text = normalize_technical(raw)
        label = labels.get(index)
        locator = {"type": "pdf_page", "page": index, "page_label": label}
        callback = options.get("on_page")
        if callback:
            callback(index, expected)
        force = index in set(options.get("force_ocr_pages") or [])
        useful = len("".join(text.split())) >= USEFUL_CHARS
        has_image = _page_has_image(page)
        method = "digital"
        chosen = text
        if force or not useful:
            ocr_text, ocr_issue = _ocr_pdf_page(path, index, options)
            if ocr_issue:
                issues.append({**ocr_issue, "locator": locator})
                if not useful:
                    method = "ocr" if ocr_text else "digital"
                    chosen = ocr_text or text
            elif useful and ocr_text and normalize_technical(ocr_text) != text:
                issues.append(_issue(
                    "MIXED_PAGE_REVIEW_REQUIRED",
                    "blocker",
                    None,
                    locator,
                    "La página tiene texto digital y un OCR distinto. Se conserva solo la lectura digital para no duplicar el contenido.",
                    "El revisor elige la representación más completa o deja la página pendiente.",
                ))
                method = "digital"
                chosen = text
            else:
                method = "ocr" if ocr_text else "digital"
                chosen = ocr_text or text
        if not chosen.strip():
            if has_image:
                issues.append(_issue("EMPTY_TEXT", "blocker", None, locator, "La página no aportó texto útil.", "Fuerza OCR o confirma si es un blanco."))
            else:
                issues.append(_issue("EMPTY_TEXT", "blocker", None, locator, "La página no tiene texto. Puede ser un blanco legítimo o un fallo de lectura.", "Confirma el blanco tras ver el original, o fuerza OCR."))
        units.append(_unit(index, "page", method, chosen, locator))
    run_status = "failed" if expected and failed == expected else "succeeded"
    return _result("pypdf", engine_version, configuration, units, issues, expected, expected - failed, failed, run_status)


def _extract_docx(path: Path, options: dict) -> dict:
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    engine_version = package_version("python-docx")
    configuration = {
        "engine": "python-docx",
        "engine_version": engine_version,
        "mode": "body-order",
        "technical_normalization": "utf8-lf",
        "contract": CONTRACT_NOTE,
        "page_number_base": None,
    }
    try:
        document = docx.Document(str(path))
    except Exception:
        return _failed("python-docx", "FILE_CORRUPT", "El DOCX no se puede abrir.", engine_version, configuration)
    issues = _docx_structure_issues(document)
    units = []
    order = 0
    section_path: list[str] = []
    table_index = 0
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            style = paragraph.style.name if paragraph.style is not None else ""
            text = normalize_technical(paragraph.text or "")
            if style.lower().startswith("heading") or style.lower().startswith("título") or style.lower().startswith("titulo"):
                section_path = _push_heading(section_path, style, text)
            order += 1
            locator = {
                "type": "docx_block",
                "block_index": order,
                "section_path": list(section_path),
                "page": None,
            }
            units.append(_unit(order, "paragraph", "digital", text, locator))
        elif child.tag == qn("w:tbl"):
            table_index += 1
            table = Table(child, document)
            order, new_units, table_issues = _table_units(table, document, order, table_index, section_path)
            units.extend(new_units)
            issues.extend(table_issues)
    if not units:
        issues.append(_issue("EMPTY_TEXT", "blocker", None, {"type": "docx_block", "block_index": None, "page": None}, "El DOCX no aportó bloques de texto.", "Comprueba que el contenido no esté solo en objetos no leídos."))
    return _result("python-docx", engine_version, configuration, units, issues, len(units), len(units), 0, "succeeded")


def _extract_image(path: Path, options: dict) -> dict:
    from PIL import Image, ImageOps

    engine_version = package_version("pillow")
    configuration = {
        "engine": "pillow+tesseract",
        "engine_version": engine_version,
        "mode": "image-ocr",
        "technical_normalization": "utf8-lf",
        "ocr_languages": options.get("ocr_languages") or "spa",
        "contract": CONTRACT_NOTE,
        "exif_transpose": True,
    }
    locator = {
        "type": "image",
        "image_index": 1,
        "page": None,
    }
    issues = []
    method = "ocr"
    text = ""
    try:
        with Image.open(path) as original:
            width, height = original.size
            locator["pixel_width"] = width
            locator["pixel_height"] = height
            locator["unit"] = "px"
            locator["origin"] = "top-left"
            oriented = ImageOps.exif_transpose(original)
            ocr_text, ocr_issue = _ocr_pil(oriented, options)
            if ocr_issue:
                issues.append({**ocr_issue, "locator": locator})
            text = normalize_technical(ocr_text or "")
    except Exception:
        return _failed("pillow", "FILE_CORRUPT", "No se pudo preparar la imagen.", engine_version, configuration)
    if not text.strip():
        issues.append(_issue("EMPTY_TEXT", "blocker", None, locator, "La imagen no aportó texto reconocible.", "Aporta una copia más legible. No se reconstruyen palabras por inferencia."))
    units = [_unit(1, "image", method, text, locator)]
    return _result("pillow+tesseract", engine_version, configuration, units, issues, 1, 1, 0, "succeeded")


def _ocr_pdf_page(path: Path, page_number: int, options: dict) -> tuple[str, dict | None]:
    status = ocr_status()
    languages = options.get("ocr_languages") or "spa"
    required = [part for part in languages.split("+") if part]
    if not status["available"] or any(lang not in status["languages"] for lang in required):
        return "", _issue("OCR_UNAVAILABLE", "blocker", None, None, "Tesseract o el idioma configurado no está disponible.", "Instala Tesseract y el paquete de idioma, y comprueba tesseract --list-langs.")
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return "", _issue("OCR_UNAVAILABLE", "blocker", None, None, "pypdfium2 no está instalado.", "Instala las dependencias de la aplicación.")
    document = pdfium.PdfDocument(str(path))
    try:
        page = document[page_number - 1]
        bitmap, limited = _render_limited(page, options)
        if limited:
            return "", _issue("SIZE_LIMIT", "blocker", None, None, "La página renderizada supera 30 megapíxeles.", "Aporta una copia de menor resolución.")
        text, issue = _ocr_pil(bitmap, options)
        return text, issue
    finally:
        document.close()


def _render_limited(page, options: dict):
    max_pixels = int(options.get("max_image_pixels") or 30_000_000)
    scale = 300 / 72
    width = page.get_width() * scale
    height = page.get_height() * scale
    if width * height > max_pixels:
        return None, True
    bitmap = page.render(scale=scale).to_pil()
    return bitmap, False


def _ocr_pil(image, options: dict) -> tuple[str, dict | None]:
    status = ocr_status()
    languages = options.get("ocr_languages") or "spa"
    required = [part for part in languages.split("+") if part]
    if not status["available"] or any(lang not in status["languages"] for lang in required):
        return "", _issue("OCR_UNAVAILABLE", "blocker", None, None, "Tesseract o el idioma configurado no está disponible.", "Instala Tesseract y el idioma español.")
    import pytesseract

    timeout = int(options.get("ocr_page_timeout_seconds") or 60)
    try:
        text = pytesseract.image_to_string(image, lang=languages, timeout=timeout)
    except RuntimeError:
        return "", _issue("TIME_LIMIT", "blocker", None, None, "El OCR superó el tiempo por página.", "Reintenta o aporta una imagen más pequeña.")
    except Exception:
        return "", _issue("OCR_UNAVAILABLE", "blocker", None, None, "El OCR no pudo ejecutarse.", "Revisa el diagnóstico de Tesseract.")
    return text or "", None


def _table_units(table, document, order: int, table_index: int, section_path: list[str]):
    from docx.oxml.ns import qn

    units = []
    issues = []
    seen = set()
    nested = False
    merged = False
    for row_index, row in enumerate(table.rows, start=1):
        for col_index, cell in enumerate(row.cells, start=1):
            tc = cell._tc
            identity = id(tc)
            if identity in seen:
                merged = True
                continue
            seen.add(identity)
            if tc.find(".//" + qn("w:tbl")) is not None:
                nested = True
            span = tc.xpath(".//w:gridSpan")
            vmerge = tc.xpath(".//w:vMerge")
            if span or vmerge:
                merged = True
            order += 1
            text = normalize_technical(cell.text or "")
            locator = {
                "type": "docx_block",
                "block_index": order,
                "section_path": list(section_path),
                "table_index": table_index,
                "row": row_index,
                "column": col_index,
                "page": None,
            }
            units.append(_unit(order, "cell", "digital", text, locator))
    if merged or nested:
        issues.append(_issue(
            "TABLE_STRUCTURE_UNCERTAIN",
            "blocker",
            None,
            {"type": "docx_block", "table_index": table_index, "page": None},
            "La tabla tiene celdas combinadas o anidadas. El texto único se conserva, pero la relación visual puede no ser fiel.",
            "Revisa la tabla en el original. Si falta contenido, pide una copia más simple o un PDF.",
        ))
    return order, units, issues


def _docx_structure_issues(document) -> list[dict]:
    issues = []
    xml = document.element.xml
    if "w:txbxContent" in xml:
        issues.append(_issue(
            "UNSUPPORTED_DOCX_CONTENT",
            "blocker",
            None,
            {"type": "docx_block", "page": None},
            "Hay cuadros de texto que este extractor no incorpora al orden de lectura.",
            "Pide una copia en la que el texto esté en el cuerpo, o un PDF.",
        ))
    if "<w:ins" in xml or "<w:del" in xml:
        issues.append(_issue(
            "UNSUPPORTED_DOCX_CONTENT",
            "warning",
            None,
            {"type": "docx_block", "page": None},
            "El documento tiene control de cambios. Se lee el texto resultante visible, no una aceptación de los cambios.",
            "Si la versión aceptada importa, sube una copia consolidada.",
        ))
    package = getattr(document.part, "package", None)
    parts = list(getattr(package, "parts", []) or [])
    for part in parts:
        partname = str(getattr(part, "partname", ""))
        blob = getattr(part, "blob", b"") or b""
        if "footnotes" in partname or "endnotes" in partname:
            if b"<w:t" in blob:
                issues.append(_issue(
                    "UNSUPPORTED_DOCX_CONTENT",
                    "blocker",
                    None,
                    {"type": "docx_block", "page": None},
                    "Hay notas al pie o al final con texto que no entra en los bloques del cuerpo.",
                    "Revisa si ese texto debe formar parte del corpus y, si es así, incorpóralo al cuerpo o aporta un PDF.",
                ))
                break
    return issues


def _push_heading(path: list[str], style: str, text: str) -> list[str]:
    digits = [char for char in style if char.isdigit()]
    level = int(digits[0]) if digits else 1
    updated = path[: level - 1]
    if text:
        updated.append(text)
    return updated


def _page_labels(reader) -> dict[int, str]:
    labels: dict[int, str] = {}
    try:
        raw = reader.page_labels
    except Exception:
        return labels
    if not raw:
        return labels
    for index, label in enumerate(raw, start=1):
        if label:
            labels[index] = str(label)
    return labels


def _page_has_image(page) -> bool:
    try:
        images = getattr(page, "images", None)
        if images is None:
            return False
        return len(list(images)) > 0
    except Exception:
        return False


def _attach_privacy(result: dict) -> None:
    for unit in result["units"]:
        hits = privacy_hits(unit["text"])
        if hits:
            issue = privacy_issue(hits, unit_id=unit["unit_id"], locator=unit["locator"])
            result["issues"].append(issue)


def _coverage(result: dict) -> bool:
    if result["run_status"] == "failed":
        return False
    if any(issue["severity"] == "blocker" and not issue.get("resolved") for issue in result["issues"]):
        return False
    return result["pages_failed"] == 0


def _unit(order: int, kind: str, method: str, text: str, locator: dict) -> dict:
    return {
        "unit_id": f"u-{order:04d}",
        "order": order,
        "kind": kind,
        "method": method,
        "text": text,
        "locator": locator,
    }


def _issue(code, severity, unit_id, locator, explanation, next_action) -> dict:
    return {
        "code": code,
        "severity": severity,
        "unit_id": unit_id,
        "locator": locator,
        "explanation": explanation,
        "next_action": next_action,
        "resolved": False,
        "resolution": None,
    }


def _result(engine, version, configuration, units, issues, expected, processed, failed, run_status) -> dict:
    return {
        "engine": engine,
        "engine_version": version,
        "configuration": configuration,
        "units": units,
        "issues": issues,
        "pages_expected": expected,
        "pages_processed": processed,
        "pages_failed": failed,
        "run_status": run_status,
        "coverage_complete": False,
    }


def _failed(engine, code, message, version="desconocida", configuration=None) -> dict:
    configuration = configuration or {"engine": engine, "contract": CONTRACT_NOTE}
    return _result(
        engine,
        version,
        configuration,
        [],
        [_issue(code, "blocker", None, None, message, "Corrige la causa y crea un reintento manual.")],
        0,
        0,
        1,
        "failed",
    )
