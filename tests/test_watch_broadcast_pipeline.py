"""End-to-end-ish test: POST prompt event → SSE consumer receives it.

Uses a local stub HTTP server so we exercise the real ``HTTPPoster`` and
``SSEConsumer`` without Spec Cloud.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from spec_cli.realtime.events import IncomingEvent, OutgoingEvent
from spec_cli.realtime.transport import HTTPPoster, SSEConsumer


class _StubHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._next_id = 0
        self._listeners: list[threading.Event] = []

    def post(self, payload: dict) -> dict:
        with self._lock:
            self._next_id += 1
            row = {
                **payload,
                "id": self._next_id,
                "project_id": 1,
                "author": {
                    "user_id": 1,
                    "handle": "alice",
                    "name": "Alice",
                },
                "received_at": "2026-05-15T12:00:00+00:00",
            }
            self._events.append(row)
            for ev in self._listeners:
                ev.set()
        return row

    def wait_for(self, count: int, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self._events) >= count:
                    return
            time.sleep(0.02)
        raise TimeoutError(f"expected {count} events, got {len(self._events)}")


_HUB = _StubHub()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        row = _HUB.post(body)
        data = json.dumps(row).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.endswith("/prompt-stream"):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last_id = int(self.headers.get("Last-Event-ID", "0") or "0")
        sent = 0
        while not self.wfile.closed:
            with _HUB._lock:
                pending = [e for e in _HUB._events if e["id"] > last_id]
            for row in pending:
                last_id = row["id"]
                frame = (
                    f"id: {row['id']}\n"
                    f"event: turn\n"
                    f"data: {json.dumps(row)}\n\n"
                )
                self.wfile.write(frame.encode())
                self.wfile.flush()
                sent += 1
            if sent >= 1:
                # One event is enough for the test; keep connection open briefly.
                time.sleep(0.05)
                return
            time.sleep(0.02)


@pytest.fixture
def stub_api() -> str:
    _HUB._events.clear()
    _HUB._next_id = 0
    # Threading server so a blocking GET (waiting for POST) cannot deadlock
    # the stub when POST arrives on another client connection.
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base
    server.shutdown()
    thread.join(timeout=2.0)


def test_post_then_sse_delivers_turn(stub_api: str) -> None:
    poster = HTTPPoster(stub_api, "tok", project_id=1)
    consumer = SSEConsumer(stub_api, "tok", project_id=1, verbose=True)
    received: list[IncomingEvent] = []
    stop = threading.Event()

    def _reader() -> None:
        for item in consumer.stream():
            if isinstance(item, IncomingEvent):
                received.append(item)
                stop.set()
                consumer.stop()
                return

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    time.sleep(0.1)

    outgoing = OutgoingEvent(
        session_id="sess-pipe",
        source="manual",
        role="user",
        summary="hello team",
        text="hello team",
    )
    ok, created_id = poster.send(outgoing)
    assert ok is True
    assert created_id == 1

    stop.wait(timeout=3.0)
    t.join(timeout=2.0)
    poster.close()

    assert len(received) == 1
    assert received[0].id == 1
    assert received[0].role == "user"
    assert "hello" in (received[0].text or "")
