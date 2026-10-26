"""Small helpers shared across the application."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config

DATE_FORMAT = "%d-%m-%Y"          # display format: 25-10-2026
ISO_DATE_FORMAT = "%Y-%m-%d"      # storage format (sorts correctly)

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-./]{0,29}$")


def format_date(moment: datetime) -> str:
    """Return the date as DD-MM-YYYY."""
    return moment.strftime(DATE_FORMAT)


def format_time(moment: datetime) -> str:
    """Return the time as HH:MM:SS AM/PM (independent of system locale)."""
    suffix = "AM" if moment.hour < 12 else "PM"
    hour = moment.hour % 12 or 12
    return f"{hour:02d}:{moment.minute:02d}:{moment.second:02d} {suffix}"


def iso_date(moment: datetime) -> str:
    """Return the date as YYYY-MM-DD for database storage."""
    return moment.strftime(ISO_DATE_FORMAT)


def iso_to_display(iso_value: str) -> str:
    """Convert YYYY-MM-DD to DD-MM-YYYY (returns input unchanged if invalid)."""
    try:
        return datetime.strptime(iso_value, ISO_DATE_FORMAT).strftime(DATE_FORMAT)
    except ValueError:
        return iso_value


def display_to_iso(display_value: str) -> str:
    """Convert DD-MM-YYYY to YYYY-MM-DD. Raises ValueError if invalid."""
    return datetime.strptime(display_value.strip(), DATE_FORMAT).strftime(
        ISO_DATE_FORMAT
    )


def timestamp_to_display_date(timestamp: str) -> str:
    """Convert an ISO timestamp (YYYY-MM-DDTHH:MM:SS) to DD-MM-YYYY."""
    return iso_to_display(timestamp[:10])


def to_ascii(text: str) -> str:
    """Make text safe for OpenCV's built-in font (ASCII only)."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch if ord(ch) < 128 else "?" for ch in normalized
                   if not unicodedata.combining(ch))


def escape_like(text: str) -> str:
    """Escape SQL LIKE wildcards so user input is matched literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def is_valid_email(value: str) -> bool:
    """Light-weight e-mail format check."""
    return bool(_EMAIL_PATTERN.match(value))


def is_valid_employee_id(value: str) -> bool:
    """Letters, digits and - _ . / only, up to 30 characters."""
    return bool(_ID_PATTERN.match(value))


def open_file(path: Path) -> None:
    """Open a file with the operating system's default application."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def setup_logging() -> None:
    """Configure logging to ``logs/app.log`` (rotating) and the console."""
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = RotatingFileHandler(
        config.LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
