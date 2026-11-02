# Service checks

First run: connect Tailscale to your tailnet, start Herdr, then follow the macOS
service setup in [README.md](README.md). Run repository commands from its root.

1. Run regression tests without touching the live Herdr server.

```bash
python3 -m unittest test_server test_error_log -v
# Expect: OK; tests use a fake Herdr executable.
```

2. Check the installed per-user service.

```bash
launchctl print "gui/$(id -u)/dev.herdr.webui"
# Expect: state = running and a pid.
```

3. Verify that the local API can reach Herdr (prints counts, not terminal output).

```bash
curl --fail --silent --show-error --max-time 25 http://127.0.0.1:8780/api/tree | python3 -c 'import json,sys; p=json.load(sys.stdin); print("spaces:", len(p["workspaces"]), "panes:", len(p["panes"]))'
# Expect: space and pane counts matching the local Herdr server, including with --allow-client-ip.
```

4. Check private HTTPS routing.

```bash
tailscale serve status
# Expect: an HTTPS URL, tailnet-only access, proxying http://127.0.0.1:8780.
```

5. Restart after updating the bridge.

```bash
launchctl kickstart -k "gui/$(id -u)/dev.herdr.webui"
# Expect: command succeeds; the service returns to running with a new pid.
```

6. On an iPhone connected to the same tailnet, open the URL from step 4 in Safari.

```text
https://<machine>.<tailnet>.ts.net/
```

Expect one focused terminal, **☰** for the space/tab/pane tree, and **Agents** for
agents sorted Working → Blocked → Done → Idle. Tap either header button again
or its popup's × to dismiss; selection and drafts remain unchanged. Tap a state count to filter;
tap that same count again to show all. Selecting a pane returns to its output
without sending input. Use **✎** beside any space, tab or pane to rename it;
the selected pane also has **··· → Rename pane**. Expect the new name everywhere
while selection and drafts stay unchanged; Cancel sends no request.
Use **··· → Close pane** for confirmed removal, or **Ctrl-C ×2** to send two
interrupts without removing the terminal. These controls affect the selected pane;
only test them on a session you intend to interrupt or close.
**Alt-Enter** sends the WebUI draft followed by Alt-Enter to the selected pane;
it requires a nonempty browser draft and clears it only after success. If delivery
fails, inspect the terminal before retrying because text may already be present.
In a disposable, idle agent pane, type `/`: expect the agent's own command menu
and highlight. Use ↑/↓, then Tab: expect the completed command plus a space in
the composer. Add parameters and Enter: expect one submission, not a duplicated
prefix. Type `/model` and Enter: expect selectable native model rows; tap/click
one to select it, or use ↑/↓ and Enter with the composer unfocused. Escape cancels native interaction. On uncertain delivery, inspect
and repair the pane before resetting browser mirroring.
Check that tapping the composer opens the keyboard while keeping input visible.
In a pane with more than 400 lines of output, scroll to the top and tap **Load
older output**: expect earlier lines above the same reading position, then
**Jump to latest** to return to live output. No input is sent.
Safari's Share > Add to Home Screen makes a convenient shortcut.

7. Run browser regressions, if Playwright Python and Google Chrome are already installed.

```bash
python3 -m unittest test_ui test_mockup -v
# Expect: OK; fictional loopback sessions only, no live terminal commands.
```

8. Open the fictional landing page/Pi chat preview in your browser.

```text
http://127.0.0.1:8780/mockups/sre-landing
# Expect: scenario-specific diagnosis/incidents/approvals; healthy upgrade prompts; LLM reset or recent models. No live changes.
```

9. Preview model selection inside a fictional Herdr pane view.

```text
http://127.0.0.1:8780/mockups/model-switch
# Expect: current-pane/all-compatible scopes, skipped/deferred agents, confirmation and simulation only.
```

10. Inspect recent safe error classifications (honors a custom log location).

```bash
python3 -c 'import json,os; from pathlib import Path; p=Path(os.environ.get("HERDR_WEBUI_LOG_FILE", str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home()/".local/state")))/"herdr-webui/errors.jsonl"))); rows=p.read_text().splitlines(); print("\n".join(json.dumps({k:e.get(k) for k in ("time","event","code","status","request_id")}) for e in map(json.loads,rows[-20:])))'
# Expect: safe JSON events with error codes and request IDs; an empty file is normal before an error.
```

11. Run on a separate port with an explicit error file (stop with Ctrl-C).

```bash
python3 server.py --port 9000 --log-file "$HOME/.local/state/herdr-webui/preview-errors.jsonl"
# Expect: a loopback listener on port 9000 and a private error log.
```

| Failure | Fix |
| --- | --- |
| Cannot open error log | Choose an owner-controlled writable `--log-file`; symlinks are rejected. |
| Browser diagnostic could not reach the server | Check service/network and stderr; reporting failure never retries a terminal action. |
| Service exits: address already in use | Stop the old manually started bridge on port 8780; launchd retries. |
| HTTP 421 / bad host | Ensure the LaunchAgent's `--allow-host` exactly matches Serve's hostname; restart. |
| API returns Herdr unavailable | Start Herdr under the same macOS user; verify `HERDR_BIN` in the LaunchAgent. |
| iPhone cannot connect | Connect both devices to the same tailnet, check tailnet access rules, and keep the Mac awake. |
| HTTP 403 for the whole page | Use localhost directly or the allowed phone through Serve; verify its current IPs match the LaunchAgent. |
| HTTP 403 only on mutations after a restart | Current UI refreshes a rejected stale token automatically once; if still failing, reload. Unsent text is preserved while handling errors. |
| Browser tests cannot start | Use an existing Playwright Python/Google Chrome environment; these are optional test tools, not application dependencies. |
