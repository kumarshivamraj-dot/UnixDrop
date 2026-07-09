from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import plistlib
import shutil
import secrets
import subprocess
import sys
import time
import threading
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request
from urllib.parse import unquote, urlparse

from unixdrop.config import DEFAULT_CONFIG_PATH, load_config
from unixdrop.http_transfer import post_file


def _run_command(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 127, f"command not found: {command[0]}"

    detail = result.stderr.strip() or result.stdout.strip() or "ok"
    return result.returncode, detail


def _result_ok(code: int, detail: str) -> bool:
    lowered = detail.lower()
    return code == 0 and "failed" not in lowered and "error" not in lowered


def _cmd_clean(_: argparse.Namespace) -> int:
    print("Deskbridge clean")

    if sys.platform not in {"darwin"} and not sys.platform.startswith("linux"):
        raise SystemExit(f"unsupported platform: {sys.platform}")

    permission_blocked = False

    if sys.platform == "darwin":
        uid = str(os.getuid())
        candidate_plists: list[Path] = []
        for launch_agents_dir in (
            Path("~/Library/LaunchAgents").expanduser(),
            Path("/Library/LaunchAgents"),
        ):
            if not launch_agents_dir.exists():
                continue
            for plist_path in launch_agents_dir.glob("*.plist"):
                lower_name = plist_path.name.lower()
                if any(token in lower_name for token in ("unixdrop", "deskflow", "barrier", "synergy")):
                    candidate_plists.append(plist_path)

        launch_labels: set[str] = {
            "com.unixdrop.agent",
            "com.unixdrop.deskflow.server",
            "com.unixdrop.deskflow.client",
        }
        for plist_path in candidate_plists:
            try:
                with plist_path.open("rb") as handle:
                    payload = plistlib.load(handle)
                label = str(payload.get("Label", "")).strip()
            except Exception:
                label = ""
            if label:
                launch_labels.add(label)

            code, detail = _run_command(["launchctl", "bootout", f"gui/{uid}", str(plist_path)])
            status = "ok" if _result_ok(code, detail) else "warn"
            print(f"[{status}] launchctl bootout gui/{uid} {plist_path.name}: {detail}")
            if "not permitted" in detail.lower() or "input/output error" in detail.lower():
                permission_blocked = True

        for label in sorted(launch_labels):
            code, detail = _run_command(["launchctl", "bootout", f"gui/{uid}/{label}"])
            if _result_ok(code, detail):
                print(f"[ok] launchctl bootout gui/{uid}/{label}")
            else:
                print(f"[warn] launchctl bootout gui/{uid}/{label}: {detail}")
                if "not permitted" in detail.lower() or "input/output error" in detail.lower():
                    permission_blocked = True
                rm_code, rm_detail = _run_command(["launchctl", "remove", label])
                rm_status = "ok" if _result_ok(rm_code, rm_detail) else "warn"
                print(f"[{rm_status}] launchctl remove {label}: {rm_detail}")

    if shutil.which("systemctl"):
        for service_name in (
            "deskflow-server.service",
            "deskflow-client.service",
            "unixdrop-receiver.service",
        ):
            code, detail = _run_command(["systemctl", "--user", "disable", "--now", service_name])
            status = "ok" if code == 0 else "warn"
            print(f"[{status}] systemctl disable --now {service_name}: {detail}")
    else:
        print("[skip] systemctl not available on this machine")

    pkill_hard_failed = False
    if shutil.which("pkill"):
        patterns = (
            "deskflow-server",
            "deskflow-client",
            "deskflow-core.*server",
            "deskflow-core.*client",
            "barriers",
            "barrierc",
            "barrier",
        )
        for pattern in patterns:
            code, detail = _run_command(["pkill", "-f", pattern])
            if code == 0:
                print(f"[ok] stopped processes matching: {pattern}")
            elif code == 1:
                print(f"[ok] no process matched: {pattern}")
            else:
                pkill_hard_failed = True
                print(f"[warn] pkill -f {pattern}: {detail}")
    else:
        print("[warn] pkill not found; could not terminate stale processes")

    if pkill_hard_failed and shutil.which("killall"):
        for process_name in (
            "deskflow-server",
            "deskflow-client",
            "deskflow-core",
            "barriers",
            "barrierc",
            "barrier",
            "synergys",
            "synergyc",
            "synergy",
        ):
            code, detail = _run_command(["killall", process_name])
            if code == 0:
                print(f"[ok] killall stopped: {process_name}")
            elif code == 1:
                print(f"[ok] killall no match: {process_name}")
            else:
                print(f"[warn] killall {process_name}: {detail}")

    if shutil.which("lsof"):
        pid_code, pid_detail = _run_command(["lsof", "-t", "-nP", "-iTCP:24800", "-sTCP:LISTEN"])
        listener_pids = sorted(
            {line.strip() for line in pid_detail.splitlines() if line.strip().isdigit()}
        ) if pid_code == 0 else []
        if listener_pids:
            print(f"[warn] forcing stop for listeners on 24800: {', '.join(listener_pids)}")
            for pid in listener_pids:
                term_code, term_detail = _run_command(["kill", "-TERM", pid])
                term_status = "ok" if term_code == 0 else "warn"
                print(f"[{term_status}] kill -TERM {pid}: {term_detail}")
                if "not permitted" in term_detail.lower():
                    permission_blocked = True
            time.sleep(0.2)
            for pid in listener_pids:
                kill_code, kill_detail = _run_command(["kill", "-KILL", pid])
                kill_status = "ok" if kill_code == 0 else "warn"
                print(f"[{kill_status}] kill -KILL {pid}: {kill_detail}")
                if "not permitted" in kill_detail.lower():
                    permission_blocked = True

        code, detail = _run_command(["lsof", "-nP", "-iTCP:24800", "-sTCP:LISTEN"])
        if code == 0:
            print("[warn] port 24800 still in use:")
            print(detail)
        else:
            print("[ok] port 24800 is free")
    else:
        print("[skip] lsof not available; could not verify port 24800")

    if permission_blocked:
        print("[warn] cleanup hit permission barriers; run elevated fallback:")
        print("  sudo launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.unixdrop.deskflow.server.plist 2>/dev/null || true")
        print("  sudo launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.unixdrop.deskflow.client.plist 2>/dev/null || true")
        print("  sudo launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.unixdrop.agent.plist 2>/dev/null || true")
        print("  sudo killall deskflow-core deskflow-server deskflow-client barriers barrierc barrier synergy synergys synergyc 2>/dev/null || true")

    print("Cleanup complete. Start only one Deskflow manager path.")
    return 0


def _cmd_tab(args: argparse.Namespace) -> int:
    from unixdrop.send_browser_url import current_browser_context, is_supported_web_url, send_url

    app_name, url = current_browser_context(args.browser, firefox_debug_url=getattr(args, "firefox_debug_url", None))
    if not url:
        raise SystemExit("no active browser url found in supported running browsers")
    if not is_supported_web_url(url):
        label = app_name or "browser"
        raise SystemExit(f"{label} returned a non-web URL: {url}")
    send_url(url, no_open=args.no_open, source="mac-browser-helper")
    print(url)
    return 0


def _cmd_url(args: argparse.Namespace) -> int:
    from unixdrop.send_browser_url import is_supported_web_url, send_url

    cfg = load_config()
    url = str(args.url).strip()
    if not is_supported_web_url(url):
        raise SystemExit(f"unsupported URL: {url}")
    send_url(
        url,
        no_open=args.no_open,
        receiver_url=args.to or cfg.receiver_url,
        auth_token=cfg.auth_token,
        timeout_seconds=cfg.request_timeout_seconds,
        source="deskbridge-url",
    )
    print(url)
    return 0


def _default_config_payload() -> dict:
    inbox_dir = "~/UnixDrop/Inbox"
    drop_dir = "~/UnixDrop/Drop"
    return {
        "auth_token": secrets.token_urlsafe(32),
        "receiver_url": "http://127.0.0.1:8765",
        "receiver": {
            "listen_host": "0.0.0.0",
            "port": 8765,
            "auto_open_links": True,
        },
        "inbox_dir": inbox_dir,
        "drop_dir": drop_dir,
        "link_log_path": f"{inbox_dir}/link-log.jsonl",
        "state_dir": "~/.local/state/unixdrop",
        "drop": {
            "delete_after_send": False,
            "max_file_mb": 500,
        },
        "clipboard": {
            "mode": "off",
            "max_chars": 20000,
        },
        "tabs": {
            "default_browser": "auto",
            "firefox_debug_url": "http://127.0.0.1:9222",
        },
        "deskflow": {
            "role": "off",
            "server_start_script": "~/.config/deskflow/start-deskflow-server.sh",
            "client_start_script": "~/.config/deskflow/start-deskflow-client.sh",
        },
        "obsidian": {
            "enabled": False,
            "local_vault": "~/Obsidian/MainVault",
            "remote_vault": "",
            "conflict_strategy": "copy",
        },
    }


def _write_initial_config(path: Path, *, force: bool = False) -> bool:
    path = path.expanduser()
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_default_config_payload(), indent=2) + "\n", encoding="utf-8")
    return True


