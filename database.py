"""SQLite storage for registered people, face embeddings and attendance."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional, Sequence

import numpy as np

import config
import utils

logger = logging.getLogger(__name__)

_BYTES_PER_EMBEDDING = config.EMBEDDING_DIM * 4   # float32


class DatabaseError(Exception):
    """Raised for any unexpected database failure (message is user friendly)."""


class DuplicatePersonError(DatabaseError):
    """Raised when an Employee/Student ID is already registered."""


@dataclass(frozen=True)
class Person:
    """A registered person (never contains the face data itself)."""

    employee_id: str
    name: str
    department: str
    email: str
    created_at: str
    sample_count: int


@dataclass(frozen=True, eq=False)
class GalleryEntry:
    """A person together with their face embeddings, used for matching."""

    employee_id: str
    name: str
    department: str
    embeddings: np.ndarray        # shape (n_samples, EMBEDDING_DIM)


@dataclass(frozen=True)
class AttendanceRecord:
    """One attendance row. ``attendance_date`` is stored as YYYY-MM-DD."""

    employee_id: str
    name: str
    department: str
    attendance_date: str
    attendance_time: str
    status: str = "P"

    @property
    def display_date(self) -> str:
        """Date formatted as DD-MM-YYYY."""
        return utils.iso_to_display(self.attendance_date)


def encode_embeddings(embeddings: Sequence[np.ndarray]) -> bytes:
    """Pack embeddings into a compact float32 byte string."""
    array = np.asarray(embeddings, dtype=np.float32).reshape(-1, config.EMBEDDING_DIM)
    return array.tobytes()


def decode_embeddings(blob: bytes) -> np.ndarray:
    """Unpack the byte string produced by :func:`encode_embeddings`."""
    return np.frombuffer(blob, dtype=np.float32).reshape(-1, config.EMBEDDING_DIM).copy()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL,
    department    TEXT NOT NULL,
    email         TEXT NOT NULL DEFAULT '',
    face_encoding BLOB NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     TEXT NOT NULL COLLATE NOCASE,
    name            TEXT NOT NULL,
    department      TEXT NOT NULL,
    attendance_date TEXT NOT NULL,
    attendance_time TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'P',
    UNIQUE (employee_id, attendance_date)
);

CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance (attendance_date);
"""


