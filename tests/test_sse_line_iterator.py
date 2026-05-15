"""Tests for safe SSE line iteration when the socket closes mid-read."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

from spec_cli.realtime.transport import _iter_sse_lines


def test_iter_sse_lines_survives_closed_socket_read() -> None:
    resp = MagicMock()
    resp.iter_lines.side_effect = AttributeError("'NoneType' object has no attribute 'read'")
    assert list(_iter_sse_lines(resp)) == []


def test_iter_sse_lines_honours_stop_event() -> None:
    resp = MagicMock()

    def _lines():
        yield "id: 1"
        yield "data: hi"
        yield ""

    resp.iter_lines.return_value = _lines()
    stop = threading.Event()
    stop.set()
    assert list(_iter_sse_lines(resp, stop_event=stop)) == []
