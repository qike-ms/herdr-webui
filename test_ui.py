"""Browser regressions with fictional sessions; never connects to live Herdr.

Uses installed Playwright and Google Chrome by default.
Set HERDR_WEBUI_BROWSER=chromium to use an installed Playwright Chromium.
Run: python3 -m unittest test_ui -v
"""
import copy
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
TREE = {
    "host": "demo", "version": "1.0", "protocol": 1, "latency_ms": 4,
    "focused": {"workspace": "w1", "tab": "t1", "pane": "p2"},
    "workspaces": [
        {"workspace_id": "w1", "label": "Website", "number": 1, "active_tab_id": "t1", "pane_count": 3},
        {"workspace_id": "w2", "label": "Docs", "number": 2, "active_tab_id": "t2", "pane_count": 1},
    ],
    "tabs": [
        {"tab_id": "t1", "workspace_id": "w1", "label": "Build", "number": 1, "pane_count": 3},
        {"tab_id": "t2", "workspace_id": "w2", "label": "Writing", "number": 1, "pane_count": 1},
        {"tab_id": "t3", "workspace_id": "w2", "label": "Empty", "number": 2, "pane_count": 0},
    ],
    "panes": [
        {"pane_id": "p1", "tab_id": "t1", "workspace_id": "w1", "label": "ui-agent", "agent": "pi", "agent_status": "working", "cwd": "src"},
        {"pane_id": "p2", "tab_id": "t1", "workspace_id": "w1", "label": "api-agent", "agent": "codex", "agent_status": "working", "cwd": "src"},
        {"pane_id": "p3", "tab_id": "t1", "workspace_id": "w1", "label": "shell", "agent": None, "agent_status": "idle", "cwd": "src"},
        {"pane_id": "p4", "tab_id": "t2", "workspace_id": "w2", "label": "review-agent", "agent": "claude", "agent_status": "blocked", "cwd": "docs"},
    ],
    "layouts": [{"tab_id": "t1", "area": {"x": 0, "y": 0, "width": 120, "height": 40}, "panes": [
        {"pane_id": "p1", "rect": {"x": 0, "y": 0, "width": 60, "height": 20}},
        {"pane_id": "p2", "rect": {"x": 60, "y": 0, "width": 60, "height": 20}},
        {"pane_id": "p3", "rect": {"x": 0, "y": 20, "width": 120, "height": 20}},
    ]}],
}


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, data, status=200, html=False):
        body = data.encode() if html else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.server.page_reads += 1
            if self.server.fail_page:
                return self.reply({"error": "Fixture page unavailable"}, 503)
            return self.reply((HERE / "index.html").read_text().replace("__CSRF_TOKEN__", self.server.csrf_token), html=True)
        if url.path == "/errors.js":
            body = (HERE / "errors.js").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/api/machines":
            return self.reply({"machines": [
                {"id": "local", "label": "Local demo", "enabled": True},
                {"id": "worker-a", "label": "Worker A", "enabled": True},
                {"id": "disabled-worker", "label": "Disabled worker", "enabled": False}
            ]})
        if url.path == "/api/tree":
            if self.server.delay_tree:
                self.server.tree_started.set()
                self.server.tree_release.wait(5)
            if self.server.fail_tree:
                return self.reply({"error": self.server.tree_error}, 502)
            tree = copy.deepcopy(self.server.tree)
            if parse_qs(url.query).get("machine") == ["worker-a"]:
                tree["host"] = "Worker A"
            return self.reply(tree)
        if url.path == "/api/read":
            pane = parse_qs(url.query)["pane"][0]
            self.server.reads.append(pane)
            query = parse_qs(url.query)
            self.server.read_queries.append({k: v[0] for k, v in query.items()})
            if pane in self.server.history and query.get("source") != ["visible"]:
                lines = int(query["lines"][0])
                if self.server.fail_history_lines == lines:
                    self.server.fail_history_lines = None
                    return self.reply({"error": "Fixture read failed"}, 502)
                if self.server.delay_lines == lines:
                    self.server.read_started.set()
                    self.server.read_release.wait(5)
                    if self.server.history_on_release:
                        self.server.history[pane] = self.server.history_on_release
                rows = self.server.history[pane][-min(lines, 2000):]
                return self.reply({"text": "\n".join(rows), "lines": min(lines, 2000), "max_lines": 2000,
                                   "line_limited": lines > 2000, "char_limited": False})
            if pane == self.server.delay_pane and "machine" not in parse_qs(url.query):
                self.server.read_started.set()
                self.server.read_release.wait(5)
            if parse_qs(url.query).get("source") == ["visible"]:
                text = self.server.native_text.get(pane, "")
                if pane in self.server.model_panes:
                    selected = self.server.native_selected.get(pane, 0) % 2
                    screen = ">\n" + "\n".join(("→ " if i == selected else "  ") + name for i, name in enumerate(("Model Alpha", "Model Beta"))) + "\nEnter to select · Escape to cancel"
                elif text and " " not in text:
                    selected = self.server.native_selected.get(pane, 0) % 2
                    screen = text + "\n" + "\n".join(("→ " if i == selected else "  ") + name for i, name in enumerate(("help    Show help", "model   Select model")))
                else:
                    screen = text + "\nReady"
                return self.reply({"text": screen})
            if pane in self.server.model_panes:
                return self.reply({"text": ">\n→ Model Alpha\n  Model Beta\nEnter to select · Escape to cancel"})
            prefix = "Remote " if parse_qs(url.query).get("machine") == ["worker-a"] else ""
            return self.reply({"text": f"{prefix}Terminal output for {pane}\n\x1b[32mReady\x1b[0m\n"})
        self.reply({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if self.path == "/api/client-error":
            self.server.reports.append(body)
            return self.reply({"ok": True})
        self.server.post_attempts += 1
        if self.headers.get("X-CSRF-Token") != self.server.csrf_token:
            return self.reply({"error": "missing or bad CSRF token", "code": "csrf_invalid"}, 403)
        if self.server.deny_action:
            return self.reply({"error": "client not allowed"}, 403)
        if self.server.delay_action:
            self.server.action_started.set()
            self.server.action_release.wait(5)
        self.server.actions.append((self.path, body, self.headers.get("X-CSRF-Token")))
        if self.server.fail_action:
            return self.reply({"error": "Fixture action failed"}, 502)
        if self.path == "/api/action/send" and not body.get("enter", True):
            self.server.native_text[body["pane"]] = self.server.native_text.get(body["pane"], "") + body["text"]
        if self.path == "/api/action/keys":
            pane = body["pane"]
            for key in body["keys"]:
                text = self.server.native_text.get(pane, "")
                if key == "backspace":
                    self.server.native_text[pane] = text[:-1]
                elif key in ("up", "down"):
                    self.server.native_selected[pane] = self.server.native_selected.get(pane, 0) + (1 if key == "down" else -1)
                elif key == "tab":
                    self.server.native_text[pane] = "/model" if self.server.native_selected.get(pane, 0) % 2 else "/help"
                elif key in ("enter", "alt+enter"):
                    if pane in self.server.model_panes:
                        self.server.model_panes.remove(pane)
                    elif text.strip() == "/model" or (" " not in text and self.server.native_selected.get(pane, 0) % 2):
                        self.server.model_panes.add(pane)
                    self.server.native_text[pane] = ""
                elif key == "esc":
                    self.server.model_panes.discard(pane)
        if self.path == "/api/action/rename":
            collection, key = {"pane": ("panes", "pane_id"), "tab": ("tabs", "tab_id"), "space": ("workspaces", "workspace_id")}[body["kind"]]
            next(item for item in self.server.tree[collection] if item[key] == body["id"])["label"] = body["label"]
        if self.path == "/api/action/close":
            self.server.tree["panes"] = [p for p in self.server.tree["panes"] if p["pane_id"] != body["pane"]]
        self.reply({"ok": True, "result": {"ok": True}})


class PhoneUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel=os.environ.get("HERDR_WEBUI_BROWSER", "chrome"), headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.httpd.tree = copy.deepcopy(TREE)
        self.httpd.fail_tree = self.httpd.fail_action = self.httpd.delay_action = False
        self.httpd.delay_tree = False
        self.httpd.tree_error = "Fixture offline"
        self.httpd.tree_started, self.httpd.tree_release = threading.Event(), threading.Event()
        self.httpd.fail_page = self.httpd.deny_action = False
        self.httpd.csrf_token = "fictional-test-token"
        self.httpd.page_reads = self.httpd.post_attempts = 0
        self.httpd.action_started, self.httpd.action_release = threading.Event(), threading.Event()
        self.httpd.actions, self.httpd.reads, self.httpd.reports = [], [], []
        self.httpd.native_text, self.httpd.native_selected, self.httpd.model_panes = {}, {}, set()
        self.httpd.delay_pane = None
        self.httpd.history, self.httpd.read_queries = {}, []
        self.httpd.fail_history_lines = self.httpd.delay_lines = self.httpd.history_on_release = None
        self.httpd.read_started, self.httpd.read_release = threading.Event(), threading.Event()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.httpd.tree_release.set()
        self.httpd.read_release.set()
        self.httpd.action_release.set()
        self.context.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.assertEqual(self.errors, [])

    def open(self, suffix=""):
        self.page.goto(self.base + "/" + suffix)
        self.page.wait_for_function("S.tree !== null")

    def choose(self, pane):
        self.page.get_by_role("button", name="Show all agents").click()
        self.page.locator(f'[data-agent-pane="{pane}"]').click()

    def test_slash_menu_forwards_arrows_and_enter_once(self):
        self.open()
        self.page.locator("#dock-input").fill("/")
        self.page.locator("#native-menu-output").get_by_text("Show help", exact=False).wait_for()
        self.assertEqual(self.httpd.actions[0][1], {"pane": "p2", "text": "/", "enter": False})
        self.page.locator("#dock-input").press("ArrowDown")
        self.page.wait_for_function("nativeInput().pending === 0")
        self.assertIn("→ model", self.page.locator("#native-menu-output").inner_text())
        self.page.locator("#dock-input").press("ArrowUp")
        self.page.locator("#dock-input").press("Enter")
        self.page.wait_for_function("!nativeInput()")
        self.assertEqual([a[1]["keys"] for a in self.httpd.actions if a[0].endswith("/keys")], [["down"], ["up"], ["enter"]])
        self.assertFalse(any(a[0].endswith("/prompt") for a in self.httpd.actions))

    def test_native_tab_parameters_do_not_duplicate_prefix(self):
        self.open()
        self.page.locator("#dock-input").fill("/")
        self.page.wait_for_function("nativeInput()?.pending === 0")
        self.page.locator("#dock-input").press("Tab")
        self.page.wait_for_function("document.querySelector('#dock-input').value === '/help '")
        self.page.locator("#dock-input").press_sequentially("topic")
        self.page.locator("#dock-input").press("Enter")
        self.page.wait_for_function("!nativeInput()")
        typed = "".join(a[1]["text"] for a in self.httpd.actions if a[0].endswith("/send"))
        self.assertEqual(typed, "/ topic")  # /help was completed by the native Tab.
        self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "keys": ["enter"]})

    def test_model_picker_arrows_and_enter_select(self):
        self.open()
        self.page.locator("#dock-input").fill("/model")
        self.page.locator("#dock-input").press("Enter")
        self.page.wait_for_function("nativeInput()?.phase === 'model' && nativeInput().pending === 0")
        self.assertIn("Model Alpha", self.page.locator("#native-menu-output").inner_text())
        self.page.locator('[data-key="down"]').click()
        self.page.wait_for_function("nativeInput().pending === 0")
        self.assertIn("→ Model Beta", self.page.locator("#native-menu-output").inner_text())
        self.page.locator("#dock-input").press("Enter")
        self.page.wait_for_function("!nativeInput()")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "")
        self.assertFalse(self.httpd.model_panes)

    def test_model_buttons_select_native_row_without_composer_focus(self):
        self.open()
        self.page.locator("#dock-input").fill("/model")
        self.page.locator("#dock-input").press("Enter")
        self.page.wait_for_function("nativeInput()?.phase === 'model' && nativeInput().pending === 0")
        self.page.get_by_role("option", name="Model Beta").focus()
        self.page.keyboard.press("ArrowDown")
        self.page.wait_for_function("nativeInput().pending === 0")
        self.assertEqual(self.page.get_by_role("option", name="→ Model Beta").get_attribute('aria-selected'), 'true')
        self.page.get_by_role("option", name="Model Alpha").click()
        self.page.wait_for_function("!nativeInput()")
        self.assertEqual(self.httpd.actions[-2][1], {"pane": "p2", "keys": ["up"]})
        self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "keys": ["enter"]})

    def test_open_model_picker_is_adopted_after_reload(self):
        self.httpd.model_panes.add('p2')
        self.open()
        self.page.wait_for_function("nativeInput()?.phase === 'model'")
        self.page.locator("#dock-input").blur()
        self.page.keyboard.press("ArrowDown")
        self.page.wait_for_function("nativeInput().pending === 0")
        self.assertTrue(self.page.get_by_role('option', name='→ Model Beta').is_visible())
        self.page.keyboard.press("Enter")
        self.page.wait_for_function("!nativeInput()")

    def test_model_picker_from_tab_completed_command(self):
        self.open()
        self.page.locator("#dock-input").fill("/")
        self.page.locator("#dock-input").press("ArrowDown")
        self.page.locator("#dock-input").press("Tab")
        self.page.wait_for_function("document.querySelector('#dock-input').value === '/model '")
        self.page.locator("#dock-input").press("Enter")
        self.page.wait_for_function("nativeInput()?.phase === 'model' && nativeInput().pending === 0")
        self.assertIn("Model Alpha", self.page.locator("#native-menu-output").inner_text())

    def test_native_transition_locks_edits_and_duplicate_enter(self):
        self.open()
        self.page.locator("#dock-input").fill("/model")
        self.page.wait_for_function("nativeInput()?.pending === 0")
        self.httpd.delay_action = True
        self.page.locator("#dock-input").press("Enter")
        self.assertTrue(self.httpd.action_started.wait(2))
        self.assertTrue(self.page.locator("#dock-input").evaluate("el => el.readOnly"))
        self.page.locator("#dock-input").press("Enter")
        self.httpd.delay_action = False
        self.httpd.action_release.set()
        self.page.wait_for_function("nativeInput()?.phase === 'model' && nativeInput().pending === 0")
        self.assertEqual([a[1].get("keys") for a in self.httpd.actions if a[0].endswith("/keys")], [["enter"]])

    def test_native_editing_and_csrf_refresh_preserve_order(self):
        self.open()
        self.httpd.csrf_token = "fictional-refreshed-token"
        self.page.locator("#dock-input").fill("/hello")
        self.page.wait_for_function("nativeInput()?.pending === 0")
        self.page.locator("#dock-input").fill("/help")
        self.page.wait_for_function("nativeInput().pending === 0")
        self.assertEqual(self.httpd.native_text['p2'], '/help')
        self.assertEqual(self.httpd.page_reads, 2)
        self.assertEqual(self.httpd.actions[1][1], {"pane": "p2", "keys": ["backspace", "backspace"]})
        self.page.locator("#dock-input").press("Escape")
        self.page.wait_for_function("!nativeInput()")
        self.assertEqual(self.httpd.native_text['p2'], '')

    def test_native_fast_typing_is_ordered_before_keys(self):
        self.open()
        self.httpd.delay_action = True
        self.page.locator("#dock-input").fill("/")
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.locator("#dock-input").fill("/model")
        self.page.locator("#dock-input").press("Enter")
        self.httpd.delay_action = False
        self.httpd.action_release.set()
        self.page.wait_for_function("nativeInput()?.phase === 'model' && nativeInput().pending === 0")
        self.assertEqual([(a[0].split('/')[-1], a[1].get('text', a[1].get('keys'))) for a in self.httpd.actions], [("send", "/"), ("send", "model"), ("keys", ["enter"])])

    def test_native_pane_switch_keeps_menu_and_input_isolated(self):
        self.open()
        self.page.locator("#dock-input").fill("/")
        self.page.wait_for_function("nativeInput()?.pending === 0")
        self.choose("p1")
        self.assertFalse(self.page.locator("#native-menu").is_visible())
        self.page.locator("#dock-input").fill("ordinary draft")
        self.choose("p2")
        self.assertTrue(self.page.locator("#native-menu").is_visible())
        self.page.locator("#dock-input").press("Escape")
        self.page.wait_for_function("!nativeInput()")
        self.choose("p1")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "ordinary draft")
        self.assertTrue(all(a[1]['pane'] == 'p2' for a in self.httpd.actions))

    def test_native_failure_preserves_draft_and_never_retries(self):
        self.open()
        self.httpd.fail_action = True
        self.page.locator("#dock-input").fill("/")
        self.page.wait_for_function("nativeInput()?.failed")
        self.page.locator("#dock-input").press("Enter")
        self.page.wait_for_function("nativeInput().pending === 0")
        self.assertEqual(len(self.httpd.actions), 1)
        self.assertEqual(self.page.locator("#dock-input").input_value(), "/")
        self.assertIn("uncertain", self.page.locator("#native-menu-title").inner_text())

    def test_deleting_slash_restores_local_composer(self):
        self.open()
        self.page.locator("#dock-input").fill("/")
        self.page.wait_for_function("nativeInput()?.pending === 0")
        self.page.locator("#dock-input").fill("")
        self.page.wait_for_function("!nativeInput()")
        self.assertFalse(self.page.locator("#native-menu").is_visible())
        self.page.locator("#dock-input").fill("ordinary message")
        with self.page.expect_response("**/api/action/prompt"):
            self.page.locator("#dock-input").press("Enter")
        self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "text": "ordinary message"})

    def test_fast_slash_deletion_keeps_new_message_local(self):
        self.open()
        self.page.locator('#dock-input').fill('/')
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.httpd.delay_action = True
        self.page.locator('#dock-input').fill('')
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.locator('#dock-input').fill('ordinary after cancellation')
        self.page.locator('#dock-input').press('Enter')
        self.httpd.delay_action = False
        self.httpd.action_release.set()
        self.page.wait_for_function("document.querySelector('#dock-input').value === '' && !nativeInput()")
        self.assertEqual(self.httpd.actions[-1][0], '/api/action/prompt')
        self.assertEqual(self.httpd.actions[-1][1]['text'], 'ordinary after cancellation')
        self.assertEqual([a[1]['text'] for a in self.httpd.actions if a[0].endswith('/send')], ['/'])

    def test_native_down_cannot_unlock_queued_tab(self):
        self.open()
        self.page.locator('#dock-input').fill('/')
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.httpd.delay_action = True
        self.page.locator('#dock-input').press('ArrowDown')
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.locator('#dock-input').press('Tab')
        self.httpd.delay_action = False
        self.httpd.action_release.set()
        self.page.wait_for_function("nativeInput()?.sent === '/model '")
        self.assertEqual([a[1].get('keys') for a in self.httpd.actions if a[0].endswith('/keys')], [['down'],['tab']])
        self.page.wait_for_function('nativeInput().pending === 0')
        self.assertFalse(self.page.locator('#dock-input').evaluate('el => el.readOnly'))

    def test_failed_native_cancellation_exposes_reset(self):
        self.open()
        self.page.locator('#dock-input').fill('/')
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.httpd.fail_action = True
        self.page.locator('#dock-input').fill('')
        self.page.wait_for_function('nativeInput()?.failed')
        self.assertTrue(self.page.locator('#native-menu-reset').is_visible())
        self.assertTrue(self.page.locator('#dock-input').evaluate('el => el.readOnly'))

    def test_replacing_slash_draft_keeps_message_local(self):
        self.open()
        self.page.locator('#dock-input').fill('/help')
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.page.locator('#dock-input').fill('ordinary replacement')
        self.page.locator('#dock-input').press('Enter')
        self.page.wait_for_function("!nativeInput() && document.querySelector('#dock-input').value === ''")
        self.assertEqual(self.httpd.actions[-1][0], '/api/action/prompt')
        self.assertEqual([a[1]['text'] for a in self.httpd.actions if a[0].endswith('/send')], ['/help'])

    def test_alt_enter_button_waits_for_native_cancellation(self):
        self.open()
        self.page.locator('#dock-input').fill('/')
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.httpd.delay_action = True
        self.page.locator('#dock-input').fill('normal followup')
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.locator('[data-key="alt+enter"]').click()
        self.httpd.delay_action = False;self.httpd.action_release.set()
        self.page.wait_for_function("!nativeInput() && document.querySelector('#dock-input').value === ''")
        self.assertEqual(self.httpd.actions[-1][0], '/api/action/followup')
        self.assertEqual(self.httpd.actions[-1][1]['text'], 'normal followup')

    def test_native_menus_and_saved_drafts_are_isolated_by_host(self):
        self.open()
        self.page.locator('#dock-input').fill('/help local')
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.page.evaluate('switchMachine("worker-a")')
        self.page.wait_for_function('S.machine === "worker-a" && S.pane === "p2"')
        self.assertTrue(self.page.evaluate('!nativeInput()'))
        self.assertEqual(self.page.locator('#dock-input').input_value(), '')
        self.page.locator('#dock-input').fill('/help remote')
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.assertEqual(self.httpd.actions[-1][1]['machine'], 'worker-a')
        self.assertEqual(self.page.evaluate('drafts.get("local/p2")?.text'), '/help local')
        self.page.evaluate('switchMachine("local")')
        self.page.wait_for_function('S.machine === "local" && S.pane === "p2"')
        self.assertEqual(self.page.locator('#dock-input').input_value(), '/help local')
        self.assertEqual(self.page.evaluate('nativeInput().sent'), '/help local')

    def check_removed_pane_during_native_read(self, origin, destination, key="Enter"):
        self.open("#pane=p2" if origin == "local" else f"#machine={origin}&pane=p2")
        self.page.locator("#dock-input").fill("/help")
        self.page.wait_for_function("nativeInput()?.pending === 0")
        self.page.evaluate("window.pendingNativeTest = nativeInput()")
        held_reads = []

        def hold_visible_read(route):
            query = parse_qs(urlparse(route.request.url).query)
            if query.get("source") == ["visible"] and query.get("machine", ["local"]) == [origin] and not held_reads:
                held_reads.append(route)
            else:
                route.continue_()

        self.page.route(self.base + "/api/read*", hold_visible_read)
        with self.page.expect_request(lambda r: parse_qs(urlparse(r.url).query).get("source") == ["visible"]):
            self.page.locator("#dock-input").press(key)
        self.page.wait_for_function("window.pendingNativeTest.pending > 0 && activeActions === 0")
        self.assertEqual(len(held_reads), 1)

        # Only the originating host loses p2; the other host retains the same ID.
        pruned_tree = copy.deepcopy(TREE)
        pruned_tree["panes"] = [p for p in pruned_tree["panes"] if p["pane_id"] != "p2"]
        self.page.route(self.base + "/api/tree" + ("" if origin == "local" else f"?machine={origin}"),
                        lambda route: route.fulfill(json=pruned_tree))
        self.page.wait_for_function("!S.busy")
        self.page.evaluate("refreshTree()")
        self.assertTrue(self.page.evaluate("!nativeInputs.has('p2')"))
        self.assertGreater(self.page.evaluate("window.pendingNativeTest.pending"), 0)
        self.assertTrue(self.page.locator(f'[data-machine="{destination}"]').is_disabled())
        switched_while_pending = self.page.evaluate("host => { switchMachine(host); return S.machine; }", destination)

        held_reads[0].fulfill(json={"text": "/help\n→ help    Show help\n  model   Select model"})
        self.page.evaluate("window.pendingNativeTest.queue")
        self.assertEqual(self.page.evaluate("window.pendingNativeTest.pending"), 0)
        self.assertFalse(self.page.evaluate("pendingNativeInputs.has(window.pendingNativeTest)"))
        self.assertFalse(self.page.locator(f'[data-machine="{destination}"]').is_disabled())
        self.page.evaluate("switchMachine", destination)
        self.page.wait_for_function("host => S.machine === host && S.pane === 'p2'", arg=destination)

        self.assertFalse(any(a[1].get("machine", "local") == destination for a in self.httpd.actions),
                         "A native operation from a removed pane must never mutate the other host")
        self.assertEqual(switched_while_pending, origin, "Pruning a pane must not drop its in-flight switch guard")
        self.assertEqual(len(self.httpd.actions), 1, "A removed native input must not send its pending key")

    def test_removed_local_pane_cannot_redirect_pending_native_enter(self):
        self.check_removed_pane_during_native_read("local", "worker-a")

    def test_removed_remote_pane_cannot_redirect_pending_native_enter(self):
        self.check_removed_pane_during_native_read("worker-a", "local")

    def test_removed_pane_cannot_send_pending_native_completion(self):
        self.check_removed_pane_during_native_read("local", "worker-a", key="Tab")

    def test_native_keypad_edits_stay_in_owned_browser_draft(self):
        self.open()
        self.page.locator('#dock-input').fill('/')
        self.page.locator('#dock-input').press('Tab')
        self.page.wait_for_function("document.querySelector('#dock-input').value === '/help '")
        self.page.locator('[data-key="1"]').click()
        self.page.locator('[data-key="left"]').click()
        self.page.locator('[data-key="y"]').click()
        self.page.wait_for_function('nativeInput().pending === 0')
        self.assertEqual(self.httpd.native_text['p2'], self.page.locator('#dock-input').input_value())
        self.page.locator('#dock-input').fill('/help changed')
        self.page.wait_for_function('nativeInput().pending === 0')
        self.assertEqual(self.httpd.native_text['p2'], '/help changed')
        self.page.locator('#dock-input').press('Escape')
        self.page.wait_for_function('!nativeInput()')
        self.assertEqual(self.httpd.native_text['p2'], '')
        self.assertFalse(any(a[1].get('keys') == ['left'] for a in self.httpd.actions))

    def test_keypad_enter_waits_for_cancel_and_rejects_failed_cancel(self):
        self.open()
        self.page.locator('#dock-input').fill('/')
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.httpd.delay_action = True
        self.page.locator('#dock-input').fill('ordinary')
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.locator('[data-key="enter"]').click()
        self.httpd.delay_action = False;self.httpd.action_release.set()
        self.page.wait_for_function('!nativeInput()')
        self.page.wait_for_function("!nativeInputs.has('p2')")
        self.assertEqual([a[1].get('keys') for a in self.httpd.actions if a[0].endswith('/keys')][:2], [['backspace'],['enter']])
        with self.page.expect_response('**/api/action/send'):
            self.page.locator('#dock-input').fill('/')
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.httpd.fail_action = True
        self.page.locator('#dock-input').fill('')
        self.page.wait_for_function('nativeInput()?.failed')
        count = len(self.httpd.actions)
        self.page.locator('[data-key="enter"]').click()
        self.page.locator('#toasts .err').last.wait_for()
        self.assertEqual(len(self.httpd.actions), count)

    def test_tab_completed_replacement_cancels_and_disables_interrupts(self):
        self.open()
        self.page.locator('#dock-input').fill('/')
        self.page.locator('#dock-input').press('Tab')
        self.page.wait_for_function("document.querySelector('#dock-input').value === '/help '")
        self.assertTrue(self.page.locator('[data-interrupt-twice]').is_disabled())
        self.assertTrue(self.page.locator('#mobile-interrupt').is_disabled())
        self.page.evaluate('render()')
        self.assertTrue(self.page.locator('#mobile-interrupt').is_disabled())
        self.page.locator('#dock-input').fill('ordinary after tab')
        self.page.locator('#dock-input').press('Enter')
        self.page.wait_for_function("!nativeInput() && document.querySelector('#dock-input').value === ''")
        self.assertEqual(self.httpd.actions[-1][0], '/api/action/prompt')
        self.assertEqual([a[1]['text'] for a in self.httpd.actions if a[0].endswith('/send')], ['/',' '])

    def test_slash_initialization_waits_for_ordinary_submit(self):
        self.open()
        self.httpd.delay_action = True
        self.page.locator('#dock-input').fill('ordinary')
        self.page.locator('#dock-input').press('Enter')
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.locator('#dock-input').fill('/')
        self.assertEqual(self.httpd.actions, [])
        self.httpd.delay_action = False;self.httpd.action_release.set()
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.assertEqual([a[0].split('/')[-1] for a in self.httpd.actions], ['prompt','send'])
        self.assertEqual(self.httpd.native_text['p2'], '/')

    def test_keypad_enter_joins_reacquired_slash_owner(self):
        self.open()
        self.page.locator('#dock-input').fill('/')
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.httpd.delay_action = True
        self.page.locator('#dock-input').fill('')
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.locator('#dock-input').fill('/model')
        self.page.locator('[data-key="enter"]').click()
        self.httpd.delay_action = False;self.httpd.action_release.set()
        self.page.wait_for_function("nativeInput()?.phase === 'model' && nativeInput().pending === 0")
        self.assertEqual([(a[0].split('/')[-1],a[1].get('text',a[1].get('keys'))) for a in self.httpd.actions], [('send','/'),('keys',['backspace']),('send','/model'),('keys',['enter'])])

    def test_composer_enter_reacquires_slash_after_cancel_in_type_mode(self):
        self.open()
        self.page.locator('#dock-mode').select_option('type')
        self.page.locator('#dock-input').fill('/')
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.httpd.delay_action = True
        self.page.locator('#dock-input').fill('')
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.locator('#dock-input').fill('/')
        self.page.locator('#dock-input').press('Enter')
        self.httpd.delay_action = False;self.httpd.action_release.set()
        self.page.wait_for_function("!nativeInput() && document.querySelector('#dock-input').value === ''")
        self.assertEqual(self.httpd.actions[-1][1], {'pane':'p2','keys':['enter']})

    def test_retyped_slash_survives_cancel_and_pane_switch(self):
        self.open()
        self.page.locator('#dock-input').fill('/')
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.httpd.delay_action = True
        self.page.locator('#dock-input').fill('')
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.locator('#dock-input').fill('/model')
        self.choose('p1')
        self.httpd.delay_action = False;self.httpd.action_release.set()
        self.page.wait_for_function("nativeInputs.get('p2')?.pending === 0")
        self.choose('p2')
        self.assertEqual(self.httpd.native_text['p2'], '/model')
        self.assertTrue(self.page.locator('#native-menu').is_visible())

    def test_native_keypad_select_all_replacement_cancels_ownership(self):
        self.open()
        self.page.locator('#dock-input').fill('/')
        self.page.locator('#dock-input').press('Tab')
        self.page.wait_for_function("document.querySelector('#dock-input').value === '/help '")
        self.page.locator('#dock-input').evaluate('el => el.setSelectionRange(0,el.value.length)')
        self.page.locator('[data-key="y"]').click()
        self.page.wait_for_function('!nativeInput()')
        self.assertEqual(self.page.locator('#dock-input').input_value(),'y')
        self.assertEqual(self.httpd.native_text['p2'],'')

    def test_native_slash_waits_for_pending_raw_key(self):
        self.open()
        self.httpd.delay_action = True
        self.page.locator('[data-key="enter"]').click()
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.locator('#dock-input').fill('/model')
        self.httpd.delay_action = False;self.httpd.action_release.set()
        self.page.wait_for_function('nativeInput()?.pending === 0')
        self.assertEqual([(a[0].split('/')[-1],a[1].get('text',a[1].get('keys'))) for a in self.httpd.actions], [('keys',['enter']),('send','/model')])

    def test_native_parameter_alt_enter_keyboard_and_button(self):
        self.open()
        for keyboard in (True, False):
            self.page.locator('#dock-input').fill('/')
            self.page.locator('#dock-input').press('Tab')
            self.page.wait_for_function("document.querySelector('#dock-input').value === '/help '")
            self.page.locator('#dock-input').press_sequentially('topic')
            if keyboard:
                self.page.locator('#dock-input').press('Alt+Enter')
            else:
                self.page.locator('[data-key="alt+enter"]').click()
            self.page.wait_for_function('!nativeInput()')
            self.assertEqual(self.httpd.actions[-1][1], {'pane':'p2','keys':['alt+enter']})
            self.assertEqual(self.page.locator('#dock-input').input_value(), '')
            self.assertEqual(self.httpd.native_text['p2'], '')

    def test_shell_slash_remains_local(self):
        self.open("#pane=p3")
        self.page.locator("#dock-input").fill("/usr/bin/example")
        self.assertFalse(self.page.locator("#native-menu").is_visible())
        self.assertEqual(self.httpd.actions, [])
        with self.page.expect_response("**/api/action/send"):
            self.page.locator("#dock-input").press("Enter")
        self.assertEqual(self.httpd.actions[-1][1], {"pane": "p3", "text": "/usr/bin/example", "enter": True})

    def test_navigation_renames_all_entity_types_without_selection_or_draft_changes(self):
        self.open()
        self.page.locator('#dock-input').fill('keep draft')
        self.page.get_by_role('button', name='Open spaces, tabs and panes').click()
        for kind, target, label in (('space','w1','Renamed Space'),('tab','t1','Renamed Tab'),('pane','p2','Renamed Pane')):
            self.page.once('dialog', lambda dialog, label=label: dialog.accept(label))
            with self.page.expect_response('**/api/action/rename'):
                self.page.locator(f'[data-rename="{kind}"][data-rename-id="{target}"]').click()
            self.page.wait_for_function(f"document.querySelector('#tree').textContent.includes('{label}')")
            self.assertEqual(self.httpd.actions[-1][1], {'kind':kind,'id':target,'label':label})
        self.page.locator('#mobile-nav-close').click()
        self.assertEqual(self.page.locator('#mobile-title').inner_text(),'Renamed Pane')
        self.assertEqual(self.page.locator('#mobile-path').inner_text(),'Renamed Space / Renamed Tab')
        self.assertEqual(self.page.locator('#dock-input').input_value(),'keep draft')
        self.assertEqual(self.page.evaluate('S.pane'),'p2')

    def test_rename_pencil_focus_survives_refresh_and_keyboard_activation(self):
        self.open()
        self.page.get_by_role('button',name='Open spaces, tabs and panes').click()
        control=self.page.locator('[data-rename="space"][data-rename-id="w1"]')
        control.focus()
        self.page.evaluate('renderSide()')
        self.assertTrue(control.evaluate('el => el === document.activeElement'))
        self.page.once('dialog',lambda dialog:dialog.accept('Keyboard Space'))
        with self.page.expect_response('**/api/action/rename'):
            self.page.keyboard.press('Enter')
        self.page.wait_for_function("document.querySelector('#tree').textContent.includes('Keyboard Space')")
        self.assertTrue(control.evaluate('el => el === document.activeElement'))

    def test_rename_cancel_failure_and_untrusted_label(self):
        self.open()
        self.page.locator('#mobile-more').click()
        self.page.once('dialog', lambda dialog: dialog.dismiss())
        self.page.locator('#mobile-rename').click()
        self.assertEqual(self.httpd.actions,[])
        self.httpd.fail_action = True
        self.page.once('dialog',lambda dialog: dialog.accept('not applied'))
        with self.page.expect_response('**/api/action/rename'):
            self.page.locator('#mobile-rename').click()
        self.assertEqual(self.page.locator('#mobile-title').inner_text(),'api-agent')
        self.httpd.fail_action = False
        label='<img src=x onerror=window.fixtureInjected=true>'
        self.page.once('dialog',lambda dialog: dialog.accept(label))
        with self.page.expect_response('**/api/action/rename'):
            self.page.locator('#mobile-rename').click()
        self.page.wait_for_function("document.querySelector('#mobile-title').textContent.startsWith('<img')")
        self.assertFalse(self.page.evaluate('!!window.fixtureInjected'))

    def test_desktop_rename_control_preserves_selected_target(self):
        self.context.close()
        self.context=self.browser.new_context(viewport={'width':1280,'height':900})
        self.page=self.context.new_page();self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.open('#pane=p2')
        self.page.once('dialog',lambda dialog:dialog.accept('Desktop Pane'))
        with self.page.expect_response('**/api/action/rename'):
            self.page.locator('#dock-rename').click()
        self.page.wait_for_function("document.querySelector('#dock-title').textContent === 'Desktop Pane'")
        self.assertEqual(self.httpd.actions[-1][1],{'kind':'pane','id':'p2','label':'Desktop Pane'})

    def test_phone_starts_with_focused_pane_without_keyboard_or_mutation(self):
        self.open()
        self.page.locator("#dock-term").get_by_text("Terminal output for p2", exact=False).wait_for()
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "api-agent")
        self.assertFalse(self.page.locator("aside").is_visible())
        self.assertFalse(self.page.locator("#tabbar").is_visible())
        self.assertFalse(self.page.locator(".canvas-wrap").is_visible())
        self.assertFalse(self.page.locator("#dock-input").evaluate("el => el === document.activeElement"))
        self.assertEqual(set(self.httpd.reads), {"p2"})
        self.assertEqual(self.httpd.actions, [])

    def test_machine_switch_isolates_colliding_pane_drafts_and_routes_actions(self):
        def select_host(host):
            node = self.page.locator(f'[data-machine="{host}"]')
            if not node.is_visible():
                self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
            node.click()
            self.page.wait_for_function("host => S.machine === host && S.tree !== null", arg=host)
            if node.is_visible():
                self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        self.open()
        self.page.locator("#dock-input").fill("local draft")
        select_host("worker-a")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "")
        self.page.locator("#dock-term").get_by_text("Remote Terminal output for p2", exact=False).wait_for()
        self.page.locator("#dock-input").fill("remote draft")
        select_host("local")
        self.assertEqual(self.page.evaluate("drafts.get('worker-a/p2')?.text"), "remote draft")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "local draft")
        select_host("worker-a")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "remote draft",
                         self.page.evaluate("({pane:S.pane, drafts:Array.from(drafts.entries())})"))
        self.page.locator("#dock-form").evaluate("el => el.requestSubmit()")
        self.page.wait_for_function("!submitBusy")
        self.assertEqual(self.httpd.actions[-1][1]["machine"], "worker-a")
        self.assertIn("machine=worker-a", self.page.url)

    def test_remote_deep_link_selects_the_host_before_reading(self):
        self.open("#machine=worker-a&pane=p4")
        self.page.locator("#dock-term").get_by_text("Remote Terminal output for p4", exact=False).wait_for()
        self.assertEqual(self.page.locator('[data-machine="worker-a"]').get_attribute("aria-expanded"), "true")
        self.assertEqual(self.httpd.actions, [])

    def test_delayed_local_snapshot_cannot_replace_remote_host(self):
        self.httpd.delay_tree = True
        self.page.goto(self.base + "/")
        self.assertTrue(self.httpd.tree_started.wait(2))
        self.httpd.delay_tree = False
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        self.page.locator('[data-machine="worker-a"]').click()
        self.page.wait_for_function("S.tree?.host === 'Worker A'")
        with self.page.expect_response(lambda r: r.url.endswith("/api/tree")):
            self.httpd.tree_release.set()
        self.page.evaluate("() => new Promise(requestAnimationFrame)")
        self.assertEqual(self.page.evaluate("S.tree.host"), "Worker A")
        self.assertEqual(self.page.evaluate("S.machine"), "worker-a")

    def test_delayed_local_output_cannot_overwrite_same_remote_pane_id(self):
        self.httpd.delay_pane = "p2"
        self.open()
        self.assertTrue(self.httpd.read_started.wait(2))
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        self.page.locator('[data-machine="worker-a"]').click()
        self.page.locator("#dock-term").get_by_text("Remote Terminal output for p2", exact=False).wait_for()
        with self.page.expect_response(lambda r: "/api/read?" in r.url and "machine=" not in r.url):
            self.httpd.read_release.set()
        self.page.evaluate("() => new Promise(requestAnimationFrame)")
        self.assertTrue(self.page.locator("#dock-term").inner_text().startswith("Remote Terminal"))

    def test_host_switch_is_disabled_during_terminal_delivery(self):
        self.open()
        self.httpd.delay_action = True
        self.page.locator("#dock-input").fill("fictional command")
        self.page.locator("#dock-form").evaluate("el => el.requestSubmit()")
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        self.assertTrue(self.page.locator('[data-machine="worker-a"]').is_disabled())
        self.httpd.action_release.set()
        self.page.wait_for_function("activeActions === 0")
        self.assertFalse(self.page.locator('[data-machine="worker-a"]').is_disabled())
        self.assertEqual(self.page.evaluate("S.machine"), "local")

    def test_desktop_hosts_are_tree_nodes_with_only_selected_host_expanded(self):
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.open()
        local = self.page.locator('#tree > [data-machine="local"]')
        remote = self.page.locator('#tree > [data-machine="worker-a"]')
        remote.wait_for()
        self.assertEqual(self.page.locator("#machine-select").count(), 0)
        self.assertTrue(local.is_visible())
        self.assertTrue(remote.is_visible())
        self.assertEqual(local.get_attribute("aria-expanded"), "true")
        self.assertEqual(remote.get_attribute("aria-expanded"), "false")
        self.assertEqual(self.page.locator("#tree > [data-machine-children]").count(), 1)
        self.assertGreater(self.page.locator('[data-machine-children="local"] [data-ws]').count(), 0)
        remote.click()
        self.page.wait_for_function("S.machine === 'worker-a' && S.tree !== null")
        self.assertEqual(local.get_attribute("aria-expanded"), "false")
        self.assertEqual(remote.get_attribute("aria-expanded"), "true")
        self.assertEqual(self.page.locator('[data-machine-children="local"]').count(), 0)
        self.assertEqual(self.page.locator("#tree > [data-machine-children]").count(), 1)
        self.assertGreater(self.page.locator('[data-machine-children="worker-a"] [data-ws]').count(), 0)
        self.assertEqual(self.httpd.actions, [])

    def test_host_nodes_support_keyboard_and_preserve_focus_on_catalog_refresh(self):
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.open()
        remote = self.page.locator('[data-machine="worker-a"]')
        remote.focus()
        self.page.evaluate("refreshMachines()")
        self.assertTrue(remote.evaluate("el => el === document.activeElement"))
        remote.press("Enter")
        self.page.wait_for_function("S.machine === 'worker-a' && S.tree !== null")
        self.assertTrue(remote.evaluate("el => el === document.activeElement"))
        self.assertEqual(self.httpd.actions, [])

    def test_phone_host_nodes_are_visible_touch_targets_and_support_keyboard(self):
        self.open()
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        remote = self.page.locator('[data-machine="worker-a"]')
        remote.wait_for(state="visible")
        self.assertGreaterEqual(remote.bounding_box()["height"], 44)
        self.assertTrue(self.page.locator('[data-machine="local"]').is_visible())
        remote.focus()
        remote.press("Enter")
        self.page.wait_for_function("S.machine === 'worker-a' && S.tree !== null")
        self.assertTrue(remote.evaluate("el => el === document.activeElement"))
        self.assertEqual(self.httpd.actions, [])

    def test_hosts_remain_available_during_loading_and_offline_recovery(self):
        self.httpd.delay_tree = True
        self.page.goto(self.base + "/")
        self.assertTrue(self.httpd.tree_started.wait(2))
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        remote = self.page.locator('[data-machine="worker-a"]')
        remote.wait_for(state="visible")
        self.assertTrue(self.page.locator('[data-machine="local"]').is_visible())
        self.httpd.delay_tree = False
        self.httpd.fail_tree = True
        self.httpd.tree_release.set()
        self.page.wait_for_function("S.err === 'Fixture offline'")
        self.assertTrue(self.page.locator('[data-machine-children="local"]').get_by_text(
            "Waiting for Herdr. Select another host or retry the connection.", exact=True).is_visible())
        self.assertTrue(remote.is_visible())
        self.assertFalse(remote.is_disabled())
        self.httpd.fail_tree = False
        remote.click()
        self.page.wait_for_function("S.machine === 'worker-a' && S.tree !== null")
        self.assertEqual(self.httpd.actions, [])

    def test_disabled_host_node_cannot_switch_or_deliver_commands(self):
        self.open()
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        disabled = self.page.locator('[data-machine="disabled-worker"]')
        disabled.wait_for(state="visible")
        self.assertTrue(disabled.is_disabled())
        disabled.evaluate("el => el.click()")
        self.assertEqual(self.page.evaluate("S.machine"), "local")
        self.assertEqual(self.httpd.actions, [])

    def test_deep_link_takes_priority(self):
        self.open("#pane=p4")
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "review-agent")
        self.assertEqual(self.page.locator("#mobile-path").inner_text(), "Docs / Writing")
        self.assertEqual(self.page.locator("#dock-mode").input_value(), "send")

    def test_navigation_selection_does_not_change_tui_focus(self):
        self.open()
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        self.page.get_by_role("textbox", name="Find a space, tab or pane").fill("shell")
        self.page.locator('[data-pane="p3"]').click()
        self.assertFalse(self.page.locator("aside").is_visible())
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "shell")
        self.assertEqual(self.page.locator("#dock-mode").input_value(), "send")
        self.assertEqual(self.httpd.actions, [])

    def test_agents_show_working_before_blocked_and_search(self):
        self.open()
        self.page.get_by_role("button", name="Show all agents").click()
        self.assertEqual(self.page.locator(".agent-card").count(), 3)
        self.assertEqual(self.page.locator(".agent-card").evaluate_all("els => els.map(el => el.dataset.agentPane)"), ["p2", "p1", "p4"])
        self.assertTrue(self.page.locator("#agents-attention").is_visible())
        self.page.get_by_role("textbox", name="Find an agent").fill("review")
        self.assertEqual(self.page.locator(".agent-card").count(), 1)
        self.page.locator(".agent-card").click()
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "review-agent")
        self.assertFalse(self.page.locator("#agents-view").is_visible())
        self.assertFalse(self.page.locator("#dock-input").evaluate("el => el === document.activeElement"))

    def add_agent_states(self):
        for pane_id, name, status in (("p5", "z-done", "done"), ("p6", "a-idle", "idle"), ("p7", "a-unknown", "unexpected")):
            self.httpd.tree["panes"].insert(0, {"pane_id": pane_id, "tab_id": "t2", "workspace_id": "w2", "label": name, "agent": "pi", "agent_status": status})

    def agent_ids(self):
        return self.page.locator(".agent-card").evaluate_all("els => els.map(el => el.dataset.agentPane)")

    def test_agents_sort_all_states_and_unknown_last(self):
        self.add_agent_states()
        self.open()
        self.page.get_by_role("button", name="Show all agents").click()
        self.assertEqual(self.agent_ids(), ["p2", "p1", "p4", "p5", "p6", "p7"])
        self.assertEqual(self.page.locator(".agents-section").all_text_contents(), ["WORKING", "BLOCKED", "DONE", "IDLE", "UNKNOWN"])

    def test_state_count_filters_and_same_count_restores_sorted_list(self):
        self.add_agent_states()
        self.open()
        self.page.get_by_role("button", name="Show all agents").click()
        for state, ids in (("working", ["p2", "p1"]), ("blocked", ["p4"]), ("done", ["p5"]), ("idle", ["p6"]), ("unknown", ["p7"])):
            button = self.page.get_by_role("button", name=f"Filter {state} agents")
            button.locator("strong").click()
            self.assertEqual(self.agent_ids(), ids)
            self.assertEqual(button.get_attribute("aria-pressed"), "true")
            button.locator("strong").click()
            self.assertEqual(self.agent_ids(), ["p2", "p1", "p4", "p5", "p6", "p7"])
            self.assertEqual(button.get_attribute("aria-pressed"), "false")
        self.assertEqual(self.httpd.actions, [])

    def test_state_filter_switches_combines_with_search_and_keeps_totals(self):
        self.open()
        self.page.get_by_role("button", name="Show all agents").click()
        self.page.get_by_role("button", name="Filter working agents").click()
        self.page.get_by_role("textbox", name="Find an agent").fill("ui-agent")
        self.assertEqual(self.agent_ids(), ["p1"])
        self.assertEqual(self.page.locator('[data-agent-state="working"] strong').inner_text(), "2")
        self.page.get_by_role("button", name="Filter blocked agents").click()
        self.assertEqual(self.agent_ids(), [])
        self.page.get_by_role("textbox", name="Find an agent").fill("")
        self.assertEqual(self.agent_ids(), ["p4"])
        self.assertEqual(self.page.locator('[data-agent-state="working"]').get_attribute("aria-pressed"), "false")
        self.page.get_by_role("button", name="Filter done agents").click()
        self.assertEqual(self.agent_ids(), [])
        self.assertIn("No matching agents", self.page.locator("#agents-list").inner_text())
        self.page.get_by_role("button", name="Filter done agents").click()
        self.assertEqual(self.agent_ids(), ["p2", "p1", "p4"])

    def test_state_filter_and_focused_count_survive_refresh_and_zero_count(self):
        self.open()
        self.page.get_by_role("button", name="Show all agents").click()
        self.page.get_by_role("button", name="Filter blocked agents").click()
        self.httpd.tree["panes"][3]["agent_status"] = "done"
        self.page.evaluate("refreshTree()")
        self.assertEqual(self.agent_ids(), [])
        button = self.page.get_by_role("button", name="Filter blocked agents")
        self.assertEqual(button.locator("strong").inner_text(), "0")
        self.assertEqual(button.get_attribute("aria-pressed"), "true")
        self.assertTrue(button.evaluate("el => el === document.activeElement"))
        self.page.keyboard.press("Enter")
        self.assertEqual(self.agent_ids(), ["p2", "p1", "p4"])

    def test_unknown_filter_remains_clearable_when_last_unknown_changes_state(self):
        self.add_agent_states()
        self.open()
        self.page.get_by_role("button", name="Show all agents").click()
        self.page.get_by_role("button", name="Filter unknown agents").click()
        next(p for p in self.httpd.tree["panes"] if p["pane_id"] == "p7")["agent_status"] = "idle"
        self.page.evaluate("refreshTree()")
        button = self.page.get_by_role("button", name="Filter unknown agents")
        self.assertEqual(button.locator("strong").inner_text(), "0")
        button.click()
        self.assertEqual(len(self.agent_ids()), 6)

    def test_state_filters_remain_available_in_short_keyboard_viewports(self):
        self.open()
        self.page.get_by_role("button", name="Show all agents").click()
        for height in (280, 220):
            self.page.evaluate("""height => {
              Object.defineProperty(window.visualViewport, 'height', {configurable:true, value:height});
              window.visualViewport.dispatchEvent(new Event('resize'));
            }""", height)
            button = self.page.get_by_role("button", name="Filter working agents")
            button.scroll_into_view_if_needed()
            button.click()
            self.assertEqual(self.agent_ids(), ["p2", "p1"])
            button.click()
            self.assertEqual(self.agent_ids(), ["p2", "p1", "p4"])
            self.page.locator('[data-agent-pane="p4"]').scroll_into_view_if_needed()
            box = self.page.locator('[data-agent-pane="p4"]').bounding_box()
            self.assertLessEqual(box["y"] + box["height"], height)

    def test_dismissal_preserves_pane_and_escape_never_closes_terminal(self):
        self.open()
        self.page.get_by_role("button", name="Show all agents").click()
        self.page.get_by_role("button", name="Return to focused pane").click()
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        self.page.keyboard.press("Escape")
        self.page.keyboard.press("Escape")
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "api-agent")
        self.assertTrue(self.page.locator("#dock").is_visible())

    def test_header_buttons_toggle_popups_and_keep_x_close(self):
        self.open()
        self.page.locator("#dock-input").fill("preserve popup draft")
        for trigger, popup, close in (("#mobile-menu", "#session-sidebar", "#mobile-nav-close"),
                                      ("#mobile-agents", "#agents-view", "#agents-close")):
            self.page.locator(trigger).tap()
            self.assertTrue(self.page.locator(popup).is_visible())
            self.assertEqual(self.page.locator(trigger).get_attribute("aria-expanded"), "true")
            self.page.locator(trigger).tap()
            self.assertFalse(self.page.locator(popup).is_visible())
            self.assertEqual(self.page.locator(trigger).get_attribute("aria-expanded"), "false")
            self.assertTrue(self.page.locator(trigger).evaluate("el => el === document.activeElement"))
            self.page.locator(trigger).tap()
            self.page.locator(close).tap()
            self.assertFalse(self.page.locator(popup).is_visible())
        self.assertEqual(self.page.locator("#dock-input").input_value(), "preserve popup draft")
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "api-agent")
        self.assertEqual(self.httpd.actions, [])

    def test_header_toggles_remain_onscreen_with_long_labels_and_many_agents(self):
        self.httpd.tree["panes"][1]["label"] = "long-agent-title-" * 12
        self.httpd.tree["workspaces"][0]["label"] = "long-workspace-" * 12
        for i in range(32):
            self.httpd.tree["panes"].append({"pane_id": f"extra-{i}", "tab_id": "t1", "workspace_id": "w1", "label": f"extra-agent-{i}", "agent": "pi", "agent_status": "idle"})
        self.open()
        for width, height in ((390, 844), (360, 240)):
            self.page.set_viewport_size({"width": width, "height": height})
            for trigger in ("#mobile-menu", "#mobile-agents"):
                self.page.locator(trigger).tap()
                self.page.locator(trigger).tap()
                box = self.page.locator(trigger).bounding_box()
                self.assertLessEqual(box["x"] + box["width"], width)
                self.assertGreaterEqual(box["x"], 0)
        self.assertEqual(self.httpd.actions, [])

    def test_header_switches_between_popups_and_keyboard_can_close(self):
        self.open()
        self.page.locator("#mobile-menu").tap()
        self.page.locator("#mobile-agents").tap()
        self.assertFalse(self.page.locator("#session-sidebar").is_visible())
        self.assertTrue(self.page.locator("#agents-view").is_visible())
        self.page.locator("#mobile-agents").focus()
        self.page.keyboard.press("Enter")
        self.assertFalse(self.page.locator("#agents-view").is_visible())
        self.page.locator("#mobile-agents").tap()
        self.page.locator("#mobile-menu").tap()
        self.assertTrue(self.page.locator("#session-sidebar").is_visible())
        self.assertFalse(self.page.locator("#agents-view").is_visible())
        self.page.locator("#mobile-menu").focus()
        self.page.keyboard.press("Space")
        self.assertFalse(self.page.locator("#session-sidebar").is_visible())
        self.assertEqual(self.httpd.actions, [])

    def test_drafts_do_not_cross_panes_and_survive_return(self):
        self.open()
        self.page.locator("#dock-input").fill("draft for api")
        self.choose("p1")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "")
        self.page.locator("#dock-input").fill("draft for ui")
        self.choose("p2")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "draft for api")

    def test_prompt_send_type_and_keys_use_selected_pane(self):
        self.open()
        for mode, action, extra in (("prompt", "prompt", {}), ("send", "send", {"enter": True}), ("type", "send", {"enter": False})):
            self.page.locator("#dock-mode").select_option(mode)
            self.page.locator("#dock-input").fill("fixture only")
            with self.page.expect_response("**/api/action/" + action):
                self.page.locator('#dock-form button[type="submit"]').click()
            self.assertEqual(self.httpd.actions[-1], ("/api/action/" + action, {"pane": "p2", "text": "fixture only", **extra}, "fictional-test-token"))
            self.assertEqual(self.page.locator("#dock-input").input_value(), "")
        with self.page.expect_response("**/api/action/keys"):
            self.page.locator('[data-key="ctrl+c"]').click()
        self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "keys": ["ctrl+c"]})

    def test_phone_close_requires_confirmation_and_targets_focused_pane(self):
        self.open()
        self.page.locator("#mobile-more").click()
        messages = []
        def cancel(dialog):
            messages.append(dialog.message)
            dialog.dismiss()
        self.page.once("dialog", cancel)
        self.page.get_by_role("button", name="Close pane", exact=True).click()
        self.assertIn('api-agent', messages[0])
        self.assertIn('Ctrl-C ×2', messages[0])
        self.assertEqual(self.httpd.actions, [])
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "api-agent")
        self.page.once("dialog", lambda dialog: dialog.accept())
        with self.page.expect_response("**/api/action/close"):
            self.page.get_by_role("button", name="Close pane", exact=True).click()
        self.page.wait_for_function("document.querySelector('#mobile-title').textContent === 'ui-agent'")
        self.assertEqual(self.httpd.actions[0], ("/api/action/close", {"pane": "p2"}, "fictional-test-token"))
        self.assertFalse(any(action[0] == "/api/action/keys" for action in self.httpd.actions))

    def test_close_failure_preserves_focused_pane_and_draft(self):
        self.open()
        self.httpd.fail_action = True
        self.page.locator("#dock-input").fill("keep this draft")
        self.page.locator("#mobile-more").click()
        self.page.once("dialog", lambda dialog: dialog.accept())
        with self.page.expect_response("**/api/action/close"):
            self.page.locator("#mobile-close").click()
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "api-agent")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "keep this draft")
        self.assertIn("Fixture action failed", self.page.locator("#toasts").inner_text())
        self.assertTrue(self.page.locator("#mobile-close").is_enabled())

    def test_closing_last_pane_leaves_empty_state(self):
        self.httpd.tree["panes"] = [p for p in self.httpd.tree["panes"] if p["pane_id"] == "p2"]
        self.open()
        self.page.locator("#mobile-more").click()
        self.page.once("dialog", lambda dialog: dialog.accept())
        with self.page.expect_response("**/api/action/close"):
            self.page.locator("#mobile-close").click()
        self.page.locator("#mobile-empty").wait_for()
        self.assertFalse(self.page.locator("#dock").is_visible())

    def test_delayed_close_does_not_close_new_selection(self):
        self.open()
        self.httpd.delay_action = True
        self.page.locator("#mobile-more").click()
        self.page.once("dialog", lambda dialog: dialog.accept())
        self.page.locator("#mobile-close").click()
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.evaluate("refreshTree()")
        self.assertFalse(self.page.locator("#mobile-close").is_enabled())
        self.choose("p1")
        with self.page.expect_response("**/api/action/close"):
            self.httpd.action_release.set()
        self.page.wait_for_function("!S.tree.panes.some(p => p.pane_id === 'p2')")
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "ui-agent")
        self.assertTrue(self.page.locator("#dock").is_visible())
        self.assertEqual(len(self.httpd.actions), 1)

    def test_double_interrupt_sends_two_keys_without_closing_pane(self):
        self.open()
        self.page.locator("#dock-input").fill("unsent draft")
        with self.page.expect_response("**/api/action/keys"):
            self.page.locator("[data-interrupt-twice]").click()
        self.assertEqual(self.httpd.actions[-1], ("/api/action/keys", {"pane": "p2", "keys": ["ctrl+c", "ctrl+c"]}, "fictional-test-token"))
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "api-agent")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "unsent draft")
        self.assertTrue(self.page.locator("#dock").is_visible())
        self.assertFalse(any(action[0] == "/api/action/close" for action in self.httpd.actions))

    def test_landscape_pane_actions_are_reachable(self):
        self.page.set_viewport_size({"width": 844, "height": 390})
        self.open()
        self.page.locator("#mobile-header-more").click()
        self.assertTrue(self.page.locator("#mobile-close").is_visible())
        with self.page.expect_response("**/api/action/keys"):
            self.page.locator("#mobile-interrupt").click()
        self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "keys": ["ctrl+c", "ctrl+c"]})
        self.assertTrue(self.page.locator("#dock").is_visible())

    def test_double_interrupt_guard_survives_refresh_and_selection_changes(self):
        self.open()
        self.httpd.delay_action = True
        self.page.locator("[data-interrupt-twice]").click()
        self.assertTrue(self.httpd.action_started.wait(2))
        self.page.evaluate("refreshTree()")
        self.page.locator("#mobile-more").click()
        self.assertFalse(self.page.locator("#mobile-interrupt").is_enabled())
        self.choose("p1")
        self.assertFalse(self.page.locator("[data-interrupt-twice]").is_enabled())
        with self.page.expect_response("**/api/action/keys"):
            self.httpd.action_release.set()
        self.assertEqual(self.httpd.actions[0][1]["pane"], "p2")
        self.assertEqual(len(self.httpd.actions), 1)
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "ui-agent")
        self.assertTrue(self.page.locator("[data-interrupt-twice]").is_enabled())

    def test_double_interrupt_error_is_reported_and_control_reenabled(self):
        self.open()
        self.httpd.fail_action = True
        with self.page.expect_response("**/api/action/keys"):
            self.page.locator("[data-interrupt-twice]").click()
        self.assertIn("Fixture action failed", self.page.locator("#toasts").inner_text())
        self.assertTrue(self.page.locator("[data-interrupt-twice]").is_enabled())
        self.assertTrue(self.page.locator("#dock").is_visible())

    def test_send_recovers_after_service_token_rotation(self):
        self.open()
        self.httpd.csrf_token = "fictional-restarted-token"
        self.page.locator("#dock-input").fill("send after restart")
        self.page.locator('#dock-form button[type="submit"]').click()
        self.page.wait_for_function("document.querySelector('#dock-input').value === ''")
        self.assertEqual(self.httpd.actions, [("/api/action/prompt", {"pane": "p2", "text": "send after restart"}, "fictional-restarted-token")])
        self.assertEqual(self.httpd.post_attempts, 2)
        self.assertEqual(self.httpd.page_reads, 2)

    def test_send_modes_and_alt_enter_recover_after_token_rotation(self):
        self.open()
        for mode, enter in (("send", True), ("type", False)):
            self.httpd.csrf_token = "fictional-token-" + mode
            self.page.locator("#dock-mode").select_option(mode)
            self.page.locator("#dock-input").fill("restart fixture")
            self.page.locator('#dock-form button[type="submit"]').click()
            self.page.wait_for_function("document.querySelector('#dock-input').value === ''")
            self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "text": "restart fixture", "enter": enter})
        self.httpd.csrf_token = "fictional-token-key"
        self.page.locator("#dock-input").fill("queued WebUI draft")
        with self.page.expect_response(lambda response: response.url.endswith('/api/action/followup') and response.status == 200):
            self.page.get_by_role("button", name="Alt-Enter", exact=True).click()
        self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "text": "queued WebUI draft"})
        self.assertEqual(self.httpd.post_attempts, 6)

    def test_alt_enter_is_next_to_enter_and_submits_webui_draft(self):
        self.open()
        keys = self.page.locator("#dock-keys [data-key]").evaluate_all("els => els.map(el => el.dataset.key)")
        self.assertEqual(keys[:2], ["enter", "alt+enter"])
        self.page.locator("#dock-input").fill("my browser follow-up")
        with self.page.expect_response("**/api/action/followup"):
            self.page.get_by_role("button", name="Alt-Enter", exact=True).click()
        self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "text": "my browser follow-up"})
        self.assertEqual(self.page.locator("#dock-input").input_value(), "")

    def test_alt_enter_without_browser_draft_never_sends_bare_key(self):
        self.open()
        self.page.get_by_role("button", name="Alt-Enter", exact=True).click()
        self.page.get_by_text("Type a message in the WebUI before using Alt-Enter.", exact=True).wait_for()
        self.assertEqual(self.httpd.actions, [])

    def test_alt_enter_keyboard_submits_draft_and_failure_preserves_it(self):
        self.open()
        self.httpd.fail_action = True
        self.page.locator("#dock-input").fill("follow-up to preserve")
        with self.page.expect_response("**/api/action/followup"):
            self.page.locator("#dock-input").press("Alt+Enter")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "follow-up to preserve")
        self.assertEqual(self.httpd.post_attempts, 1)
        self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "text": "follow-up to preserve"})

    def test_followup_response_loss_warns_before_manual_retry(self):
        self.open()
        def drop_response(route):
            route.fetch()
            route.abort("failed")
        self.page.route(self.base + "/api/action/followup", drop_response)
        self.page.locator("#dock-input").fill("possibly delivered follow-up")
        self.page.get_by_role("button", name="Alt-Enter", exact=True).click()
        self.page.get_by_text("inspect it before retrying", exact=False).wait_for()
        self.assertEqual(self.page.locator("#dock-input").input_value(), "possibly delivered follow-up")
        self.assertEqual(self.httpd.post_attempts, 1)
        self.assertEqual(len(self.httpd.actions), 1)
        self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "text": "possibly delivered follow-up"})

    def test_followup_truncated_success_body_preserves_draft_and_warns(self):
        self.open()
        def truncate_response(route):
            route.fetch()
            route.fulfill(status=200, content_type="application/json", body='{"ok":true,"result":')
        self.page.route(self.base + "/api/action/followup", truncate_response)
        self.page.locator("#dock-input").fill("uncertain body delivery")
        self.page.get_by_role("button", name="Alt-Enter", exact=True).click()
        self.page.get_by_text("inspect it before retrying", exact=False).wait_for()
        self.assertEqual(self.page.locator("#dock-input").input_value(), "uncertain body delivery")
        self.assertEqual(self.httpd.post_attempts, 1)
        self.assertEqual(len(self.httpd.actions), 1)

    def test_delayed_followup_keeps_original_pane_and_new_draft(self):
        self.open()
        self.httpd.delay_action = True
        self.page.locator("#dock-input").fill("original api follow-up")
        self.page.get_by_role("button", name="Alt-Enter", exact=True).click()
        self.assertTrue(self.httpd.action_started.wait(2))
        self.assertFalse(self.page.get_by_role("button", name="Alt-Enter", exact=True).is_enabled())
        self.choose("p1")
        self.page.locator("#dock-input").fill("new unsent ui draft")
        with self.page.expect_response("**/api/action/followup"):
            self.httpd.action_release.set()
        self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "text": "original api follow-up"})
        self.assertEqual(self.page.locator("#dock-input").input_value(), "new unsent ui draft")
        self.assertEqual(len(self.httpd.actions), 1)
        self.choose("p2")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "")

    def test_csrf_refresh_failure_keeps_draft_and_does_not_retry_action(self):
        self.open()
        self.httpd.csrf_token = "fictional-restarted-token"
        self.httpd.fail_page = True
        self.page.locator("#dock-input").fill("keep after refresh failure")
        self.page.locator('#dock-form button[type="submit"]').click()
        self.page.get_by_text("Could not refresh the session.", exact=False).wait_for()
        self.assertEqual(self.page.locator("#dock-input").input_value(), "keep after refresh failure")
        self.assertEqual(self.httpd.post_attempts, 1)
        self.assertEqual(self.httpd.actions, [])

    def test_other_forbidden_response_is_not_retried(self):
        self.open()
        self.httpd.deny_action = True
        self.page.locator("#dock-input").fill("keep forbidden draft")
        self.page.locator('#dock-form button[type="submit"]').click()
        self.page.get_by_text("client not allowed", exact=True).wait_for()
        self.assertEqual(self.httpd.page_reads, 1)
        self.assertEqual(self.httpd.post_attempts, 1)
        self.assertEqual(self.httpd.actions, [])
        self.assertEqual(self.page.locator("#dock-input").input_value(), "keep forbidden draft")

    def test_action_failure_after_execution_is_never_retried(self):
        self.open()
        self.httpd.fail_action = True
        self.page.locator("#dock-input").fill("do not duplicate")
        self.page.locator('#dock-form button[type="submit"]').click()
        self.page.get_by_text("Fixture action failed", exact=True).wait_for()
        self.assertEqual(self.httpd.post_attempts, 1)
        self.assertEqual(len(self.httpd.actions), 1)
        self.assertEqual(self.httpd.page_reads, 1)

    def test_csrf_retry_is_bounded_when_token_rotates_again(self):
        self.open()
        self.httpd.csrf_token = "fictional-restarted-token"
        self.page.route(self.base + "/", lambda route: route.fulfill(status=200, content_type="text/html", body='<meta name="csrf-token" content="fictional-already-obsolete">'))
        self.page.locator("#dock-input").fill("keep if second retry rejected")
        self.page.locator('#dock-form button[type="submit"]').click()
        self.page.get_by_text("missing or bad CSRF token", exact=True).wait_for()
        self.assertEqual(self.httpd.post_attempts, 2)
        self.assertEqual(self.httpd.actions, [])
        self.assertEqual(self.page.locator("#dock-input").input_value(), "keep if second retry rejected")

    def test_missing_refresh_token_does_not_retry_action(self):
        self.open()
        self.httpd.csrf_token = "fictional-restarted-token"
        self.page.route(self.base + "/", lambda route: route.fulfill(status=200, content_type="text/html", body="<html>No session token</html>"))
        self.page.locator("#dock-input").fill("keep missing token draft")
        self.page.locator('#dock-form button[type="submit"]').click()
        self.page.get_by_text("Could not refresh the session.", exact=False).wait_for()
        self.assertEqual(self.httpd.post_attempts, 1)
        self.assertEqual(self.httpd.actions, [])

    def test_csrf_refresh_keeps_original_pane_and_new_draft(self):
        self.open()
        self.httpd.csrf_token = "fictional-restarted-token"
        pending = []
        self.page.route(self.base + "/", lambda route: pending.append(route))
        self.page.locator("#dock-input").fill("send only to api")
        self.page.locator('#dock-form button[type="submit"]').click()
        self.page.wait_for_function("document.querySelector('#dock-input').value !== ''")
        self.choose("p1")
        self.page.locator("#dock-input").fill("new unsent ui draft")
        self.assertEqual(len(pending), 1)
        with self.page.expect_response("**/api/action/prompt"):
            pending[0].fulfill(status=200, content_type="text/html", body='<meta name="csrf-token" content="fictional-restarted-token">')
        self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "text": "send only to api"})
        self.assertEqual(self.page.locator("#dock-input").input_value(), "new unsent ui draft")
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "ui-agent")

    def test_concurrent_stale_actions_share_one_token_refresh(self):
        self.open()
        self.httpd.csrf_token = "fictional-restarted-token"
        refreshes = []
        def refresh(route):
            refreshes.append(True)
            route.fulfill(status=200, content_type="text/html", body='<meta name="csrf-token" content="fictional-restarted-token">')
        self.page.route(self.base + "/", refresh)
        self.page.evaluate("Promise.all([act('keys',{pane:'p2',keys:['enter']}),act('keys',{pane:'p2',keys:['alt+enter']})])")
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(len(self.httpd.actions), 2)
        self.assertTrue(all(action[2] == "fictional-restarted-token" for action in self.httpd.actions))

    def test_inflight_send_does_not_clear_new_pane_draft(self):
        self.open()
        self.httpd.delay_action = True
        self.page.locator("#dock-input").fill("sent to api")
        self.page.locator('#dock-form button[type="submit"]').click()
        self.assertTrue(self.httpd.action_started.wait(2))
        self.choose("p1")
        self.page.locator("#dock-input").fill("unsent ui draft")
        with self.page.expect_response("**/api/action/prompt"):
            self.httpd.action_release.set()
        self.assertEqual(self.page.locator("#dock-input").input_value(), "unsent ui draft")
        self.assertEqual(self.httpd.actions[0][1]["pane"], "p2")
        self.choose("p2")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "")

    def test_failed_send_keeps_draft(self):
        self.open()
        self.httpd.fail_action = True
        self.page.locator("#dock-input").fill("keep this")
        with self.page.expect_response("**/api/action/prompt"):
            self.page.locator('#dock-form button[type="submit"]').click()
        self.assertEqual(self.page.locator("#dock-input").input_value(), "keep this")
        self.assertIn("Fixture action failed", self.page.locator("#toasts").inner_text())

    def test_empty_tab_stays_empty_instead_of_showing_another_tab(self):
        self.open()
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        self.page.get_by_role("textbox", name="Find a space, tab or pane").fill("Empty")
        self.page.locator('[data-tab="t3"]').first.click()
        self.page.evaluate("refreshTree()")
        self.assertTrue(self.page.locator("#mobile-empty").is_visible())
        self.assertFalse(self.page.locator("#dock").is_visible())
        self.assertEqual(self.page.locator("#mobile-path").inner_text(), "Docs / Empty")

    def test_deep_link_is_consumed_before_empty_tab_selection(self):
        self.open("#pane=p4")
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        self.page.get_by_role("textbox", name="Find a space, tab or pane").fill("Empty")
        self.page.locator('[data-tab="t3"]').first.click()
        self.page.evaluate("loop()")
        self.assertTrue(self.page.locator("#mobile-empty").is_visible())
        self.assertFalse(self.page.locator("#dock").is_visible())
        self.assertEqual(self.page.locator("#mobile-path").inner_text(), "Docs / Empty")

    def test_closed_pane_falls_back_in_selected_tab(self):
        self.open()
        self.httpd.tree["panes"] = [p for p in self.httpd.tree["panes"] if p["pane_id"] != "p2"]
        self.page.evaluate("refreshTree()")
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "ui-agent")

    def test_external_tui_focus_does_not_steal_selected_phone_pane(self):
        self.open()
        self.choose("p1")
        self.httpd.tree["focused"] = {"workspace": "w2", "tab": "t2", "pane": "p4"}
        self.page.evaluate("refreshTree()")
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "ui-agent")

    def test_stale_read_cannot_replace_new_pane_output(self):
        self.httpd.delay_pane = "p2"
        self.open()
        self.assertTrue(self.httpd.read_started.wait(2))
        self.choose("p1")
        self.page.locator("#dock-term").get_by_text("Terminal output for p1", exact=False).wait_for()
        with self.page.expect_response(lambda r: "/api/read?pane=p2" in r.url):
            self.httpd.read_release.set()
        self.assertNotIn("output for p2", self.page.locator("#dock-term").inner_text())

    def test_empty_offline_and_unknown_states_are_safe(self):
        self.httpd.tree["workspaces"], self.httpd.tree["tabs"], self.httpd.tree["panes"] = [], [], []
        self.open()
        self.assertTrue(self.page.locator("#mobile-empty").is_visible())
        self.page.get_by_role("button", name="Show all agents").click()
        self.assertIn("No agents yet", self.page.locator("#agents-list").inner_text())
        self.page.get_by_role("button", name="Return to focused pane").click()
        self.httpd.fail_tree = True
        self.page.evaluate("refreshTree()")
        self.assertIn("Fixture offline", self.page.locator("#mobile-empty-text").inner_text())
        self.assertIn("STALE", self.page.locator("#mobile-live").inner_text())

    def test_navigation_focus_is_contained(self):
        self.open()
        self.page.get_by_role("button", name="Show all agents").click()
        self.page.locator(".agent-card").last.focus()
        self.page.keyboard.press("Tab")
        self.assertTrue(self.page.locator("#mobile-menu").evaluate("el => el === document.activeElement"))
        self.page.keyboard.press("Tab")
        self.assertTrue(self.page.locator("#mobile-agents").evaluate("el => el === document.activeElement"))
        self.page.keyboard.press("Tab")
        self.assertTrue(self.page.locator("#agents-close").evaluate("el => el === document.activeElement"))
        self.assertTrue(self.page.locator("#main-content").evaluate("el => el.inert"))
        self.page.locator("#mobile-menu").focus()
        self.page.keyboard.press("Shift+Tab")
        self.assertTrue(self.page.locator(".agent-card").last.evaluate("el => el === document.activeElement"))
        self.assertFalse(self.page.locator("#mobile-header").evaluate("el => el.inert"))

    def test_resizing_preserves_desktop_grid_and_mobile_selection(self):
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.open()
        self.assertTrue(self.page.locator("aside").is_visible())
        self.assertEqual(self.page.locator(".pane").count(), 3)
        self.assertFalse(self.page.locator("#dock").is_visible())
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.wait_for_function("document.querySelector('#mobile-title').textContent === 'api-agent'")
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "api-agent")
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.page.wait_for_function("!document.querySelector('#dock').classList.contains('open')")
        self.assertFalse(self.page.locator("#dock").is_visible())
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.wait_for_function("document.querySelector('#dock').classList.contains('open')")
        self.choose("p1")
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.assertTrue(self.page.locator("#dock").is_visible())
        self.assertEqual(self.page.locator("#dock-title").inner_text(), "ui-agent")

    def test_status_refresh_never_changes_existing_prompt_delivery(self):
        self.open()
        self.page.locator("#dock-input").fill("task draft, not an approval reply")
        self.httpd.tree["panes"][1]["agent_status"] = "blocked"
        self.page.evaluate("refreshTree()")
        self.assertEqual(self.page.locator("#dock-mode").input_value(), "prompt")
        with self.page.expect_response("**/api/action/prompt"):
            self.page.locator('#dock-form button[type="submit"]').click()
        self.assertEqual(self.httpd.actions[-1][0], "/api/action/prompt")

    def test_desktop_interaction_follows_workspace_selection_without_tui_focus(self):
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.open()
        self.page.locator('[data-pane="p1"]').click()
        self.page.locator('[data-ws="w2"]').click()
        self.assertEqual(self.page.locator("#dock-title").inner_text(), "review-agent")
        self.assertFalse(self.page.locator("#canvas-wrap").is_visible())
        self.assertEqual(self.httpd.actions, [])
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.wait_for_function("document.querySelector('#mobile-path').textContent === 'Docs / Writing'")
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "review-agent")
        self.assertEqual(self.page.locator("#mobile-path").inner_text(), "Docs / Writing")

    def test_desktop_interaction_has_one_output_and_overview_keeps_draft(self):
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.open()
        self.page.locator('[data-pane="p1"]').click()
        self.assertFalse(self.page.locator("#canvas-wrap").is_visible())
        self.assertTrue(self.page.locator("#dock-term").is_visible())
        self.page.locator("#dock-input").fill("saved draft")
        dock = self.page.locator("#dock").bounding_box()
        main = self.page.locator("#main-content").bounding_box()
        tabs = self.page.locator("#tabbar").bounding_box()
        self.assertAlmostEqual(dock["height"], main["height"] - tabs["height"], delta=2)
        self.page.get_by_role("button", name="Return to pane overview").click()
        self.assertTrue(self.page.locator("#canvas-wrap").is_visible())
        self.assertFalse(self.page.locator("#dock").is_visible())
        self.page.locator('[data-pane="p1"]').click()
        self.assertEqual(self.page.locator("#dock-input").input_value(), "saved draft")
        self.assertEqual(self.httpd.actions, [])

    def test_desktop_tab_selection_changes_interactive_pane_and_handles_empty_tab(self):
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.open()
        self.page.locator('[data-pane="p1"]').click()
        self.page.locator('[data-toggle="w2"]').click()
        self.page.locator('[data-tab="t2"]').first.click()
        self.assertEqual(self.page.locator("#dock-title").inner_text(), "review-agent")
        self.assertFalse(self.page.locator("#canvas-wrap").is_visible())
        self.page.locator('#tabbar [data-tab="t3"]').click()
        self.assertFalse(self.page.locator("#dock").is_visible())
        self.assertTrue(self.page.locator("#canvas-wrap").is_visible())
        self.assertIn("no panes", self.page.locator("#canvas").inner_text().lower())
        self.assertEqual(self.httpd.actions, [])

    def test_initial_loading_state_is_distinct_from_offline(self):
        self.httpd.delay_tree = True
        self.page.goto(self.base)
        self.assertTrue(self.httpd.tree_started.wait(2))
        for width, height in ((1440, 1000), (390, 844)):
            self.page.set_viewport_size({"width": width, "height": height})
            self.assertTrue(self.page.locator("#connection-state").is_visible())
            self.assertEqual(self.page.locator("#connection-title").inner_text(), "Connecting to Herdr…")
            self.assertFalse(self.page.locator("#connection-help").is_visible())
            self.assertFalse(self.page.locator("#mobile-empty").is_visible())
            self.assertFalse(self.page.locator("#canvas-wrap").is_visible())
            self.assertIn("CONNECTING", self.page.locator("#conn").inner_text())
        self.httpd.tree_release.set()
        self.page.wait_for_function("S.tree !== null")
        self.assertFalse(self.page.locator("#connection-state").is_visible())

    def test_initial_offline_state_recovers_automatically_with_deep_link(self):
        self.httpd.fail_tree = True
        self.page.goto(self.base + "/#pane=p4")
        self.page.wait_for_function("S.err !== null")
        self.assertTrue(self.page.locator("#connection-state").is_visible())
        self.assertEqual(self.httpd.reads, [])
        self.httpd.fail_tree = False
        self.page.wait_for_function("S.tree !== null")
        self.page.wait_for_function("document.querySelector('#mobile-title').textContent === 'review-agent'")
        self.assertFalse(self.page.locator("#connection-state").is_visible())
        self.assertTrue(self.page.locator("#dock").is_visible())
        self.assertEqual(self.httpd.actions, [])

    def test_offline_guidance_and_manual_retry_on_desktop_and_phone(self):
        for width, height in ((1440, 1000), (390, 844)):
            with self.subTest(width=width):
                self.httpd.fail_tree, self.httpd.delay_tree = True, False
                self.httpd.tree_started.clear()
                self.httpd.tree_release.clear()
                self.page.set_viewport_size({"width": width, "height": height})
                self.page.goto(self.base)
                self.page.wait_for_function("S.err !== null")
                self.assertEqual(self.page.locator("#connection-title").inner_text(), "Waiting for Herdr")
                self.assertFalse(self.page.locator("#connection-description").is_visible())
                self.assertEqual(self.page.locator("#connection-help p").inner_text(), "Run herdr in a terminal on the dashboard host.")
                self.assertFalse(self.page.locator("#connection-error").is_visible())
                self.assertFalse(self.page.locator("#mobile-empty").is_visible())
                self.assertFalse(self.page.locator("#canvas-wrap").is_visible())
                for control in ("btn-spawn", "mobile-spawn", "mobile-nav-spawn", "mobile-agents"):
                    self.assertTrue(self.page.locator(f"#{control}").is_disabled())
                self.assertEqual(self.page.locator("#active-pill").text_content(), "Offline")
                self.page.get_by_text("Connection details", exact=True).click()
                self.assertEqual(self.page.locator("#connection-error").inner_text(), "Fixture offline")
                with self.page.expect_response(lambda r: r.url.endswith("/api/tree") and r.status == 502):
                    self.page.get_by_role("button", name="Retry connection").click()
                self.page.wait_for_function("!S.busy")
                self.assertTrue(self.page.locator("#connection-error").is_visible())
                self.assertTrue(self.page.locator("#connection-retry").evaluate("el => el === document.activeElement"))
                self.httpd.delay_tree = True
                retry_width = self.page.locator("#connection-retry").bounding_box()["width"]
                self.page.get_by_role("button", name="Retry connection").click()
                self.assertTrue(self.httpd.tree_started.wait(2))
                self.assertTrue(self.page.locator("#connection-retry").is_disabled())
                self.assertEqual(self.page.locator("#connection-retry").inner_text(), "Connecting…")
                self.assertEqual(self.page.locator("#connection-retry").get_attribute("aria-busy"), "true")
                self.assertEqual(self.page.locator("#connection-retry").evaluate("el => getComputedStyle(el, '::before').animationName"), "spin")
                self.assertEqual(self.page.locator("#connection-retry").bounding_box()["width"], retry_width)
                self.page.emulate_media(reduced_motion="reduce")
                self.assertEqual(self.page.locator("#connection-retry").evaluate("el => getComputedStyle(el, '::before').animationName"), "none")
                self.page.emulate_media(reduced_motion="no-preference")
                self.httpd.fail_tree = False
                self.httpd.tree_release.set()
                self.page.wait_for_function("S.tree !== null")
                self.assertFalse(self.page.locator("#connection-state").is_visible())
                self.assertFalse(self.page.locator("#btn-spawn").is_disabled())
                self.assertTrue(self.page.locator("#dock" if width < 900 else "#canvas-wrap").is_visible())
        self.assertEqual(self.httpd.actions, [])

    def test_background_reconnect_does_not_flash_refresh_controls(self):
        self.httpd.fail_tree = True
        self.page.goto(self.base)
        self.page.wait_for_function("S.err !== null")
        self.page.locator("#connection-retry").focus()
        self.page.evaluate("""() => {
          window.refreshMutations = 0;
          window.refreshObserver = new MutationObserver(records => window.refreshMutations += records.length);
          for (const id of ['connection-retry', 'refresh-ico']) {
            window.refreshObserver.observe(document.getElementById(id), {attributes:true, childList:true, characterData:true, subtree:true});
          }
        }""")
        self.httpd.delay_tree = True
        self.assertTrue(self.httpd.tree_started.wait(3))
        for width, height in ((1440, 1000), (390, 844)):
            self.page.set_viewport_size({"width": width, "height": height})
            self.assertFalse(self.page.locator("#connection-retry").is_disabled())
            self.assertEqual(self.page.locator("#connection-retry").inner_text(), "Retry connection")
            self.assertIsNone(self.page.locator("#connection-retry").get_attribute("aria-busy"))
            self.assertEqual(self.page.locator("#connection-retry").evaluate("el => getComputedStyle(el, '::before').animationName"), "none")
            self.assertTrue(self.page.locator("#connection-retry").evaluate("el => el === document.activeElement"))
            self.assertFalse(self.page.locator("#refresh-ico").evaluate("el => el.classList.contains('spin')"))
        self.httpd.delay_tree = False
        self.httpd.tree_release.set()
        self.page.wait_for_function("!S.busy")
        self.page.evaluate("refreshTree()")
        self.assertEqual(self.page.evaluate("window.refreshMutations"), 0)
        self.page.evaluate("window.refreshObserver.disconnect()")

    def test_structured_connection_error_shows_code_and_message(self):
        self.httpd.fail_tree = True
        message = "No Herdr server is running at /tmp/demo-herdr.sock; run `herdr` to start or attach it."
        self.httpd.tree_error = json.dumps({"id": "cli:api:snapshot", "error": {"code": "server_not_running", "message": message}})
        self.page.goto(self.base)
        self.page.wait_for_function("S.err !== null")
        self.page.get_by_text("Connection details", exact=True).click()
        self.assertEqual(self.page.locator("#connection-error-code").inner_text(), "server_not_running")
        self.assertEqual(self.page.locator("#connection-error").inner_text(), message)
        for width, height in ((320, 700), (844, 390), (1440, 1000)):
            self.page.set_viewport_size({"width": width, "height": height})
            self.page.locator("#connection-error").scroll_into_view_if_needed()
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            self.assertTrue(self.page.locator("#connection-error").evaluate("el => el.scrollWidth <= el.clientWidth"))
        for error in ('{"error":', '{"error":{"message":42}}', "null", "Plain-text error"):
            with self.subTest(error=error):
                self.httpd.tree_error = error
                self.page.evaluate("refreshTree()")
                self.assertEqual(self.page.locator("#connection-error").inner_text(), error)
                self.assertFalse(self.page.locator("#connection-error-code").is_visible())

    def test_offline_details_are_plain_text_and_fit_small_viewports(self):
        self.httpd.fail_tree = True
        self.httpd.tree_error = '<img src=x onerror="window.fixtureInjected=true">' + "x" * 600
        self.page.goto(self.base)
        self.page.wait_for_function("S.err !== null")
        self.page.get_by_text("Connection details", exact=True).click()
        self.assertEqual(self.page.locator("#connection-error").inner_text(), self.httpd.tree_error)
        self.assertFalse(self.page.locator("#connection-error-code").is_visible())
        self.assertEqual(self.page.locator("#connection-error img").count(), 0)
        self.assertFalse(self.page.evaluate("Boolean(window.fixtureInjected)"))
        for width, height in ((320, 700), (844, 390), (1440, 1000)):
            self.page.set_viewport_size({"width": width, "height": height})
            self.page.locator("#connection-retry").scroll_into_view_if_needed()
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            box = self.page.locator("#connection-retry").bounding_box()
            self.assertGreaterEqual(box["y"], 0)
            self.assertLessEqual(box["y"] + box["height"], height)

    def test_connection_loss_preserves_last_session_and_draft(self):
        self.open("#pane=p4")
        self.page.locator("#dock-input").fill("keep this draft")
        self.httpd.fail_tree = True
        self.page.evaluate("refreshTree()")
        self.assertFalse(self.page.locator("#connection-state").is_visible())
        self.assertTrue(self.page.locator("#dock").is_visible())
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "review-agent")
        self.assertIn("STALE", self.page.locator("#mobile-live").inner_text())
        self.assertEqual(self.page.locator("#dock-input").input_value(), "keep this draft")
        self.httpd.fail_tree = False
        self.page.evaluate("refreshTree()")
        self.assertEqual(self.page.locator("#dock-input").input_value(), "keep this draft")
        self.assertEqual(self.httpd.actions, [])

    def test_keyboard_visual_viewport_keeps_input_visible(self):
        self.open()
        self.page.locator("#dock-input").focus()
        self.page.evaluate("""() => {
          Object.defineProperty(window.visualViewport, 'height', {configurable:true, value:430});
          Object.defineProperty(window.visualViewport, 'offsetTop', {configurable:true, value:40});
          window.visualViewport.dispatchEvent(new Event('resize'));
        }""")
        box = self.page.locator("#dock-form").bounding_box()
        self.assertGreaterEqual(box["y"], 40)
        self.assertLessEqual(box["y"] + box["height"], 470)
        self.assertTrue(self.page.locator("#dock-input").evaluate("el => el === document.activeElement"))

    def test_short_keyboard_viewports_keep_expanded_composer_visible(self):
        self.open()
        self.page.locator("#dock-input").fill("one\\ntwo\\nthree\\nfour\\nfive\\nsix")
        for height in (280, 220, 180):
            self.page.evaluate("""height => {
              Object.defineProperty(window.visualViewport, 'height', {configurable:true, value:height});
              window.visualViewport.dispatchEvent(new Event('resize'));
            }""", height)
            form = self.page.locator("#dock-form").bounding_box()
            self.assertGreaterEqual(form["y"], 44)
            self.assertLessEqual(form["y"] + form["height"], height)
            self.assertTrue(self.page.locator("#dock-keys").is_visible())
            self.page.locator('[data-key="ctrl+l"]').scroll_into_view_if_needed()
            with self.page.expect_response("**/api/action/keys"):
                self.page.locator('[data-key="ctrl+l"]').click()
            self.assertEqual(self.httpd.actions[-1][1], {"pane": "p2", "keys": ["ctrl+l"]})
            self.page.locator("#mobile-header-more").click()
            self.assertTrue(self.page.locator("#mobile-refresh").is_visible())
            self.page.locator("#mobile-header-more").click()

    def test_collapsed_current_workspace_stays_collapsed_after_poll(self):
        self.open()
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        self.page.locator('[data-toggle="w1"]').click()
        self.assertEqual(self.page.locator('[data-pane="p1"]').count(), 0)
        self.page.evaluate("refreshTree()")
        self.assertEqual(self.page.locator('[data-pane="p1"]').count(), 0)

    def test_agents_search_remains_usable_in_short_keyboard_viewports(self):
        self.open()
        for height in (280, 220):
            self.page.get_by_role("button", name="Show all agents").click()
            self.page.evaluate("""height => {
              Object.defineProperty(window.visualViewport, 'height', {configurable:true, value:height});
              window.visualViewport.dispatchEvent(new Event('resize'));
            }""", height)
            self.page.get_by_role("textbox", name="Find an agent").fill("review")
            self.page.locator('[data-agent-pane="p4"]').scroll_into_view_if_needed()
            box = self.page.locator('[data-agent-pane="p4"]').bounding_box()
            self.assertLessEqual(box["y"] + box["height"], height)
            self.page.locator('[data-agent-pane="p4"]').click()
            self.assertEqual(self.page.locator("#mobile-title").inner_text(), "review-agent")

    def test_navigation_search_remains_usable_in_short_keyboard_viewports(self):
        self.open()
        for height in (280, 220):
            self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
            self.page.evaluate("""height => {
              Object.defineProperty(window.visualViewport, 'height', {configurable:true, value:height});
              window.visualViewport.dispatchEvent(new Event('resize'));
            }""", height)
            self.page.get_by_role("textbox", name="Find a space, tab or pane").fill("review")
            self.page.locator('[data-pane="p4"]').scroll_into_view_if_needed()
            box = self.page.locator('[data-pane="p4"]').bounding_box()
            self.assertLessEqual(box["y"] + box["height"], height)
            self.page.locator('[data-pane="p4"]').click()
            self.assertEqual(self.page.locator("#mobile-title").inner_text(), "review-agent")

    def test_empty_landscape_can_launch_first_session(self):
        self.httpd.tree["workspaces"], self.httpd.tree["tabs"], self.httpd.tree["panes"] = [], [], []
        self.page.set_viewport_size({"width": 844, "height": 390})
        self.open()
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        self.page.get_by_role("button", name="New session", exact=True).click()
        self.assertTrue(self.page.locator("#spawn.open").is_visible())
        self.assertTrue(self.page.locator("#spawn-go").is_visible())

    def test_launch_dialog_follows_short_keyboard_viewport(self):
        self.open()
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        self.page.locator("#mobile-spawn").click()
        self.page.evaluate("""() => {
          Object.defineProperty(window.visualViewport, 'height', {configurable:true, value:220});
          Object.defineProperty(window.visualViewport, 'offsetTop', {configurable:true, value:30});
          window.visualViewport.dispatchEvent(new Event('resize'));
        }""")
        self.page.locator("#spawn-go").scroll_into_view_if_needed()
        box = self.page.locator("#spawn-go").bounding_box()
        self.assertGreaterEqual(box["y"], 30)
        self.assertLessEqual(box["y"] + box["height"], 250)
        self.assertEqual(self.httpd.actions, [])

    def test_unknown_status_and_untrusted_labels_are_plain_text(self):
        self.httpd.tree["panes"][0].update(label='<img src=x onerror="window.fixtureInjected=true">', agent_status="unexpected")
        self.open()
        self.page.get_by_role("button", name="Show all agents").click()
        self.assertIn("unknown", self.page.locator("#agents-list").inner_text())
        self.assertEqual(self.page.locator("#agents-list img").count(), 0)
        self.assertFalse(self.page.evaluate("Boolean(window.fixtureInjected)"))

    def test_refresh_preserves_focused_agent_and_tree_controls(self):
        self.open()
        self.page.get_by_role("button", name="Show all agents").click()
        self.page.locator('[data-agent-pane="p1"]').focus()
        self.page.evaluate("refreshTree()")
        self.assertTrue(self.page.locator('[data-agent-pane="p1"]').evaluate("el => el === document.activeElement"))
        self.page.get_by_role("button", name="Return to focused pane").click()
        self.page.get_by_role("button", name="Open spaces, tabs and panes").click()
        self.page.locator('[data-pane="p1"]').focus()
        self.page.evaluate("refreshTree()")
        self.assertTrue(self.page.locator('[data-pane="p1"]').evaluate("el => el === document.activeElement"))

    def test_phone_wraps_long_terminal_lines_without_changing_desktop_layout(self):
        token = "unbroken_" * 60
        text = "wrap-fixture\n  " + "Long output with spaces. " * 60 + "\n  " + token + "\n" + "─" * 300 + "\n  indented code\n"
        self.page.route(self.base + "/api/read*", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({
                "text": "\x1b[32m" + text + "\x1b[0m"
                if parse_qs(urlparse(route.request.url).query).get("ansi") == ["1"] else text})))
        self.open()
        term = self.page.locator("#dock-term")
        term.get_by_text("wrap-fixture", exact=False).first.wait_for()
        for color in (True, False):
            self.page.get_by_role("button", name="Pane actions", exact=True).first.click()
            self.page.locator("#mobile-color").set_checked(color)
            self.page.get_by_role("button", name="Pane actions", exact=True).first.click()
            self.page.wait_for_function(
                "color => Boolean(document.querySelector('#dock-term span[style]')) === color", arg=color)
            for width, height in ((320, 700), (375, 812), (430, 932), (844, 390), (390, 430),
                                  (1440, 1000), (390, 844)):
                with self.subTest(color=color, width=width, height=height):
                    self.page.set_viewport_size({"width": width, "height": height})
                    if width > 900:
                        self.page.locator('#tree [data-pane="p2"]').click()
                    term.get_by_text("wrap-fixture", exact=False).first.wait_for()
                    self.assertEqual(term.inner_text(), text)
                    geometry = term.evaluate("""(el, token) => {
                        const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                        let node;
                        while ((node = walker.nextNode())) {
                            const start = node.textContent.indexOf(token);
                            if (start < 0) continue;
                            const range = document.createRange();
                            range.setStart(node, start); range.setEnd(node, start + token.length);
                            return {lines: range.getClientRects().length, width: el.clientWidth,
                                    scrollWidth: el.scrollWidth, whiteSpace: getComputedStyle(el).whiteSpace};
                        }
                        throw new Error("Fixture token missing");
                    }""", token)
                    if width <= 900:
                        self.assertEqual(geometry["whiteSpace"], "pre-wrap")
                        self.assertLessEqual(geometry["scrollWidth"], geometry["width"])
                        self.assertGreater(geometry["lines"], 1)
                        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                    else:
                        self.assertEqual(geometry["whiteSpace"], "pre")
                        self.assertGreater(geometry["scrollWidth"], geometry["width"])
                        self.assertEqual(geometry["lines"], 1)
        self.assertEqual(self.httpd.actions, [])

    def test_narrow_and_landscape_views_keep_composer_inside_viewport(self):
        self.open()
        for width, height in ((320, 700), (375, 812), (430, 932), (844, 390), (390, 430)):
            self.page.set_viewport_size({"width": width, "height": height})
            self.page.wait_for_function("document.querySelector('.app').clientHeight === window.visualViewport.height")
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            box = self.page.locator("#dock-form").bounding_box()
            self.assertLessEqual(box["y"] + box["height"], height)
            self.page.get_by_role("button", name="Show all agents").click()
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            self.page.get_by_role("button", name="Return to focused pane").click()
    # ---------------- on-demand scrollback (issue #3) ----------------
    def wait_live(self, text):
        self.page.wait_for_function(f"document.querySelector('#dock-term').textContent.includes({json.dumps(text)}) && !document.querySelector('#dock-older').disabled")

    def anchor_offset(self):
        return self.page.evaluate("""() => {const el=document.querySelector('#dock-term'),m=el.querySelector('[data-history-anchor]');
            return m.getBoundingClientRect().top-el.getBoundingClientRect().top-parseFloat(getComputedStyle(el).paddingTop);}""")

    def history_reads(self):
        return [int(q["lines"]) for q in self.httpd.read_queries if q.get("pane") == "p2" and q.get("source") != "visible"]

    def test_load_older_keeps_place_duplicates_draft_and_live_tail(self):
        rows = ["repeat" if i % 7 == 0 else f"line {i}" for i in range(1000)]
        self.httpd.history["p2"] = rows
        self.open()
        self.page.locator("#dock-input").fill("keep this draft")
        self.wait_live("line 999")
        self.page.evaluate("document.querySelector('#dock-term').scrollTop=0")
        self.page.locator("#dock-older").click()
        self.page.get_by_text("Showing up to 800 recent lines").wait_for()
        self.assertEqual(self.page.locator("#dock-term").text_content(), "\n".join(rows[-800:]))  # repeated lines intact
        self.assertLess(abs(self.anchor_offset()), 2)  # previous top line is still at the top
        self.httpd.history["p2"] = rows + ["fresh output"]
        self.page.get_by_role("button", name="New output · Jump to latest").wait_for()
        self.assertEqual(self.page.locator("#dock-term").text_content(), "\n".join(rows[-800:]))
        self.assertLess(abs(self.anchor_offset()), 2)
        self.page.locator("#dock-latest").click()
        self.wait_live("fresh output")
        self.assertNotIn("line 300", self.page.locator("#dock-term").text_content())
        self.assertTrue(self.page.evaluate("(el => el.scrollHeight-el.scrollTop-el.clientHeight < 30)(document.querySelector('#dock-term'))"))
        self.assertEqual(self.page.locator("#dock-input").input_value(), "keep this draft")
        self.assertEqual(self.httpd.actions, [])
        self.assertEqual([n for n in self.history_reads() if n != 400], [800])

    def test_desktop_load_older_keeps_place(self):
        self.context.close()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.httpd.history["p2"] = [f"desktop {i}" for i in range(900)]
        self.open()
        self.page.evaluate("openDock('p2',{autofocus:false})")
        self.wait_live("desktop 899")
        self.page.evaluate("document.querySelector('#dock-term').scrollTop=0")
        self.page.locator("#dock-older").click()
        self.page.get_by_text("Showing up to 800 recent lines").wait_for()
        self.assertLess(abs(self.anchor_offset()), 2)
        self.assertTrue(self.page.locator("#dock-term").text_content().startswith("desktop 100\n"))

    def test_repeated_loads_stop_at_line_limit_or_retained_start(self):
        self.httpd.history["p2"] = [f"row {i}" for i in range(3000)]
        self.open()
        self.wait_live("row 2999")
        for status in ("Showing up to 800 recent lines", "Showing up to 1,600 recent lines", "Showing the 2,000-line limit"):
            self.page.locator("#dock-older").click()
            self.page.get_by_text(status).wait_for()
        self.assertTrue(self.page.locator("#dock-older").is_disabled())
        self.assertTrue(self.page.locator("#dock-term").text_content().startswith("row 1000\n"))
        self.assertEqual([n for n in self.history_reads() if n != 400], [800, 1600, 2000])
        self.page.locator("#dock-latest").click()
        self.httpd.history["p2"] = [f"short {i}" for i in range(500)]
        self.wait_live("short 499")
        self.page.locator("#dock-older").click()
        self.page.get_by_text("Showing up to 800 recent lines").wait_for()
        self.page.locator("#dock-older").click()
        self.page.get_by_text("No older output is retained by Herdr for this pane.").wait_for()
        self.assertTrue(self.page.locator("#dock-older").is_disabled())
        self.assertTrue(self.page.locator("#dock-term").text_content().startswith("short 0\n"))

    def test_history_errors_retry_and_changed_snapshots_keep_view(self):
        rows = [f"line {i}" for i in range(1000)]
        self.httpd.history["p2"] = rows
        self.httpd.fail_history_lines = 800
        self.open()
        self.wait_live("line 999")
        self.page.locator("#dock-older").click()
        self.page.get_by_role("button", name="Retry loading older output").wait_for()
        self.assertIn("Could not load older output", self.page.locator("#dock-history-status").text_content())
        self.assertIn("line 999", self.page.locator("#dock-term").text_content())
        self.page.locator("#dock-older").click()
        self.page.get_by_text("Showing up to 800 recent lines").wait_for()
        self.httpd.delay_lines, self.httpd.history_on_release = 1600, ["cleared", "screen"]
        self.page.locator("#dock-older").click()
        self.assertTrue(self.httpd.read_started.wait(5))
        self.assertTrue(self.page.locator("#dock-older").is_disabled())  # loads are serialized
        self.httpd.read_release.set()
        self.page.locator("#dock-history-apply").wait_for()
        self.assertEqual(self.page.locator("#dock-term").text_content(), "\n".join(rows[-800:]))
        self.page.locator("#dock-history-apply").click()
        self.assertEqual(self.page.locator("#dock-term").text_content(), "cleared\nscreen")
        self.assertEqual(self.httpd.actions, [])

    def test_history_response_after_pane_switch_is_discarded(self):
        self.httpd.history["p2"] = [f"line {i}" for i in range(1000)]
        self.httpd.delay_lines = 800
        self.open()
        self.wait_live("line 999")
        self.page.locator("#dock-older").click()
        self.assertTrue(self.httpd.read_started.wait(5))
        self.page.evaluate("openDock('p1',{autofocus:false})")
        self.page.locator("#dock-term").get_by_text("Terminal output for p1", exact=False).wait_for()
        self.httpd.read_release.set()
        self.page.wait_for_timeout(400)
        self.assertNotIn("line ", self.page.locator("#dock-term").text_content())
        self.assertEqual(self.page.locator("#dock-history-status").text_content(), "")
        self.assertTrue(self.page.locator("#dock-latest").is_hidden())
        self.assertEqual(self.httpd.actions, [])


