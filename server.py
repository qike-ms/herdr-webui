#!/usr/bin/env python3
"""Tiny loopback-only HTTP bridge from a browser dashboard to the local Herdr server.

Stdlib only. Every Herdr call is an argv list (no shell). Mutations require a
per-process CSRF token that is only embedded in the served page.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from error_log import BROWSER_KINDS, ERROR_CODES, ERROR_MESSAGES, ErrorLog, default_log_path, exception_info, route_name, status_name

HERE = Path(__file__).resolve().parent
HERDR = os.environ.get("HERDR_BIN", "herdr")
SESSION = os.environ.get("HERDR_SESSION")
TIMEOUT = float(os.environ.get("HERDR_TIMEOUT", "20"))
TOKEN = secrets.token_urlsafe(32)

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.\-]{0,127}$")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_\-]{0,31}$")
KINDS = {"pi", "claude", "codex", "gemini", "cursor", "opencode", "copilot", "amp", "hermes", "qwen", "kimi", "droid"}
MAX_TEXT = 16_000
MAX_READ = 250_000


class BridgeError(Exception):
    def __init__(self, message: str, status: int = 502, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code or ERROR_CODES.get(message, "herdr_error" if status >= 500 else "validation_error")


def herdr(args: list[str], *, parse: bool = True, machine: str = "local"):
    selector = ["--machine", machine] if machine != "local" else (["--session", SESSION] if SESSION else [])
    try:
        done = subprocess.run([HERDR, *selector, *args], capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except FileNotFoundError:
        raise BridgeError(f"herdr not found: {HERDR}", code="herdr_not_found")
    except subprocess.TimeoutExpired:
        raise BridgeError(f"herdr timed out after {TIMEOUT:g}s", 504, "herdr_timeout")
    if done.returncode != 0:
        lines = (done.stderr or done.stdout).strip().splitlines()
        raise BridgeError((lines[-1] if lines else f"exit {done.returncode}")[:500], code="herdr_exit")
    if not parse:
        return done.stdout
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise BridgeError("herdr returned invalid JSON", code="herdr_invalid_json")
    return payload.get("result", payload) if isinstance(payload, dict) else payload


# ---------------------------------------------------------------------- tree
_cache: dict = {}
_lock = threading.Lock()
PANE_KEYS = ("pane_id", "tab_id", "workspace_id", "label", "cwd", "foreground_cwd", "focused",
             "agent_status", "terminal_title_stripped", "revision")


def snapshot(max_age: float = 1.0, *, machine: str = "local", label: str | None = None) -> dict:
    """Host > Space(workspace) > Tab > Pane, normalized from `herdr api snapshot`."""
    with _lock:
        cached = _cache.get(machine)
        if cached and time.time() - cached["at"] < max_age:
            return cached["tree"]
    started = time.time()
    raw = herdr(["api", "snapshot"], machine=machine)
    snap = raw.get("snapshot", raw) if isinstance(raw, dict) else {}
    agents = {a.get("pane_id"): a for a in snap.get("agents", []) if isinstance(a, dict)}
    panes = []
    for p in snap.get("panes", []):
        a = agents.get(p.get("pane_id"), {})
        panes.append({k: p.get(k) for k in PANE_KEYS} | {
            "agent": p.get("agent") or a.get("agent"),
            "has_session": bool(p.get("agent_session") or a.get("agent_session")),
        })
    tree = {
        "host": label or socket.gethostname().split(".")[0],
        "machine": machine,
        "version": snap.get("version"),
        "protocol": snap.get("protocol"),
        "focused": {k: snap.get(f"focused_{k}_id") for k in ("workspace", "tab", "pane")},
        "workspaces": snap.get("workspaces", []),
        "tabs": snap.get("tabs", []),
        "panes": panes,
        "layouts": [{"tab_id": l.get("tab_id"), "zoomed": l.get("zoomed"), "area": l.get("area"),
                     "panes": l.get("panes", [])} for l in snap.get("layouts", [])],
        "latency_ms": round((time.time() - started) * 1000),
    }
    with _lock:
        _cache[machine] = {"at": time.time(), "tree": tree}
    return tree


def invalidate(machine: str | None = None) -> None:
    with _lock:
        if machine is None:
            _cache.clear()
        else:
            _cache.pop(machine, None)


def local_machine() -> dict:
    return {"id": "local", "label": socket.gethostname().split(".")[0], "local": True, "enabled": True}


def machines() -> dict:
    profiles = herdr(["machine", "list", "--json"])
    if not isinstance(profiles, list) or any(
        not isinstance(p, dict) or not isinstance(p.get("id"), str) or not ID_RE.fullmatch(p["id"])
        or not isinstance(p.get("label"), str) or type(p.get("enabled")) is not bool for p in profiles
    ):
        raise BridgeError("herdr returned invalid machine catalog", code="herdr_invalid_json")
    return {"machines": [local_machine(), *[
        {"id": p["id"], "label": p["label"], "local": False, "enabled": p["enabled"]} for p in profiles
    ]]}


def need_machine(value) -> dict:
    if value is None or value == "local":
        return local_machine()
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise BridgeError("invalid machine", 400)
    target = next((p for p in machines()["machines"] if p["id"] == value), None)
    if target is None:
        raise BridgeError("machine is not registered", 404)
    if not target["enabled"]:
        raise BridgeError("machine is disabled", 503)
    return target


def action_herdr(body, args, *, parse=True):
    return herdr(args, parse=parse, machine=body.get("machine", "local"))


# ------------------------------------------------------------------ validate
def need_id(body: dict, key: str) -> str:
    value = str(body.get(key) or "")
    if not ID_RE.fullmatch(value):
        raise BridgeError(f"invalid {key}", 400)
    return value


def need_text(body: dict, key: str, *, allow_empty: bool = False) -> str:
    value = body.get(key)
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > MAX_TEXT:
        raise BridgeError(f"invalid {key}", 400)
    return value


def opt_label(body: dict, key: str) -> list[str]:
    value = body.get(key)
    if not value:
        return []
    if not isinstance(value, str) or len(value) > 120 or "\n" in value or value.startswith("-"):
        raise BridgeError(f"invalid {key}", 400)
    return ["--label", value]


# ------------------------------------------------------------------- actions
def act_send(b):
    """Type literal text into the pane, then (by default) press Enter."""
    pane = need_id(b, "pane")
    text = need_text(b, "text", allow_empty=True)
    enter = bool(b.get("enter", True))
    if text and enter:
        action_herdr(b, ["pane", "run", pane, text], parse=False)  # atomic text + Enter (no race with blocked prompts)
    elif text:
        action_herdr(b, ["pane", "send-text", pane, text], parse=False)
    elif enter:
        action_herdr(b, ["pane", "send-keys", pane, "enter"], parse=False)
    return {"ok": True}


def act_keys(b):
    pane = need_id(b, "pane")
    keys = b.get("keys")
    if not isinstance(keys, list) or not 0 < len(keys) <= 16 or not all(isinstance(k, str) and KEY_RE.fullmatch(k) for k in keys):
        raise BridgeError("invalid keys", 400)
    action_herdr(b, ["pane", "send-keys", pane, *keys], parse=False)
    return {"ok": True}


def act_followup(b):
    """Type the browser draft, then submit Alt-Enter to the same explicit pane."""
    pane = need_id(b, "pane")
    text = need_text(b, "text")
    try:
        action_herdr(b, ["pane", "send-text", pane, text], parse=False)
        action_herdr(b, ["pane", "send-keys", pane, "alt+enter"], parse=False)
    except BridgeError as error:
        raise BridgeError("Follow-up delivery failed. Text may already be in the terminal; inspect it before retrying.", error.status, error.code) from error
    return {"ok": True}


def act_prompt(b):
    """Agent-aware submit (rejects when the agent is blocked)."""
    return action_herdr(b, ["agent", "prompt", need_id(b, "pane"), need_text(b, "text")])


def act_focus(b):
    """Focus the pane in the Herdr TUI: agent panes directly, others via their tab."""
    if b.get("agent"):
        return action_herdr(b, ["agent", "focus", need_id(b, "pane")])
    return action_herdr(b, ["tab", "focus", need_id(b, "tab")])


def act_split(b):
    direction = b.get("direction", "right")
    if direction not in ("right", "down"):
        raise BridgeError("invalid direction", 400)
    return action_herdr(b, ["pane", "split", need_id(b, "pane"), "--direction", direction, "--no-focus"])


def act_close(b):
    return action_herdr(b, ["pane", "close", need_id(b, "pane")])


def act_spawn(b):
    """New space (workspace) or new tab in an existing space; optionally start an agent in its root pane."""
    if b.get("workspace"):
        res = action_herdr(b, ["tab", "create", "--workspace", need_id(b, "workspace"), *opt_label(b, "tab_label"), "--no-focus"])
    else:
        res = action_herdr(b, ["workspace", "create", *opt_label(b, "space_label"), "--no-focus"])
    pane = (res.get("root_pane") or {}).get("pane_id")
    if not b.get("workspace") and b.get("tab_label") and res.get("tab", {}).get("tab_id"):
        action_herdr(b, ["tab", "rename", res["tab"]["tab_id"], *opt_label(b, "tab_label")[1:]])
    kind = b.get("kind")
    if kind:
        if kind not in KINDS or not pane:
            raise BridgeError("invalid agent kind", 400)
        name = str(b.get("name") or kind)
        if not ID_RE.fullmatch(name):
            raise BridgeError("invalid agent name", 400)
        action_herdr(b, ["agent", "start", name, "--kind", kind, "--pane", pane, "--timeout", "30000"])
    return {"pane_id": pane, "tab_id": (res.get("tab") or {}).get("tab_id"),
            "workspace_id": (res.get("root_pane") or {}).get("workspace_id")}


ACTIONS = {"send": act_send, "keys": act_keys, "followup": act_followup, "prompt": act_prompt, "focus": act_focus,
           "split": act_split, "close": act_close, "spawn": act_spawn}


# -------------------------------------------------------------------- server
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class WebUIServer(ThreadingHTTPServer):
    def __init__(self, address, log_file):
        self.errors = ErrorLog(log_file)
        try:
            super().__init__(address, Handler)
        except Exception as error:
            self.errors.event("startup_error", **exception_info(error))
            self.errors.close()
            raise

    def handle_error(self, request, client_address):
        self.errors.event("handler_error", **exception_info(sys.exc_info()[1]))

    def server_close(self):
        super().server_close()
        self.errors.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "herdr-webui/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.request_timeout)

    def _event(self, event, **fields):
        method = getattr(self, "command", "")
        self.server.errors.event(event, request_id=getattr(self, "request_id", None),
                                 method=method if method in {"GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS", "PATCH"} else "OTHER",
                                 route=route_name(getattr(self, "path", "")),
                                 duration_ms=round((time.monotonic() - self.started) * 1000), **fields)

    def log_message(self, fmt, *args):
        pass  # Successful polls stay quiet; never log raw request targets.

    def log_error(self, fmt, *args):
        if not self.error_logged:
            self._event("request_error", code="request_timeout" if "timed out" in fmt else "protocol_error")
            self.error_logged = True

    def send_error(self, code, message=None, explain=None):
        # Includes parser failures and inherited unsupported-method responses.
        if not self.error_logged:
            self._event("http_error", status=code, code="http_protocol_error", reason=status_name(code))
            self.error_logged = True
        self.close_connection = True
        return self._json(code, {"error": status_name(code)})

    def handle_one_request(self):
        self.request_id = secrets.token_hex(8)
        self.started = time.monotonic()
        self.error_logged = self.response_started = False
        self.error_code = None
        self.command, self.path = "", ""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout) as error:
            self.close_connection = True
            self._event("connection_error", code="connection_lost", **exception_info(error))
        except Exception as error:
            self.close_connection = True
            self._event("unexpected_error", code="internal_error", status=500, **exception_info(error))
            self.error_logged = True
            if not self.response_started:
                self._json(500, {"error": "internal server error"})

    def _send(self, status: int, body: bytes, ctype: str):
        self.response_started = True
        self.send_response(status)
        for k, v in (("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                     ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                     ("X-Frame-Options", "DENY"), ("Content-Security-Policy", CSP), ("X-Request-ID", self.request_id)):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout) as error:
            self.close_connection = True
            self._event("connection_error", code="response_write_failed", **exception_info(error))
        except Exception as error:
            self.close_connection = True
            self._event("unexpected_error", code="response_write_failed", **exception_info(error))

    def _json(self, status: int, data):
        if status >= 400 and not self.error_logged:
            code = self.error_code or ERROR_CODES.get(data.get("error"), "http_error")
            self._event("http_error", status=status, code=code, reason=ERROR_MESSAGES.get(code, status_name(status)))
            self.error_logged = True
        self._send(status, json.dumps(data).encode(), "application/json")

    def _host_ok(self) -> bool:
        # Defeat DNS rebinding; proxy hostnames must be explicitly configured.
        host = (self.headers.get("Host") or "").split(":", 1)[0].lower()
        return host in self.server.allowed_hosts

    def _client_ok(self) -> bool:
        allowed = self.server.allowed_client_ips
        if allowed is None:
            return True
        # Trust only the single source address overwritten by local Tailscale Serve.
        forwarded = self.headers.get_all("X-Forwarded-For", [])
        if not ipaddress.ip_address(self.client_address[0]).is_loopback:
            return False
        if not forwarded:
            return True  # Direct localhost testing; Serve always supplies a source address.
        if len(forwarded) != 1:
            return False
        try:
            return ipaddress.ip_address(forwarded[0]) in allowed
        except ValueError:
            return False

    def do_GET(self):
        if not self._host_ok():
            return self._json(421, {"error": "bad host"})
        if not self._client_ok():
            return self._json(403, {"error": "client not allowed"})
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                page = (HERE / "index.html").read_text().replace("__CSRF_TOKEN__", TOKEN)
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if url.path == "/errors.js":
                return self._send(200, (HERE / "errors.js").read_bytes(), "text/javascript; charset=utf-8")
            if url.path in {"/mockups/sre-landing", "/mockups/model-switch"}:
                mockup = "sre-landing.html" if url.path == "/mockups/sre-landing" else "model-switch.html"
                return self._send(200, (HERE / "docs/mockups" / mockup).read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/machines":
                return self._json(200, machines())
            if url.path == "/api/tree":
                target = need_machine(q.get("machine"))
                return self._json(200, snapshot(machine=target["id"], label=target["label"]))
            if url.path == "/api/read":
                pane = need_id(q, "pane")
                target = need_machine(q.get("machine"))
                if not any(p["pane_id"] == pane for p in snapshot(max_age=5, machine=target["id"], label=target["label"])["panes"]):
                    raise BridgeError("pane is not in the current snapshot", 404)
                try:
                    lines = max(5, min(int(q.get("lines", "120")), 2000))
                except ValueError:
                    raise BridgeError("bad request", 400) from None
                fmt = "ansi" if q.get("ansi") == "1" else "text"
                text = herdr(["pane", "read", pane, "--source", "recent-unwrapped", "--lines", str(lines), "--format", fmt], parse=False, machine=target["id"])
                return self._json(200, {"pane": pane, "text": text[-MAX_READ:]})
            return self._json(404, {"error": "not found"})
        except BridgeError as e:
            self.error_code = e.code
            return self._json(e.status, {"error": str(e)})

    def do_POST(self):
        if not self._host_ok():
            return self._json(421, {"error": "bad host"})
        if not self._client_ok():
            return self._json(403, {"error": "client not allowed"})
        if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), TOKEN):
            return self._json(403, {"error": "missing or bad CSRF token", "code": "csrf_invalid"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(415, {"error": "expected application/json"})
        is_report = urlparse(self.path).path == "/api/client-error"
        action = ACTIONS.get(urlparse(self.path).path.removeprefix("/api/action/"))
        if not action and not is_report:
            return self._json(404, {"error": "unknown action"})
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                raise BridgeError("invalid request framing", 400)
            length = int(lengths[0])
            if length > (2048 if is_report else 64_000):
                raise BridgeError("body too large", 413)
            try:
                raw = self.rfile.read(length)
            except socket.timeout:
                raise BridgeError("request body timed out", 408) from None
            if len(raw) != length:
                raise BridgeError("incomplete request body", 400)
            try:
                body = json.loads(raw or b"{}")
            except (ValueError, UnicodeError):
                raise BridgeError("bad request", 400, "invalid_json") from None
            if not isinstance(body, dict):
                raise BridgeError("expected object", 400)
            if is_report:
                kind = body.get("kind")
                if not isinstance(kind, str) or kind not in BROWSER_KINDS:
                    raise BridgeError("bad request", 400)
                status = body.get("status")
                related = body.get("request_id")
                self._event("browser_error", code=kind, source_route=route_name(body.get("route", "")),
                            status=status if type(status) is int and 100 <= status <= 599 else None,
                            related_request_id=related if isinstance(related, str) and re.fullmatch(r"[a-f0-9]{16}", related) else None)
                return self._json(200, {"ok": True})
            body["machine"] = need_machine(body.get("machine"))["id"]
            result = action(body)
            invalidate(body["machine"])
            return self._json(200, {"ok": True, "result": result})
        except BridgeError as e:
            self.error_code = e.code
            return self._json(e.status, {"error": str(e)})


def make_server(port: int, allowed_hosts=(), allowed_client_ips=(), *, log_file=None) -> ThreadingHTTPServer:
    clients = frozenset(ipaddress.ip_address(ip) for ip in allowed_client_ips)
    httpd = WebUIServer(("127.0.0.1", port), log_file or os.environ.get("HERDR_WEBUI_LOG_FILE") or default_log_path())
    httpd.allowed_hosts = frozenset({"127.0.0.1", "localhost", *(h.lower() for h in allowed_hosts)})
    httpd.allowed_client_ips = clients or None
    httpd.request_timeout = 10.0
    return httpd


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8780")))
    ap.add_argument("--allow-host", action="append", default=[], metavar="HOSTNAME",
                    help="additional exact Host hostname for a trusted private reverse proxy (repeatable)")
    ap.add_argument("--allow-client-ip", action="append", default=[], type=ipaddress.ip_address, metavar="IP",
                    help="allow only these exact Tailscale client IPs via local Serve, plus direct localhost (repeatable)")
    ap.add_argument("--log-file", default=None, metavar="PATH", help="private rotating JSONL error log (or HERDR_WEBUI_LOG_FILE)")
    args = ap.parse_args()
    httpd = make_server(args.port, args.allow_host, args.allow_client_ip, log_file=args.log_file)
    print(f"Herdr web UI: http://127.0.0.1:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        httpd.errors.event("server_error", **exception_info(error))
        raise
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
