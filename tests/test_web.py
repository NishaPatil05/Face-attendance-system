"""End-to-end tests of the web API using a fake camera and a fake face engine.

Run from the project folder:   python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import camera  # noqa: E402
import config  # noqa: E402
from attendance import AttendanceService  # noqa: E402
from config import Settings  # noqa: E402
from database import Database  # noqa: E402
from excel_manager import ExcelManager  # noqa: E402
from face_engine import DetectedFace, FaceGallery  # noqa: E402
from web_common import AppContext  # noqa: E402
from webapp import create_app  # noqa: E402

TOKEN = "test-token"
RAHUL = np.random.default_rng(7).normal(size=config.EMBEDDING_DIM).astype(np.float32)
RAHUL /= np.linalg.norm(RAHUL)


class FakeCapture:
    """Pretends to be cv2.VideoCapture and produces blank frames."""

    def set(self, *_args) -> None:
        pass

    def read(self):
        time.sleep(0.01)
        return True, np.zeros((480, 640, 3), np.uint8)

    def release(self) -> None:
        pass


class FakeEngine:
    """Always sees one big face belonging to Rahul."""

    def detect(self, _frame):
        return [DetectedFace((100, 80, 160, 160), 0.99, np.zeros(15, np.float32))]

    def embed(self, _frame, _face):
        return RAHUL


class WebTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        folder = Path(self._tmp.name)
        db = Database(folder / "t.db")
        db.initialize()
        excel = ExcelManager(folder / "attendance.xlsx")
        self.ctx = AppContext(
            settings=Settings(confirm_frames=2), db=db, excel=excel, engine=FakeEngine(),
            gallery=FaceGallery(), service=AttendanceService(db, excel), token=TOKEN)
        self.client = create_app(self.ctx).test_client()
        self.headers = {"X-App-Token": TOKEN}
        patches = [
            mock.patch.object(camera, "_open_capture", lambda _index: FakeCapture()),
            mock.patch.object(config, "PROMPT_DELAY_SECONDS", 0.05),
            mock.patch.object(config, "STABLE_FRAMES_REQUIRED", 2),
            mock.patch.object(config, "EXPORT_DIR", folder / "exports"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        self.ctx.sessions.stop_all()
        self._tmp.cleanup()

    # helpers -----------------------------------------------------------
    def get(self, path, **kwargs):
        return self.client.get(path, headers=self.headers, **kwargs)

    def post(self, path, body=None):
        return self.client.post(path, json=body or {}, headers=self.headers)

    def wait_until(self, check, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.05)
        self.fail("Timed out waiting for condition")

    def register_rahul(self) -> None:
        form = {"employee_id": "EMP001", "name": "Rahul Patil", "department": "IT",
                "email": "rahul@example.com"}
        self.assertEqual(self.post("/api/register/start", form).status_code, 200)
        self.wait_until(lambda: self.get("/api/register/status").json["state"] == "complete")
        saved = self.post("/api/register/save", form)
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertEqual(saved.json["message"], "Person registered successfully.")


class SecurityTests(WebTestCase):
    def test_index_contains_token_and_api_requires_it(self) -> None:
        page = self.client.get("/")
        self.assertIn(TOKEN, page.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/dashboard").status_code, 403)
        self.assertEqual(self.get("/api/dashboard").status_code, 200)
        wrong = self.client.get("/api/dashboard", headers={"X-App-Token": "nope"})
        self.assertEqual(wrong.status_code, 403)

    def test_foreign_host_header_rejected(self) -> None:
        response = self.client.get("/api/dashboard", headers=self.headers,
                                   base_url="http://evil.example.com")
        self.assertEqual(response.status_code, 403)

    def test_static_frontend_is_served(self) -> None:
        self.assertEqual(self.client.get("/static/js/app.js").status_code, 200)
        self.assertEqual(self.client.get("/static/css/style.css").status_code, 200)


class RegistrationFlowTests(WebTestCase):
    def test_validation_and_duplicate_id(self) -> None:
        bad = self.post("/api/register/start", {"employee_id": "bad id!", "name": "X",
                                                "department": "IT"})
        self.assertEqual(bad.status_code, 422)
        self.register_rahul()
        again = self.post("/api/register/start", {"employee_id": "emp001", "name": "Other",
                                                  "department": "HR"})
        self.assertEqual(again.status_code, 409)
        self.assertIn("already exists", again.json["error"])

    def test_save_requires_captured_face(self) -> None:
        response = self.post("/api/register/save", {"employee_id": "E1", "name": "A",
                                                    "department": "IT"})
        self.assertEqual(response.status_code, 409)

    def test_same_face_cannot_be_registered_twice(self) -> None:
        self.register_rahul()
        self.assertEqual(self.post("/api/register/start", {
            "employee_id": "EMP002", "name": "Copycat", "department": "IT"}).status_code, 200)
        status = self.wait_until(
            lambda: (s := self.get("/api/register/status").json)["state"] == "error" and s)
        self.assertIn("already registered as Rahul Patil", status["error"])

    def test_camera_unavailable_gives_friendly_error(self) -> None:
        def broken(_index):
            raise camera.CameraError("Unable to access webcam. Please check your camera connection.")
        with mock.patch.object(camera, "_open_capture", broken):
            response = self.post("/api/register/start", {
                "employee_id": "E1", "name": "A", "department": "IT"})
            self.assertEqual(response.status_code, 503)
            self.assertIn("Unable to access webcam", response.json["error"])
            self.assertEqual(self.post("/api/attendance/start").status_code, 503)


class AttendanceFlowTests(WebTestCase):
    def test_full_flow_register_recognise_records_export_delete(self) -> None:
        self.register_rahul()
        self.assertEqual(self.get("/api/dashboard").json["registered"], 1)

        self.assertEqual(self.post("/api/attendance/start").status_code, 200)
        events = self.wait_until(lambda: self.get("/api/attendance/status?since=0").json["events"])
        self.assertEqual(events[0]["status"], "marked")
        self.assertEqual(events[0]["name"], "Rahul Patil")

        # Live video is a real MJPEG stream of JPEG frames.
        stream = self.get("/api/attendance/stream")
        self.assertIn("multipart/x-mixed-replace", stream.content_type)
        first_chunk = next(iter(stream.response))
        self.assertIn(b"image/jpeg", first_chunk)
        stream.close()

        # Recognised again later -> exactly one record (duplicate rule).
        time.sleep(0.3)
        self.assertEqual(self.get("/api/dashboard").json["present_today"], 1)
        rows = self.get("/api/records").json["records"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "P")
        self.assertEqual(rows[0]["employee_id"], "EMP001")

        self.assertEqual(self.get("/api/records?search=rahul").json["records"][0]["name"],
                         "Rahul Patil")
        self.assertEqual(self.get("/api/records?search=zzz").json["records"], [])
        self.assertEqual(self.get("/api/records?date=31-12-2000").json["records"], [])
        self.assertEqual(self.get("/api/records?date=2026/01/01").status_code, 422)

        exported = self.get("/api/records/export")
        self.assertEqual(exported.status_code, 200)
        self.assertTrue(exported.data.startswith(b"PK"))     # a real .xlsx (zip) file
        empty = self.get("/api/records/export?search=zzz")
        self.assertEqual(empty.status_code, 404)

        self.assertEqual(self.post("/api/attendance/stop").status_code, 200)

        people = self.get("/api/people").json["people"]
        self.assertEqual(people[0]["samples"], len(config.REGISTRATION_PROMPTS))
        self.assertNotIn("face_encoding", people[0])
        self.assertEqual(self.client.delete("/api/people/EMP001",
                                            headers=self.headers).status_code, 200)
        self.assertEqual(self.get("/api/people").json["people"], [])
        self.assertEqual(self.client.delete("/api/people/EMP001",
                                            headers=self.headers).status_code, 404)

    def test_excel_locked_does_not_create_database_record(self) -> None:
        from excel_manager import ExcelLockedError
        self.register_rahul()

        def locked(_record):
            raise ExcelLockedError("Please close attendance.xlsx and try again.")
        with mock.patch.object(self.ctx.excel, "append_record", locked):
            self.post("/api/attendance/start")
            events = self.wait_until(
                lambda: self.get("/api/attendance/status?since=0").json["events"])
            self.assertEqual(events[0]["status"], "excel_error")
            self.assertIn("close attendance.xlsx", events[0]["message"])
            self.assertEqual(self.get("/api/records").json["records"], [])

    def test_settings_roundtrip_and_validation(self) -> None:
        with mock.patch.object(Settings, "save"):
            ok = self.post("/api/settings", {
                "camera_index": 1, "recognition_threshold": 0.45,
                "attendance_cooldown_seconds": 90, "confirm_frames": 4})
            self.assertEqual(ok.status_code, 200)
            bad = self.post("/api/settings", {"camera_index": "x"})
            self.assertEqual(bad.status_code, 422)
        settings = self.get("/api/settings").json
        self.assertEqual((settings["camera_index"], settings["recognition_threshold"],
                          settings["confirm_frames"]), (1, 0.45, 4))
        self.assertEqual(self.post("/api/settings/test-camera", {"camera_index": 0}).status_code, 200)

    def test_open_excel_and_shutdown(self) -> None:
        with mock.patch("utils.open_file") as opener:
            self.assertEqual(self.post("/api/excel/open").status_code, 200)
            opener.assert_called_once()
        called = []
        self.ctx.shutdown = lambda: called.append(True)
        self.assertEqual(self.post("/api/shutdown").status_code, 200)
        self.wait_until(lambda: called, timeout=2)

    def test_idle_session_releases_camera(self) -> None:
        with mock.patch.object(config, "SESSION_IDLE_TIMEOUT_SECONDS", 0.3):
            self.post("/api/attendance/start")
            self.wait_until(lambda: not self.ctx.sessions.attendance.running, timeout=5)


if __name__ == "__main__":
    unittest.main()
