# app.py

from __future__ import annotations

import logging
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.exceptions import (
    BadRequest,
    HTTPException,
    HTTPVersionNotSupported,
    MethodNotAllowed,
    NotFound,
    RequestEntityTooLarge,
)
from werkzeug.middleware.proxy_fix import ProxyFix


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "portfolio.db"

MAX_MESSAGE_LENGTH = 3000
MAX_NAME_LENGTH = 120
MAX_EMAIL_LENGTH = 254

CONTACT_RATE_LIMIT = 5
CONTACT_RATE_WINDOW = 60

rate_limit_store: dict[str, deque[float]] = defaultdict(deque)

EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def create_app() -> Flask:
    app = Flask(__name__)

    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", secrets.token_hex(32)),
        MAX_CONTENT_LENGTH=16 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
        JSON_SORT_KEYS=False,
    )

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_prefix=1,
    )

    configure_logging(app)
    register_database(app)
    register_routes(app)
    register_error_handlers(app)
    register_security_headers(app)

    with app.app_context():
        initialize_database()

    return app


def configure_logging(app: Flask) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    app.logger.setLevel(logging.INFO)


def register_database(app: Flask) -> None:
    @app.teardown_appcontext
    def close_database(_error=None):
        database = g.pop("database", None)

        if database is not None:
            database.close()


def get_database() -> sqlite3.Connection:
    if "database" not in g:
        database = sqlite3.connect(
            DATABASE_PATH,
            timeout=10,
        )

        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("PRAGMA journal_mode = WAL")
        database.execute("PRAGMA busy_timeout = 5000")

        g.database = database

    return g.database