class BrowserDiagnosticsTest(unittest.TestCase):
    setUpClass = classmethod(PhoneUITest.setUpClass.__func__)
    tearDownClass = classmethod(PhoneUITest.tearDownClass.__func__)
    setUp = PhoneUITest.setUp
    tearDown = PhoneUITest.tearDown
    open = PhoneUITest.open

    def test_missing_diagnostic_script_does_not_break_offline_recovery_or_toasts(self):
        self.page.route(self.base + "/errors.js", lambda route: route.abort())
        self.httpd.fail_tree = True
        self.page.goto(self.base + "/")
        self.page.wait_for_function("S.err !== null")
        self.page.evaluate("toast('Fixture visible error', 'err')")
        self.page.get_by_text("Fixture visible error", exact=True).wait_for()
        self.httpd.fail_tree = False
        self.page.wait_for_function("S.tree !== null && S.err === null")
        self.assertEqual(self.httpd.actions, [])

    def test_browser_reports_invalid_json_without_sensitive_content(self):
        self.open()
        self.page.route(self.base + "/api/read*", lambda route: route.fulfill(status=200, content_type="application/json", body="SECRET-RESPONSE"))
        with self.page.expect_response(lambda response: response.url.endswith("/api/client-error")):
            self.page.evaluate("getJSON('/api/read?pane=SECRET-PANE').catch(() => {})")
        self.assertTrue(any(report["kind"] == "invalid_json" for report in self.httpd.reports))
        self.assertNotIn("SECRET", json.dumps(self.httpd.reports))
        self.assertEqual(self.httpd.actions, [])

    def test_uncaught_errors_and_rejections_are_reported_without_message(self):
        self.open()
        for script, kind in (("dispatchEvent(new ErrorEvent('error', {message:'SECRET-MESSAGE'}))", "javascript_error"),
                             ("dispatchEvent(new PromiseRejectionEvent('unhandledrejection', {promise:Promise.resolve(),reason:'SECRET-REJECTION'}))", "unhandled_rejection")):
            with self.page.expect_response(lambda response: response.url.endswith("/api/client-error")):
                self.page.evaluate(script)
            self.assertTrue(any(report["kind"] == kind for report in self.httpd.reports))
        self.assertNotIn("SECRET", json.dumps(self.httpd.reports))

    def test_report_failure_does_not_recurse_or_replay_action(self):
        self.open()
        attempts = []
        def fail_report(route):
            attempts.append(route.request.url)
            route.abort()
        self.page.route(self.base + "/api/client-error", fail_report)
        self.page.route(self.base + "/api/action/followup", lambda route: route.abort())
        with self.page.expect_console_message(predicate=lambda message: "diagnostic could not reach" in message.text):
            self.page.evaluate("act('followup',{pane:'p2',text:'SECRET-DRAFT'}).catch(() => {})")
        self.assertEqual(self.httpd.actions, [])
        self.assertEqual(len(attempts), 1)


