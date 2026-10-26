"""Dashboard, settings and shutdown API routes."""

from __future__ import annotations

import logging
import threading

from flask import Blueprint, jsonify

import config
from camera import CameraError, test_camera
from config import Settings
from database import DatabaseError
from web_common import get_ctx, json_body, json_error

logger = logging.getLogger(__name__)
bp = Blueprint("dashboard", __name__, url_prefix="/api")


@bp.get("/dashboard")
def dashboard():
    ctx = get_ctx()
    try:
        return jsonify(app_name=config.APP_NAME,
                       registered=ctx.db.count_persons(),
                       present_today=ctx.service.present_today())
    except DatabaseError as exc:
        return json_error(str(exc), 500)


@bp.get("/settings")
def get_settings():
    s = get_ctx().settings
    return jsonify(
        camera_index=s.camera_index,
        recognition_threshold=s.recognition_threshold,
        attendance_cooldown_seconds=s.attendance_cooldown_seconds,
        confirm_frames=s.confirm_frames,
        paths={"Database (people, face signatures, attendance)": str(config.DATABASE_PATH),
               "Excel attendance file": str(config.EXCEL_PATH),
               "Face model files (no personal data)": str(config.MODEL_DIR),
               "Log file": str(config.LOG_FILE)})


@bp.post("/settings")
def save_settings():
    ctx = get_ctx()
    data = json_body()
    try:
        candidate = Settings(
            camera_index=int(data["camera_index"]),
            recognition_threshold=float(data["recognition_threshold"]),
            attendance_cooldown_seconds=int(data["attendance_cooldown_seconds"]),
            confirm_frames=int(data["confirm_frames"]),
        )
    except (KeyError, TypeError, ValueError):
        return json_error("Please enter valid numbers for every setting.", 422)
    if candidate.camera_index < 0:
        return json_error("Camera index must be 0 or higher.", 422)
    candidate.sanitize()
    try:
        candidate.save()
    except OSError as exc:
        logger.error("Could not save settings: %s", exc)
        return json_error(f"Could not save settings: {exc}", 500)
    ctx.settings.update_from(candidate)
    logger.info("Settings saved")
    return jsonify(ok=True, message="Settings saved.")


@bp.post("/settings/test-camera")
def check_camera():
    ctx = get_ctx()
    data = json_body()
    try:
        index = int(data.get("camera_index", ctx.settings.camera_index))
    except (TypeError, ValueError):
        return json_error("Camera index must be a whole number such as 0 or 1.", 422)
    ctx.sessions.stop_cameras()                 # the camera must be free to test it
    try:
        test_camera(index)
    except CameraError as exc:
        return json_error(str(exc), 503)
    return jsonify(ok=True, message=f"Camera {index} is working.")


@bp.post("/shutdown")
def shutdown():
    ctx = get_ctx()
    ctx.sessions.stop_all()
    logger.info("Shutdown requested from the browser")
    threading.Timer(0.3, ctx.shutdown).start()      # let this response go out first
    return jsonify(ok=True)
