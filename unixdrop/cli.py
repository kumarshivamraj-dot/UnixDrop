from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
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


def _cmd_clean(_: argparse.Namespace) -> int:
    print("Deskbridge clean")

    if sys.platform not in {"darwin"} and not sys.platform.startswith("linux"):
        raise SystemExit(f"unsupported platform: {sys.platform}")

    if sys.platform == "darwin":
        launch_agents = [
            Path("~/Library/LaunchAgents/com.unixdrop.agent.plist").expanduser(),
            Path("~/Library/LaunchAgents/com.unixdrop.deskflow.server.plist").expanduser(),
            Path("~/Library/LaunchAgents/com.unixdrop.deskflow.client.plist").expanduser(),
        ]
        for plist in launch_agents:
            if not plist.exists():
                print(f"[skip] launch agent not found: {plist}")
                continue
            code, detail = _run_command(["launchctl", "unload", str(plist)])
            status = "ok" if code == 0 else "warn"
            print(f"[{status}] launchctl unload {plist.name}: {detail}")

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
                print(f"[warn] pkill -f {pattern}: {detail}")
    else:
        print("[warn] pkill not found; could not terminate stale processes")

    if shutil.which("lsof"):
        code, detail = _run_command(["lsof", "-nP", "-iTCP:24800", "-sTCP:LISTEN"])
        if code == 0:
            print("[warn] port 24800 still in use:")
            print(detail)
        else:
            print("[ok] port 24800 is free")
    else:
        print("[skip] lsof not available; could not verify port 24800")

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

    up_parser = subparsers.add_parser("up", help="Install/refresh and start local UnixDrop service")
    up_parser.set_defaults(func=_cmd_up)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
