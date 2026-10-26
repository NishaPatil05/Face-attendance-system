"""Webcam access with friendly error handling and a background capture thread."""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Optional

import cv2
import numpy as np

import config

logger = logging.getLogger(__name__)

CONNECTION_HELP = (
    "Unable to access webcam. Please check your camera connection."
)
IN_USE_HELP = (
    "The camera was found but no image was received. It may be in use by another "
    "application (Zoom, Teams, browser), or camera access may be blocked. On "
    "Windows, open Settings > Privacy & security > Camera and allow desktop apps "
    "to use the camera."
)
DISCONNECTED_HELP = "The camera was disconnected. Please reconnect it and try again."

_MAX_READ_FAILURES = 30          # consecutive failed reads before giving up


class CameraError(Exception):
    """Raised for any camera problem; the message is safe to show to users."""


def _open_capture(index: int) -> cv2.VideoCapture:
    """Open a capture device, preferring DirectShow on Windows (faster, more
    reliable startup), and falling back to OpenCV's default backend."""
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise CameraError(
            f"Invalid camera index '{index}'. Choose 0, 1, 2... in Settings."
        )
    backends = [cv2.CAP_DSHOW, cv2.CAP_ANY] if sys.platform.startswith("win") \
        else [cv2.CAP_ANY]
    for backend in backends:
        try:
            capture = cv2.VideoCapture(index, backend)
        except Exception as exc:  # OpenCV can raise cv2.error on odd drivers
            logger.error("VideoCapture raised: %s", exc)
            continue
        if capture.isOpened():
            return capture
        capture.release()
    raise CameraError(f"{CONNECTION_HELP} (camera index {index})")


def _warm_up(capture: cv2.VideoCapture, attempts: int = 20) -> np.ndarray:
    """Read frames until one arrives (cameras often need a moment to start)."""
    for _ in range(attempts):
        ok, frame = capture.read()
        if ok and frame is not None and frame.size > 0:
            return frame
        time.sleep(0.1)
    raise CameraError(IN_USE_HELP)


def test_camera(index: int) -> None:
    """Open the camera, grab one frame and release it. Raises CameraError."""
    capture = _open_capture(index)
    try:
        _warm_up(capture)
    finally:
        capture.release()


class CameraStream:
    """Continuously reads frames on a background thread.

    ``read_latest`` always returns the newest frame, so slow processing never
    builds up a backlog of old frames.
    """

    def __init__(self, index: int, mirror: bool = config.MIRROR_CAMERA) -> None:
        self._index = index
        self._mirror = mirror
        self._capture: Optional[cv2.VideoCapture] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._frame_id = 0
        self._error: Optional[CameraError] = None

    @property
    def error(self) -> Optional[CameraError]:
        """Set if the camera failed after starting (e.g. disconnected)."""
        return self._error

    def start(self) -> None:
        """Open the camera. Raises CameraError with a friendly message."""
        capture = _open_capture(self._index)
        try:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, config.CAMERA_WIDTH)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, config.CAMERA_HEIGHT)
            first_frame = _warm_up(capture)
        except CameraError as exc:
            capture.release()
            logger.error("Camera error: %s", exc)
            raise
        self._capture = capture
        self._store(first_frame)
        self._thread = threading.Thread(target=self._run, name="camera-reader", daemon=True)
        self._thread.start()
        logger.info("Camera %s started", self._index)

    def read_latest(self) -> Optional[tuple[np.ndarray, int]]:
        """Return ``(frame, frame_id)`` for the newest frame, or None."""
        with self._lock:
            if self._frame is None:
                return None
            return self._frame, self._frame_id

    def stop(self) -> None:
        """Stop the thread and release the camera."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self._capture is not None:
            self._capture.release()
            self._capture = None
            logger.info("Camera %s released", self._index)

    # ------------------------------------------------------------------
    def _store(self, frame: np.ndarray) -> None:
        if self._mirror:
            frame = cv2.flip(frame, 1)
        with self._lock:
            self._frame = frame
            self._frame_id += 1

    def _run(self) -> None:
        failures = 0
        while not self._stop_event.is_set():
            try:
                ok, frame = self._capture.read()
            except Exception as exc:
                logger.error("Camera read raised: %s", exc)
                ok, frame = False, None
            if not ok or frame is None:
                failures += 1
                if failures >= _MAX_READ_FAILURES:
                    self._error = CameraError(DISCONNECTED_HELP)
                    logger.error("Camera disconnected")
                    return
                time.sleep(0.03)
                continue
            failures = 0
            self._store(frame)