def _cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser() if args.config else DEFAULT_CONFIG_PATH
    created = _write_initial_config(path, force=args.force)
    if created:
        print(f"Created UnixDrop config: {path}")
        print("Edit receiver_url on each machine so it points at the peer receiver.")
        return 0
    print(f"Config already exists: {path}")
    print("Use --force to overwrite it.")
    return 1


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _normalized_receiver_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("empty peer receiver URL")
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid peer receiver URL: {value}")
    try:
        port = parsed.port or 8765
    except ValueError as exc:
        raise ValueError(f"invalid peer receiver URL: {exc}") from None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{port}"


def _discover_receiver_url(timeout_seconds: float) -> tuple[str | None, str]:
    try:
        from unixdrop.discovery import discover

        result = discover(timeout_seconds)
    except Exception as exc:
        return None, f"discovery failed: {exc}"
    if result is None:
        return None, "no peer discovered on LAN"
    host, payload = result
    name = str(payload.get("name", "peer") or "peer")
    return f"http://{host}:8765", f"discovered {name} at {host}; inferred UnixDrop receiver port 8765"


def _probe_receiver(receiver_url: str, timeout_seconds: int = 2) -> tuple[bool, str]:
    try:
        req = request.Request(receiver_url.rstrip("/") + "/health")
        with request.urlopen(req, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return False, str(exc)
    if payload.get("ok"):
        host = payload.get("hostname", "peer")
        version = payload.get("version", "unknown")
        return True, f"reachable ({host}, version {version})"
    return False, "health endpoint returned unexpected payload"


def _cmd_setup(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser() if args.config else DEFAULT_CONFIG_PATH
    created = False
    if path.exists() and not args.force:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"could not read existing config {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SystemExit(f"existing config is not a JSON object: {path}")
    else:
        payload = _default_config_payload()
        created = True

    if args.auth_token:
        payload["auth_token"] = args.auth_token

    receiver_url = args.peer_url
    discovery_detail = "discovery skipped"
    if not receiver_url and args.discover:
        receiver_url, discovery_detail = _discover_receiver_url(args.discovery_timeout)
    if receiver_url:
        normalized = _normalized_receiver_url(receiver_url)
        payload["receiver_url"] = normalized

    if args.clipboard:
        clipboard = payload.get("clipboard") if isinstance(payload.get("clipboard"), dict) else {}
        clipboard["mode"] = args.clipboard
        payload["clipboard"] = clipboard

    if args.role:
        deskflow = payload.get("deskflow") if isinstance(payload.get("deskflow"), dict) else {}
        deskflow["enabled"] = args.role != "off"
        deskflow["role"] = args.role
        payload["deskflow"] = deskflow

    _write_json_atomic(path, payload)
    cfg = load_config(path)
    reachable, reachability = _probe_receiver(cfg.receiver_url)

    print("Deskbridge setup")
    print(f"Config: {'created' if created else 'updated'} {path}")
    print(f"Peer receiver: {cfg.receiver_url}")
    if not args.peer_url:
        print(f"Discovery: {discovery_detail}")
    print(f"Peer health: {'ok' if reachable else 'warn'} - {reachability}")
    print("")
    print("Next commands:")
    print("  deskbridge up")
    if cfg.deskflow_role == "server":
        client_name = args.client_name or "peer-laptop"
        direction = args.direction or "right"
        autostart = " --autostart" if args.autostart else ""
        print(f"  deskbridge deskflow --role server --client-name {client_name} --direction {direction}{autostart}")
        print(f"  On the peer: deskbridge deskflow --role client --client-name {client_name}{autostart}")
    elif cfg.deskflow_role == "client":
        autostart = " --autostart" if args.autostart else ""
        client_name = f" --client-name {args.client_name}" if args.client_name else ""
        print(f"  deskbridge deskflow --role client{client_name}{autostart}")
        print("  On the keyboard/mouse machine: deskbridge deskflow --role server --client-name <this-machine-name>")
    print("  deskbridge doctor")
    print("  deskbridge health")
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    from unixdrop.status import status_lines, status_report

    if getattr(_, "json", False):
        print(json.dumps(status_report(), indent=2, sort_keys=True))
    else:
        for line in status_lines():
            print(line)
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    from unixdrop.health import health_lines, health_report

    if args.json:
        print(json.dumps(health_report(), indent=2, sort_keys=True))
    else:
        for line in health_lines():
            print(line)
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from unixdrop.doctor import doctor_checks, doctor_exit_code, doctor_report

    config_path = Path(args.config).expanduser() if args.config else None
    if args.json:
        report = doctor_report(config_path)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    checks = doctor_checks(config_path)
    print("Deskbridge doctor")
    for check in checks:
        print(check.line())
    return doctor_exit_code(checks)


def _cmd_tui(args: argparse.Namespace) -> int:
    from unixdrop.tui import run_tui

    return run_tui(interval_seconds=args.interval, once=args.once)


def _drop_destination(drop_dir: Path, source: Path) -> Path:
    destination = drop_dir / source.name
    if not destination.exists():
        return destination

    timestamp = time.strftime("%Y-%m-%d %H-%M-%S")
    return destination.with_name(f"{destination.stem} (drop {timestamp}){destination.suffix}")


def _open_drop_folder(drop_dir: Path) -> tuple[bool, str]:
    if sys.platform == "darwin":
        command = ["open", str(drop_dir)]
    elif sys.platform.startswith("linux"):
        opener = shutil.which("xdg-open")
        if not opener:
            return False, "xdg-open not found"
        command = [opener, str(drop_dir)]
    else:
        return False, f"unsupported platform: {sys.platform}"

    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        return False, str(exc)
    return True, str(drop_dir)


def _stage_drop_files(paths: list[str], drop_dir: Path) -> list[Path]:
    drop_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    for raw_path in paths:
        source = Path(raw_path).expanduser()
        if not source.exists():
            raise SystemExit(f"drop source not found: {source}")
        if not source.is_file():
            raise SystemExit(f"drop source must be a file: {source}")
        destination = _drop_destination(drop_dir, source)
        shutil.copy2(source, destination)
        staged.append(destination)
    return staged


def _cmd_drop(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.drop_dir.mkdir(parents=True, exist_ok=True)

    staged = _stage_drop_files(args.files, cfg.drop_dir) if args.files else []
    if staged:
        for destination in staged:
            print(f"staged for peer: {destination}")
        print("UnixDrop node will transfer staged files to the peer inbox.")

    if args.open or not staged:
        opened, detail = _open_drop_folder(cfg.drop_dir)
        if opened:
            print(f"drop folder: {detail}")
        else:
            print(f"drop folder: {cfg.drop_dir}")
            print(f"open folder skipped: {detail}")

    if not staged:
        print("Drag files into this folder; UnixDrop will transfer them to the peer.")
    return 0


def _send_file_to_receiver(file_path: Path, receiver_url: str, auth_token: str, timeout_seconds: int) -> dict:
    if not file_path.exists():
        raise FileNotFoundError(f"send source not found: {file_path}")
    if not file_path.is_file():
        raise ValueError(f"send source must be a file: {file_path}")

    return post_file(
        url=receiver_url.rstrip("/") + "/api/file",
        file_path=file_path,
        timeout_seconds=timeout_seconds,
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/octet-stream",
            "X-Filename": file_path.name,
        },
    )


def _file_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_dropwatch_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"files": {}, "pending": {}}
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"files": {}, "pending": {}}
    return loaded if isinstance(loaded, dict) else {"files": {}, "pending": {}}


