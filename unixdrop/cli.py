from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from unixdrop.health import health_lines
from unixdrop.send_browser_url import current_browser_context, is_supported_web_url, send_url
from unixdrop.status import status_lines
from unixdrop.tui import run_tui


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
    app_name, url = current_browser_context(args.browser)
    if not url:
        raise SystemExit("no active browser url found in supported running browsers")
    if not is_supported_web_url(url):
        label = app_name or "browser"
        raise SystemExit(f"{label} returned a non-web URL: {url}")
    send_url(url, no_open=args.no_open)
    print(url)
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    for line in status_lines():
        print(line)
    return 0


def _cmd_health(_: argparse.Namespace) -> int:
    for line in health_lines():
        print(line)
    return 0


def _cmd_tui(args: argparse.Namespace) -> int:
    return run_tui(interval_seconds=args.interval, once=args.once)


def _cmd_up(_: argparse.Namespace) -> int:
    project_dir = Path(__file__).resolve().parents[1]
    if sys.platform == "darwin":
        install_script = project_dir / "scripts" / "install_mac_agent.sh"
        target = Path("~/Library/LaunchAgents/com.unixdrop.agent.plist").expanduser()
        subprocess.run([str(install_script)], check=True)
        subprocess.run(["launchctl", "unload", str(target)], check=False)
        subprocess.run(["launchctl", "load", str(target)], check=True)
    elif sys.platform.startswith("linux"):
        install_script = project_dir / "scripts" / "install_linux_service.sh"
        subprocess.run([str(install_script)], check=True)
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


def _cmd_deskflow(args: argparse.Namespace) -> int:
    project_dir = Path(__file__).resolve().parents[1]
    script = project_dir / "scripts" / "configure_deskflow.sh"
    if not script.exists():
        raise SystemExit(f"missing script: {script}")

    command = [
        str(script),
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

    subprocess.run(command, check=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deskbridge", description="Desk bridge between macOS and Linux")
    subparsers = parser.add_subparsers(dest="command", required=True)

    tab_parser = subparsers.add_parser("tab", help="Send active browser tab from Mac to Linux")
    tab_parser.add_argument(
        "--browser",
        default="auto",
        help="auto, safari, chrome, arc, brave, chromium, edge, vivaldi, opera",
    )
    tab_parser.add_argument("--no-open", action="store_true", help="Queue link on Linux instead of opening")
    tab_parser.set_defaults(func=_cmd_tab)

    status_parser = subparsers.add_parser("status", help="Show desk bridge status")
    status_parser.set_defaults(func=_cmd_status)

    health_parser = subparsers.add_parser("health", help="Run health checks")
    health_parser.set_defaults(func=_cmd_health)

    tui_parser = subparsers.add_parser("tui", help="Live terminal dashboard")
    tui_parser.add_argument("--interval", type=float, default=3.0, help="Refresh interval in seconds")
    tui_parser.add_argument("--once", action="store_true", help="Render one snapshot and exit")
    tui_parser.set_defaults(func=_cmd_tui)

    clean_parser = subparsers.add_parser(
        "clean",
        aliases=["wipe"],
        help="Stop Deskflow/Barrier processes and disable conflicting autostarts",
    )
    clean_parser.set_defaults(func=_cmd_clean)

    deskflow_parser = subparsers.add_parser(
        "deskflow",
        help="Configure Deskflow server/client scripts using deskbridge",
    )
    deskflow_parser.add_argument("--role", choices=["server", "client"], required=True, help="Deskflow role")
    deskflow_parser.add_argument("--server-ip", help="Server endpoint host or host:port (client role)")
    deskflow_parser.add_argument("--server-hosts", help="CSV endpoints (client role), e.g. lan:24800,tailscale:24800")
    deskflow_parser.add_argument("--client-name", help="Client screen/runtime name")
    deskflow_parser.add_argument("--server-name", help="Server screen name (server role)")
    deskflow_parser.add_argument(
        "--direction",
        choices=["right", "left", "up", "down"],
        help="Client position relative to server (server role)",
    )
    deskflow_parser.add_argument("--autostart", action="store_true", help="Install Deskflow autostart service/agent")
    deskflow_parser.add_argument("--verify", action="store_true", help="Verify existing Deskflow setup")
    deskflow_parser.add_argument("--config-dir", help="Deskflow config directory")
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
