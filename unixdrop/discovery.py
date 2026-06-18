from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path


DISCOVERY_PORT = 24801
MULTICAST_GROUP = "239.255.77.77"
REQUEST = b"UNIXDROP_DISCOVER_V1 deskflow"
PROTOCOL = "unixdrop-discovery-v1"


def _response(name: str, service_port: int) -> bytes:
    return json.dumps(
        {"protocol": PROTOCOL, "service": "deskflow", "name": name, "port": service_port},
        separators=(",", ":"),
    ).encode("utf-8")


def serve(name: str, service_port: int, discovery_port: int = DISCOVERY_PORT) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", discovery_port))
    try:
        membership = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    except OSError:
        # Broadcast discovery still works on systems without multicast support.
        pass

    payload = _response(name, service_port)
    while True:
        data, source = sock.recvfrom(2048)
        if data.strip() == REQUEST:
            sock.sendto(payload, source)


def _valid_reply(data: bytes) -> dict | None:
    try:
        payload = json.loads(data.decode("utf-8"))
        port = int(payload.get("port", 0))
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if payload.get("protocol") != PROTOCOL or payload.get("service") != "deskflow":
        return None
    if not 1 <= port <= 65535:
        return None
    payload["port"] = port
    return payload


def discover(timeout: float = 4.0, discovery_port: int = DISCOVERY_PORT) -> tuple[str, dict] | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.25)
    sock.bind(("", 0))
    deadline = time.monotonic() + max(0.1, timeout)
    next_send = 0.0
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                for target in (("255.255.255.255", discovery_port), (MULTICAST_GROUP, discovery_port)):
                    try:
                        sock.sendto(REQUEST, target)
                    except OSError:
                        pass
                next_send = now + 0.75
            try:
                data, source = sock.recvfrom(2048)
            except socket.timeout:
                continue
            payload = _valid_reply(data)
            if payload is not None:
                return source[0], payload
    finally:
        sock.close()
    return None


def _read_cache(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        endpoint = str(payload["endpoint"]).strip()
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return endpoint or None


def _write_cache(path: Path, endpoint: str, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"endpoint": endpoint, "name": name, "updated_at": int(time.time())}) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dependency-free LAN discovery for UnixDrop services")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--name", default=socket.gethostname())
    serve_parser.add_argument("--service-port", type=int, default=24800)

    discover_parser = subparsers.add_parser("discover")
    discover_parser.add_argument("--timeout", type=float, default=4.0)
    discover_parser.add_argument("--cache", type=Path)
    discover_parser.add_argument("--no-cache-fallback", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "serve":
        try:
            serve(args.name, args.service_port)
        except KeyboardInterrupt:
            pass
        return 0

    result = discover(args.timeout)
    if result is not None:
        host, payload = result
        endpoint = f"{host}:{payload['port']}"
        if args.cache:
            _write_cache(args.cache.expanduser(), endpoint, str(payload.get("name", "")))
        print(endpoint)
        return 0

    if args.cache and not args.no_cache_fallback:
        cached = _read_cache(args.cache.expanduser())
        if cached:
            print(cached)
            return 0
    print("no Deskflow server discovered on this LAN", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
