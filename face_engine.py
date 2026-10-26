"""Face detection, embedding and matching.

Uses OpenCV's built-in YuNet (detector) and SFace (recognizer) ONNX models.
They run fully offline on the CPU through ``opencv-python`` - no dlib, CMake
or Visual Studio build tools required, which makes Windows installation easy.
"""

from __future__ import annotations

import logging
import shutil
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import cv2
import numpy as np

import config
from database import GalleryEntry

logger = logging.getLogger(__name__)


class FaceEngineError(Exception):
    """Raised when the face models cannot be downloaded or loaded."""


@dataclass(frozen=True)
class _ModelSpec:
    filename: str
    urls: tuple[str, ...]
    min_bytes: int


_MODELS = (
    _ModelSpec(config.DETECTOR_MODEL_FILE, config.DETECTOR_MODEL_URLS,
               config.DETECTOR_MIN_BYTES),
    _ModelSpec(config.RECOGNIZER_MODEL_FILE, config.RECOGNIZER_MODEL_URLS,
               config.RECOGNIZER_MIN_BYTES),
)


# ----------------------------------------------------------------------
# Model files
# ----------------------------------------------------------------------
def _model_ok(spec: _ModelSpec) -> bool:
    path = config.MODEL_DIR / spec.filename
    return path.is_file() and path.stat().st_size >= spec.min_bytes


def models_available() -> bool:
    """True when both model files are present locally."""
    return all(_model_ok(spec) for spec in _MODELS)


def download_models(progress: Optional[Callable[[str], None]] = None) -> None:
    """Download missing model files once (only the public AI models are
    downloaded; no face data is ever sent anywhere)."""
    config.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for spec in _MODELS:
        if _model_ok(spec):
            continue
        if progress:
            progress(f"Downloading {spec.filename} ...")
        _download(spec)


def _download(spec: _ModelSpec) -> None:
    target = config.MODEL_DIR / spec.filename
    temp = target.with_suffix(".part")
    for url in spec.urls:
        try:
            logger.info("Downloading model %s", spec.filename)
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=60) as response, \
                    open(temp, "wb") as output:
                shutil.copyfileobj(response, output)
            if temp.stat().st_size < spec.min_bytes:
                raise FaceEngineError("Downloaded file is incomplete.")
            temp.replace(target)
            logger.info("Model %s ready", spec.filename)
            return
        except (OSError, FaceEngineError) as exc:
            logger.warning("Download from %s failed: %s", url, exc)
            temp.unlink(missing_ok=True)
    raise FaceEngineError(
        f"Could not download {spec.filename}. Check your internet connection, or "
        f"download it manually from:\n{spec.urls[0]}\n"
        f"and place it in:\n{config.MODEL_DIR}"
    )


# ----------------------------------------------------------------------
# Detection and embedding
# ----------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class DetectedFace:
    """A detected face: bounding box ``(x, y, w, h)``, score and raw detector row."""

    box: tuple[int, int, int, int]
    score: float
    raw: np.ndarray


class FaceEngine:
    """Wraps the OpenCV YuNet detector and SFace recognizer."""

    def __init__(self) -> None:
        if not models_available():
            raise FaceEngineError(
                "Face models are missing. Restart the application with an internet "
                f"connection, or place the model files in:\n{config.MODEL_DIR}"
            )
        try:
            self._detector = cv2.FaceDetectorYN.create(
                str(config.MODEL_DIR / config.DETECTOR_MODEL_FILE), "", (320, 320),
                config.DETECTION_SCORE_THRESHOLD, 0.3, 5000,
            )
            self._recognizer = cv2.FaceRecognizerSF.create(
                str(config.MODEL_DIR / config.RECOGNIZER_MODEL_FILE), ""
            )
        except (cv2.error, AttributeError) as exc:
            raise FaceEngineError(
                "Could not load the face models. Please make sure opencv-python "
                f"4.8 or newer is installed. Details: {exc}"
            ) from exc
        self._lock = threading.Lock()
        logger.info("Face engine loaded (OpenCV %s)", cv2.__version__)

    def detect(self, frame: np.ndarray) -> list[DetectedFace]:
        """Detect all faces in a BGR frame."""
        height, width = frame.shape[:2]
        with self._lock:
            self._detector.setInputSize((width, height))
            _, rows = self._detector.detect(frame)
        if rows is None:
            return []
        faces = []
        for row in rows:
            x, y, w, h = (int(round(float(v))) for v in row[:4])
            x, y = max(x, 0), max(y, 0)
            w, h = min(w, width - x), min(h, height - y)
            if w <= 0 or h <= 0:
                continue
            faces.append(DetectedFace((x, y, w, h), float(row[-1]), row.copy()))
        return faces

    def embed(self, frame: np.ndarray, face: DetectedFace) -> np.ndarray:
        """Return a unit-length 128-d embedding for a detected face."""
        with self._lock:
            aligned = self._recognizer.alignCrop(frame, face.raw)
            feature = self._recognizer.feature(aligned)
        vector = np.asarray(feature, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm > 0 else vector


# ----------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class MatchResult:
    """Outcome of comparing one face against the registered people.

    ``status`` is ``"match"``, ``"uncertain"`` (do not guess) or ``"unknown"``.
    """

    status: str
    employee_id: str = ""
    name: str = ""
    department: str = ""
    score: float = 0.0


class FaceGallery:
    """In-memory index of every registered person's embeddings."""

    def __init__(self) -> None:
        # Replaced atomically so the worker thread never sees a half-built index.
        self._state: tuple[list[GalleryEntry], np.ndarray, np.ndarray] = (
            [], np.empty((0, config.EMBEDDING_DIM), np.float32), np.empty(0, np.int64)
        )

    def __len__(self) -> int:
        return len(self._state[0])

    def load(self, entries: Iterable[GalleryEntry]) -> None:
        """Rebuild the index from database entries."""
        entry_list = list(entries)
        if not entry_list:
            self._state = ([], np.empty((0, config.EMBEDDING_DIM), np.float32),
                           np.empty(0, np.int64))
            return
        matrix = np.vstack([entry.embeddings for entry in entry_list]).astype(np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = matrix / np.where(norms == 0, 1, norms)
        sizes = [len(entry.embeddings) for entry in entry_list]
        offsets = np.concatenate(([0], np.cumsum(sizes)[:-1])).astype(np.int64)
        self._state = (entry_list, matrix, offsets)

    def match(self, embedding: np.ndarray, threshold: float) -> MatchResult:
        """Identify an embedding.

        * ``match``     - best score >= threshold and clearly better than the
                          runner-up.
        * ``uncertain`` - score just below the threshold, or two people score
                          almost the same. Never guess in these cases.
        * ``unknown``   - nobody is similar enough.
        """
        entries, matrix, offsets = self._state
        if not entries:
            return MatchResult("unknown")
        similarities = matrix @ embedding
        person_scores = np.maximum.reduceat(similarities, offsets)
        order = np.argsort(person_scores)[::-1]
        best_index = int(order[0])
        best_score = float(person_scores[best_index])

        if best_score >= threshold:
            if len(order) > 1:
                runner_up = float(person_scores[int(order[1])])
                if runner_up >= threshold and best_score - runner_up < config.AMBIGUITY_MARGIN:
                    return MatchResult("uncertain", score=best_score)
            entry = entries[best_index]
            return MatchResult("match", entry.employee_id, entry.name,
                               entry.department, best_score)
        if best_score >= threshold - config.UNCERTAIN_MARGIN:
            return MatchResult("uncertain", score=best_score)
        return MatchResult("unknown", score=best_score)
