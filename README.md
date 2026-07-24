# UnixDrop

UnixDrop is a small **desk bridge** between two macOS/Linux machines.

The architecture stays simple:

- **UnixDrop Node** runs on both machines.
- Each node accepts incoming files/links/clipboard over HTTP and watches a local drop folder to send to its peer.
- Deskflow server/client roles are configured separately, so either machine can own the keyboard/mouse.

This project does **not** implement mouse/keyboard sharing directly. Use Deskflow (or another software KVM) for HID.

## Purpose

Make both machines feel like one desk:

- Copy text here, paste there.
- Press hotkey, tab opens there.
- Drop file here, file appears there.

## Features

- Shared clipboard bridge with explicit direction modes.
- Active macOS tab send plus explicit URL send from either machine.
- Drag/drop style file sending from either machine to its configured peer.
- Inbox conflict-safe writes on either receiver.
- Health and status commands.
- Optional Obsidian vault sync (kept separate from desk bridge core).

## Install

### Package install

UnixDrop is a Python CLI package. Install it on both machines:

```bash
pipx install .
```

For development from a checkout:

```bash
python3 -m pip install -e .
```

Create the first config on each machine:

```bash
deskbridge init
```

For a guided first-run pass that creates or updates config, attempts LAN discovery, probes the peer receiver, and prints the next commands:

```bash
deskbridge setup
deskbridge setup --peer-url http://192.168.1.50:8765 --clipboard two_way --role client --autostart
```

Edit `~/.config/unixdrop/config.json` on each machine:

- Set the same `auth_token` on both machines.
- Set `receiver_url` to the other machine, for example `http://192.168.1.50:8765`.
- Adjust `inbox_dir` and `drop_dir` if you want different folders.

Start or refresh the local background service:

```bash
deskbridge up
```

On Linux this writes a user systemd service and runs `systemctl --user enable --now unixdrop-receiver.service`.
On macOS this writes `~/Library/LaunchAgents/com.unixdrop.agent.plist` and loads it with `launchctl`.

Host tools still come from the operating system: Deskflow for mouse/keyboard sharing, `pbcopy`/`pbpaste` on macOS, and one of `wl-copy`/`wl-paste`, `xclip`, or `xsel` for Linux clipboard support.

## Development

Run the test suite:

```bash
make test
```

Run the full local CI gate:

```bash
make ci
```

Run an installed-package smoke test:

```bash
make package-smoke
```

Clean generated build/test artifacts:

```bash
make clean-artifacts
```

Local runtime files stay out of version control. Keep real configs, inbox contents, link logs, backup configs, build directories, and Python cache files local; use `config.example.json` for committed config examples.

## Commands

After install:

```bash
deskbridge init
deskbridge setup
deskbridge up
deskbridge tab
deskbridge tab --browser safari
deskbridge tab --browser firefox
deskbridge tab --browser firefox --firefox-debug-url http://127.0.0.1:9222
deskbridge tab --no-open
deskbridge url https://example.com
deskbridge status
deskbridge status --json
deskbridge health
deskbridge health --json
deskbridge doctor
deskbridge doctor --json
deskbridge tui
deskbridge drop
deskbridge drop ~/Downloads/report.pdf
deskbridge dropzone
deskbridge clean
deskbridge deskflow --role server --client-name peer-laptop --direction right
deskbridge deskflow --role client --client-name peer-laptop
```

The checked-out source tree still includes `./deskbridge` and `./scripts/*.sh` wrappers for compatibility.

TUI keys:

- On first interactive start, `deskbridge tui` opens a small startup setup prompt for the peer IP/host and Deskflow screen name. Once the peer receiver, role, and start script are saved, later starts skip the prompt and go straight to the dashboard.
- `s`: quick setup (Mac server, Linux client, automatic discovery, two-way clipboard)
- `q`: close only the dashboard; background services keep running
- `x`: stop all UnixDrop, Deskflow, discovery, and receiver background processes
- `e`: enter/update Deskflow server endpoints; blank input uses LAN discovery only
- `d`: start Deskflow using configured start script
- `r`: reverse Deskflow role using the opposite configured start script
- `o`: open the local drop folder

If Deskflow gets stuck in duplicate client loops (`already connected`), run:

```bash
deskbridge clean
```

This unloads conflicting autostarts and kills stale Deskflow/Barrier processes.

Source checkout compatibility wrappers:

```bash
./scripts/send_current_tab.sh
./scripts/status.sh
./scripts/health.sh
./scripts/run_mac_agent.sh
./scripts/run_linux_receiver.sh
```

## Install Deskflow (Free)

Deskflow is free and open source.

### macOS

Recommended (Homebrew):

```bash
brew tap deskflow/tap
brew install deskflow
```

Optional continuous build:

```bash
brew install deskflow-dev
```

If you downloaded `.app` from releases and macOS blocks launch:

```bash
xattr -c /Applications/Deskflow.app
```

### Linux

Recommended fallback across distros (Flatpak):

```bash
flatpak install flathub org.deskflow.deskflow
```

You can also install distro-native packages or direct release assets from:

- https://github.com/deskflow/deskflow/releases
- https://flathub.org/apps/org.deskflow.deskflow

## Deskflow Keyboard/Mouse Setup

Use the installed command on each machine:

1. On the machine that owns the keyboard/mouse (server):

```bash
deskbridge deskflow --role server --client-name peer-laptop --direction right --autostart
```

2. On the other machine (client):

```bash
deskbridge deskflow --role client --autostart
```

The source checkout also includes an equivalent helper script:

```bash
./scripts/configure_deskflow.sh --role server --client-name peer-laptop --direction right --autostart
./scripts/configure_deskflow.sh --role client --autostart
```

No IP address is stored in the default setup. The server answers UnixDrop LAN discovery on UDP 24801; the client discovers it at every start and caches the last working address. DHCP address changes therefore require no reconfiguration.

For networks that block broadcast/multicast discovery, fixed LAN/Tailscale fallbacks remain available:

```bash
deskbridge deskflow --role client --server-hosts <lan-ip>:24800,<tailscale-ip>:24800 --autostart
```

Verify each side after setup:

```bash
deskbridge deskflow --role server --verify
deskbridge deskflow --role client --verify
```

Notes:

- `--direction` is where the client is positioned relative to the server (`right|left|up|down`).
- Client setup uses automatic LAN discovery when neither `--server-ip` nor `--server-hosts` is supplied.
- `--server-hosts` accepts a comma-separated manual endpoint list and picks the first reachable endpoint at startup.
- The setup command writes Deskflow files under `~/.config/deskflow`.
- With `--autostart`, it installs either a user `systemd` service (Linux) or a LaunchAgent (macOS).
- Ensure TCP `24800` and UDP `24801` are reachable from client to server.

### Integrate Deskflow Into UnixDrop Services

If you want `launchctl`/`systemctl` for UnixDrop to also manage Deskflow startup, set this in each machine's UnixDrop config:

```json
"deskflow": {
  "role": "server",
  "server_start_script": "~/.config/deskflow/start-deskflow-server.sh",
  "client_start_script": "~/.config/deskflow/start-deskflow-client.sh"
}
```

Use `"role": "client"` on the Deskflow client and `"role": "off"` to let UnixDrop ignore Deskflow.

Backward-compatible keys still load:

- `deskflow.enabled`
- `deskflow.mac_start_script`
- `deskflow.linux_start_script`

## Clipboard Modes

Use `clipboard.mode` (or top-level `clipboard_mode`):

- `off`: disable clipboard sync
- `mac_to_linux`: local sender push behavior from the original Mac-to-Linux workflow
- `linux_to_mac`: peer pull behavior from the original Linux-to-Mac workflow
- `two_way`: both directions with loop protection

Rules:

- plain text only
- `max_chars`/`max_clipboard_chars` limit (default `20000`)
- large clipboard payloads are ignored

### Backward compatibility

Old keys still work:

- `clipboard_sync_enabled`
- `shared_clipboard_enabled`

They are mapped to `clipboard_mode` with a deprecation warning.

## Drop to Peer Workflow

Defaults:

- starter config drop folder: `~/UnixDrop/Drop`
- starter config inbox: `~/UnixDrop/Inbox`

You can override these per machine in config, for example `~/Drop to Peer` and `~/UnixDrop/Inbox`.

Behavior:

- `deskbridge tui` shows the local drop folder, local inbox, pending file count, and last upload result.
- Press `o` in the TUI or run `deskbridge drop` to open the drop folder.
- Run `deskbridge dropzone` for a local browser page with a boxed drag-and-drop target.
- Drag files into the drop folder, or stage files from a terminal:
  ```bash
  deskbridge drop ~/Downloads/report.pdf
  ```
- Send a file directly to any running UnixDrop receiver:
  ```bash
  deskbridge send ~/Downloads/report.pdf --to http://<receiver-ip>:8765
  ```