def _save_dropwatch_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _sync_dropwatch_once(
    *,
    folder: Path,
    receiver_url: str,
    auth_token: str,
    timeout_seconds: int,
    max_file_mb: int,
    delete_after_send: bool,
    state: dict,
) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    tracked = state.setdefault("files", {})
    pending = state.setdefault("pending", {})
    max_bytes = max_file_mb * 1024 * 1024
    uploaded = 0
    now = time.time()
    existing_names: set[str] = set()

    for file_path in sorted(folder.iterdir()):
        if not file_path.is_file():
            continue

        existing_names.add(file_path.name)
        stat = file_path.stat()
        size = stat.st_size
        mtime = stat.st_mtime

        if size > max_bytes:
            state["last_result"] = f"skipped {file_path.name}: exceeds max_file_mb={max_file_mb}"
            pending.pop(file_path.name, None)
            print(state["last_result"])
            continue

        previous = pending.get(file_path.name)
        if not previous or previous.get("size") != size or previous.get("mtime") != mtime:
            pending[file_path.name] = {"size": size, "mtime": mtime, "stable_checks": 0}
            continue

        previous["stable_checks"] = int(previous.get("stable_checks", 0)) + 1
        if previous["stable_checks"] < 1 or now - mtime < 1:
            continue

        digest = _file_sha256(file_path)
        if tracked.get(file_path.name) == digest:
            continue

        response = _send_file_to_receiver(file_path, receiver_url, auth_token, timeout_seconds)
        tracked[file_path.name] = digest
        pending.pop(file_path.name, None)
        uploaded += 1

        remote_path = response.get("path", "")
        state["last_result"] = (
            f"uploaded {file_path.name} ({size} bytes) at {datetime.now().isoformat(timespec='seconds')}"
        )
        print(f"Uploaded: {file_path.name} -> {remote_path}")

        if delete_after_send:
            file_path.unlink(missing_ok=True)
            state["last_result"] += " (deleted local file)"

    for name in [name for name in pending if name not in existing_names]:
        pending.pop(name, None)
    for name in [name for name in tracked if name not in existing_names]:
        tracked.pop(name, None)
    return uploaded


