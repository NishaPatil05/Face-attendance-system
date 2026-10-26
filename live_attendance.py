"""Live attendance: camera session + API routes (/api/attendance/...)."""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime

import cv2
import numpy as np
from flask import Blueprint, jsonify, request

import utils
from attendance import FaceOverlay, MarkStatus, RecognitionSession
from camera import CameraError
from database import DatabaseError
from live_session import LiveSession
from web_common import AppContext, get_ctx, json_error, mjpeg_response

logger = logging.getLogger(__name__)
bp = Blueprint("attendance", __name__, url_prefix="/api/attendance")

_BOX_COLORS_BGR = {          # OpenCV uses blue-green-red order
    "known": (74, 163, 22),
    "unknown": (38, 38, 220),
    "uncertain": (11, 120, 245),
}
_FONT = cv2.FONT_HERSHEY_SIMPLEX


def draw_overlays(frame: np.ndarray, overlays: list[FaceOverlay]) -> None:
    """Draw a rectangle and label around each face (modifies ``frame``)."""
    for overlay in overlays:
        x, y, w, h = overlay.box
        color = _BOX_COLORS_BGR[overlay.kind]
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
        label = utils.to_ascii(overlay.label)       # OpenCV fonts are ASCII only
        (text_w, text_h), _ = cv2.getTextSize(label, _FONT, 0.55, 1)
        top = y - text_h - 12
        if top < 0:
            top = y
        cv2.rectangle(frame, (x, top), (x + text_w + 10, top + text_h + 12), color, -1)
        cv2.putText(frame, label, (x + 5, top + text_h + 4), _FONT, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA)


class AttendanceLiveSession(LiveSession):
    """Recognises faces continuously and marks attendance."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx.settings.camera_index, "attendance")
        self._recognition = RecognitionSession(ctx.engine, ctx.gallery,
                                               ctx.service, ctx.settings)
        self._registered = len(ctx.gallery)
        self._lock = threading.Lock()
        self._events: deque[dict] = deque(maxlen=200)
        self._event_id = 0
        self._faces = 0

    def process_frame(self, frame: np.ndarray, canvas: np.ndarray) -> None:
        overlays, results = self._recognition.process_frame(frame)
        draw_overlays(canvas, overlays)
        with self._lock:
            self._faces = len(overlays)
            for result in results:
                self._event_id += 1
                record = result.record
                self._events.append({
                    "id": self._event_id,
                    "status": result.status.value,
                    "name": result.name,
                    "employee_id": result.employee_id,
                    "message": result.message,
                    "time": record.attendance_time if record else "",
                    "stamp": utils.format_time(datetime.now()),
                })

    def status(self, since: int) -> dict:
        """Snapshot for the browser: new events since ``since`` plus counters."""
        with self._lock:
            events = [event for event in self._events if event["id"] > since]
            return {"running": self.running, "error": self.error, "faces": self._faces,
                    "registered": self._registered, "events": events}


@bp.post("/start")
def start():
    ctx = get_ctx()
    ctx.sessions.stop_all()                    # only one camera user at a time
    try:
        ctx.refresh_gallery()
    except DatabaseError as exc:
        return json_error(str(exc), 500)
    session = AttendanceLiveSession(ctx)
    try:
        session.start()
    except CameraError as exc:
        logger.error("Attendance camera error: %s", exc)
        return json_error(str(exc), 503)
    ctx.sessions.attendance = session
    logger.info("Attendance session started")
    return jsonify(ok=True, registered=len(ctx.gallery))


@bp.get("/status")
def status():
    session = get_ctx().sessions.attendance
    if session is None:
        return jsonify(running=False, error=None, faces=0, registered=0, events=[])
    session.touch()
    return jsonify(session.status(request.args.get("since", 0, type=int)))


@bp.get("/stream")
def stream():
    session = get_ctx().sessions.attendance
    if session is None or not session.running:
        return json_error("Attendance camera is not running.", 409)
    return mjpeg_response(session)


@bp.post("/stop")
def stop():
    ctx = get_ctx()
    session = ctx.sessions.attendance
    if session is not None:
        session.stop()
        ctx.sessions.attendance = None
        logger.info("Attendance session stopped")
    return jsonify(ok=True)
