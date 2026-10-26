"""Central configuration for the Face Recognition Attendance System.

Edit the constants in this file to change paths or fixed behaviour.
User-adjustable values (camera, threshold, cooldown) are stored in
``data/settings.json`` and can be changed from the Settings screen.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Application identity
# --------------------------------------------------------------------------
APP_NAME = "Face Recognition Attendance System"

# --------------------------------------------------------------------------
# Paths (all relative to the project folder)
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
FACE_DATA_DIR = BASE_DIR / "face_data"      # holds the local AI model files
MODEL_DIR = FACE_DATA_DIR / "models"
EXPORT_DIR = BASE_DIR / "exports"
LOG_DIR = BASE_DIR / "logs"
WEB_DIR = BASE_DIR / "web"                  # HTML / CSS / JavaScript frontend

DATABASE_PATH = DATA_DIR / "attendance.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
EXCEL_PATH = BASE_DIR / "attendance.xlsx"
LOG_FILE = LOG_DIR / "app.log"

# --------------------------------------------------------------------------
# Face models (OpenCV Zoo: YuNet detector + SFace recognizer, ONNX format)
# --------------------------------------------------------------------------
DETECTOR_MODEL_FILE = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_MODEL_FILE = "face_recognition_sface_2021dec.onnx"
DETECTOR_MODEL_URLS = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx",
)
RECOGNIZER_MODEL_URLS = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_recognition_sface/face_recognition_sface_2021dec.onnx",
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
    "face_recognition_sface/face_recognition_sface_2021dec.onnx",
)
DETECTOR_MIN_BYTES = 100_000          # guards against partial / LFS pointer files
RECOGNIZER_MIN_BYTES = 10_000_000

EMBEDDING_DIM = 128                   # SFace produces 128-dimension embeddings
DETECTION_SCORE_THRESHOLD = 0.85      # minimum detector confidence for a face
MIN_FACE_SIZE = 70                    # faces smaller than this (pixels) are ignored

# --------------------------------------------------------------------------
# Recognition tuning
# --------------------------------------------------------------------------
UNCERTAIN_MARGIN = 0.08       # scores within this margin below the threshold
                              # are shown as "Face not recognized"
AMBIGUITY_MARGIN = 0.03       # two people scoring this close -> do not guess
RETRY_AFTER_FAILURE_SECONDS = 5   # retry delay if Excel/DB write failed
RESULT_DISPLAY_SECONDS = 4        # how long the success message stays visible

# --------------------------------------------------------------------------
# Local web server (the app is only reachable from this computer)
# --------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 5000                          # if busy, the next free port is used
SESSION_IDLE_TIMEOUT_SECONDS = 20    # camera is released if the browser stops polling
JPEG_QUALITY = 80                    # quality of the live video sent to the browser

# --------------------------------------------------------------------------
# Camera
# --------------------------------------------------------------------------
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
MIRROR_CAMERA = True          # mirror image so it behaves like a mirror

# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
REGISTRATION_PROMPTS = (
    "Look straight at the camera",
    "Turn your head slightly to your left",
    "Turn your head slightly to your right",
    "Tilt your head slightly up",
    "Tilt your head slightly down",
)
STABLE_FRAMES_REQUIRED = 6        # frames a face must be steady before capture
PROMPT_DELAY_SECONDS = 2.0        # time given to change pose between captures
SAMPLE_CONSISTENCY_THRESHOLD = 0.30   # all samples must be the same person


@dataclass
class Settings:
    """User-adjustable settings persisted in ``data/settings.json``."""

    camera_index: int = 0
    recognition_threshold: float = 0.40
    attendance_cooldown_seconds: int = 60
    confirm_frames: int = 3

    def sanitize(self) -> None:
        """Clamp every value into a safe range."""
        self.camera_index = max(0, min(int(self.camera_index), 10))
        self.recognition_threshold = round(
            max(0.20, min(float(self.recognition_threshold), 0.90)), 2
        )
        self.attendance_cooldown_seconds = max(
            10, min(int(self.attendance_cooldown_seconds), 86_400)
        )
        self.confirm_frames = max(1, min(int(self.confirm_frames), 10))

    @classmethod
    def load(cls, path: Path = SETTINGS_PATH) -> "Settings":
        """Load settings from disk, falling back to defaults on any problem."""
        settings = cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            for field in fields(cls):
                if field.name in data:
                    caster = type(getattr(settings, field.name))
                    setattr(settings, field.name, caster(data[field.name]))
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, OSError) as exc:
            logger.warning("Could not read settings file, using defaults: %s", exc)
        settings.sanitize()
        return settings

    def save(self, path: Path = SETTINGS_PATH) -> None:
        """Write settings to disk."""
        self.sanitize()
        Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def update_from(self, other: "Settings") -> None:
        """Copy values in place so every screen sees the new settings."""
        for field in fields(self):
            setattr(self, field.name, getattr(other, field.name))


def ensure_directories() -> None:
    """Create every folder the application needs."""
    for folder in (DATA_DIR, FACE_DATA_DIR, MODEL_DIR, EXPORT_DIR, LOG_DIR):
        folder.mkdir(parents=True, exist_ok=True)
