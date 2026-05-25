from __future__ import annotations

import argparse

from unixdrop.health import health_lines
from unixdrop.send_browser_url import current_browser_context, is_supported_web_url, send_url
from unixdrop.status import status_lines


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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