class RealBridgeUITest(unittest.TestCase):
    def test_native_slash_and_keys_use_real_bridge_order(self):
        from unittest.mock import patch
        import test_server
        bridge = test_server.server
        test_server.LOG.write_text("")
        with patch.object(bridge, "HERDR", str(test_server.FAKE)), patch.object(bridge, "TOKEN", "fictional-native-token"):
            bridge.invalidate()
            httpd = bridge.make_server(0)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel=os.environ.get("HERDR_WEBUI_BROWSER", "chrome"), headless=True)
                    try:
                        page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                        page.goto(f"http://127.0.0.1:{httpd.server_port}/")
                        page.locator("#dock.open").wait_for()
                        page.locator("#dock-input").fill("/")
                        page.locator("#dock-input").press("ArrowDown")
                        page.locator("#dock-input").press("Enter")
                        page.wait_for_function("() => !nativeInput()")
                        calls = [json.loads(line) for line in test_server.LOG.read_text().splitlines()]
                        inputs = [call for call in calls if call[:2] in (["pane", "send-text"], ["pane", "send-keys"])]
                        self.assertEqual(inputs, [["pane", "send-text", "w1:p1", "/"], ["pane", "send-keys", "w1:p1", "down"], ["pane", "send-keys", "w1:p1", "enter"]])
                        self.assertTrue(any("visible" in call for call in calls))
                    finally:
                        browser.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                bridge.invalidate()

    def test_followup_with_plain_cli_acknowledgement_and_token_rotation(self):
        from unittest.mock import patch
        import test_server
        bridge = test_server.server
        test_server.LOG.write_text("")
        with patch.object(bridge, "HERDR", str(test_server.FAKE)), patch.object(bridge, "TOKEN", "fictional-initial-token"):
            bridge.invalidate()
            httpd = bridge.make_server(0)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel=os.environ.get("HERDR_WEBUI_BROWSER", "chrome"), headless=True)
                    try:
                        page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                        page.goto(f"http://127.0.0.1:{httpd.server_port}/")
                        page.locator("#dock.open").wait_for()
                        bridge.TOKEN = "fictional-rotated-token"
                        page.locator("#dock-input").fill("browser follow-up, not a bare key")
                        with page.expect_response(lambda response: response.url.endswith('/api/action/followup') and response.status == 200):
                            page.get_by_role("button", name="Alt-Enter", exact=True).click()
                        self.assertEqual(page.locator("#dock-input").input_value(), "")
                        calls = [json.loads(line) for line in test_server.LOG.read_text().splitlines()]
                        inputs = [call for call in calls if call[:2] in (["pane", "send-text"], ["pane", "send-keys"])]
                        self.assertEqual(inputs, [["pane", "send-text", "w1:p1", "browser follow-up, not a bare key"], ["pane", "send-keys", "w1:p1", "alt+enter"]])
                        self.assertEqual(page.locator("#toasts .err").count(), 0)
                    finally:
                        browser.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                bridge.invalidate()


if __name__ == "__main__":
    unittest.main()