def _cmd_send(args: argparse.Namespace) -> int:
    cfg = load_config()
    target_url = args.to or cfg.receiver_url
    failed = False

    for raw_path in args.files:
        file_path = Path(raw_path).expanduser()
        try:
            payload = _send_file_to_receiver(
                file_path,
                target_url,
                cfg.auth_token,
                cfg.request_timeout_seconds,
            )
        except error.HTTPError as exc:
            failed = True
            detail = exc.read().decode("utf-8", errors="replace").strip() or str(exc)
            print(f"failed to send {file_path}: receiver rejected request ({exc.code}) {detail}")
        except Exception as exc:
            failed = True
            print(f"failed to send {file_path}: {exc}")
        else:
            remote_path = payload.get("path", "remote inbox")
            print(f"sent {file_path} -> {remote_path}")

    return 1 if failed else 0


def _cmd_dropwatch(args: argparse.Namespace) -> int:
    cfg = load_config()
    folder = Path(args.folder).expanduser() if args.folder else cfg.drop_dir
    receiver_url = args.to or cfg.receiver_url
    state_path = cfg.state_dir / "dropwatch-state.json"
    state = _load_dropwatch_state(state_path)

    print(f"Watching: {folder}")
    print(f"Sending to: {receiver_url}")
    print("Press Ctrl-C to stop.")

    try:
        while True:
            try:
                _sync_dropwatch_once(
                    folder=folder,
                    receiver_url=receiver_url,
                    auth_token=cfg.auth_token,
                    timeout_seconds=cfg.request_timeout_seconds,
                    max_file_mb=cfg.max_file_mb,
                    delete_after_send=args.delete_after_send,
                    state=state,
                )
                _save_dropwatch_state(state_path, state)
            except Exception as exc:
                state["last_result"] = f"upload failed: {exc}"
                _save_dropwatch_state(state_path, state)
                print(state["last_result"])
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("")
    return 0