- Run a receiver manually on a machine:
  ```bash
  deskbridge receive
  ```
- Create a watched folder on the current machine that sends to another receiver:
  ```bash
  deskbridge dropwatch --folder ~/Drop\ to\ Peer --to http://<peer-ip>:8765
  ```
- The UnixDrop node watches the configured drop folder.
- Upload waits until file appears stable (not still writing).
- Max upload size defaults to `500 MB` (`max_file_mb`).
- The receiver preserves filename.
- If target exists, the receiver writes:
  - `file.txt`
  - `file (conflict YYYY-MM-DD HH-MM-SS).txt`
- Local files are kept by default (`delete_after_send = false`).

## Tab Send Workflow

`deskbridge tab` reads the active URL from supported browser sources:

- Safari
- Google Chrome
- Arc
- Brave
- Chromium
- Microsoft Edge
- Firefox (requires opt-in debug endpoint)
- Firefox Developer Edition (requires opt-in debug endpoint)
- LibreWolf (requires opt-in debug endpoint)
- Vivaldi
- Opera

Firefox-family browsers do not expose the active tab through the same AppleScript API as Safari/Chromium browsers. UnixDrop supports them through a non-intrusive local debug endpoint and will fail instead of guessing if multiple tabs are exposed without an active marker.

Start Firefox with a local debugging port before using `deskbridge tab --browser firefox`:

```bash
firefox --remote-debugging-port 9222
```

On macOS, if `firefox` is not on `PATH`, run the app binary directly:

```bash
/Applications/Firefox.app/Contents/MacOS/firefox --remote-debugging-port 9222
```

The default endpoint is `http://127.0.0.1:9222`. Override it in config:

```json
"tabs": {
  "default_browser": "firefox",
  "firefox_debug_url": "http://127.0.0.1:9222"
}
```

Or per command:

```bash
deskbridge tab --browser firefox --firefox-debug-url http://127.0.0.1:9222
```

For either machine, send an explicit URL:

```bash
deskbridge url https://example.com
deskbridge url https://example.com --to http://<peer-ip>:8765
```

Receiver behavior:

- If `auto_open_links=true` and no `--no-open`, URL opens via `open` on macOS or `xdg-open` on Linux.
- Otherwise URL is appended to `links.md` in the configured inbox.

## Status and Health

`deskbridge status` shows:

- local node service status
- peer receiver reachability
- peer receiver version
- `auto_open_links`
- `clipboard_mode`
- local drop folder and inbox paths
- pending drop files
- last upload result
- Obsidian enabled + vault drift summary
- Deskflow management note

`deskbridge health` checks:

- peer HTTP reachability
- auth ping endpoint
- clipboard roundtrip (when enabled)
- peer inbox write test
- peer link opener availability
- local active-tab script availability
- drop folder existence
- launchd/systemd status checks (when available)

`deskbridge doctor` is read-only and checks local portability prerequisites:

- Python executable used by generated services
- config file/load status
- launchd or systemd availability
- link opener and clipboard tools
- Deskflow binary detection
- Firefox debug endpoint when configured

## Obsidian Sync

Obsidian is supported but separate from core desk bridge ergonomics.

Use the `obsidian` section:

- `enabled`
- `local_vault`
- `remote_vault`
- `conflict_strategy` (currently `copy`)

Current behavior remains intentionally simple:

- bidirectional sync via the configured peer receiver
- conflict copies on the local sender
- excludes cache/workspace paths
- no delete propagation

## Software KVM

Mouse/keyboard sharing is external to UnixDrop.

Recommended setup:

- install Deskflow (or Input Leap)
- choose the machine with the active keyboard/mouse as the Deskflow server
- configure the other machine as the Deskflow client
- set matching `deskflow.role` values in each UnixDrop config if the node service should supervise Deskflow
- keep UnixDrop focused on clipboard/tab/file workflows

## Safety Notes

- No file execution on receive.
- No automatic opening of received files.
- Only URLs can auto-open (`open` on macOS, `xdg-open` on Linux) when enabled.
- File uploads are size-limited and filename-sanitized.

## Release Checklist

- Run `make test`.
- Run `make package-smoke`.
- Run `make clean-artifacts`.
- Run `deskbridge doctor` on one macOS node and one Linux node after install.
- Confirm `git status --short` shows only intentional source, docs, and test changes.
- Install from the release artifact or checkout on one macOS node and one Linux node before tagging.