class Database:
    """Thin wrapper around SQLite. Every call uses its own connection, which
    makes the class safe to use from the GUI thread and the camera worker."""

    def __init__(self, path: Path = config.DATABASE_PATH) -> None:
        self._path = Path(path)

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------
    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            yield connection
        except sqlite3.Error as exc:
            logger.error("Database error: %s", exc)
            raise DatabaseError(
                "A database error occurred. Please check logs/app.log."
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run several statements atomically.

        Any exception raised inside the ``with`` block (including errors from
        other systems such as Excel) rolls everything back.
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        """Create tables and indexes if they do not exist."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(_SCHEMA)
        logger.info("Database ready")

    # ------------------------------------------------------------------
    # Persons
    # ------------------------------------------------------------------
    def add_person(
        self,
        employee_id: str,
        name: str,
        department: str,
        email: str,
        embeddings: Sequence[np.ndarray],
    ) -> None:
        """Register a person. Raises DuplicatePersonError if the ID exists."""
        blob = encode_embeddings(embeddings)
        with self._connection() as connection:
            try:
                connection.execute(
                    "INSERT INTO persons (employee_id, name, department, email, "
                    "face_encoding, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (employee_id, name, department, email, blob,
                     datetime.now().isoformat(timespec="seconds")),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicatePersonError(
                    f"Employee/Student ID '{employee_id}' already exists."
                ) from exc

    def employee_exists(self, employee_id: str) -> bool:
        """True if the ID is already registered (case-insensitive)."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM persons WHERE employee_id = ?", (employee_id,)
            ).fetchone()
        return row is not None

    def list_persons(self, search: str = "") -> list[Person]:
        """List registered people (without face data), optionally filtered."""
        sql = (
            "SELECT employee_id, name, department, email, created_at, "
            "length(face_encoding) AS blob_length FROM persons"
        )
        params: list[str] = []
        if search.strip():
            sql += (" WHERE employee_id LIKE ? ESCAPE '\\' "
                    "OR name LIKE ? ESCAPE '\\' OR department LIKE ? ESCAPE '\\'")
            pattern = f"%{utils.escape_like(search.strip())}%"
            params = [pattern, pattern, pattern]
        sql += " ORDER BY name COLLATE NOCASE"
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            Person(
                employee_id=row["employee_id"],
                name=row["name"],
                department=row["department"],
                email=row["email"] or "",
                created_at=row["created_at"],
                sample_count=row["blob_length"] // _BYTES_PER_EMBEDDING,
            )
            for row in rows
        ]

    def count_persons(self) -> int:
        """Number of registered people."""
        with self._connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM persons").fetchone()[0]

    def delete_person(self, employee_id: str) -> bool:
        """Delete a person and their face data. Attendance history is kept."""
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM persons WHERE employee_id = ?", (employee_id,)
            )
            return cursor.rowcount > 0

    def load_gallery(self) -> list[GalleryEntry]:
        """Load every person's embeddings for face matching."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT employee_id, name, department, face_encoding FROM persons "
                "ORDER BY id"
            ).fetchall()
        entries = []
        for row in rows:
            try:
                embeddings = decode_embeddings(row["face_encoding"])
            except ValueError:
                logger.error("Skipping person %s: corrupt face data", row["employee_id"])
                continue
            if len(embeddings) == 0:
                continue
            entries.append(GalleryEntry(row["employee_id"], row["name"],
                                        row["department"], embeddings))
        return entries

    # ------------------------------------------------------------------
    # Attendance
    # ------------------------------------------------------------------
    def has_attendance(self, employee_id: str, iso_date: str) -> bool:
        """True if the person is already marked present on that date."""
        return self.get_attendance(employee_id, iso_date) is not None

    def get_attendance(self, employee_id: str, iso_date: str) -> Optional[AttendanceRecord]:
        """Return the attendance record for a person and date, if any."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM attendance WHERE employee_id = ? AND attendance_date = ?",
                (employee_id, iso_date),
            ).fetchone()
        return self._row_to_record(row) if row else None

    @staticmethod
    def insert_attendance(connection: sqlite3.Connection,
                          record: AttendanceRecord) -> bool:
        """Insert inside an open transaction.

        Returns False (and inserts nothing) when the UNIQUE(employee_id,
        attendance_date) constraint rejects a duplicate.
        """
        try:
            connection.execute(
                "INSERT INTO attendance (employee_id, name, department, "
                "attendance_date, attendance_time, status) VALUES (?, ?, ?, ?, ?, ?)",
                (record.employee_id, record.name, record.department,
                 record.attendance_date, record.attendance_time, record.status),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def query_attendance(self, iso_date: Optional[str] = None,
                         search: str = "") -> list[AttendanceRecord]:
        """Fetch attendance filtered by date and/or name/ID text."""
        conditions: list[str] = []
        params: list[str] = []
        if iso_date:
            conditions.append("attendance_date = ?")
            params.append(iso_date)
        if search.strip():
            conditions.append("(name LIKE ? ESCAPE '\\' OR employee_id LIKE ? ESCAPE '\\')")
            pattern = f"%{utils.escape_like(search.strip())}%"
            params.extend([pattern, pattern])
        sql = "SELECT * FROM attendance"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY attendance_date DESC, id ASC"
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def count_present(self, iso_date: str) -> int:
        """Number of people marked present on a date."""
        with self._connection() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM attendance WHERE attendance_date = ?",
                (iso_date,),
            ).fetchone()[0]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AttendanceRecord:
        return AttendanceRecord(
            employee_id=row["employee_id"],
            name=row["name"],
            department=row["department"],
            attendance_date=row["attendance_date"],
            attendance_time=row["attendance_time"],
            status=row["status"],
        )