def _cmd_receive(_: argparse.Namespace) -> int:
    from unixdrop.linux_service import main as receiver_main

    receiver_main(start_deskflow=False, start_clipboard_watcher=True)
    return 0


def _dropzone_html(drop_dir: Path) -> bytes:
    escaped_drop_dir = html.escape(str(drop_dir))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Drop to Peer</title>
  <style>
    :root {{
      color-scheme: light dark;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: Canvas;
      color: CanvasText;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      box-sizing: border-box;
    }}
    main {{
      width: min(720px, 100%);
    }}
    h1 {{
      margin: 0 0 14px;
      font-size: 28px;
      line-height: 1.15;
      font-weight: 700;
    }}
    .path {{
      margin: 0 0 20px;
      font: 13px ui-monospace, SFMono-Regular, Menlo, monospace;
      opacity: 0.72;
      overflow-wrap: anywhere;
    }}
    #dropzone {{
      min-height: 280px;
      border: 2px dashed color-mix(in srgb, CanvasText 35%, transparent);
      border-radius: 8px;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 28px;
      box-sizing: border-box;
      background: color-mix(in srgb, CanvasText 4%, Canvas);
      transition: border-color 120ms ease, background 120ms ease, transform 120ms ease;
      cursor: pointer;
    }}
    #dropzone.active {{
      border-color: #0a7cff;
      background: color-mix(in srgb, #0a7cff 12%, Canvas);
      transform: translateY(-1px);
    }}
    .title {{
      font-size: 20px;
      font-weight: 650;
      margin-bottom: 8px;
    }}
    .hint {{
      font-size: 14px;
      opacity: 0.7;
    }}
    #fileInput {{
      display: none;
    }}
    #log {{
      margin-top: 18px;
      padding: 0;
      list-style: none;
      font-size: 14px;
      line-height: 1.4;
    }}
    #log li {{
      padding: 8px 0;
      border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
    }}
  </style>
