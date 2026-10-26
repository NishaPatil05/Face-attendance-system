"""Shared web-layer pieces: app context, session registry, security, helpers."""

from __future__ import annotations

import hmac
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional

from flask import Flask, Response, current_app, jsonify, request

import config

if TYPE_CHECKING:
    from attendance import AttendanceService
    from config import Settings
    from database import Database
    from excel_manager import ExcelManager
    from face_engine import FaceEngine, FaceGallery
    from live_session import LiveSession

logger = logging.getLogger(__name__)


class SessionRegistry:
    """Keeps track of the (single) camera session in use."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.attendance: Optional["LiveSession"] = None
        self.registration: Optional[Any] = None   # RegistrationSession (kept after capture)

    def stop_cameras(self) -> None:
        """Release the camera from whoever is using it."""
        with self._lock:
            for session in (self.attendance, self.registration):
                if session is not None and session.running:
                    session.stop()

    def stop_all(self) -> None:
        """Stop everything and discard held face samples."""
        self.stop_cameras()
        with self._lock:
            self.attendance = None
            self.registration = None


@dataclass
class AppContext:
    """Everything the web routes share. Created once in ``main.py``."""

    settings: "Settings"
    db: "Database"
    excel: "ExcelManager"
    engine: "FaceEngine"
    gallery: "FaceGallery"
    service: "AttendanceService"
    token: str = ""
    shutdown: Callable[[], None] = lambda: None
    sessions: SessionRegistry = field(default_factory=SessionRegistry)

    def refresh_gallery(self) -> None:
        """Reload registered faces from the database into memory."""
        self.gallery.load(self.db.load_gallery())


def get_ctx() -> AppContext:
    """Return the application context inside a request."""
    return current_app.config["CTX"]


def json_error(message: str, status: int = 400, **extra: Any) -> tuple[Response, int]:
    """Standard JSON error response."""
    return jsonify({"error": message, **extra}), status


def json_body() -> dict:
    """Request JSON as a dict (empty dict when absent or malformed)."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def mjpeg_response(session: "LiveSession") -> Response:
    """Stream a live session's frames as multipart MJPEG."""

    def generate() -> Iterator[bytes]:
        last_id = -1
        while session.running:
            session.touch()
            jpeg, frame_id = session.wait_for_jpeg(last_id, timeout=1.0)
            if jpeg is None or frame_id == last_id:
                continue
            last_id = frame_id
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")

    response = Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")
    response.headers["Cache-Control"] = "no-store"
    return response


def install_security(app: Flask) -> None:
    """Protect the local API.

    The server only listens on 127.0.0.1, but a malicious web page open in the
    same browser could still try to call it. Every /api request must therefore
    (1) use a local Host header (defeats DNS rebinding) and (2) carry the random
    per-run token that only our own page knows (defeats cross-site requests).
    """

    @app.before_request
    def _check_request() -> Optional[tuple[Response, int]]:
        if not request.path.startswith("/api/"):
            return None
        host = (request.host or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost"):
            return json_error("Forbidden host.", 403)
        expected = get_ctx().token
        supplied = request.headers.get("X-App-Token") or request.args.get("token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            logger.warning("Rejected API request without a valid token: %s", request.path)
            return json_error("Forbidden.", 403)
        return None

    @app.after_request
    def _headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response