def initialize_database() -> None:
    database = get_database()

    database.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            message TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    database.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_contact_messages_created_at
        ON contact_messages(created_at)
        """
    )

    database.commit()


def get_client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")

    if forwarded:
        return forwarded.split(",")[0].strip()[:64]

    return (request.remote_addr or "unknown")[:64]


def check_rate_limit(client_ip: str) -> bool:
    now = time.time()
    queue = rate_limit_store[client_ip]

    while queue and now - queue[0] > CONTACT_RATE_WINDOW:
        queue.popleft()

    if len(queue) >= CONTACT_RATE_LIMIT:
        return False

    queue.append(now)
    return True


def clean_text(value: str, max_length: int) -> str:
    value = value.strip()
    value = re.sub(r"\r\n?", "\n", value)

    return value[:max_length]


def validate_contact_form(
    name: str,
    email: str,
    message: str,
) -> list[str]:
    errors: list[str] = []

    if not name:
        errors.append("Please enter your name.")

    if len(name) > MAX_NAME_LENGTH:
        errors.append("Your name is too long.")

    if not email:
        errors.append("Please enter your email address.")
    elif len(email) > MAX_EMAIL_LENGTH:
        errors.append("Your email address is too long.")
    elif not EMAIL_PATTERN.fullmatch(email):
        errors.append("Please enter a valid email address.")

    if not message:
        errors.append("Please enter a message.")

    if len(message) > MAX_MESSAGE_LENGTH:
        errors.append("Your message is too long.")

    return errors


def wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True

    return (
        request.accept_mimetypes.best == "application/json"
        and request.accept_mimetypes["application/json"]
        > request.accept_mimetypes["text/html"]
    )


def register_routes(app: Flask) -> None:
    @app.route("/", methods=["GET"])
    def home():
        sent = request.args.get("sent") == "1"

        return render_template(
            "index.html",
            sent=sent,
        )

    @app.route("/contact", methods=["POST"])
    def contact():
        client_ip = get_client_ip()

        if not check_rate_limit(client_ip):
            if wants_json():
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": "Too many requests.",
                            "status": 429,
                        }
                    ),
                    429,
                )

            flash(
                "Too many messages. Please try again shortly.",
                "error",
            )

            return redirect(
                url_for("home") + "#contact",
                code=303,
            )

        name = clean_text(
            request.form.get("name", ""),
            MAX_NAME_LENGTH + 1,
        )

        email = clean_text(
            request.form.get("email", ""),
            MAX_EMAIL_LENGTH + 1,
        ).lower()

        message = clean_text(
            request.form.get("message", ""),
            MAX_MESSAGE_LENGTH + 1,
        )

        errors = validate_contact_form(
            name,
            email,
            message,
        )

        if errors:
            for error in errors:
                flash(error, "error")

            return redirect(
                url_for("home") + "#contact",
                code=303,
            )

        try:
            database = get_database()

            database.execute(
                """
                INSERT INTO contact_messages (
                    name,
                    email,
                    message,
                    ip_address,
                    user_agent
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    name,
                    email,
                    message,
                    client_ip,
                    request.headers.get("User-Agent", "")[:500],
                ),
            )

            database.commit()

        except sqlite3.Error:
            app.logger.exception(
                "Database error while saving contact message"
            )

            flash(
                "Unable to send your message right now.",
                "error",
            )

            return redirect(
                url_for("home") + "#contact",
                code=303,
            )

        flash(
            "Message received. Thank you.",
            "success",
        )

        return redirect(
            url_for("home") + "?sent=1#contact",
            code=303,
        )

    @app.route("/api/status", methods=["GET"])
    def api_status():
        return jsonify(
            {
                "ok": True,
                "service": "Chaiyaphol Pankampa Portfolio",
                "status": 200,
            }
        )

    @app.route("/api/health", methods=["GET"])
    def api_health():
        try:
            database = get_database()
            database.execute("SELECT 1").fetchone()

            database_status = "healthy"

        except sqlite3.Error:
            database_status = "unhealthy"

            return (
                jsonify(
                    {
                        "ok": False,
                        "service": "portfolio",
                        "database": database_status,
                        "status": 503,
                    }
                ),
                503,
            )

        return jsonify(
            {
                "ok": True,
                "service": "portfolio",
                "database": database_status,
                "status": 200,
            }
        )

    @app.route("/api/contact", methods=["POST"])
    def api_contact():
        client_ip = get_client_ip()

        if not check_rate_limit(client_ip):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Too many requests.",
                        "status": 429,
                    }
                ),
                429,
            )

        data = request.get_json(silent=True)

        if data is None:
            raise BadRequest(
                "Request body must contain valid JSON."
            )

        name = clean_text(
            str(data.get("name", "")),
            MAX_NAME_LENGTH + 1,
        )

        email = clean_text(
            str(data.get("email", "")),
            MAX_EMAIL_LENGTH + 1,
        ).lower()

        message = clean_text(
            str(data.get("message", "")),
            MAX_MESSAGE_LENGTH + 1,
        )

        errors = validate_contact_form(
            name,
            email,
            message,
        )

        if errors:
            return (
                jsonify(
                    {
                        "ok": False,
                        "errors": errors,
                        "status": 400,
                    }
                ),
                400,
            )

        try:
            database = get_database()

            cursor = database.execute(
                """
                INSERT INTO contact_messages (
                    name,
                    email,
                    message,
                    ip_address,
                    user_agent
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    name,
                    email,
                    message,
                    client_ip,
                    request.headers.get("User-Agent", "")[:500],
                ),
            )

            database.commit()

            message_id = cursor.lastrowid

        except sqlite3.Error:
            app.logger.exception(
                "API database error"
            )

            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Database error.",
                        "status": 500,
                    }
                ),
                500,
            )

        return (
            jsonify(
                {
                    "ok": True,
                    "message": "Message received.",
                    "id": message_id,
                    "status": 201,
                }
            ),
            201,
        )


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(400)
    def bad_request(error):
        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": getattr(
                            error,
                            "description",
                            "Bad request.",
                        ),
                        "status": 400,
                    }
                ),
                400,
            )

        return (
            render_template(
                "error.html",
                status_code=400,
                title="Bad Request",
                description="The request could not be processed.",
            ),
            400,
        )

    @app.errorhandler(404)
    def not_found(_error: NotFound):
        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Resource not found.",
                        "status": 404,
                    }
                ),
                404,
            )

        return (
            render_template(
                "error.html",
                status_code=404,
                title="Page Not Found",
                description=(
                    "The page you requested does not exist."
                ),
            ),
            404,
        )

    @app.errorhandler(405)
    def method_not_allowed(error: MethodNotAllowed):
        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "HTTP method not allowed.",
                        "allowed_methods": list(
                            error.valid_methods or []
                        ),
                        "status": 405,
                    }
                ),
                405,
            )

        return (
            render_template(
                "error.html",
                status_code=405,
                title="Method Not Allowed",
                description=(
                    "That HTTP method is not allowed "
                    "for this page."
                ),
            ),
            405,
        )

    @app.errorhandler(413)
    def request_too_large(
        _error: RequestEntityTooLarge,
    ):
        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Request is too large.",
                        "status": 413,
                    }
                ),
                413,
            )

        return (
            render_template(
                "error.html",
                status_code=413,
                title="Request Too Large",
                description=(
                    "The submitted request exceeded "
                    "the allowed size."
                ),
            ),
            413,
        )

    @app.errorhandler(429)
    def too_many_requests(_error):
        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Too many requests.",
                        "status": 429,
                    }
                ),
                429,
            )

        return (
            render_template(
                "error.html",
                status_code=429,
                title="Too Many Requests",
                description=(
                    "Please wait before trying again."
                ),
            ),
            429,
        )

    @app.errorhandler(500)
    def internal_server_error(error):
        app.logger.error(
            "500 Internal Server Error: %s",
            error,
        )

        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Internal server error.",
                        "status": 500,
                    }
                ),
                500,
            )

        return (
            render_template(
                "error.html",
                status_code=500,
                title="Server Error",
                description=(
                    "Something went wrong on the server."
                ),
            ),
            500,
        )

    @app.errorhandler(HTTPVersionNotSupported)
    def http_version_not_supported(
        _error: HTTPVersionNotSupported,
    ):
        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            "HTTP version not supported."
                        ),
                        "status": 505,
                    }
                ),
                505,
            )

        return (
            render_template(
                "error.html",
                status_code=505,
                title="HTTP Version Not Supported",
                description=(
                    "The HTTP protocol version is "
                    "not supported."
                ),
            ),
            505,
        )

    @app.errorhandler(Exception)
    def unexpected_error(error):
        if isinstance(error, HTTPException):
            return error

        app.logger.exception(
            "Unhandled application exception"
        )

        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Internal server error.",
                        "status": 500,
                    }
                ),
                500,
            )

        return (
            render_template(
                "error.html",
                status_code=500,
                title="Server Error",
                description=(
                    "An unexpected error occurred."
                ),
            ),
            500,
        )


def register_security_headers(app: Flask) -> None:
    @app.after_request
    def add_security_headers(response):
        response.headers[
            "X-Content-Type-Options"
        ] = "nosniff"

        response.headers[
            "X-Frame-Options"
        ] = "DENY"

        response.headers[
            "Referrer-Policy"
        ] = "strict-origin-when-cross-origin"

        response.headers[
            "Permissions-Policy"
        ] = (
            "camera=(), microphone=(), "
            "geolocation=(), payment=()"
        )

        response.headers[
            "Cross-Origin-Opener-Policy"
        ] = "same-origin"

        response.headers[
            "Content-Security-Policy"
        ] = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline' "
            "https://fonts.googleapis.com; "
            "font-src 'self' "
            "https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "script-src 'self'; "
            "connect-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self';"
        )

        return response


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("DEBUG", "0") == "1",
    )