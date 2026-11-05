from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from bs4 import BeautifulSoup
from docx import Document
from docx.parts.image import ImagePart
from openpyxl import load_workbook
from pypdf import PdfReader
import pypdfium2 as pdfium
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from .ocr import OCRError, ocr_provider


@dataclass(frozen=True)
class SourceBlock:
    locator: str
    text: str


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def _parse_pdf(path: Path) -> list[SourceBlock]:
    reader = PdfReader(str(path))
    if len(reader.pages) > ocr_provider.max_pdf_pages:
        raise ValueError(f"PDF 页数超过限制 {ocr_provider.max_pdf_pages}")
    extracted = [page.extract_text() or "" for page in reader.pages]
    sparse_pages = [index for index, text in enumerate(extracted) if len(text.strip()) < ocr_provider.text_threshold]
    if not sparse_pages:
        return [SourceBlock(f"第 {index + 1} 页", text) for index, text in enumerate(extracted)]
    if not ocr_provider.enabled:
        raise OCRError("PDF 包含扫描页，但 OCR_MODE=disabled")

    document = pdfium.PdfDocument(str(path))
    try:
        for index in sparse_pages:
            page = document[index]
            try:
                bitmap = page.render(scale=ocr_provider.pdf_scale)
                try:
                    image = bitmap.to_pil()
                    ocr_text = ocr_provider.extract_image(image, source=f"PDF 第 {index + 1} 页")
                finally:
                    bitmap.close()
            finally:
                page.close()
            if ocr_text:
                base = extracted[index].strip()
                extracted[index] = f"{base}\n{ocr_text}".strip() if base and ocr_text not in base else ocr_text
    finally:
        document.close()
    return [SourceBlock(f"第 {index + 1} 页 / OCR", text) for index, text in enumerate(extracted)]


def _parse_docx(path: Path) -> list[SourceBlock]:
    document = Document(str(path))
    blocks = [SourceBlock(f"段落 {index + 1}", paragraph.text) for index, paragraph in enumerate(document.paragraphs)]
    seen: set[str] = set()
    image_index = 0
    for part in document.part.related_parts.values():
        if not isinstance(part, ImagePart):
            continue
        digest = part.sha1
        if digest in seen:
            continue
        seen.add(digest)
        image_index += 1
        text = ocr_provider.extract_bytes(part.blob, source=f"Word 内嵌图片 {image_index}")
        blocks.append(SourceBlock(f"内嵌图片 {image_index} / OCR", text))
    return blocks


def _shape_text_and_images(shape, slide_index: int, image_counter: list[int]) -> tuple[list[str], list[SourceBlock]]:
    texts: list[str] = []
    images: list[SourceBlock] = []
    if getattr(shape, "has_text_frame", False) and shape.text.strip():
        texts.append(shape.text)
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        image_counter[0] += 1
        text = ocr_provider.extract_bytes(
            shape.image.blob,
            source=f"PPT 第 {slide_index} 页图片 {image_counter[0]}",
        )
        images.append(SourceBlock(f"第 {slide_index} 页 / 图片 {image_counter[0]} / OCR", text))
    elif shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        for child in shape.shapes:
            child_texts, child_images = _shape_text_and_images(child, slide_index, image_counter)
            texts.extend(child_texts)
            images.extend(child_images)
    return texts, images


def _parse_pptx(path: Path) -> list[SourceBlock]:
    presentation = Presentation(str(path))
    blocks: list[SourceBlock] = []
    for slide_index, slide in enumerate(presentation.slides, start=1):
        texts: list[str] = []
        images: list[SourceBlock] = []
        image_counter = [0]
        for shape in slide.shapes:
            shape_texts, shape_images = _shape_text_and_images(shape, slide_index, image_counter)
            texts.extend(shape_texts)
            images.extend(shape_images)
        blocks.append(SourceBlock(f"第 {slide_index} 页", "\n".join(texts)))
        blocks.extend(images)
    return blocks


def parse_document(path: Path) -> list[SourceBlock]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _parse_pdf(path)
    if suffix == ".docx":
        return _parse_docx(path)
    if suffix == ".pptx":
        return _parse_pptx(path)
    if suffix in IMAGE_SUFFIXES:
        return [SourceBlock("图片 OCR", ocr_provider.extract_image(str(path), source=path.name))]
    if suffix in {".xlsx", ".xlsm"}:
        workbook = load_workbook(path, read_only=True, data_only=True)
        blocks = []
        for sheet in workbook.worksheets:
            lines = ["\t".join("" if value is None else str(value) for value in row) for row in sheet.iter_rows(values_only=True)]
            blocks.append(SourceBlock(f"工作表 {sheet.title}", "\n".join(lines)))
        return blocks
    text = path.read_text(encoding="utf-8", errors="ignore")
    if suffix in {".html", ".htm"}:
        text = BeautifulSoup(text, "html.parser").get_text("\n")
    return [SourceBlock("全文", text)]


def chunk_blocks(blocks: list[SourceBlock], chunk_size: int = 900, overlap: int = 120) -> list[SourceBlock]:
    chunks: list[SourceBlock] = []
    for block in blocks:
        text = "\n".join(line.strip() for line in block.text.splitlines() if line.strip())
        if not text:
            continue
        start = 0
        part = 1
        while start < len(text):
            end = min(len(text), start + chunk_size)
            if end < len(text):
                boundary = max(text.rfind("。", start, end), text.rfind("\n", start, end))
                if boundary > start + chunk_size // 2:
                    end = boundary + 1
            chunks.append(SourceBlock(f"{block.locator} / 分块 {part}", text[start:end]))
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
            part += 1
    return chunks
