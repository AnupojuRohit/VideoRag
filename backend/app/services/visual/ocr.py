import logging
from pathlib import Path

import pytesseract
from PIL import Image, UnidentifiedImageError
from pytesseract import Output

from app.core.config import get_settings
from app.services.media.audio import MediaProcessingError

logger = logging.getLogger(__name__)


def extract_visible_text(image_path: Path) -> str:
    settings = get_settings()
    pytesseract.pytesseract.tesseract_cmd = settings.tesseract_path
    try:
        with Image.open(image_path) as image:
            rows = pytesseract.image_to_data(
                image,
                lang=settings.tesseract_language,
                config="--psm 6",
                output_type=Output.DICT,
                timeout=settings.ocr_timeout_seconds,
            )
    except pytesseract.TesseractNotFoundError as exc:
        logger.error(
            "Tesseract OCR executable is unavailable",
            extra={"error_type": type(exc).__name__},
        )
        raise MediaProcessingError(
            "ocr_unavailable",
            "Local OCR is unavailable because Tesseract is not installed.",
            retryable=False,
        ) from exc
    except RuntimeError as exc:
        raise MediaProcessingError(
            "ocr_timeout",
            "Text recognition exceeded the configured time limit.",
            retryable=True,
            transient=True,
        ) from exc
    except pytesseract.TesseractError as exc:
        logger.error(
            "Tesseract failed to recognize a selected frame",
            extra={"error_type": type(exc).__name__},
        )
        raise MediaProcessingError(
            "ocr_failed",
            "Text recognition failed for a selected frame.",
            retryable=False,
        ) from exc
    except (OSError, UnidentifiedImageError) as exc:
        raise MediaProcessingError(
            "ocr_frame_invalid",
            "A selected frame could not be opened for text recognition.",
            retryable=False,
        ) from exc

    words: list[str] = []
    for text, confidence in zip(rows.get("text", []), rows.get("conf", [])):
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            score = float(confidence)
        except (TypeError, ValueError):
            continue
        if score >= 0:
            words.append(text.strip())
    return " ".join(words)
