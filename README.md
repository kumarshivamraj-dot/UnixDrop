# UnixDrop

UnixDrop is a small cross-device sync tool for a macOS sender and a Linux receiver on the same Tailscale network.

It is built for this flow:

- You are browsing on your Mac.
- A link is copied, or a file is dropped into a sync folder.
- The Linux machine receives it over Tailscale.
- Links can be opened automatically on Linux.
- Files are stored in a local inbox on Linux.
- An Obsidian vault can be synced between both machines through the Linux receiver.

## What This MVP Does

- Runs a Linux user service that exposes a small HTTP receiver.
- Runs a macOS user agent that:
  - polls the clipboard for URLs
  - watches a local folder for files to upload
  - syncs an Obsidian vault against Linux if enabled
- Authenticates requests with a shared token.
- Stores received files and a link log on Linux.

## Layout

- `unixdrop/linux_service.py`: Linux receiver service
- `unixdrop/mac_agent.py`: macOS sender agent
- `unixdrop/config.py`: shared config loader
- `systemd/unixdrop-receiver.service`: Linux user service template
- `launchd/com.unixdrop.agent.plist`: macOS LaunchAgent template
- `scripts/install_linux_service.sh`: installs the Linux user service
- `scripts/install_mac_agent.sh`: installs the macOS LaunchAgent

## Config

Copy `config.example.json` to:

- macOS: `~/.config/unixdrop/config.json`
- Linux: `~/.config/unixdrop/config.json`

Then adjust the values for each machine.

The important values:

- `auth_token`: same on both machines
- `receiver_url`: on macOS, set this to the Linux Tailscale URL, for example `http://100.x.y.z:8765`
- `inbox_dir`: where Linux stores received files
- `sync_dir`: where macOS watches for files to send
- `obsidian_enabled`: turn vault sync on or off
- `obsidian_vault_dir`: path to the same Obsidian vault on each machine
- `obsidian_poll_seconds`: how often the Mac checks and syncs the vault

## Install

### Linux receiver

1. Put the project on the Linux machine.
2. Create `~/.config/unixdrop/config.json`.
3. Run:

```bash
./scripts/install_linux_service.sh
systemctl --user enable --now unixdrop-receiver.service
```

### macOS agent

1. Put the project on the Mac.
2. Create `~/.config/unixdrop/config.json`.
3. Run:

```bash
./scripts/install_mac_agent.sh
launchctl load ~/Library/LaunchAgents/com.unixdrop.agent.plist
```

## Usage

### Sync a link

- Copy a URL on macOS. The agent will notice it and send it.
- On Linux, the service can automatically run `xdg-open` if `auto_open_links` is `true`.
- If you want to push the active Safari, Chrome, or Arc tab directly, run:

```bash
python3 -m unixdrop.send_browser_url
```

### Sync a file

- Drop a file into the configured macOS `sync_dir`.
- The agent uploads it to Linux.
- The file appears in the configured Linux `inbox_dir`.

### Sync an Obsidian vault

- Set `obsidian_enabled` to `true` on both machines.
- Point `obsidian_vault_dir` to your vault path on each machine.
- Keep the Linux receiver running and the macOS agent loaded.
- Mac changes are pushed to Linux.
- Linux changes are pulled back to Mac on the next poll.

This is bidirectional through Linux as the hub. It is much better than Git for active note edits, but it still keeps the logic intentionally simple:

- no delete propagation yet
- conflicts create a `*.linux-conflict-xxxxxxxx.md` style sibling copy on Mac
- workspace and cache files are excluded by default

### Check sync status

Run this on the Mac:

```bash
python3 -m unixdrop.status
```

It shows:

- whether the Linux receiver is reachable
- whether Obsidian sync is enabled
- local vs remote vault file counts
- mismatched files and one-sided files
- when the local sync state was last written

## Notes

- This is intentionally simple and avoids external Python dependencies.
- The Linux receiver is a user service, not a system service, because opening links should happen in your desktop session.
- Clipboard-based URL sync is the most reliable always-on approach without building a browser extension.
- For Obsidian, this is viable for a personal two-device setup. If you later want delete propagation, rename tracking, or more than two peers, you should switch that part to Syncthing instead of growing custom sync logic forever.
