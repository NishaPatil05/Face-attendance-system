"""Entry point: python main.py  (starts the local web app and opens your browser)."""

from __future__ import annotations

import logging
import secrets
import sys
import threading
import webbrowser

from werkzeug.serving import make_server

import config
import face_engine
import utils
from attendance import AttendanceService
from config import Settings
from database import Database, DatabaseError
from excel_manager import ExcelError, ExcelManager
from face_engine import FaceEngine, FaceEngineError, FaceGallery
from web_common import AppContext
from webapp import create_app

logger = logging.getLogger(__name__)


def _create_server(app, host: str, first_port: int):
    """Start on ``first_port`` or the next free port."""
    for port in range(first_port, first_port + 10):
        try:
            return make_server(host, port, app, threaded=True), port
        except OSError:
            logger.warning("Port %d is busy, trying the next one", port)
    raise OSError(f"No free port found between {first_port} and {first_port + 9}.")


def main() -> int:
    """Prepare everything and serve the app. Returns a process exit code."""
    config.ensure_directories()
    utils.setup_logging()
    logger.info("Application started")

    try:
        if not face_engine.models_available():
            print("Downloading face recognition models (one time, about 40 MB)...")
            face_engine.download_models(print)
        engine = FaceEngine()
    except FaceEngineError as exc:
        logger.error("Face engine unavailable: %s", exc)
        print(f"\nERROR: {exc}")
        return 1

    database = Database(config.DATABASE_PATH)
    try:
        database.initialize()
    except DatabaseError as exc:
        print(f"\nERROR: {exc}")
        return 1

    excel = ExcelManager(config.EXCEL_PATH)
    try:
        excel.ensure_file()
    except ExcelError as exc:           # not fatal: retried when attendance is marked
        logger.warning("Could not prepare Excel file at start-up: %s", exc)

    context = AppContext(
        settings=Settings.load(), db=database, excel=excel, engine=engine,
        gallery=FaceGallery(), service=AttendanceService(database, excel),
        token=secrets.token_urlsafe(32),
    )
    try:
        context.refresh_gallery()
    except DatabaseError as exc:
        logger.error("Could not load registered faces: %s", exc)

    app = create_app(context)
    try:
        server, port = _create_server(app, config.HOST, config.PORT)
    except OSError as exc:
        print(f"\nERROR: {exc}")
        return 1
    context.shutdown = server.shutdown

    url = f"http://{config.HOST}:{port}/"
    print(f"\n{config.APP_NAME} is running at {url}")
    print("Your browser will open automatically. Press Ctrl+C here to quit.\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        context.sessions.stop_all()
        logger.info("Application shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
