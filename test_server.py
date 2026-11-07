"""Stdlib tests for webui/server.py using a fake `herdr` executable."""
import http.client
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
(Path.home() / "tmp").mkdir(exist_ok=True)
TMP = Path(tempfile.mkdtemp(dir=Path.home() / "tmp"))
LOG = TMP / "argv.log"
FAKE = TMP / "herdr"
FAKE.write_text(f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
open({str(LOG)!r}, "a").write(json.dumps(args) + "\\n")
machine = "local"
if args[:1] == ["--session"]:
    args = args[2:]
elif args[:1] == ["--machine"]:
    machine, args = args[1], args[2:]
if machine == "offline":
    print("fixture remote unavailable", file=sys.stderr); sys.exit(1)
if args[:2] == ["machine", "list"]:
    print(json.dumps([
        {{"id": "worker-a", "label": "Worker A", "enabled": True}},
        {{"id": "offline", "label": "Offline worker", "enabled": True}},
        {{"id": "disabled", "label": "Disabled worker", "enabled": False}}
    ]))
elif args[:2] == ["api", "snapshot"]:
    print(json.dumps({{"result": {{"snapshot": {{
        "version": "9.9", "protocol": 22, "focused_workspace_id": "w1", "focused_tab_id": "w1:t1", "focused_pane_id": "w1:p1",
        "workspaces": [{{"workspace_id": "w1", "label": "space" if machine == "local" else "remote space", "number": 1, "active_tab_id": "w1:t1", "pane_count": 1, "tab_count": 1}}],
        "tabs": [{{"tab_id": "w1:t1", "workspace_id": "w1", "label": "tab", "number": 1, "pane_count": 1}}],
        "panes": [{{"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1", "agent": "pi", "agent_status": "idle",
                    "agent_session": {{"value": "/secret/session.jsonl"}}, "cwd": "/tmp"}}],
        "agents": [], "layouts": [{{"tab_id": "w1:t1", "area": {{"x": 0, "y": 0, "width": 10, "height": 10}},
                     "panes": [{{"pane_id": "w1:p1", "rect": {{"x": 0, "y": 0, "width": 10, "height": 10}}}}]}}]}}}}}}))
elif args[:2] == ["pane", "read"]:
    print(("REMOTE " if machine != "local" else "") + "hello \\x1b[31mred\\x1b[0m")
elif args[:2] == ["pane", "send-text"] and args[3] == "fixture-type-failure":
    print("fixture type failed", file=sys.stderr); sys.exit(1)
elif args[:2] == ["pane", "send-keys"] and args[2] == "w8:p8":
    print("fixture key failed", file=sys.stderr); sys.exit(1)
elif args[:2] in (["pane", "send-keys"], ["pane", "send-text"]):
    print("ok")
elif args[:2] == ["workspace", "create"]:
    print(json.dumps({{"result": {{"root_pane": {{"pane_id": "w2:p1", "workspace_id": "w2"}}, "tab": {{"tab_id": "w2:t1"}}}}}}))
elif args[:2] == ["pane", "close"] and args[2] == "w9:p9":
    print("boom", file=sys.stderr); sys.exit(1)
else:
    print(json.dumps({{"result": {{"type": "ok"}}}}))
""")
FAKE.chmod(FAKE.stat().st_mode | stat.S_IEXEC)
os.environ["HERDR_BIN"] = str(FAKE)
os.environ["HERDR_WEBUI_LOG_FILE"] = str(TMP / "errors.jsonl")
sys.path.insert(0, str(HERE))
import server  # noqa: E402


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.make_server(0, ["dashboard.example.ts.net"])
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        LOG.write_text("")
        server.invalidate()

    def calls(self):
        return [json.loads(l) for l in LOG.read_text().splitlines()]

    def req(self, path, body=None, *, token=server.TOKEN, host=None, ctype="application/json"):
        headers = {"Content-Type": ctype} if body is not None else {}
        if token and body is not None:
            headers["X-CSRF-Token"] = token
        if host:
            headers["Host"] = host
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=headers)) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_index_embeds_token_and_csp(self):
        status, body = self.req("/")
        self.assertEqual(status, 200)
        self.assertIn(server.TOKEN.encode(), body)
        self.assertNotIn(b"__CSRF_TOKEN__", body)

    def test_machine_catalog_exposes_only_selection_fields(self):
        status, body = self.req("/api/machines")
        self.assertEqual(status, 200)
        items = json.loads(body)["machines"]
        self.assertEqual(items[0]["id"], "local")
        self.assertEqual(items[1], {"id": "worker-a", "label": "Worker A", "local": False, "enabled": True})
        self.assertTrue(all(set(m) == {"id", "label", "local", "enabled"} for m in items))

    def test_remote_and_local_snapshots_do_not_share_cache_or_pane_output(self):
        remote = json.loads(self.req("/api/tree?machine=worker-a")[1])
        local = json.loads(self.req("/api/tree")[1])
        self.assertEqual(remote["machine"], "worker-a")
        self.assertEqual(remote["workspaces"][0]["label"], "remote space")
        self.assertEqual(local["workspaces"][0]["label"], "space")
        text = json.loads(self.req("/api/read?machine=worker-a&pane=w1:p1")[1])["text"]
        self.assertIn("REMOTE hello", text)
        self.assertIn(["--machine", "worker-a", "api", "snapshot"], self.calls())
        self.assertIn(["api", "snapshot"], self.calls())

    def test_remote_native_viewport_and_rename_keep_machine_routing(self):
        self.assertEqual(self.req('/api/read?machine=worker-a&pane=w1:p1&source=visible')[0], 200)
        self.assertIn(['--machine', 'worker-a', 'pane', 'read', 'w1:p1',
                       '--source', 'visible', '--lines', '120', '--format', 'text'], self.calls())
        self.assertEqual(self.req('/api/action/rename', {
            'machine': 'worker-a', 'kind': 'pane', 'id': 'w1:p1', 'label': 'Remote pane'
        })[0], 200)
        self.assertIn(['--machine', 'worker-a', 'pane', 'rename', 'w1:p1', 'Remote pane'], self.calls())

    def test_local_session_selector_is_never_combined_with_remote_machine(self):
        from unittest.mock import patch
        with patch.object(server, "SESSION", "personal"):
            self.assertEqual(self.req("/api/tree")[0], 200)
            self.assertEqual(self.req("/api/action/send", {"machine": "worker-a", "pane": "w1:p1", "text": "fictional"})[0], 200)
        self.assertIn(["--session", "personal", "api", "snapshot"], self.calls())
        self.assertIn(["--machine", "worker-a", "pane", "run", "w1:p1", "fictional"], self.calls())
        self.assertFalse(any("--session" in call and "--machine" in call for call in self.calls()))

    def test_unavailable_remote_never_falls_back_to_local(self):
        self.assertEqual(self.req("/api/tree?machine=offline")[0], 502)
        self.assertEqual(self.req("/api/action/send", {"machine": "offline", "pane": "w1:p1", "text": "fictional"})[0], 502)
        self.assertFalse(any(call[:2] == ["api", "snapshot"] or call[:2] == ["pane", "run"] for call in self.calls()))

    def test_unknown_disabled_and_invalid_targets_cannot_mutate(self):
        for target, status in (("unknown", 404), ("disabled", 503), ("worker-a\n", 400), ([], 400)):
            self.assertEqual(self.req("/api/action/close", {"machine": target, "pane": "w1:p1"})[0], status)
        self.assertFalse(any("close" in call for call in self.calls()))

    def test_identifiers_and_keys_reject_trailing_newlines(self):
        self.assertEqual(self.req("/api/action/close", {"pane": "w1:p1\n"})[0], 400)
        self.assertEqual(self.req("/api/action/keys", {"pane": "w1:p1", "keys": ["ctrl+c\n"]})[0], 400)
        self.assertEqual(self.calls(), [])

    def test_security_response_headers_and_no_cors(self):
        with urllib.request.urlopen(self.base + "/") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_rejects_ambiguous_or_negative_request_lengths(self):
        for lengths, transfer in ((["-1"], None), (["+1"], None), (["1", "1"], None), (["0"], "chunked"), ([], None), (["64001"], None)):
            conn = http.client.HTTPConnection(*self.httpd.server_address, timeout=2)
            try:
                conn.putrequest("POST", "/api/action/send")
                conn.putheader("Content-Type", "application/json")
                conn.putheader("X-CSRF-Token", server.TOKEN)
                for length in lengths:
                    conn.putheader("Content-Length", length)
                if transfer:
                    conn.putheader("Transfer-Encoding", transfer)
                conn.endheaders()
                response = conn.getresponse()
                self.assertEqual(response.status, 413 if lengths == ["64001"] else 400)
                response.read()
            finally:
                conn.close()
        self.assertEqual(self.calls(), [])

    def test_body_timeout_handles_distinct_legacy_socket_exception(self):
        from email.message import Message
        from unittest.mock import patch
        class LegacySocketTimeout(OSError):
            pass
        class SlowBody:
            def read(self, _):
                raise LegacySocketTimeout()
        handler = object.__new__(server.Handler)
        handler.path = "/api/action/send"
        handler.headers = Message()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = "10"
        handler.headers["X-CSRF-Token"] = server.TOKEN
        handler.rfile = SlowBody()
        handler._host_ok = handler._client_ok = lambda: True
        responses = []
        handler._json = lambda status, body: responses.append((status, body))
        with patch.object(server.socket, "timeout", LegacySocketTimeout):
            handler.do_POST()
        self.assertEqual(responses, [(408, {"error": "request body timed out"})])
        self.assertEqual(self.calls(), [])

    def test_slow_headers_and_bodies_have_bounded_lifetime(self):
        httpd = server.make_server(0)
        httpd.request_timeout = 0.1
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with socket.create_connection(httpd.server_address, timeout=2) as conn:
                conn.settimeout(2)
                conn.sendall(b"GET / HTTP/1.1\\r\\nHost: localhost\\r\\nX-Incomplete: ")
                self.assertEqual(conn.recv(1024), b"")
            conn = http.client.HTTPConnection(*httpd.server_address, timeout=2)
            try:
                conn.putrequest("POST", "/api/action/send")
                conn.putheader("Content-Type", "application/json")
                conn.putheader("X-CSRF-Token", server.TOKEN)
                conn.putheader("Content-Length", "10")
                conn.endheaders(b"{")
                response = conn.getresponse()
                self.assertEqual(response.status, 408)
                response.read()
            finally:
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(self.calls(), [])

    def test_tree_is_normalized_and_hides_session_paths(self):
        status, body = self.req("/api/tree")
        tree = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(tree["panes"][0]["agent"], "pi")
        self.assertTrue(tree["panes"][0]["has_session"])
        self.assertNotIn(b"/secret/session.jsonl", body)
        self.assertEqual(tree["focused"]["pane"], "w1:p1")

    def test_read_reports_line_and_character_limits(self):
        from unittest.mock import patch
        body = json.loads(self.req("/api/read?pane=w1:p1&lines=100000")[1])
        self.assertEqual(self.calls()[-1][-3], "50000")
        self.assertEqual((body["lines"], body["max_lines"], body["line_limited"], body["char_limited"]), (50000, 50000, True, False))
        body = json.loads(self.req("/api/read?pane=w1:p1&lines=5000")[1])
        self.assertEqual(self.calls()[-1][-3], "5000")
        self.assertEqual((body["lines"], body["line_limited"]), (5000, False))
        body = json.loads(self.req("/api/read?pane=w1:p1&lines=800")[1])
        self.assertEqual((body["lines"], body["line_limited"]), (800, False))
        with patch.object(server, "MAX_READ", 8):
            body = json.loads(self.req("/api/read?pane=w1:p1")[1])
        self.assertTrue(body["char_limited"])
        self.assertLessEqual(len(body["text"]), 8)
        self.assertNotIn("hello", body["text"])  # The cut-off partial line is omitted.

    def test_read_passes_argv_and_rejects_bad_ids(self):
        status, body = self.req("/api/read?pane=w1:p1&lines=50&ansi=1")
        self.assertEqual(status, 200)
        self.assertIn("red", json.loads(body)["text"])
        self.assertEqual(self.calls()[-1], ["pane", "read", "w1:p1", "--source", "recent-unwrapped", "--lines", "50", "--format", "ansi"])
        self.assertEqual(self.req("/api/read?pane=w1:p1&source=visible&lines=80&ansi=1")[0], 200)
        self.assertEqual(self.calls()[-1], ["pane", "read", "w1:p1", "--source", "visible", "--lines", "80", "--format", "ansi"])
        self.assertEqual(self.req("/api/read?pane=w1:p1&source=arbitrary")[0], 400)
        self.assertEqual(self.req("/api/read?pane=--help")[0], 400)
        self.assertEqual(self.req("/api/read?pane=w7:p7")[0], 404)

    def test_rename_uses_explicit_native_target(self):
        for kind, target, native in (("pane", "w1:p1", "pane"), ("tab", "w1:t1", "tab"), ("space", "w1", "workspace")):
            self.assertEqual(self.req("/api/action/rename", {"kind": kind, "id": target, "label": "New name; literal"})[0], 200)
            self.assertEqual(self.calls()[-1], [native, "rename", target, "New name; literal"])

    def test_rename_rejects_invalid_labels_and_targets(self):
        for label in ("", "   ", "-option", "line\nbreak", "tab\tname", "null\0name", "x" * 121, 123):
            self.assertEqual(self.req("/api/action/rename", {"kind": "pane", "id": "w1:p1", "label": label})[0], 400)
        for kind, target in (("host", "w1"), ("pane", "--help")):
            self.assertEqual(self.req("/api/action/rename", {"kind": kind, "id": target, "label": "valid"})[0], 400)
        self.assertEqual(self.calls(), [])

    def test_send_is_literal_and_atomic(self):
        self.assertEqual(self.req("/api/action/send", {"pane": "w1:p1", "text": "--help; rm -rf /"})[0], 200)
        self.req("/api/action/send", {"pane": "w1:p1", "text": "ab", "enter": False})
        self.req("/api/action/send", {"pane": "w1:p1", "text": ""})
        self.assertEqual(self.calls(), [["pane", "run", "w1:p1", "--help; rm -rf /"], ["pane", "send-text", "w1:p1", "ab"],
                                        ["pane", "send-keys", "w1:p1", "enter"]])

    def test_keys_validated(self):
        self.assertEqual(self.req("/api/action/keys", {"pane": "w1:p1", "keys": ["ctrl+c"]})[0], 200)
        self.assertEqual(self.req("/api/action/keys", {"pane": "w1:p1", "keys": ["--json"]})[0], 400)
        self.assertEqual(self.req("/api/action/keys", {"pane": "w1:p1", "keys": "enter"})[0], 400)

    def test_followup_types_text_before_alt_enter_with_plain_cli_output(self):
        status, body = self.req("/api/action/followup", {"pane": "w1:p1", "text": "WebUI draft"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.calls(), [["pane", "send-text", "w1:p1", "WebUI draft"], ["pane", "send-keys", "w1:p1", "alt+enter"]])

    def test_followup_rejects_empty_text_before_any_terminal_input(self):
        self.assertEqual(self.req("/api/action/followup", {"pane": "w1:p1", "text": ""})[0], 400)
        self.assertEqual(self.calls(), [])

    def test_followup_failure_is_not_replayed_and_warns_about_partial_delivery(self):
        status, body = self.req("/api/action/followup", {"pane": "w1:p1", "text": "fixture-type-failure"})
        self.assertEqual(status, 502)
        self.assertIn("inspect it before retrying", json.loads(body)["error"])
        self.assertEqual(self.calls(), [["pane", "send-text", "w1:p1", "fixture-type-failure"]])
        LOG.write_text("")
        status, body = self.req("/api/action/followup", {"pane": "w8:p8", "text": "WebUI draft"})
        self.assertEqual(status, 502)
        self.assertIn("Text may already be in the terminal", json.loads(body)["error"])
        self.assertEqual(self.calls(), [["pane", "send-text", "w8:p8", "WebUI draft"], ["pane", "send-keys", "w8:p8", "alt+enter"]])

    def test_alt_enter_passes_literal_key(self):
        status, _ = self.req("/api/action/keys", {"pane": "w1:p1", "keys": ["alt+enter"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.calls(), [["pane", "send-keys", "w1:p1", "alt+enter"]])

    def test_csrf_rejection_is_identifiable_and_runs_no_action(self):
        status, body = self.req("/api/action/send", {"pane": "w1:p1", "text": "fixture only"}, token="stale")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"error": "missing or bad CSRF token", "code": "csrf_invalid"})
        self.assertEqual(self.calls(), [])

    def test_double_interrupt_keeps_both_keys_and_never_closes(self):
        status, _ = self.req("/api/action/keys", {"pane": "w1:p1", "keys": ["ctrl+c", "ctrl+c"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.calls(), [["pane", "send-keys", "w1:p1", "ctrl+c", "ctrl+c"]])

    def test_prompt_and_focus(self):
        self.req("/api/action/prompt", {"pane": "w1:p1", "text": "hi"})
        self.req("/api/action/focus", {"pane": "w1:p1", "tab": "w1:t1", "agent": True})
        self.req("/api/action/focus", {"pane": "w1:p1", "tab": "w1:t1", "agent": False})
        self.assertEqual(self.calls(), [["agent", "prompt", "w1:p1", "hi"], ["agent", "focus", "w1:p1"], ["tab", "focus", "w1:t1"]])

    def test_spawn_new_space_with_agent(self):
        status, body = self.req("/api/action/spawn", {"space_label": "s", "tab_label": "t", "kind": "pi", "name": "bob"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.calls(), [["workspace", "create", "--label", "s", "--no-focus"], ["tab", "rename", "w2:t1", "t"],
                                        ["agent", "start", "bob", "--kind", "pi", "--pane", "w2:p1", "--timeout", "30000"]])
        self.assertEqual(self.req("/api/action/spawn", {"kind": "rm"})[0], 400)
        self.assertEqual(self.req("/api/action/spawn", {"space_label": "--evil"})[0], 400)

    def test_herdr_failure_surfaces(self):
        status, body = self.req("/api/action/close", {"pane": "w9:p9"})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"], "boom")

    def test_private_proxy_host_is_exact_and_keeps_csrf(self):
        host = "dashboard.example.ts.net"
        self.assertEqual(self.req("/", host=host)[0], 200)
        self.assertEqual(self.req("/api/tree", host=host.upper() + ":443")[0], 200)
        body = {"pane": "w1:p1", "text": "hi"}
        self.assertEqual(self.req("/api/action/send", body, host=host, token="bad")[0], 403)
        self.assertEqual(self.req("/api/action/send", body, host=host, token=None)[0], 403)
        self.assertEqual(self.req("/api/action/send", body, host=host)[0], 200)
        for bad in (host + ".evil.example", "evil." + host, "other.example.ts.net"):
            self.assertEqual(self.req("/api/tree", host=bad)[0], 421)
            self.assertEqual(self.req("/api/action/send", body, host=bad)[0], 421)
        self.assertEqual(self.calls()[-1], ["pane", "run", "w1:p1", "hi"])

    def test_default_server_stays_loopback_only(self):
        httpd = server.make_server(0)
        try:
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
            self.assertEqual(httpd.allowed_hosts, {"127.0.0.1", "localhost"})
        finally:
            httpd.server_close()

    def test_private_proxy_stays_loopback_only(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_csrf_content_type_and_host_guards(self):
        self.assertEqual(self.req("/api/action/send", {"pane": "w1:p1", "text": "x"}, token="bad")[0], 403)
        self.assertEqual(self.req("/api/action/send", {"pane": "w1:p1", "text": "x"}, ctype="text/plain")[0], 415)
        self.assertEqual(self.req("/api/tree", host="evil.example:80")[0], 421)
        self.assertEqual(self.req("/api/action/nope", {})[0], 404)
        self.assertEqual(self.calls(), [])


class RestrictedClientTest(unittest.TestCase):
    IPV4 = "192.0.2.10"
    IPV6 = "2001:db8::10"
    HOST = "dashboard.example.ts.net"

    @classmethod
    def setUpClass(cls):
        cls.httpd = server.make_server(0, [cls.HOST], [cls.IPV4, cls.IPV6])
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        LOG.write_text("")
        server.invalidate()

    def req(self, path="/", *, forwarded=(), method="GET", token=server.TOKEN, host=HOST):
        conn = http.client.HTTPConnection(*self.httpd.server_address, timeout=5)
        try:
            conn.putrequest(method, path, skip_host=True)
            conn.putheader("Host", host)
            for value in forwarded:
                conn.putheader("X-Forwarded-For", value)
            if method == "POST":
                conn.putheader("Content-Type", "application/json")
                conn.putheader("X-CSRF-Token", token)
                conn.putheader("Content-Length", "0")
            conn.endheaders()
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_allowed_ipv4_and_ipv6_can_read(self):
        for ip in (self.IPV4, self.IPV6, "2001:0db8:0:0:0:0:0:10"):
            self.assertEqual(self.req(forwarded=[ip])[0], 200)
            self.assertEqual(self.req("/api/tree", forwarded=[ip])[0], 200)
            self.assertEqual(self.req("/api/read?pane=w1:p1", forwarded=[ip])[0], 200)

    def test_denied_clients_get_no_page_or_api_and_cannot_mutate(self):
        for forwarded in ([""], ["192.0.2.11"], ["2001:db8::11"], ["invalid"], [self.IPV4 + ", 192.0.2.11"],
                          [self.IPV4, self.IPV4], ["192.0.2.11", self.IPV4]):
            for path in ("/", "/api/tree", "/api/read?pane=w1:p1"):
                status, body = self.req(path, forwarded=forwarded)
                self.assertEqual(status, 403, (path, forwarded))
                self.assertNotIn(server.TOKEN.encode(), body)
            status, body = self.req("/api/action/spawn", method="POST", forwarded=forwarded)
            self.assertEqual(status, 403)
            self.assertNotIn(b"csrf_invalid", body)
        self.assertEqual(LOG.read_text(), "")

    def test_direct_loopback_remains_accessible(self):
        for host in ("127.0.0.1:8780", "localhost:8780"):
            self.assertEqual(self.req(host=host)[0], 200)
            self.assertEqual(self.req("/api/tree", host=host)[0], 200)
            self.assertEqual(self.req("/api/action/nope", method="POST", host=host)[0], 404)
            self.assertEqual(self.req("/api/action/nope", method="POST", host=host, token="bad")[0], 403)

    def test_remote_device_cannot_bypass_with_local_host(self):
        for host in ("127.0.0.1:8780", "localhost:8780"):
            self.assertEqual(self.req(host=host, forwarded=["192.0.2.11"])[0], 403)

    def test_allowed_device_still_needs_csrf(self):
        self.assertEqual(self.req("/api/action/nope", method="POST", forwarded=[self.IPV4], token="bad")[0], 403)
        self.assertEqual(self.req("/api/action/nope", method="POST", forwarded=[self.IPV4])[0], 404)
        self.assertEqual(LOG.read_text(), "")

    def test_restriction_rejects_non_loopback_proxy_connection(self):
        from email.message import Message
        handler = object.__new__(server.Handler)
        handler.server = self.httpd
        handler.headers = Message()
        handler.headers["X-Forwarded-For"] = self.IPV4
        handler.client_address = ("192.0.2.20", 1234)
        self.assertFalse(handler._client_ok())

    def test_invalid_ip_configuration_fails_before_listening(self):
        with self.assertRaises(ValueError):
            server.make_server(0, allowed_client_ips=["not-an-ip"])


if __name__ == "__main__":
    unittest.main()
