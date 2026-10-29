"""Observe conda's Requests transport without changing retries."""

from __future__ import annotations

import json
import os
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

REQUEST_HEADERS = frozenset({"accept", "range", "if-modified-since"})
RESPONSE_HEADERS = frozenset(
    {
        "date",
        "server",
        "age",
        "retry-after",
        "content-type",
        "content-length",
        "content-range",
        "cf-ray",
        "cf-cache-status",
        "x-cache",
        "x-cache-hits",
        "x-amz-cf-id",
        "x-amz-cf-pop",
        "x-served-by",
        "x-timer",
    }
)
_installed = False


def select_headers(headers: Any, allowed: frozenset[str]) -> dict[str, str]:
    return {
        name.lower(): value[:512]
        for name, value in headers.items()
        if name.lower() in allowed and isinstance(value, str)
    }


class Recorder:
    """Append complete events to one private file per process."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.pid = os.getpid()
        self.path = directory / f"{self.pid}-{uuid4().hex}.jsonl"
        self.lock = Lock()
        self.disabled = False

    def write(self, event: dict[str, Any]) -> None:
        if self.pid != os.getpid():
            self.pid = os.getpid()
            self.path = self.directory / f"{self.pid}-{uuid4().hex}.jsonl"
            self.lock = Lock()
        with self.lock:
            if self.disabled:
                return
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                line = json.dumps(event, separators=(",", ":")) + "\n"
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as stream:
                    stream.write(line)
            except Exception:
                self.disabled = True
                print(
                    "conda-cdn-log: capture disabled after a log write failure",
                    file=sys.stderr,
                )


def install(directory: Path, hosts: frozenset[str]) -> None:
    """Enable process-local observation after a conda command starts."""
    global _installed
    if _installed:
        return

    from conda.gateways.connection.adapters.http import HTTPAdapter
    from requests.exceptions import ConnectionError, SSLError, Timeout
    from urllib3.connectionpool import HTTPConnectionPool

    recorder = Recorder(directory)
    allowed_hosts = frozenset(
        host.strip().rstrip(".") for host in hosts if host.strip()
    )
    active: ContextVar[dict[str, Any] | None] = ContextVar(
        "conda_cdn_request", default=None
    )
    original_send = HTTPAdapter.send
    original_make_request = HTTPConnectionPool._make_request

    def emit(event: dict[str, Any]) -> None:
        recorder.write(
            {
                "schema_version": 1,
                "timestamp": datetime.now(UTC).isoformat(),
                **event,
            }
        )

    @wraps(original_send)
    def send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        host = (urlsplit(request.url).hostname or "").lower().rstrip(".")
        details = None
        if host in allowed_hosts:
            details = {
                "host": host,
                "method": request.method,
                "request_id": uuid4().hex,
                "request_headers": select_headers(request.headers, REQUEST_HEADERS),
            }
        token = active.set(details)
        started = perf_counter()
        try:
            return original_send(self, request, *args, **kwargs)
        except (ConnectionError, Timeout) as error:
            if details is not None:
                error_type = (
                    "SSLError"
                    if isinstance(error, SSLError)
                    else "Timeout"
                    if isinstance(error, Timeout)
                    else "ConnectionError"
                )
                emit(
                    {
                        **details,
                        "event": "transport_error",
                        "error_type": error_type,
                        "duration_ms": round((perf_counter() - started) * 1000, 3),
                    }
                )
            raise
        finally:
            active.reset(token)

    # shortcut: urllib3 has no public per-attempt observer, retest on major upgrades.
    @wraps(original_make_request)
    def make_request(
        self: Any, conn: Any, method: str, url: str, *args: Any, **kwargs: Any
    ) -> Any:
        started = perf_counter()
        response = original_make_request(self, conn, method, url, *args, **kwargs)
        if (details := active.get()) is not None:
            event = {
                **details,
                "event": "response",
                "status": response.status,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
            }
            if response.status >= 400:
                event["response_headers"] = select_headers(
                    response.headers, RESPONSE_HEADERS
                )
            else:
                event.pop("request_headers")
            emit(event)
        return response

    # These wrappers forward all arguments to the original methods.
    HTTPAdapter.send = send  # ty: ignore[invalid-assignment]
    HTTPConnectionPool._make_request = make_request  # ty: ignore[invalid-assignment]
    _installed = True