</head>
<body>
  <main>
    <h1>Drop to Peer</h1>
    <p class="path">{escaped_drop_dir}</p>
    <div id="dropzone" role="button" tabindex="0" aria-label="Drop files to send to peer">
      <div>
        <div class="title">Drop files here</div>
        <div class="hint">or click to choose files</div>
      </div>
    </div>
    <input id="fileInput" type="file" multiple>
    <ul id="log" aria-live="polite"></ul>
  </main>
  <script>
    const dropzone = document.getElementById('dropzone');
    const input = document.getElementById('fileInput');
    const log = document.getElementById('log');

    function addLog(message) {{
      const item = document.createElement('li');
      item.textContent = message;
      log.prepend(item);
    }}

    async function uploadFiles(files) {{
      for (const file of files) {{
        const response = await fetch('/upload?name=' + encodeURIComponent(file.name), {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/octet-stream' }},
          body: file
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          addLog('Failed: ' + file.name + ' - ' + (payload.error || response.status));
          continue;
        }}
        addLog('Staged: ' + payload.name);
      }}
    }}

    dropzone.addEventListener('click', () => input.click());
    dropzone.addEventListener('keydown', (event) => {{
      if (event.key === 'Enter' || event.key === ' ') {{
        event.preventDefault();
        input.click();
      }}
    }});
    input.addEventListener('change', () => uploadFiles(input.files));
    for (const name of ['dragenter', 'dragover']) {{
      dropzone.addEventListener(name, (event) => {{
        event.preventDefault();
        dropzone.classList.add('active');
      }});
    }}
    for (const name of ['dragleave', 'drop']) {{
      dropzone.addEventListener(name, (event) => {{
        event.preventDefault();
        dropzone.classList.remove('active');
      }});
    }}
    dropzone.addEventListener('drop', (event) => uploadFiles(event.dataTransfer.files));
  </script>
