# UnixDrop

UnixDrop is a small **desk bridge** between a MacBook and a Linux ThinkPad.

The architecture stays simple:

- **Mac Agent** pushes clipboard text, active browser tabs, and dropped files.
- **Linux Receiver** accepts data over HTTP and stores/opens it.

This project does **not** implement mouse/keyboard sharing directly. Use Deskflow (or another software KVM) for HID.

## Purpose

Make both machines feel like one desk:

- Copy text here, paste there.
- Press hotkey, tab opens there.
- Drop file here, file appears there.

## Features

- Shared clipboard bridge with explicit direction modes.
- Active tab send from macOS to Linux.
- Drag/drop style file sending from macOS drop folder to Linux inbox.
- Linux inbox conflict-safe writes.
- Health and status commands.
- Optional Obsidian vault sync (kept separate from desk bridge core).

## Install

### Linux receiver

1. Put this repo on Linux.
2. Create `~/.config/unixdrop/config.json` from `config.example.json`.
3. Install service:

```bash
./scripts/install_linux_service.sh
systemctl --user enable --now unixdrop-receiver.service
```

### macOS agent

1. Put this repo on macOS.
2. Create `~/.config/unixdrop/config.json` from `config.example.json`.
3. Install agent:

```bash
./scripts/install_mac_agent.sh
launchctl load ~/Library/LaunchAgents/com.unixdrop.agent.plist
```

## Commands

Run from repo root:

```bash
./deskbridge up
./deskbridge tab
./deskbridge tab --browser safari
./deskbridge tab --no-open
./deskbridge status
./deskbridge health
./deskbridge tui
./deskbridge drop
./deskbridge drop ~/Downloads/report.pdf
./deskbridge dropzone
./deskbridge clean
./deskbridge deskflow --role server --client-name thinkpad --direction right
./deskbridge deskflow --role client --server-hosts <lan-ip>:24800 --client-name thinkpad
```

TUI keys:

- `q`: quit
- `e`: enter/update Deskflow server endpoints (LAN first, fallback next)
- `d`: start Deskflow using configured start script
- `o`: open the Drop to ThinkPad folder

If Deskflow gets stuck in duplicate client loops (`already connected`), run:

```bash
./deskbridge clean
```

This unloads conflicting autostarts and kills stale Deskflow/Barrier processes.

Compatibility wrappers:

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

Use the included setup script on each machine:

1. On the machine that owns the keyboard/mouse (server):

```bash
./scripts/configure_deskflow.sh --role server --client-name thinkpad --direction right --autostart
```

2. On the other machine (client):

```bash
./scripts/configure_deskflow.sh --role client --server-ip <server-ip> --autostart
```

Equivalent `deskbridge` commands:

```bash
./deskbridge deskflow --role server --client-name thinkpad --direction right --autostart
./deskbridge deskflow --role client --server-ip <server-ip> --client-name thinkpad --autostart
```

Prefer LAN first, Tailscale fallback:

```bash
./scripts/configure_deskflow.sh --role client --server-hosts <lan-ip>:24800,<tailscale-ip>:24800 --autostart
```

Verify each side after setup:

```bash
./scripts/configure_deskflow.sh --role server --verify
./scripts/configure_deskflow.sh --role client --server-ip <server-ip> --verify
```

Notes:

- `--direction` is where the client is positioned relative to the server (`right|left|up|down`).
- `--server-hosts` accepts a comma-separated endpoint list and picks the first reachable endpoint at startup.
- The script writes Deskflow files under `~/.config/deskflow`.
- With `--autostart`, it installs either a user `systemd` service (Linux) or a LaunchAgent (macOS).
- Ensure TCP `24800` is reachable from client to server.

### Integrate Deskflow Into UnixDrop Services

If you want `launchctl`/`systemctl` for UnixDrop to also manage Deskflow startup, set this in each machine's UnixDrop config:

