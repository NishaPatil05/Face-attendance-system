"""Flask application factory: wires routes, security and the static frontend."""

from __future__ import annotations

import logging

from flask import Flask, Response, jsonify

import config
import dashboard
import live_attendance
import registration
import reports
from web_common import AppContext, install_security

logger = logging.getLogger(__name__)


def create_app(ctx: AppContext) -> Flask:
    """Build the Flask app that serves the HTML/CSS/JS frontend and the API."""
    app = Flask(__name__, static_folder=str(config.WEB_DIR), static_url_path="/static")
    app.config["CTX"] = ctx
    app.config["MAX_CONTENT_LENGTH"] = 1_000_000
    install_security(app)

    for module in (dashboard, live_attendance, registration, reports):
        app.register_blueprint(module.bp)

    @app.get("/")
    def index() -> Response:
        html = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")
        response = Response(html.replace("__APP_TOKEN__", ctx.token),
                            mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify(error="Not found."), 404

    @app.errorhandler(Exception)
    def unexpected(error):
        logger.exception("Unhandled server error: %s", error)
        return jsonify(error="Something went wrong. Details were saved to logs/app.log."), 500

    return app