</body>
</html>
""".encode("utf-8")


def _write_dropzone_upload(drop_dir: Path, raw_name: str, data: bytes) -> Path:
    safe_name = Path(unquote(raw_name)).name
    if not safe_name or safe_name in {".", ".."}:
        raise ValueError("invalid filename")
    drop_dir.mkdir(parents=True, exist_ok=True)
    destination = _drop_destination(drop_dir, Path(safe_name))
    destination.write_bytes(data)
    return destination


def _build_dropzone_handler(drop_dir: Path) -> type[BaseHTTPRequestHandler]:
    class DropzoneHandler(BaseHTTPRequestHandler):
        server_version = "UnixDropDropzone/1"

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/":
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                return
            body = _dropzone_html(drop_dir)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/upload":
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid content length"})
                return
            if content_length <= 0:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing file body"})
                return
            params = dict(
                part.split("=", 1)
                for part in parsed.query.split("&")
                if "=" in part
            )
            raw_name = params.get("name", "")
            try:
                destination = _write_dropzone_upload(drop_dir, raw_name, self.rfile.read(content_length))
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                return
            self._json(HTTPStatus.OK, {"ok": True, "name": destination.name, "path": str(destination)})

        def log_message(self, format: str, *args: object) -> None:
            return

    return DropzoneHandler


def _cmd_dropzone(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.drop_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _build_dropzone_handler(cfg.drop_dir))
    host, port = server.server_address
    url = f"http://{host}:{port}/"

    if args.open:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()

    print(f"Drop zone: {url}")
    print(f"Writing staged files to: {cfg.drop_dir}")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        server.server_close()
    return 0


def _cmd_up(_: argparse.Namespace) -> int:
    from unixdrop.health import health_lines
    from unixdrop.service_install import install_linux_service, install_mac_agent

    if sys.platform == "darwin":
        target = install_mac_agent()
        print(f"UnixDrop service file: {target}")
        print(f"Python executable: {sys.executable}")
        subprocess.run(["launchctl", "unload", str(target)], check=False)
        subprocess.run(["launchctl", "load", str(target)], check=True)
    elif sys.platform.startswith("linux"):
        target = install_linux_service()
        print(f"UnixDrop service file: {target}")
        print(f"Python executable: {sys.executable}")
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "unixdrop-receiver.service"],
            check=True,
        )
    else:
        raise SystemExit(f"unsupported platform: {sys.platform}")

    print("UnixDrop service started. Health snapshot:")
    for line in health_lines():
        print(line)
    return 0


def _deskflow_command_args(args: argparse.Namespace) -> list[str]:
    command = [
        "--role",
        args.role,
    ]
    if args.server_ip:
        command.extend(["--server-ip", args.server_ip])
    if args.server_hosts:
        command.extend(["--server-hosts", args.server_hosts])
    if args.client_name:
        command.extend(["--client-name", args.client_name])
    if args.server_name:
        command.extend(["--server-name", args.server_name])
    if args.direction:
        command.extend(["--direction", args.direction])
    if args.config_dir:
        command.extend(["--config-dir", args.config_dir])
    if args.autostart:
        command.append("--autostart")
    if args.verify:
        command.append("--verify")
    return command


def _cmd_deskflow(args: argparse.Namespace) -> int:
    command_args = _deskflow_command_args(args)
    from unixdrop.deskflow_setup import main as deskflow_main

    deskflow_main(command_args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deskbridge", description="Desk bridge between macOS and Linux")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a starter UnixDrop config")
    init_parser.add_argument("--config", help="Config path, defaults to ~/.config/unixdrop/config.json")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config file")
    init_parser.set_defaults(func=_cmd_init)

    setup_parser = subparsers.add_parser("setup", help="Create/update config and print first-run next steps")
    setup_parser.add_argument("--config", help="Config path, defaults to ~/.config/unixdrop/config.json")
    setup_parser.add_argument("--force", action="store_true", help="Overwrite config with fresh defaults before applying options")
    setup_parser.add_argument("--peer-url", help="Peer receiver URL or host, e.g. http://192.168.1.50:8765")
    setup_parser.add_argument("--auth-token", help="Shared auth token to write into config")
    setup_parser.add_argument(
        "--discover",
        dest="discover",
        action="store_true",
        default=True,
        help="Try LAN discovery when --peer-url is not provided",
    )
    setup_parser.add_argument("--no-discover", dest="discover", action="store_false", help="Skip LAN discovery")
    setup_parser.add_argument("--discovery-timeout", type=float, default=2.0, help="LAN discovery timeout in seconds")
    setup_parser.add_argument("--role", choices=["off", "server", "client"], help="Optional Deskflow role to save")
    setup_parser.add_argument(
        "--clipboard",
        choices=["off", "mac_to_linux", "linux_to_mac", "two_way"],
        help="Optional clipboard mode to save",
    )
    setup_parser.add_argument("--client-name", help="Deskflow client screen name for printed commands")
    setup_parser.add_argument("--direction", choices=["right", "left", "up", "down"], default="right")
    setup_parser.add_argument("--autostart", action="store_true", help="Include --autostart in printed Deskflow commands")
    setup_parser.set_defaults(func=_cmd_setup)

    tab_parser = subparsers.add_parser("tab", help="Send active macOS browser tab to peer")
    tab_parser.add_argument(
        "--browser",
        default="auto",
        help="auto, safari, chrome, arc, brave, chromium, edge, firefox, firefox-developer, librewolf, vivaldi, opera",
    )
    tab_parser.add_argument(
        "--firefox-debug-url",
        help="Firefox-compatible debug endpoint, defaults to tabs.firefox_debug_url or http://127.0.0.1:9222",
    )
    tab_parser.add_argument("--no-open", action="store_true", help="Queue link on peer instead of opening")
    tab_parser.set_defaults(func=_cmd_tab)

    url_parser = subparsers.add_parser("url", help="Send an explicit URL to the peer")
    url_parser.add_argument("url", help="http or https URL to send")
    url_parser.add_argument("--no-open", action="store_true", help="Queue link on peer instead of opening")
    url_parser.add_argument("--to", help="Receiver base URL, defaults to receiver_url from config")
    url_parser.set_defaults(func=_cmd_url)

    status_parser = subparsers.add_parser("status", help="Show desk bridge status")
    status_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    status_parser.set_defaults(func=_cmd_status)

    health_parser = subparsers.add_parser("health", help="Run health checks")
    health_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    health_parser.set_defaults(func=_cmd_health)

    doctor_parser = subparsers.add_parser("doctor", help="Check local portability prerequisites")
    doctor_parser.add_argument("--config", help="Config path, defaults to ~/.config/unixdrop/config.json")
    doctor_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    doctor_parser.set_defaults(func=_cmd_doctor)

    tui_parser = subparsers.add_parser("tui", help="Live terminal dashboard")
    tui_parser.add_argument("--interval", type=float, default=3.0, help="Refresh interval in seconds")
    tui_parser.add_argument("--once", action="store_true", help="Render one snapshot and exit")
    tui_parser.set_defaults(func=_cmd_tui)

    drop_parser = subparsers.add_parser("drop", help="Open or stage files into the peer drop folder")
    drop_parser.add_argument("files", nargs="*", help="Files to copy into the drop folder")
    drop_parser.add_argument("--open", action="store_true", help="Open the drop folder after staging files")
    drop_parser.set_defaults(func=_cmd_drop)

    send_parser = subparsers.add_parser("send", help="Send files directly to a UnixDrop receiver")
    send_parser.add_argument("files", nargs="+", help="Files to send")
    send_parser.add_argument("--to", help="Receiver base URL, defaults to receiver_url from config")
    send_parser.set_defaults(func=_cmd_send)

    dropwatch_parser = subparsers.add_parser(
        "dropwatch",
        help="Watch a local folder and send new files to a UnixDrop receiver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Watch a local folder and send files to another machine.\n\n"
            "Reverse-direction drop folder:\n"
            "  1. On the receiving machine, run:\n"
            "     ./deskbridge receive\n\n"
            "  2. On the sending machine, run:\n"
            "     ./deskbridge dropwatch --folder ~/Drop\\ to\\ Peer --to http://<peer-ip>:8765\n\n"
            "  3. Drop files into the watched folder.\n"
        ),
    )
    dropwatch_parser.add_argument("--folder", help="Folder to watch, defaults to drop.folder from config")
    dropwatch_parser.add_argument("--to", help="Receiver base URL, e.g. http://192.168.1.10:8765")
    dropwatch_parser.add_argument("--interval", type=float, default=5.0, help="Polling interval in seconds")
    dropwatch_parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    dropwatch_parser.add_argument(
        "--delete-after-send",
        action="store_true",
        help="Delete local files after successful upload",
    )
    dropwatch_parser.set_defaults(func=_cmd_dropwatch)

    receive_parser = subparsers.add_parser("receive", help="Run a UnixDrop file receiver on this machine")
    receive_parser.set_defaults(func=_cmd_receive)

    dropzone_parser = subparsers.add_parser("dropzone", help="Run a browser drag-and-drop box for peer files")
    dropzone_parser.add_argument("--port", type=int, default=0, help="Local port to bind, 0 chooses a free port")
    dropzone_parser.add_argument("--no-open", dest="open", action="store_false", help="Do not open the browser")
    dropzone_parser.set_defaults(func=_cmd_dropzone, open=True)

    clean_parser = subparsers.add_parser(
        "clean",
        aliases=["wipe"],
        help="Stop Deskflow/Barrier processes and disable conflicting autostarts",
    )
    clean_parser.set_defaults(func=_cmd_clean)

    deskflow_parser = subparsers.add_parser(
        "deskflow",
        help="Configure Deskflow keyboard/mouse sharing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Configure Deskflow keyboard/mouse sharing.\n\n"
            "Copy-paste setup:\n"
            "  1. On the machine with the keyboard/mouse, run server setup:\n"
            "     ./deskbridge deskflow --role server --client-name peer-laptop --direction right --autostart\n\n"
            "  2. On the other machine, run client setup:\n"
            "     ./deskbridge deskflow --role client --client-name peer-laptop --autostart\n\n"
            "  3. If you have LAN and Tailscale endpoints, use fallback hosts on the client:\n"
            "     ./deskbridge deskflow --role client --server-hosts <lan-ip>:24800,<tailscale-ip>:24800 --client-name peer-laptop --autostart\n\n"
            "  4. Verify later:\n"
            "     ./deskbridge deskflow --role server --verify\n"
            "     ./deskbridge deskflow --role client --verify\n\n"
            "Common values:\n"
            "  --direction right   Client screen is to the right of the server screen.\n"
            "  --direction left    Client screen is to the left of the server screen.\n"
            "  --client-name       Name of the client screen, for example peer-laptop.\n"
            "  --server-ip         Optional fixed fallback when LAN discovery is unavailable.\n"
        ),
    )
    deskflow_parser.add_argument(
        "--role",
        choices=["server", "client"],
        required=True,
        help="Required. Use server on the keyboard/mouse machine; use client on the other machine.",
    )
    deskflow_parser.add_argument(
        "--server-ip",
        help="Client setup: optional fixed host or host:port; otherwise discover the server automatically.",
    )
    deskflow_parser.add_argument(
        "--server-hosts",
        help="Client setup: comma-separated fallback endpoints, e.g. 192.168.1.20:24800,100.x.y.z:24800.",
    )
    deskflow_parser.add_argument(
        "--client-name",
        help="Client screen name used by Deskflow, e.g. peer-laptop. Use the same name on server and client setup.",
    )
    deskflow_parser.add_argument("--server-name", help="Server screen name for Deskflow config.")
    deskflow_parser.add_argument(
        "--direction",
        choices=["right", "left", "up", "down"],
        help="Server setup: where the client screen sits relative to the server screen.",
    )
    deskflow_parser.add_argument("--autostart", action="store_true", help="Install startup service/agent after writing config.")
    deskflow_parser.add_argument("--verify", action="store_true", help="Check the existing Deskflow setup instead of changing it.")
    deskflow_parser.add_argument("--config-dir", help="Advanced: Deskflow config directory override.")
    deskflow_parser.set_defaults(func=_cmd_deskflow)

    up_parser = subparsers.add_parser("up", help="Install/refresh and start local UnixDrop service")
    up_parser.set_defaults(func=_cmd_up)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
