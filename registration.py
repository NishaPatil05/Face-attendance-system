"""Person registration: guided face capture session + API routes."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
from flask import Blueprint, jsonify

import config
import utils
from camera import CameraError
from database import DatabaseError, DuplicatePersonError
from face_engine import DetectedFace
from live_session import LiveSession
from web_common import AppContext, get_ctx, json_body, json_error, mjpeg_response

logger = logging.getLogger(__name__)
bp = Blueprint("registration", __name__, url_prefix="/api/register")

_GREEN = (74, 163, 22)
_RED = (38, 38, 220)
_ORANGE = (11, 120, 245)


@dataclass(frozen=True)
class PersonForm:
    """Validated values from the registration form."""

    employee_id: str
    name: str
    department: str
    email: str


def validate_form(data: dict) -> tuple[Optional[PersonForm], Optional[str]]:
    """Validate raw form data. Returns ``(form, None)`` or ``(None, message)``."""
    employee_id = str(data.get("employee_id", "")).strip()
    name = " ".join(str(data.get("name", "")).split())
    department = " ".join(str(data.get("department", "")).split())
    email = str(data.get("email", "")).strip()

    if not employee_id:
        return None, "Please enter the Employee/Student ID."
    if not utils.is_valid_employee_id(employee_id):
        return None, ("The ID may contain only letters, digits and - _ . / "
                      "(maximum 30 characters).")
    if not name:
        return None, "Please enter the full name."
    if len(name) > 80:
        return None, "The name is too long (maximum 80 characters)."
    if not department:
        return None, "Please enter the department."
    if len(department) > 60:
        return None, "The department is too long (maximum 60 characters)."
    if email and not utils.is_valid_email(email):
        return None, "The email address does not look valid."
    return PersonForm(employee_id, name, department, email), None


class RegistrationSession(LiveSession):
    """Captures several face samples, one pose at a time.

    Only face *embeddings* (numbers) are kept, in memory, until the person is
    saved. No photograph is ever written to disk.
    """

    def __init__(self, ctx: AppContext, form: PersonForm) -> None:
        super().__init__(ctx.settings.camera_index, "registration")
        self._ctx = ctx
        self.form = form
        self._lock = threading.Lock()
        self._samples: list[np.ndarray] = []
        self._state = "capturing"          # capturing | complete | error
        self._message = ""
        self._error_message = ""
        self._ready_at = time.monotonic() + config.PROMPT_DELAY_SECONDS
        self._stable_frames = 0

    # ------------------------------------------------------------------
    @property
    def total(self) -> int:
        return len(config.REGISTRATION_PROMPTS)

    @property
    def samples(self) -> list[np.ndarray]:
        with self._lock:
            return list(self._samples)

    @property
    def is_complete(self) -> bool:
        with self._lock:
            return self._state == "complete" and len(self._samples) >= self.total

    def discard(self) -> None:
        """Stop and forget all captured biometric data."""
        self.stop()
        with self._lock:
            self._samples.clear()

    def status(self) -> dict:
        """Snapshot for the browser."""
        with self._lock:
            state, message, error = self._state, self._message, self._error_message
            done = len(self._samples)
        if state == "capturing" and not self.running:
            state = "error"
            error = self.error or "Capture stopped. Please try again."
        elif state == "capturing" and self.error:
            state, error = "error", self.error
        instruction = ""
        if state == "capturing" and done < self.total:
            instruction = f"Step {done + 1} of {self.total}: {config.REGISTRATION_PROMPTS[done]}"
        elif state == "complete":
            instruction = "\u2713 Face captured successfully."
        return {"state": state, "samples": done, "total": self.total,
                "instruction": instruction, "message": message, "error": error}

    # ------------------------------------------------------------------
    def process_frame(self, frame: np.ndarray, canvas: np.ndarray) -> None:
        if self._state != "capturing":
            return
        faces = self._ctx.engine.detect(frame)
        now = time.monotonic()
        capture: Optional[DetectedFace] = None

        if not faces:
            self._stable_frames = 0
            self._set_message("No face detected. Please face the camera.")
        elif len(faces) > 1:
            self._stable_frames = 0
            self._draw(canvas, faces, _RED)
            self._set_message("Multiple faces detected. Only one person may be visible.")
        else:
            face = faces[0]
            if min(face.box[2], face.box[3]) < config.MIN_FACE_SIZE:
                self._stable_frames = 0
                self._draw(canvas, faces, _ORANGE)
                self._set_message("Please move closer to the camera.")
            elif now < self._ready_at:
                self._stable_frames = 0
                self._draw(canvas, faces, _GREEN)
                self._set_message(f"Get ready... ({max(self._ready_at - now, 0):.0f}s)")
            else:
                self._stable_frames += 1
                self._draw(canvas, faces, _GREEN)
                self._set_message("Hold still...")
                if self._stable_frames >= config.STABLE_FRAMES_REQUIRED:
                    capture = face
        if capture is not None:
            self._capture_sample(frame, capture)

    @staticmethod
    def _draw(canvas: np.ndarray, faces: list[DetectedFace],
              color: tuple[int, int, int]) -> None:
        for face in faces:
            x, y, w, h = face.box
            cv2.rectangle(canvas, (x, y), (x + w, y + h), color, 2)

    def _set_message(self, message: str) -> None:
        with self._lock:
            self._message = message

    def _capture_sample(self, frame: np.ndarray, face: DetectedFace) -> None:
        embedding = self._ctx.engine.embed(frame, face)
        self._stable_frames = 0

        if self._samples:
            if float(np.dot(self._samples[0], embedding)) < config.SAMPLE_CONSISTENCY_THRESHOLD:
                self._set_message("The face changed during capture. Only one person "
                                  "should be in front of the camera.")
                return
        else:
            match = self._ctx.gallery.match(embedding,
                                            self._ctx.settings.recognition_threshold)
            if match.status == "match":
                logger.warning("Registration blocked: face already registered as %s",
                               match.employee_id)
                with self._lock:
                    self._state = "error"
                    self._error_message = (f"This face is already registered as "
                                           f"{match.name} ({match.employee_id}).")
                self.request_stop()
                return

        with self._lock:
            self._samples.append(embedding)
            count = len(self._samples)
        if count >= self.total:
            with self._lock:
                self._state = "complete"
                self._message = "Check the details, then click Save Person."
            logger.info("Face capture completed (%d samples)", count)
            self.request_stop()                 # releases the camera
        else:
            self._ready_at = time.monotonic() + config.PROMPT_DELAY_SECONDS


@bp.post("/start")
def start():
    ctx = get_ctx()
    form, problem = validate_form(json_body())
    if form is None:
        return json_error(problem or "Invalid form.", 422)
    try:
        if ctx.db.employee_exists(form.employee_id):
            return json_error(f"Employee/Student ID '{form.employee_id}' already exists. "
                              "Use a different ID, or delete the existing person first.", 409)
        ctx.refresh_gallery()
    except DatabaseError as exc:
        return json_error(str(exc), 500)

    ctx.sessions.stop_all()
    session = RegistrationSession(ctx, form)
    try:
        session.start()
    except CameraError as exc:
        logger.error("Registration camera error: %s", exc)
        return json_error(str(exc), 503)
    ctx.sessions.registration = session
    return jsonify(ok=True, total=session.total)


@bp.get("/status")
def status():
    session = get_ctx().sessions.registration
    if session is None:
        return jsonify(state="idle", samples=0, total=len(config.REGISTRATION_PROMPTS),
                       instruction="", message="", error="")
    session.touch()
    return jsonify(session.status())


@bp.get("/stream")
def stream():
    session = get_ctx().sessions.registration
    if session is None or not session.running:
        return json_error("Registration camera is not running.", 409)
    return mjpeg_response(session)


@bp.post("/cancel")
def cancel():
    ctx = get_ctx()
    session = ctx.sessions.registration
    if session is not None:
        session.discard()
        ctx.sessions.registration = None
    return jsonify(ok=True)


@bp.post("/save")
def save():
    ctx = get_ctx()
    session = ctx.sessions.registration
    if session is None or not session.is_complete:
        return json_error("Please capture the face before saving.", 409)
    form, problem = validate_form(json_body())
    if form is None:
        return json_error(problem or "Invalid form.", 422)
    try:
        ctx.db.add_person(form.employee_id, form.name, form.department,
                          form.email, session.samples)
    except DuplicatePersonError:
        return json_error(f"Employee/Student ID '{form.employee_id}' already exists.", 409)
    except DatabaseError as exc:
        return json_error(str(exc), 500)

    logger.info("Person registered: %s", form.employee_id)
    session.discard()
    ctx.sessions.registration = None
    try:
        ctx.refresh_gallery()
    except DatabaseError:
        logger.exception("Could not refresh gallery after registration")
    return jsonify(ok=True, message="Person registered successfully.")
