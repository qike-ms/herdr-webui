# herdr-webui

A tiny, dependency-free web dashboard for a local [Herdr](https://herdr.dev) server.
It mirrors Herdr's TUI hierarchy — **Host › Space › Tab › Pane** — with live terminal
output and direct agent interaction from the browser.

*Unofficial community project; not affiliated with the Herdr authors.*

- One Python file (`server.py`, stdlib only) + one HTML file (`index.html`, no CDN, no build).
- Talks to Herdr only through its supported CLI (`herdr api snapshot`, `pane read`, …).
- Local Herdr session plus saved SSH machines, shown as top-level sidebar tree nodes.
  Click a host to expand its spaces, tabs, and panes; only the selected host is
  expanded. Other hosts remain visible, including while a connection is unavailable.
  Each request explicitly targets its selected host; pane IDs, drafts, snapshots,
  and delayed output never cross hosts. Remote errors never fall back to Local.

## Features

- Sidebar tree of hosts, spaces, tabs, and panes with live agent status (working / blocked / done / idle).
- Tab bar and pane canvas laid out from Herdr's real split geometry; each pane streams recent output.
- Single interactive terminal view: opening a pane replaces the desktop preview
  grid instead of showing duplicate output. **Overview** returns to the grid
  without stopping the terminal or agent; selecting another tab/space while
  interacting opens its focused/first pane and preserves drafts.
  The full-color (ANSI) terminal view includes
  - **Prompt agent** (`herdr agent prompt`),
  - **Type + Enter** (`herdr pane run`, atomic — safe for blocked approval prompts),
  - **Type only**, and one-click keys (Enter, Alt-Enter, Esc, Ctrl-C, arrows, y/n, 1/2/3…).
- Launch a new space or tab, optionally starting an agent (`pi`, `claude`, `codex`, …) — without stealing TUI focus.
- Split / close panes, focus a pane in the Herdr TUI, filter (⌘K), `#pane=<id>` deep links.
- **Pull down at the top** of terminal output to load older history. Keep pulling
  again at each new top to go further back; desktop users can scroll upward or
  press Page Up with the output focused. Larger recent snapshots load only on
  demand (400 → 800 → 1,600 → 3,200 → … lines), keeping your reading position.
  While you read history, live polling continues in the background without
  replacing it; **Jump to latest** returns to live output. Herdr offers only
  "last N lines" snapshots (no history cursor), so snapshots are never stitched
  together. If output changes so your position cannot be mapped exactly, the
  current view stays and **Show changed snapshot** is offered. Limits are
  explicit: the 50,000-line and 2,000,000-character bridge response caps, or no
  additional output returned by Herdr. **Herdr 0.9.5 caps each native read at
  1,000 rendered rows**, even if its terminal retains more. Repeated pulls cannot
  bypass that upstream limit; deep history needs an upstream read API change.
  Alternate-screen history availability also depends on Herdr and the agent.
- Select and copy terminal text with the browser's usual drag or touch selection.
  Selected live output stays stable while polling continues; clear the selection
  to display the latest output again. Copying does not send keys to the pane.
- Phone layout (900px wide and below): one focused terminal, hamburger navigation,
  a separate all-agents view, and touch-friendly input. Desktop offers the pane
  overview or one interactive terminal, not both at once.

## Phone UI

- The phone opens the focused pane, or the pane in a `#pane=<id>` deep link.
  It keeps your browser selection when another terminal gains TUI focus.
- **☰** opens the searchable space → tab → pane tree. Choose a pane to return
  to its terminal; choosing a space or tab opens its focused/first pane.
- **Agents** shows agents across every space, sorted **Working → Blocked → Done →
  Idle** (unknown states last), with alphabetical ordering within each state.
  Tap a state count to show only that state; tap the same count again to restore
  all states. Search combines with the selected state, and counts always show
  totals across all agents. An amber indicator means an agent needs attention.
- The message composer and horizontally scrollable terminal keys stay below the
  output. Input drafts are kept separately per pane while the page is open.
  Selecting a pane does not open the phone keyboard or send a command.
- Phone terminal output wraps to the screen, including long paths and unbroken
  lines, while preserving newlines, indentation, and ANSI colors. Desktop output
  keeps its original unwrapped terminal layout.
- Input modes remain **Prompt agent**, **Type + Enter**, and **Type only**.
  Blocked agents default to **Type + Enter** so approval replies reach the terminal.
- **Alt-Enter**, beside Enter, sends the WebUI message text to the selected pane
  and then presses Alt-Enter for agents that support follow-up submission. It
  does not send a bare key when the browser draft is empty. A successful send
  clears only that draft. Like other raw typing, it appends to any existing
  terminal input; use a separate pane if the TUI is being edited elsewhere.
  If delivery fails midway, inspect the terminal before retrying to avoid duplicates.
- **···** contains **Close pane**, **Ctrl-C ×2**, refresh, ANSI color, and the
  explicit **Focus in Herdr** action. Close asks for confirmation and removes
  the focused terminal, killing any agent inside it. To close another pane,
  select it from **☰** or **Agents** first, then choose **Close pane**.
- **Ctrl-C ×2** also appears beside Ctrl-C in the terminal key row. It sends two
  interrupts to the selected pane without closing it; agents that use double
  Ctrl-C to exit can return to the shell. Exit behavior depends on the agent.
- Normal navigation never changes the TUI's focus. Use **New session** in the
  navigation drawer to launch a session.
- On phones, tap the hamburger or Agents button again to close its popup;
  the × buttons, Escape, and navigation backdrop still close it too.
- Terminal output scrolls independently; viewport sizing follows the on-screen
  keyboard. After a service restart, an explicitly rejected stale security token
  is refreshed automatically and the original action is retried once. Other
  errors are not retried; unsuccessful sends keep your text. Refresh the page
  after an update to load new UI features.

## Run

Requires Python 3.9+ and `herdr` on `PATH` with a running server.

```bash
python3 server.py            # http://127.0.0.1:8780
python3 server.py --port 9000
HERDR_BIN=/path/to/herdr HERDR_TIMEOUT=20 python3 server.py
```

For a managed named local session, set `HERDR_SESSION`:

```bash
HERDR_BIN=/path/to/herdr HERDR_SESSION=personal python3 server.py
```

Use the canonical Herdr executable, not a wrapper that always inserts `--session`.
Local calls use the configured session; remote calls use only `--machine` and the
saved profile's remote session. Register and enable SSH machines in Herdr first.
The sidebar's host nodes list saved machines without copying credentials or starting remote
servers. The dashboard cannot assign work automatically or override native agent
approvals. Select a host before creating a space or sending terminal input.
Switching hosts is blocked during command delivery; other read-only navigation
does not change TUI focus. Links include both host and pane for remote selections.

## Private remote access (Tailscale)

With Tailscale already installed and connected, use Serve for tailnet-only HTTPS:

```bash
HOST=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
python3 server.py --allow-host "$HOST"
# In another terminal:
tailscale serve --bg --https=443 http://127.0.0.1:8780
```

On an iPhone connected to the same tailnet, open the HTTPS URL printed by Serve
in Safari. Only devices allowed by your tailnet policy can connect; anyone with
access to this endpoint can read and control your local terminals. Do not enable
Funnel or forward a public port. The backend still listens only on loopback.

### Restrict access to one device

Use `--allow-client-ip` once for each of the device's exact Tailscale IPv4/IPv6
addresses. When configured, **every proxied** page, API read, and mutation
requires an allowed source address. Malformed, duplicate, and unlisted source
addresses are denied. Direct localhost requests without proxy headers remain
accessible for local testing, with the usual Host and CSRF guards.

For an installed macOS service, replace `phone.example.ts.net` below with the
phone's MagicDNS name (listed in `tailscale status --json`):

```bash
(
set -e
# Disable remote serving before changing the restriction.
tailscale serve --https=443 off
python3 - phone.example.ts.net <<'PY'
import json, plistlib, subprocess, sys
from pathlib import Path
peers = json.loads(subprocess.check_output(["tailscale", "status", "--json"]))["Peer"].values()
matches = [p for p in peers if p["DNSName"].rstrip(".") == sys.argv[1].rstrip(".")]
assert len(matches) == 1 and matches[0]["TailscaleIPs"], "Device not uniquely found"
agent = Path.home() / "Library/LaunchAgents/dev.herdr.webui.plist"
config = plistlib.loads(agent.read_bytes())
args = config["ProgramArguments"]
assert "--allow-client-ip" not in args, "Restriction already configured; inspect existing arguments"
for ip in matches[0]["TailscaleIPs"]:
    args.extend(["--allow-client-ip", ip])
agent.write_bytes(plistlib.dumps(config))
PY
launchctl bootout "gui/$(id -u)/dev.herdr.webui"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/dev.herdr.webui.plist"
tailscale serve --bg --https=443 http://127.0.0.1:8780
)
```

If setup fails, remote serving stays disabled; correct the error before retrying.

This mode trusts **only a local Tailscale Serve proxy** that overwrites
`X-Forwarded-For` with the authenticated connection's source IP on every request.
A request without that header is treated as direct localhost traffic. Do not put
an arbitrary proxy in front of it. Remote clients cannot bypass the check by
supplying that header: Serve replaces it. Local processes on the host remain
trusted and could forge proxy headers; this is not isolation from the host
owner. Other tailnet devices receive HTTP 403 rather than the UI. Their ability
to discover the machine or connect to its HTTPS port is unchanged; network-level
invisibility requires separate tailnet policy rules. Re-enrolling the phone can
change its IPs and requires updating the allowlist.

### macOS service

From this repository, create a per-user LaunchAgent using existing Python,
Herdr, and Tailscale installations (no packages are installed):

```bash
python3 - <<'PY'
import json, os, plistlib, shutil, subprocess, sys
from pathlib import Path
home, repo = Path.home(), Path.cwd()
host = json.loads(subprocess.check_output(["tailscale", "status", "--json"]))["Self"]["DNSName"].rstrip(".")
herdr = shutil.which("herdr")
assert herdr and (repo / "server.py").is_file(), "Run from this repository with herdr on PATH"
logs = home / "Library/Logs/herdr-webui"
logs.mkdir(parents=True, exist_ok=True)
agent = home / "Library/LaunchAgents/dev.herdr.webui.plist"
agent.parent.mkdir(parents=True, exist_ok=True)
config = {
    "Label": "dev.herdr.webui",
    "ProgramArguments": [sys.executable, "-u", str(repo / "server.py"), "--allow-host", host],
    "WorkingDirectory": str(repo),
    "EnvironmentVariables": {"HOME": str(home), "PATH": os.environ["PATH"], "HERDR_BIN": herdr,
                             "HERDR_SESSION": os.environ.get("HERDR_SESSION", "")},
    "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 5,
    "StandardOutPath": str(logs / "stdout.log"),
    "StandardErrorPath": str(logs / "stderr.log"),
}
with agent.open("wb") as f:
    plistlib.dump(config, f)
agent.chmod(0o600)
PY
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/dev.herdr.webui.plist"
tailscale serve --bg --https=443 http://127.0.0.1:8780
```

Stop any manually started instance first so port 8780 is free. The service starts
at login and restarts if the bridge exits. Herdr itself must already be running;
the Mac must remain awake. Serve's `--bg` configuration persists across Tailscale
restarts. After changing code, restart with
`launchctl kickstart -k "gui/$(id -u)/dev.herdr.webui"`.

See [SAMPLE_COMMANDS.md](SAMPLE_COMMANDS.md) for checks and troubleshooting.

## Error logs and recovery-page mockup

Every handled HTTP error (including GET failures, bad JSON, CSRF rejection,
parser errors and unsupported methods), unexpected request/server exception,
and detected connection failure writes a structured JSONL error event. Successful
polling is quiet. Events include a timestamp, safe error code/reason, normalized
route, request ID and duration. Responses include `X-Request-ID` for correlation.
Unexpected exceptions record their type and source locations, never exception
messages, source lines or local variables. Herdr failures use classifications
such as `herdr_invalid_json`, `herdr_timeout` and `herdr_exit`; command output is
not copied into the log.

The default file is `~/.local/state/herdr-webui/errors.jsonl` (respects
`XDG_STATE_HOME`). Override with `--log-file PATH` or `HERDR_WEBUI_LOG_FILE`.
Files are owner-only (`0600`), append across restarts, and rotate at 5 MB with
three backups. New log directories are `0700`. An unusable log path prevents
startup; a later disk/write failure emits a safe diagnostic and event to stderr
instead. Keep the service's stderr capture enabled as the fallback.

Browser diagnostics report fetch failures, malformed JSON/responses, CSRF-refresh
failures, resource/script errors and unhandled promise rejections to a protected
local endpoint. Reports contain only allowlisted categories, normalized routes,
HTTP status and an optional related request ID—not raw messages/stacks, full
URLs, drafts, request bodies, headers, credentials, CSRF tokens or terminal output.
The reporter never retries terminal actions. Browser failures cannot reach the
file while the server/network is unreachable; a fixed console warning indicates
that limitation. Abrupt process termination and failures before the browser
reporter loads likewise cannot guarantee an application-log record.

Preview the [landing page and Pi SRE chat mockup](docs/mockups/sre-landing.html)
at `/mockups/sre-landing` on the running bridge. It uses fictional health and chat
responses and makes no service/API requests. It is **not a live watchdog, chat
integration or independent recovery server**. Its incidents, root causes and
approval proposals change with the selected outage. LLM recovery illustrates
Reset to default or the last five user-used models sorted by recency, not a fixed
provider priority.

A separate [model-switching mockup](docs/mockups/model-switch.html) is served at
`/mockups/model-switch`: preview a Change model control in the Herdr pane view,
scoped to the current agent or all compatible agents. Unsupported runtimes are
skipped and working agents defer until idle; all actions are simulated. There
is no live model-switch endpoint in this bridge. The proposed production
companion must run separately so it remains reachable when the bridge fails.
See [mockup notes](docs/mockups/README.md).

## Rename spaces, tabs and panes

Use the **✎ Rename** button beside any space, tab or pane in Workspace navigation
(on desktop or phone). The selected pane also has **Rename pane** in its inspector
header or phone **···** actions. Edit the pre-filled name and confirm; Cancel sends
nothing. Names may contain up to 120 characters, but must be nonempty and contain
no control characters or leading dash. Renaming updates displayed names without
moving focus, interrupting agents, or changing message drafts.

## Native slash commands and models

In an agent pane, typing `/` immediately types it into that pane **without Enter**.
The composer shows the agent's live viewport, including its own command list,
selection highlight and page count—not a hardcoded catalogue. Up/Down forwards
the same key to the agent; Enter executes its selection. Tab completes in the
agent, copies the confirmed prefix into the composer, and leaves room for
parameters. Enter then submits once, without retyping the prefix.

Choose `/model` with Enter to open the agent's native model picker. The WebUI
shows selectable model rows from its live viewport: click/tap a row to select it,
or use Up/Down and Enter even when the composer is not focused. Reopening the
WebUI while the native model picker is open restores these controls. This is single-pane native control, unrelated
to the simulated multi-agent Change model preview above. Commands and models
remain runtime-specific; only the currently visible native page is shown.

Input is serialized per pane. Completion/selection briefly locks editing;
uncertain delivery preserves the draft and stops further forwarding. Inspect
and repair the terminal input before using **Reset after inspecting pane**.
Menus and drafts stay attached to their original pane. Shell panes and ordinary
messages retain their existing delivery modes. Use slash mirroring only when
the agent's editor is ready for input; do not mix it with direct terminal typing.

## Security model

This UI can type into your terminals, so it is deliberately loopback-bound:

- Binds `127.0.0.1` only; rejects non-loopback `Host` headers unless an exact hostname is opted in with `--allow-host` (DNS-rebinding defense).
- Every mutation needs a per-process CSRF token embedded in the served page, plus `Content-Type: application/json`.
- Herdr is invoked with argv lists (never a shell); IDs, key names, labels, and agent kinds are allow-listed.
- Strict CSP (no external origins), `X-Frame-Options: DENY`, `no-store`.
- Agent session file paths from the snapshot are never sent to the browser.
- Socket reads have a 10-second idle timeout; mutation bodies require an
  unambiguous nonnegative Content-Length, reject transfer encodings, and are
  capped at 64 KB.

See [the security review](docs/security-review.md) for verified fixes, CVE
applicability, and residual risks; this is not a public-service security boundary.

Do not bind it to a public or LAN interface. Use the private proxy above or an
SSH tunnel: `ssh -L 8780:127.0.0.1:8780 host`. `--allow-host` does not add
application authentication; use it only behind a trusted private proxy.
`--allow-client-ip` adds an optional exact-device restriction at the HTTP layer,
not a password or a tailnet firewall rule.

## API

| Method | Path | Herdr call |
| --- | --- | --- |
| GET | `/api/tree` | `api snapshot` (normalized, cached 1s) |
| GET | `/api/read?pane=ID&lines=N&ansi=1` | `pane read --source recent-unwrapped` (`source=visible` for native menus) |
| POST | `/api/action/send` `{pane,text,enter}` | `pane run` / `pane send-text` |
| POST | `/api/action/keys` `{pane,keys[]}` | `pane send-keys` |
| POST | `/api/action/followup` `{pane,text}` | `pane send-text`, then `pane send-keys alt+enter` |
| POST | `/api/action/prompt` `{pane,text}` | `agent prompt` |
| POST | `/api/action/focus` `{pane,tab,agent}` | `agent focus` / `tab focus` |
| POST | `/api/action/split` `{pane,direction}` | `pane split --no-focus` |
| POST | `/api/action/close` `{pane}` | `pane close` |
| POST | `/api/action/rename` `{kind,id,label}` | `pane` / `tab` / `workspace rename` (`kind=space` for workspace) |
| POST | `/api/action/spawn` `{workspace?,space_label?,tab_label?,kind?,name?}` | `workspace create` / `tab create` [+ `agent start`] |

## Test

```bash
python3 -m unittest test_server test_error_log -v   # fictional requests; no live terminal commands
```

Optional browser regressions use an **already installed** Playwright Python package
and Google Chrome. They run against fictional sessions on an ephemeral loopback
server. For the dashboard tests, an installed Playwright Chromium can be selected
with `HERDR_WEBUI_BROWSER=chromium`; the tests do not download browsers or send
commands to your real Herdr server:

```bash
python3 -m unittest test_ui test_mockup -v
```

No browser packages are required to run the application. Browser viewport tests
cover responsive layout, but do not replace checking the actual phone keyboard.

## Roadmap

- Multiple hosts via `herdr --machine` (saved SSH machines).
- Event streaming instead of polling.
- Inline typing directly inside pane cards.

## License

MIT
