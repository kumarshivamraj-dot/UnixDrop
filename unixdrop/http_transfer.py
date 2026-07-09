from __future__ import annotations

import http.client
import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse


def _target_path(path: str, query: str) -> str:
    value = path or "/"
    if query:
        value += f"?{query}"
    return value


def _connection(parsed, timeout_seconds: int) -> http.client.HTTPConnection:
    if parsed.scheme == "http":
        return http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout_seconds)
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout_seconds)
    raise ValueError(f"unsupported receiver URL scheme: {parsed.scheme}")


def post_file(
    *,
    url: str,
    file_path: Path,
    headers: dict[str, str],
    timeout_seconds: int,
    chunk_size: int = 1024 * 1024,
) -> dict:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError(f"invalid receiver URL: {url}")

    stat = file_path.stat()
    request_headers = dict(headers)
    request_headers["Content-Length"] = str(stat.st_size)

    conn = _connection(parsed, timeout_seconds)
    try:
        conn.putrequest("POST", _target_path(parsed.path, parsed.query))
        for name, value in request_headers.items():
            conn.putheader(name, value)
        conn.endheaders()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                conn.send(chunk)

        response = conn.getresponse()
        body = response.read()
        if response.status >= 400:
            raise HTTPError(url, response.status, response.reason, None, io.BytesIO(body))
        if not 200 <= response.status < 300:
            raise HTTPError(url, response.status, response.reason, None, io.BytesIO(body))
        return json.loads(body.decode("utf-8"))
    finally:
        conn.close()
