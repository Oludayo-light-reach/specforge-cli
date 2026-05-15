"""Integration: POST prompt-event then receive it over SSE (in-process hub mock)."""
from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from spec_cli.realtime.events import OutgoingEvent
from spec_cli.realtime.transport import HTTPPoster, SSEConsumer, run_consumer_in_thread


class _LiveHub:
    def __init__(self) -> None:
        self._subs: list[queue.Queue[str]] = []
        self._lock = threading.Lock()
        self._next_id = 0

    def publish(self, frame: str) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(frame)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=64)
        with self._lock:
            self._subs.append(q)
        return q

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id


_HUB = _LiveHub()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:  # noqa: D401
        return

    def do_POST(self) -> None:
        if not self.path.endswith("/prompt-events"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        eid = _HUB.next_id()
        out = {
            "id": eid,
            "project_id": 1,
            "session_id": body.get("session_id") or "sess",
            "source": body.get("source") or "cursor",
            "role": body.get("role") or "user",
            "branch": body.get("branch"),
            "summary": body.get("summary"),
            "text": body.get("text"),
            "cwd": body.get("cwd"),
            "paths_touched": body.get("paths_touched") or [],
            "author": {
                "user_id": 1,
                "handle": "alice",
                "name": "Alice",
            },
        }
        frame = f"id: {eid}\nevent: turn\ndata: {json.dumps(out)}\n\n"
        _HUB.publish(frame)
        out = json.dumps({"id": eid}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self) -> None:
        if "prompt-stream" not in self.path:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b": connected\n\n")
        sub = _HUB.subscribe()
        try:
            while True:
                try:
                    frame = sub.get(timeout=0.3)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(frame.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="module")
def live_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def test_post_then_sse_delivers_turn(live_server: str) -> None:
    """Simulate cloud fan-out: POST creates row, SSE client receives it."""
    base = live_server
    received: queue.Queue[int] = queue.Queue()

    connected = threading.Event()

    consumer = SSEConsumer(
        base,
        "token",
        project_id=1,
        on_connect=lambda: connected.set(),
    )
    thread = run_consumer_in_thread(
        consumer,
        on_event=lambda ev: received.put(ev.id),
        on_fatal=lambda err: pytest.fail(str(err)),
    )
    assert connected.wait(timeout=3.0), "SSE consumer did not connect"

    poster = HTTPPoster(base, "token", project_id=1)
    evt = OutgoingEvent(
        session_id="sess-integration",
        source="cursor",
        role="user",
        branch="main",
        summary="integration probe",
        text="hello from integration test",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=None,
    )
    ok, created = poster.send(evt, timeout=5.0)
    assert ok and created is not None

    deadline = time.monotonic() + 5.0
    got: int | None = None
    while time.monotonic() < deadline:
        try:
            got = received.get(timeout=0.2)
            break
        except queue.Empty:
            continue
    consumer.stop()
    thread.join(timeout=2.0)
    assert got == created
