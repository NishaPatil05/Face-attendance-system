"""Background camera session whose annotated video is streamed to the browser.

Both live attendance and registration build on :class:`LiveSession`: it owns the
camera, runs a processing thread, and publishes the newest JPEG frame so the
web server can stream it as MJPEG.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import cv2
import numpy as np

import config
from camera import CameraStream

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_FAILURES = 5
PROCESSING_ERROR_MESSAGE = ("An unexpected error occurred while processing the camera. "
                            "Details were saved to logs/app.log.")


class LiveSession:
    """Owns a camera and a worker thread. Subclasses implement ``process_frame``."""

    def __init__(self, camera_index: int, name: str) -> None:
        self._name = name
        self._stream = CameraStream(camera_index)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._condition = threading.Condition()
        self._jpeg: Optional[bytes] = None
        self._jpeg_id = 0
        self._last_touch = time.monotonic()
        self._error: Optional[str] = None
        self._running = False

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        """True while the camera is open and frames are being processed."""
        return self._running

    @property
    def error(self) -> Optional[str]:
        """User-friendly error message if the session failed."""
        return self._error

    def start(self) -> None:
        """Open the camera and start processing. Raises CameraError."""
        self._stream.start()
        self._running = True
        self.touch()
        self._thread = threading.Thread(target=self._run, name=f"{self._name}-session",
                                        daemon=True)
        self._thread.start()

    def touch(self) -> None:
        """Called by the web layer whenever the browser is still watching."""
        self._last_touch = time.monotonic()

    def request_stop(self) -> None:
        """Ask the worker to stop (safe to call from inside ``process_frame``)."""
        self._stop_event.set()

    def stop(self) -> None:
        """Stop the worker, release the camera and wake any waiting stream."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._condition:
            self._condition.notify_all()

    def wait_for_jpeg(self, last_id: int, timeout: float = 1.0) -> tuple[Optional[bytes], int]:
        """Block until a newer frame than ``last_id`` is available."""
        with self._condition:
            if self._jpeg_id == last_id and self._running:
                self._condition.wait(timeout)
            return self._jpeg, self._jpeg_id

    def process_frame(self, frame: np.ndarray, canvas: np.ndarray) -> None:
        """Analyse ``frame``; draw annotations onto ``canvas`` (a copy)."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    def _run(self) -> None:
        last_id = -1
        failures = 0
        try:
            while not self._stop_event.is_set():
                if time.monotonic() - self._last_touch > config.SESSION_IDLE_TIMEOUT_SECONDS:
                    logger.info("%s session idle - releasing camera", self._name)
                    break
                if self._stream.error is not None:
                    self._error = str(self._stream.error)
                    break
                item = self._stream.read_latest()
                if item is None or item[1] == last_id:
                    time.sleep(0.01)
                    continue
                frame, last_id = item
                canvas = frame.copy()
                try:
                    self.process_frame(frame, canvas)
                    failures = 0
                except Exception:
                    failures += 1
                    logger.exception("Error while processing %s frame", self._name)
                    if failures >= _MAX_CONSECUTIVE_FAILURES:
                        self._error = PROCESSING_ERROR_MESSAGE
                        break
                    time.sleep(0.2)
                ok, buffer = cv2.imencode(".jpg", canvas,
                                          [cv2.IMWRITE_JPEG_QUALITY, config.JPEG_QUALITY])
                if ok:
                    with self._condition:
                        self._jpeg = buffer.tobytes()
                        self._jpeg_id += 1
                        self._condition.notify_all()
        finally:
            self._stream.stop()
            self._running = False
            with self._condition:
                self._condition.notify_all()
