"""Attendance business logic (no GUI code).

* :class:`AttendanceService` marks attendance once per person per day and keeps
  the SQLite database and the Excel file consistent.
* :class:`RecognitionSession` turns camera frames into overlays and attendance
  events, using multi-frame confirmation and a cooldown (anti-repeat logic).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Optional

import numpy as np

import config
import utils
from config import Settings
from database import AttendanceRecord, Database, DatabaseError
from excel_manager import ExcelError, ExcelManager
from face_engine import FaceEngine, FaceGallery, MatchResult

logger = logging.getLogger(__name__)

LABEL_UNKNOWN = "Unknown Person"
LABEL_NOT_RECOGNIZED = "Face not recognized"


class MarkStatus(Enum):
    """Possible outcomes of an attempt to mark attendance."""

    MARKED = "marked"
    ALREADY_PRESENT = "already_present"
    EXCEL_ERROR = "excel_error"
    DATABASE_ERROR = "database_error"

    @property
    def is_failure(self) -> bool:
        return self in (MarkStatus.EXCEL_ERROR, MarkStatus.DATABASE_ERROR)


@dataclass(frozen=True)
class MarkResult:
    """Result of :meth:`AttendanceService.mark_present`."""

    status: MarkStatus
    employee_id: str
    name: str
    message: str = ""
    record: Optional[AttendanceRecord] = None


class AttendanceService:
    """Marks attendance in SQLite and Excel as one consistent operation."""

    def __init__(self, database: Database, excel: ExcelManager) -> None:
        self._db = database
        self._excel = excel

    def mark_present(self, employee_id: str, name: str, department: str,
                     when: Optional[datetime] = None) -> MarkResult:
        """Mark a person present for the day of ``when`` (default: now).

        Duplicate protection happens in three layers:
        1. a Python check against the database,
        2. the UNIQUE(employee_id, attendance_date) database constraint,
        3. a duplicate check against the Excel sheet itself.

        The database row is written inside a transaction and committed only
        after Excel was written successfully, so a locked Excel file never
        leaves a "present" row behind that Excel does not have.
        """
        moment = when or datetime.now()
        today = utils.iso_date(moment)

        try:
            existing = self._db.get_attendance(employee_id, today)
        except DatabaseError as exc:
            return MarkResult(MarkStatus.DATABASE_ERROR, employee_id, name, str(exc))
        if existing is not None:
            logger.info("Duplicate attendance attempt ignored: %s", employee_id)
            return MarkResult(MarkStatus.ALREADY_PRESENT, employee_id, name,
                              "Already Present Today", existing)

        record = AttendanceRecord(
            employee_id=employee_id, name=name, department=department,
            attendance_date=today, attendance_time=utils.format_time(moment),
            status="P",
        )
        try:
            with self._db.transaction() as connection:
                inserted = self._db.insert_attendance(connection, record)
                if inserted:
                    # Raises ExcelError -> the transaction is rolled back.
                    self._excel.append_record(record)
        except ExcelError as exc:
            logger.error("Attendance for %s not saved (Excel problem): %s",
                         employee_id, exc)
            return MarkResult(MarkStatus.EXCEL_ERROR, employee_id, name, str(exc))
        except DatabaseError as exc:
            return MarkResult(MarkStatus.DATABASE_ERROR, employee_id, name, str(exc))

        if not inserted:     # lost a race: the DB constraint blocked the duplicate
            logger.info("Duplicate attendance blocked by database constraint: %s",
                        employee_id)
            existing = self._db.get_attendance(employee_id, today)
            return MarkResult(MarkStatus.ALREADY_PRESENT, employee_id, name,
                              "Already Present Today", existing)

        logger.info("Attendance marked: %s on %s at %s", employee_id,
                    record.display_date, record.attendance_time)
        return MarkResult(MarkStatus.MARKED, employee_id, name,
                          "Attendance Marked", record)

    def present_today(self) -> int:
        """Number of people marked present today."""
        return self._db.count_present(utils.iso_date(datetime.now()))

    def sync_excel(self) -> int:
        """Add any database attendance rows missing from Excel."""
        return self._excel.sync_missing(self._db.query_attendance())


# ----------------------------------------------------------------------
# Live recognition
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class FaceOverlay:
    """What to draw for one face. ``kind``: known / unknown / uncertain."""

    box: tuple[int, int, int, int]
    label: str
    kind: str


class RecognitionSession:
    """Processes frames and decides when attendance should be marked.

    Anti-repeat strategy
    --------------------
    1. A person must be recognised in ``confirm_frames`` consecutive processed
       frames before attendance is attempted (avoids one-frame misidentification).
    2. After an attempt the person is ignored for ``cooldown`` seconds
       (a short retry delay if saving failed).
    3. The database UNIQUE constraint remains the final protection.
    """

    _GAP_TOLERANCE_SECONDS = 1.0   # how long a person may disappear mid-confirmation

    def __init__(self, engine: FaceEngine, gallery: FaceGallery,
                 service: AttendanceService, settings: Settings,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._engine = engine
        self._gallery = gallery
        self._service = service
        self._settings = settings
        self._clock = clock
        self._votes: dict[str, tuple[int, float]] = {}      # id -> (count, last seen)
        self._blocked_until: dict[str, float] = {}          # id -> monotonic time

    def process_frame(self, frame: np.ndarray) -> tuple[list[FaceOverlay], list[MarkResult]]:
        """Recognise every face in ``frame``.

        Returns the overlays to draw and any attendance results produced.
        """
        threshold = self._settings.recognition_threshold
        overlays: list[FaceOverlay] = []
        best_by_person: dict[str, MatchResult] = {}

        for face in self._engine.detect(frame):
            if min(face.box[2], face.box[3]) < config.MIN_FACE_SIZE:
                overlays.append(FaceOverlay(face.box, LABEL_NOT_RECOGNIZED, "uncertain"))
                continue
            match = self._gallery.match(self._engine.embed(frame, face), threshold)
            if match.status == "match":
                overlays.append(FaceOverlay(
                    face.box, f"{match.name} ({match.employee_id})", "known"))
                # Same person twice in one frame (e.g. a photo)? keep best only.
                current = best_by_person.get(match.employee_id)
                if current is None or match.score > current.score:
                    best_by_person[match.employee_id] = match
            elif match.status == "uncertain":
                overlays.append(FaceOverlay(face.box, LABEL_NOT_RECOGNIZED, "uncertain"))
            else:
                overlays.append(FaceOverlay(face.box, LABEL_UNKNOWN, "unknown"))

        results = self._update_votes(best_by_person)
        return overlays, results

    def _update_votes(self, matches: dict[str, MatchResult]) -> list[MarkResult]:
        now = self._clock()
        results: list[MarkResult] = []

        # Forget people who have not been seen for a while.
        for employee_id in list(self._votes):
            _, last_seen = self._votes[employee_id]
            if employee_id not in matches and now - last_seen > self._GAP_TOLERANCE_SECONDS:
                del self._votes[employee_id]

        for employee_id, match in matches.items():
            count = self._votes.get(employee_id, (0, now))[0] + 1
            self._votes[employee_id] = (count, now)

            if now < self._blocked_until.get(employee_id, 0.0):
                continue                                   # cooldown active
            if count < self._settings.confirm_frames:
                continue                                   # not confirmed yet

            logger.info("Face recognized: %s (score %.2f)", employee_id, match.score)
            result = self._service.mark_present(employee_id, match.name, match.department)
            delay = (config.RETRY_AFTER_FAILURE_SECONDS if result.status.is_failure
                     else self._settings.attendance_cooldown_seconds)
            self._blocked_until[employee_id] = now + delay
            self._votes[employee_id] = (0, now)
            results.append(result)
        return results
