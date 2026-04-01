from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from unixdrop.config import load_config
from unixdrop.vault import build_manifest, manifest_to_json, should_skip_relative


CONFIG = load_config()


def _ensure_dirs() -> None:
    CONFIG.inbox_dir.mkdir(parents=True, exist_ok=True)
    CONFIG.link_log_path.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG.obsidian_enabled:
        CONFIG.obsidian_vault_dir.mkdir(parents=True, exist_ok=True)


def _append_link_log(payload: dict) -> None:
    line = json.dumps(payload, sort_keys=True)
    with CONFIG.link_log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _is_valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _open_link(url: str) -> None:
    if not CONFIG.auto_open_links:
        return
    subprocess.Popen(
        ["xdg-open", url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class UnixDropHandler(BaseHTTPRequestHandler):
    server_version = "UnixDrop/0.1"

    def _json_response(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, status: int, message: str) -> None:
        self._json_response(status, {"ok": False, "error": message})

    def _check_auth(self) -> bool:
        token = self.headers.get("Authorization", "")
        expected = f"Bearer {CONFIG.auth_token}"
        if token != expected:
            self._reject(HTTPStatus.UNAUTHORIZED, "invalid auth token")
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self._json_response(HTTPStatus.OK, {"ok": True})
            return

        if parsed.path == "/api/vault/manifest":
            self._handle_vault_manifest()
            return

        if parsed.path == "/api/vault/file":
            self._handle_vault_file(parsed.query)
            return

        self._reject(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        if not self._check_auth():
            return

        if self.path == "/api/link":
            self._handle_link()
            return

        if self.path == "/api/file":
            self._handle_file()
            return

        if self.path == "/api/vault/file":
            self._handle_vault_push()
            return

        self._reject(HTTPStatus.NOT_FOUND, "not found")

    def _obsidian_enabled(self) -> bool:
        if not CONFIG.obsidian_enabled:
            self._reject(HTTPStatus.NOT_FOUND, "obsidian sync disabled")
            return False
        return True

    def _handle_link(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reject(HTTPStatus.BAD_REQUEST, "invalid content length")
            return

        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        url = payload.get("url", "").strip()
        source = payload.get("source", "unknown")

        if not _is_valid_url(url):
            self._reject(HTTPStatus.BAD_REQUEST, "invalid url")
            return

        event = {
            "type": "link",
            "url": url,
            "source": source,
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        _append_link_log(event)
        _open_link(url)
        self._json_response(HTTPStatus.OK, {"ok": True})

    def _handle_file(self) -> None:
        file_name = self.headers.get("X-Filename", "").strip()
        if not file_name:
            self._reject(HTTPStatus.BAD_REQUEST, "missing filename")
            return

        safe_name = Path(file_name).name
        destination = CONFIG.inbox_dir / safe_name

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reject(HTTPStatus.BAD_REQUEST, "invalid content length")
            return

        data = self.rfile.read(content_length)
        destination.write_bytes(data)

        event = {
            "type": "file",
            "file_name": safe_name,
            "size_bytes": len(data),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        _append_link_log(event)
        self._json_response(HTTPStatus.OK, {"ok": True, "path": str(destination)})

    def _handle_vault_manifest(self) -> None:
        if not self._check_auth() or not self._obsidian_enabled():
            return
        body = manifest_to_json(build_manifest(CONFIG.obsidian_vault_dir, CONFIG))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_vault_file(self, query: str) -> None:
        if not self._check_auth() or not self._obsidian_enabled():
            return
        params = parse_qs(query)
        requested = unquote(params.get("path", [""])[0]).strip("/")
        if not requested or should_skip_relative(requested, CONFIG):
            self._reject(HTTPStatus.BAD_REQUEST, "invalid vault path")
            return

        file_path = (CONFIG.obsidian_vault_dir / requested).resolve()
        if not str(file_path).startswith(str(CONFIG.obsidian_vault_dir.resolve())):
            self._reject(HTTPStatus.BAD_REQUEST, "invalid vault path")
            return
        if not file_path.exists() or not file_path.is_file():
            self._reject(HTTPStatus.NOT_FOUND, "vault file not found")
            return

        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_vault_push(self) -> None:
        if not self._obsidian_enabled():
            return
        relative_path = self.headers.get("X-Relative-Path", "").strip().strip("/")
        if not relative_path or should_skip_relative(relative_path, CONFIG):
            self._reject(HTTPStatus.BAD_REQUEST, "invalid vault path")
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            mtime = float(self.headers.get("X-File-Mtime", "0"))
        except ValueError:
            self._reject(HTTPStatus.BAD_REQUEST, "invalid file metadata")
            return

        destination = (CONFIG.obsidian_vault_dir / relative_path).resolve()
        if not str(destination).startswith(str(CONFIG.obsidian_vault_dir.resolve())):
            self._reject(HTTPStatus.BAD_REQUEST, "invalid vault path")
            return

        data = self.rfile.read(content_length)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if mtime > 0:
            os.utime(destination, (mtime, mtime))

        self._json_response(
            HTTPStatus.OK,
            {"ok": True, "path": str(destination)},
        )

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    _ensure_dirs()
    server = ThreadingHTTPServer((CONFIG.listen_host, CONFIG.port), UnixDropHandler)
    print(f"UnixDrop receiver listening on {CONFIG.listen_host}:{CONFIG.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