```json
"deskflow": {
  "enabled": true,
  "mac_start_script": "~/.config/deskflow/start-deskflow-server.sh",
  "linux_start_script": "~/.config/deskflow/start-deskflow-client.sh"
}
```

Behavior:

- macOS `unixdrop.mac_agent` starts and supervises `mac_start_script`.
- Linux `unixdrop.linux_service` starts and supervises `linux_start_script`.

## Clipboard Modes

Use `clipboard.mode` (or top-level `clipboard_mode`):

- `off`: disable clipboard sync
- `mac_to_linux`: Mac clipboard pushes to Linux
- `linux_to_mac`: Linux clipboard pulls to Mac
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

## Drop to ThinkPad Workflow

Defaults:

- macOS drop folder: `~/Drop to ThinkPad`
- Linux inbox: `~/Inbox/MacDrop`

Behavior:

- `deskbridge tui` shows a Drop to ThinkPad section with the folder, Linux inbox, pending file count, and last upload result.
- Press `o` in the TUI or run `deskbridge drop` to open the drop folder.
- Run `deskbridge dropzone` for a local browser page with a boxed drag-and-drop target.
- Drag files into the drop folder, or stage files from a terminal:
  ```bash
  ./deskbridge drop ~/Downloads/report.pdf
  ```
- Send a file directly to any running UnixDrop receiver:
  ```bash
  ./deskbridge send ~/Downloads/report.pdf --to http://<receiver-ip>:8765
  ```
- Run a receiver on the other machine when you want files to flow back:
  ```bash
  ./deskbridge receive
  ```
- Create a watched folder on the current machine that sends to another receiver:
  ```bash
  ./deskbridge dropwatch --folder ~/Drop\ to\ Mac --to http://<mac-ip>:8765
  ```
- Mac agent watches drop folder.
- Upload waits until file appears stable (not still writing).
- Max upload size defaults to `500 MB` (`max_file_mb`).
- Linux preserves filename.
- If target exists, Linux writes:
  - `file.txt`
  - `file (conflict YYYY-MM-DD HH-MM-SS).txt`
- Local files are kept by default (`delete_after_send = false`).

## Tab Send Workflow

`deskbridge tab` reads active URL from supported browsers:

- Safari
- Google Chrome
- Arc
- Brave
- Chromium
- Microsoft Edge
- Vivaldi
- Opera

Linux behavior:

- If `auto_open_links=true` and no `--no-open`, URL opens via `xdg-open`.
- Otherwise URL is appended to `~/Inbox/MacDrop/links.md`.

## Status and Health

`deskbridge status` shows:

- macOS agent running
- Linux receiver reachable
- receiver version
- `auto_open_links`
- `clipboard_mode`
- drop folder and inbox paths
- pending drop files
- last upload result
- Obsidian enabled + vault drift summary
- Input Leap/Barrier note

`deskbridge health` checks:

- receiver HTTP reachability
- auth ping endpoint
- clipboard roundtrip (when enabled)
- Linux inbox write test
- `xdg-open` availability on Linux
- macOS browser script availability
- drop folder existence
- launchd/systemd status checks (when available)

## Obsidian Sync

Obsidian is supported but separate from core desk bridge ergonomics.

Use the `obsidian` section:

- `enabled`
- `local_vault`
- `remote_vault`
- `conflict_strategy` (currently `copy`)

Current behavior remains intentionally simple:

- bidirectional sync via Linux receiver
- conflict copies on Mac
- excludes cache/workspace paths
- no delete propagation

## Software KVM

Mouse/keyboard sharing is external to UnixDrop.

Recommended setup:

- install Deskflow (or Input Leap)
- MacBook as server
- ThinkPad as client
- keep UnixDrop focused on clipboard/tab/file workflows

## Safety Notes

- No file execution on receive.
- No automatic opening of received files.
- Only URLs can auto-open (via `xdg-open`) when enabled.
- File uploads are size-limited and filename-sanitized.

-- Todo

extend the support for firefox
Drag and drop
