"""Browser regressions with fictional sessions; never connects to live Herdr.

Uses an already installed Playwright and Google Chrome (no downloads).
Run: python3 -m unittest test_ui -v
"""
import copy
import json
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
        if url.path == "/api/tree":
            if self.server.delay_tree:
                self.server.tree_started.set()
                self.server.tree_release.wait(5)
            if self.server.fail_tree:
                return self.reply({"error": self.server.tree_error}, 502)
            return self.reply(self.server.tree)
        if url.path == "/api/read":
            pane = parse_qs(url.query)["pane"][0]
            self.server.reads.append(pane)
            if pane == self.server.delay_pane:
                self.server.read_started.set()
                self.server.read_release.wait(5)
            return self.reply({"text": f"Terminal output for {pane}\n\x1b[32mReady\x1b[0m\n"})
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
        if self.path == "/api/action/close":
            self.server.tree["panes"] = [p for p in self.server.tree["panes"] if p["pane_id"] != body["pane"]]
        self.reply({"ok": True, "result": {"ok": True}})


class PhoneUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

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
        self.httpd.delay_pane = None
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

    def test_existing_desktop_inspector_normalizes_mobile_location(self):
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.open()
        self.page.locator('[data-pane="p1"]').click()
        self.page.locator('[data-ws="w2"]').click()
        self.assertEqual(self.page.locator("#dock-title").inner_text(), "ui-agent")
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.wait_for_function("document.querySelector('#mobile-path').textContent === 'Website / Build'")
        self.assertEqual(self.page.locator("#mobile-title").inner_text(), "ui-agent")
        self.assertEqual(self.page.locator("#mobile-path").inner_text(), "Website / Build")

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
                    browser = playwright.chromium.launch(channel="chrome", headless=True)
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
