"""Private, rotating JSONL diagnostics. Never accepts arbitrary error text."""
import json
import logging
import os
import stat
import sys
import traceback
from datetime import datetime, timezone
from http import HTTPStatus
from logging.handlers import RotatingFileHandler
from pathlib import Path


ERROR_CODES = {
    "bad host": "bad_host", "client not allowed": "client_not_allowed",
    "missing or bad CSRF token": "csrf_invalid", "expected application/json": "content_type_invalid",
    "unknown action": "unknown_action", "not found": "not_found", "bad request": "bad_request",
    "invalid request framing": "request_framing_invalid", "body too large": "body_too_large",
    "incomplete request body": "body_incomplete", "expected object": "object_expected",
    "request body timed out": "body_timeout", "pane is not in the current snapshot": "pane_not_found",
    "invalid keys": "keys_invalid", "invalid direction": "direction_invalid",
    "invalid agent kind": "agent_kind_invalid", "invalid agent name": "agent_name_invalid",
    "invalid machine": "machine_invalid", "machine is not registered": "machine_not_found",
    "machine is disabled": "machine_disabled",
    **{f"invalid {key}": f"{key}_invalid" for key in ("pane", "tab", "workspace", "text", "space_label", "tab_label")},
}
ERROR_MESSAGES = {code: message for message, code in ERROR_CODES.items()}
ERROR_MESSAGES.update({"herdr_not_found": "herdr executable not found", "herdr_timeout": "herdr timed out",
                       "herdr_exit": "herdr command failed", "herdr_invalid_json": "herdr returned invalid JSON",
                       "invalid_json": "invalid JSON request body", "internal_error": "internal server error"})
BROWSER_KINDS = frozenset({"network_error", "invalid_json", "invalid_response", "csrf_refresh_failed",
                           "javascript_error", "unhandled_rejection", "resource_error", "http_error", "ui_error"})
ROUTES = frozenset({"/", "/api/machines", "/api/tree", "/api/read", "/api/client-error", "/errors.js", "/mockups/sre-landing", "/mockups/model-switch",
                    *(f"/api/action/{action}" for action in ("send", "keys", "followup", "prompt", "focus", "split", "close", "spawn"))})


def route_name(path):
    # Never retain query strings, pane IDs, arbitrary paths, or request targets.
    route = str(path).split("?", 1)[0].split("#", 1)[0]
    return route if route in ROUTES else "(unknown)"


def exception_info(error):
    # Exception messages, source lines, and locals can contain submitted secrets.
    return {"exception": type(error).__name__, "frames": [
        {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
        for frame in traceback.extract_tb(error.__traceback__)[-8:]
    ]}


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise OSError("error log must be an owner-controlled regular file")
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "a", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise

    def handleError(self, record):
        # Do not let logging's default error handler dump record contents.
        raise OSError("could not write the error log") from None


class ErrorLog:
    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handler = PrivateRotatingHandler(self.path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger = logging.Logger("herdr-webui-errors", level=logging.INFO)
        self.logger.addHandler(self.handler)

    def event(self, event, **fields):
        entry = {"time": datetime.now(timezone.utc).isoformat(), "level": "error", "event": event, **fields}
        try:
            self.logger.error(json.dumps(entry, ensure_ascii=True, separators=(",", ":")))
        except OSError:
            # A full/failed disk cannot persist a record; retain safe diagnostics on stderr.
            print("herdr-webui: error log write failed", file=sys.stderr, flush=True)
            print(json.dumps(entry, ensure_ascii=True), file=sys.stderr, flush=True)

    def close(self):
        self.handler.close()


def default_log_path():
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return state / "herdr-webui/errors.jsonl"


def status_name(status):
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "HTTP error"
