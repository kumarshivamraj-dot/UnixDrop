from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from unixdrop import __version__
from unixdrop.config import clipboard_pull_enabled, clipboard_send_enabled, load_config
from unixdrop.vault import build_manifest, manifest_to_json, should_skip_relative


CONFIG = load_config()
CLIPBOARD_STATE = {
    "text": "",
    "hash": "",
    "source": "",
    "updated_at": "",
}
CLIPBOARD_LOCK = threading.Lock()


def _log(level: str, message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] [unixdrop receiver] [{level}] {message}", flush=True)


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


def should_open_link(auto_open_links: bool, no_open_requested: bool) -> bool:
    return auto_open_links and not no_open_requested


def _queue_link(url: str, source: str) -> None:
    queue_file = CONFIG.inbox_dir / "links.md"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"- [{stamp}] {url} ({source})\n"
    with queue_file.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _open_link(url: str) -> tuple[bool, str | None]:
    command = shutil.which("xdg-open")
    if not command:
        return False, "xdg-open not found"
    try:
        subprocess.Popen(
            [command, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return False, str(exc)
    return True, None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clipboard_get_command() -> list[str] | None:
    if shutil.which("wl-paste"):
        return ["wl-paste", "--no-newline"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--output"]
    return None


def _clipboard_set_command() -> list[str] | None:
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


def _read_linux_clipboard() -> str | None:
    command = _clipboard_get_command()
    if not command:
        return None
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _write_linux_clipboard(text: str) -> bool:
    command = _clipboard_set_command()
    if not command:
        return False
    try:
        result = subprocess.run(
            command,
            input=text,
            text=True,
            capture_output=True,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


def _update_clipboard_state(text: str, source: str) -> None:
    digest = _hash_text(text)
    with CLIPBOARD_LOCK:
        CLIPBOARD_STATE["text"] = text
        CLIPBOARD_STATE["hash"] = digest
        CLIPBOARD_STATE["source"] = source
        CLIPBOARD_STATE["updated_at"] = _utc_now_iso()


def _clipboard_snapshot() -> dict:
    with CLIPBOARD_LOCK:
        return dict(CLIPBOARD_STATE)


def _linux_clipboard_watcher() -> None:
    if not clipboard_pull_enabled(CONFIG.clipboard_mode):
        return

    last_seen_hash = ""
    last_error_log = 0.0
    while True:
        try:
            current = _read_linux_clipboard()
            if current is None or len(current) > CONFIG.max_clipboard_chars:
                time.sleep(CONFIG.clipboard_poll_seconds)
                continue

            digest = _hash_text(current)
            if digest == last_seen_hash:
                time.sleep(CONFIG.clipboard_poll_seconds)
                continue
            last_seen_hash = digest

            snapshot = _clipboard_snapshot()
            if digest != snapshot.get("hash"):
                _update_clipboard_state(current, "linux-local")
        except Exception as exc:
            now = time.monotonic()
            if now - last_error_log >= 30:
                _log("warn", f"clipboard watcher error: {exc}")
                last_error_log = now

        time.sleep(CONFIG.clipboard_poll_seconds)


def resolve_conflict_destination(inbox_dir: Path, original_name: str, now: datetime | None = None) -> Path:
    safe_name = Path(original_name).name
    destination = inbox_dir / safe_name
    if not destination.exists():
        return destination

    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H-%M-%S")
    stem = destination.stem
    suffix = destination.suffix
    return destination.with_name(f"{stem} (conflict {timestamp}){suffix}")


def _inbox_writable() -> bool:
    probe = CONFIG.inbox_dir / ".unixdrop-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _xdg_open_available() -> bool:
    return shutil.which("xdg-open") is not None


class UnixDropHandler(BaseHTTPRequestHandler):
    server_version = f"UnixDrop/{__version__}"

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

    def _read_body_bytes(self, *, max_bytes: int | None = None) -> tuple[bytes | None, str | None]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "invalid content length"
        if content_length <= 0:
            return None, "missing request body"
        if max_bytes is not None and content_length > max_bytes:
            return None, "request body exceeds limit"
        payload = self.rfile.read(content_length)
        if len(payload) != content_length:
            return None, "incomplete request body"
        return payload, None

    def _read_json_payload(self, *, max_bytes: int = 1024 * 1024) -> tuple[dict | None, str | None]:
        raw, error = self._read_body_bytes(max_bytes=max_bytes)
        if error:
            return None, error
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "invalid json payload"
        if not isinstance(payload, dict):
            return None, "json payload must be an object"
        return payload, None

    def _handle_internal_error(self, exc: Exception) -> None:
        _log("error", f"{self.command} {self.path} failed: {exc}")
        try:
            self._reject(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:
        try:
            self._dispatch_get()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._handle_internal_error(exc)

    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self._json_response(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "version": __version__,
                    "auto_open_links": CONFIG.auto_open_links,
                    "clipboard_mode": CONFIG.clipboard_mode,
                },
            )
            return

        if parsed.path == "/api/vault/manifest":
            self._handle_vault_manifest()
            return

        if parsed.path == "/api/vault/file":
            self._handle_vault_file(parsed.query)
            return

        if parsed.path == "/api/clipboard":
            if not self._check_auth():
                return
            self._handle_clipboard_get()
            return

        if parsed.path == "/api/diagnostics":
            if not self._check_auth():
                return
            self._handle_diagnostics()
            return

        self._reject(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        try:
            self._dispatch_post()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._handle_internal_error(exc)

    def _dispatch_post(self) -> None:
        if self.path == "/api/ping":
            if not self._check_auth():
                return
            self._json_response(HTTPStatus.OK, {"ok": True, "pong": True})
            return

        if self.path == "/api/health/write-check":
            if not self._check_auth():
                return
            self._json_response(HTTPStatus.OK, {"ok": _inbox_writable()})
            return

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

        if self.path == "/api/clipboard":
            self._handle_clipboard_post()
            return

        self._reject(HTTPStatus.NOT_FOUND, "not found")

    def _obsidian_enabled(self) -> bool:
        if not CONFIG.obsidian_enabled:
            self._reject(HTTPStatus.NOT_FOUND, "obsidian sync disabled")
            return False
        return True

    def _handle_link(self) -> None:
        payload, error = self._read_json_payload(max_bytes=64 * 1024)
        if error:
            self._reject(HTTPStatus.BAD_REQUEST, error)
            return

        url = payload.get("url", "").strip()
        source = payload.get("source", "unknown")
        no_open_requested = bool(payload.get("no_open", False))

        if not _is_valid_url(url):
            self._reject(HTTPStatus.BAD_REQUEST, "invalid url")
            return

        action = "queued"
        open_error = None
        if should_open_link(CONFIG.auto_open_links, no_open_requested):
            opened, open_error = _open_link(url)
            if opened:
                action = "opened"
            else:
                _log("warn", f"failed to open link, queueing instead: {open_error}")
                try:
                    _queue_link(url, source)
                except Exception as exc:
                    self._reject(HTTPStatus.INTERNAL_SERVER_ERROR, f"failed to queue link: {exc}")
                    return
        else:
            try:
                _queue_link(url, source)
            except Exception as exc:
                self._reject(HTTPStatus.INTERNAL_SERVER_ERROR, f"failed to queue link: {exc}")
                return

        event = {
            "type": "link",
            "url": url,
            "source": source,
            "received_at": _utc_now_iso(),
            "opened": action == "opened",
            "action": action,
        }
        if open_error:
            event["open_error"] = open_error
        try:
            _append_link_log(event)
        except Exception as exc:
            _log("warn", f"failed to append link log: {exc}")

        self._json_response(HTTPStatus.OK, {"ok": True, "action": action})

    def _handle_file(self) -> None:
        file_name = self.headers.get("X-Filename", "").strip()
        if not file_name:
            self._reject(HTTPStatus.BAD_REQUEST, "missing filename")
            return
        safe_name = Path(file_name).name
        if not safe_name or safe_name in {".", ".."}:
            self._reject(HTTPStatus.BAD_REQUEST, "invalid filename")
            return

        max_bytes = CONFIG.max_file_mb * 1024 * 1024
        data, error = self._read_body_bytes(max_bytes=max_bytes)
        if error:
            if error == "request body exceeds limit":
                self._reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "file exceeds max_file_mb")
            else:
                self._reject(HTTPStatus.BAD_REQUEST, error)
            return

        destination = resolve_conflict_destination(CONFIG.inbox_dir, safe_name)
        try:
            destination.write_bytes(data)
        except Exception as exc:
            self._reject(HTTPStatus.INTERNAL_SERVER_ERROR, f"failed to write file: {exc}")
            return

        event = {
            "type": "file",
            "file_name": destination.name,
            "size_bytes": len(data),
            "received_at": _utc_now_iso(),
        }
        try:
            _append_link_log(event)
        except Exception as exc:
            _log("warn", f"failed to append file event log: {exc}")
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
            mtime = float(self.headers.get("X-File-Mtime", "0"))
        except ValueError:
            self._reject(HTTPStatus.BAD_REQUEST, "invalid file metadata")
            return

        destination = (CONFIG.obsidian_vault_dir / relative_path).resolve()
        if not str(destination).startswith(str(CONFIG.obsidian_vault_dir.resolve())):
            self._reject(HTTPStatus.BAD_REQUEST, "invalid vault path")
            return

        data, error = self._read_body_bytes()
        if error:
            self._reject(HTTPStatus.BAD_REQUEST, error)
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if mtime > 0:
            os.utime(destination, (mtime, mtime))

        self._json_response(
            HTTPStatus.OK,
            {"ok": True, "path": str(destination)},
        )

    def _handle_clipboard_get(self) -> None:
        if not clipboard_pull_enabled(CONFIG.clipboard_mode):
            self._reject(HTTPStatus.NOT_FOUND, "clipboard pull disabled by clipboard_mode")
            return
        snapshot = _clipboard_snapshot()
        self._json_response(HTTPStatus.OK, {"ok": True, **snapshot})

    def _handle_clipboard_post(self) -> None:
        if not clipboard_send_enabled(CONFIG.clipboard_mode):
            self._reject(HTTPStatus.NOT_FOUND, "clipboard push disabled by clipboard_mode")
            return

        payload, error = self._read_json_payload(max_bytes=2 * 1024 * 1024)
        if error:
            self._reject(HTTPStatus.BAD_REQUEST, error)
            return
        text = payload.get("text", "")
        source = payload.get("source", "unknown")
        if not isinstance(text, str):
            self._reject(HTTPStatus.BAD_REQUEST, "invalid clipboard text")
            return
        if len(text) > CONFIG.max_clipboard_chars:
            self._reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "clipboard text exceeds max_clipboard_chars")
            return

        _update_clipboard_state(text, source)
        _write_linux_clipboard(text)
        self._json_response(HTTPStatus.OK, {"ok": True, "hash": _hash_text(text)})

    def _handle_diagnostics(self) -> None:
        self._json_response(
            HTTPStatus.OK,
            {
                "ok": True,
                "version": __version__,
                "auto_open_links": CONFIG.auto_open_links,
                "clipboard_mode": CONFIG.clipboard_mode,
                "xdg_open_available": _xdg_open_available(),
                "inbox_writable": _inbox_writable(),
                "inbox_dir": str(CONFIG.inbox_dir),
            },
        )

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    _ensure_dirs()
    watcher = threading.Thread(target=_linux_clipboard_watcher, daemon=True)
    watcher.start()
    try:
        server = ThreadingHTTPServer((CONFIG.listen_host, CONFIG.port), UnixDropHandler)
    except Exception as exc:
        _log("error", f"failed to start receiver on {CONFIG.listen_host}:{CONFIG.port}: {exc}")
        raise
    _log("info", f"listening on {CONFIG.listen_host}:{CONFIG.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
