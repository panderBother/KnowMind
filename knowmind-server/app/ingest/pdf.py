from __future__ import annotations

import logging
from pathlib import Path

from app.ingest.ocr import ocr_image_bytes, ocr_pil_image
from app.ingest.types import PageText, ParseResult

log = logging.getLogger(__name__)

_MIN_TEXT_CHARS = 40


def _ocr_pdf_page(page) -> str:
    """将 PDF 页渲染为图片后 OCR（扫描版兜底）。"""
    try:
        from PIL import Image  # noqa: PLC0415

        pix = page.get_pixmap(dpi=150)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        text = ocr_pil_image(img)
        if text:
            return text
    except Exception:
        log.debug("PDF 页 OCR 失败", exc_info=True)
    return ""


def _ocr_pdf_images(page) -> str:
    """提取页内嵌图片并 OCR。"""
    parts: list[str] = []
    try:
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            base = page.parent.extract_image(xref)
            if not base or not base.get("image"):
                continue
            text = ocr_image_bytes(base["image"])
            if text:
                parts.append(text)
    except Exception:
        log.debug("PDF 内嵌图片 OCR 失败", exc_info=True)
    return "\n".join(parts)


def extract_pdf_pages(path: str) -> list[PageText]:
    import fitz  # pymupdf

    doc = fitz.open(path)
    out: list[PageText] = []
    try:
        for i in range(doc.page_count):
            page = doc.load_page(i)
            txt = (page.get_text("text") or "").strip()
            if len(txt) < _MIN_TEXT_CHARS:
                img_text = _ocr_pdf_images(page)
                if img_text:
                    txt = f"{txt}\n\n{img_text}".strip() if txt else img_text
                if len(txt) < _MIN_TEXT_CHARS:
                    ocr_txt = _ocr_pdf_page(page)
                    if ocr_txt:
                        txt = f"{txt}\n\n{ocr_txt}".strip() if txt else ocr_txt
            if not txt:
                txt = "（本页未提取到文本；若为扫描版请安装 Tesseract OCR）"
            out.append(PageText(page_index=i, text=txt))
    finally:
        doc.close()
    return out


def parse_pdf(path: str, filename: str | None = None) -> ParseResult:
    pages = extract_pdf_pages(path)
    base = (filename or Path(path).name).rsplit(".", 1)[0]
    content_parts: list[str] = []
    for p in pages:
        content_parts.append(f"## 第 {p.page_index + 1} 页\n\n{p.text}")
    content = "\n\n".join(content_parts)
    title = base[:200] if base else None
    summary = None
    if pages and pages[0].text:
        first_line = pages[0].text.split("\n")[0].strip()
        if first_line:
            summary = first_line[:500]
    return ParseResult(pages=pages, title=title, summary=summary, content=content)
