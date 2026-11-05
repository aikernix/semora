from __future__ import annotations

from io import BytesIO
import os
import subprocess
from threading import Lock
from typing import BinaryIO

from PIL import Image, ImageOps
import pytesseract


class OCRError(RuntimeError):
    """Raised when local OCR cannot safely process an input."""


class OCRProvider:
    def __init__(self) -> None:
        self.mode = os.getenv("OCR_MODE", "tesseract").strip().lower()
        if self.mode not in {"tesseract", "disabled"}:
            raise RuntimeError("OCR_MODE must be tesseract or disabled")
        self.languages = os.getenv("OCR_LANG", "chi_sim+eng").strip()
        self.timeout_seconds = max(1, int(os.getenv("OCR_TIMEOUT_SECONDS", "60")))
        self.pdf_scale = max(1.0, min(4.0, float(os.getenv("OCR_PDF_SCALE", "2.0"))))
        self.max_pixels = max(1_000_000, int(os.getenv("OCR_MAX_PIXELS", "50000000")))
        self.max_pdf_pages = max(1, int(os.getenv("OCR_MAX_PDF_PAGES", "200")))
        self.text_threshold = max(0, int(os.getenv("OCR_TEXT_THRESHOLD", "24")))
        self._ready = False
        self._lock = Lock()

    @property
    def enabled(self) -> bool:
        return self.mode == "tesseract"

    def ensure_ready(self) -> None:
        if not self.enabled or self._ready:
            return
        with self._lock:
            if self._ready:
                return
            try:
                result = subprocess.run(
                    ["tesseract", "--list-langs"],
                    capture_output=True,
                    check=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
            except (FileNotFoundError, subprocess.SubprocessError) as exc:
                raise OCRError("本地 Tesseract OCR 不可用") from exc
            installed = {line.strip() for line in result.stdout.splitlines()[1:] if line.strip()}
            required = {language for language in self.languages.split("+") if language}
            missing = sorted(required - installed)
            if missing:
                raise OCRError(f"缺少 OCR 语言包：{', '.join(missing)}")
            self._ready = True

    def status(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "languages": self.languages if self.enabled else None,
            "available": self._ready if self.enabled else False,
        }

    def extract_bytes(self, content: bytes, *, source: str) -> str:
        return self.extract_image(BytesIO(content), source=source)

    def extract_image(self, image_source: str | BinaryIO | Image.Image, *, source: str) -> str:
        if not self.enabled:
            raise OCRError(f"{source} 需要 OCR，但 OCR_MODE=disabled")
        self.ensure_ready()
        try:
            if isinstance(image_source, Image.Image):
                image = image_source.copy()
            else:
                with Image.open(image_source) as opened:
                    image = opened.copy()
            image = ImageOps.exif_transpose(image)
            if image.width * image.height > self.max_pixels:
                raise OCRError(
                    f"{source} 像素数 {image.width * image.height} 超过限制 {self.max_pixels}"
                )
            if image.mode not in {"L", "RGB"}:
                image = image.convert("RGB")
            gray = ImageOps.autocontrast(ImageOps.grayscale(image))
            text = pytesseract.image_to_string(
                gray,
                lang=self.languages,
                timeout=self.timeout_seconds,
                config="--psm 6",
            )
            return "\n".join(line.strip() for line in text.splitlines() if line.strip())
        except OCRError:
            raise
        except RuntimeError as exc:
            raise OCRError(f"{source} OCR 超时或执行失败") from exc
        except Exception as exc:
            raise OCRError(f"{source} 不是可解析的图片") from exc


ocr_provider = OCRProvider()
