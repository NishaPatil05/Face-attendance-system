"""Attendance records, export, Excel and registered-people API routes."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from flask import Blueprint, jsonify, request, send_file

import config
import utils
from database import DatabaseError
from excel_manager import ExcelError, ExcelLockedError
from web_common import get_ctx, json_error

logger = logging.getLogger(__name__)
bp = Blueprint("reports", __name__, url_prefix="/api")


def _filters() -> tuple[Optional[str], str, Optional[str]]:
    """Read ``date`` (DD-MM-YYYY) and ``search`` from the query string.

    Returns ``(iso_date, search, error_message)``.
    """
    date_text = request.args.get("date", "").strip()
    search = request.args.get("search", "").strip()
    if not date_text:
        return None, search, None
    try:
        return utils.display_to_iso(date_text), search, None
    except ValueError:
        return None, search, "Please enter the date as DD-MM-YYYY, for example 25-10-2026."


@bp.get("/records")
def records():
    iso_date, search, problem = _filters()
    if problem:
        return json_error(problem, 422)
    try:
        rows = get_ctx().db.query_attendance(iso_date, search)
    except DatabaseError as exc:
        return json_error(str(exc), 500)
    return jsonify(records=[{
        "employee_id": r.employee_id, "name": r.name, "department": r.department,
        "date": r.display_date, "time": r.attendance_time, "status": r.status,
    } for r in rows])


@bp.get("/records/export")
def export_records():
    ctx = get_ctx()
    iso_date, search, problem = _filters()
    if problem:
        return json_error(problem, 422)
    try:
        rows = ctx.db.query_attendance(iso_date, search)
        if not rows:
            return json_error("There are no records in the current view to export.", 404)
        config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        target = config.EXPORT_DIR / f"attendance_export_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        ctx.excel.export(rows, target)
    except (DatabaseError, ExcelError) as exc:
        return json_error(str(exc), 500)
    return send_file(target, as_attachment=True, download_name=target.name)


@bp.post("/excel/open")
def open_excel():
    ctx = get_ctx()
    try:
        ctx.service.sync_excel()            # make the file match the database
    except ExcelLockedError:
        pass                                # already open in Excel: just bring it up
    except (ExcelError, DatabaseError) as exc:
        return json_error(str(exc), 500)
    try:
        utils.open_file(ctx.excel.path)
    except OSError as exc:
        logger.error("Could not open Excel file: %s", exc)
        return json_error(f"Could not open {ctx.excel.path.name}. "
                          f"It is located at: {ctx.excel.path}", 500)
    return jsonify(ok=True)


@bp.get("/people")
def people():
    try:
        persons = get_ctx().db.list_persons(request.args.get("search", ""))
    except DatabaseError as exc:
        return json_error(str(exc), 500)
    return jsonify(people=[{
        "employee_id": p.employee_id, "name": p.name, "department": p.department,
        "email": p.email, "registered_on": utils.timestamp_to_display_date(p.created_at),
        "samples": p.sample_count,           # face data itself is never sent to the UI
    } for p in persons])


@bp.delete("/people/<path:employee_id>")
def delete_person(employee_id: str):
    ctx = get_ctx()
    try:
        deleted = ctx.db.delete_person(employee_id)
        ctx.refresh_gallery()
    except DatabaseError as exc:
        return json_error(str(exc), 500)
    if not deleted:
        return json_error("That person no longer exists.", 404)
    logger.info("Person deleted: %s", employee_id)
    return jsonify(ok=True)
