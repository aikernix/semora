from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from docx import Document
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches

from app.ocr import ocr_provider
from app.parsers import chunk_blocks, parse_document


class OCRParserTest(unittest.TestCase):
    phrase = "SEMORA SCANNED BANK POLICY 2026"

    @classmethod
    def setUpClass(cls) -> None:
        ocr_provider.ensure_ready()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        image = Image.new("RGB", (1500, 260), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 56)
        draw.text((30, 45), self.phrase, font=font, fill="black")
        draw.text((30, 140), "REVIEWER APPROVAL REQUIRED", font=font, fill="black")
        self.png = self.root / "policy.png"
        image.save(self.png)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def assert_ocr_text(self, path: Path) -> None:
        chunks = chunk_blocks(parse_document(path))
        self.assertTrue(chunks)
        self.assertIn(self.phrase, "\n".join(chunk.text for chunk in chunks))
        self.assertTrue(any("OCR" in chunk.locator for chunk in chunks))

    def test_direct_image(self) -> None:
        self.assert_ocr_text(self.png)

    def test_scanned_pdf(self) -> None:
        with Image.open(self.png) as image:
            image.save(self.root / "policy.pdf", "PDF", resolution=150)
        self.assert_ocr_text(self.root / "policy.pdf")

    def test_docx_embedded_image(self) -> None:
        document = Document()
        document.add_picture(str(self.png))
        document.save(self.root / "policy.docx")
        self.assert_ocr_text(self.root / "policy.docx")

    def test_pptx_embedded_image(self) -> None:
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_picture(str(self.png), Inches(0.5), Inches(0.5), width=Inches(9))
        presentation.save(self.root / "policy.pptx")
        self.assert_ocr_text(self.root / "policy.pptx")


if __name__ == "__main__":
    unittest.main()
