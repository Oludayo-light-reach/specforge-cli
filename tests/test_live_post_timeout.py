"""POST timeout helpers for Spec Live transport."""
from __future__ import annotations

import pytest

from spec_cli.realtime.transport import (
    HTTPPoster,
    POST_CONNECT_TIMEOUT_SECS,
    POST_READ_TIMEOUT_SECS,
    post_timeout,
)


def test_post_timeout_default_is_connect_read_tuple() -> None:
    assert post_timeout() == (POST_CONNECT_TIMEOUT_SECS, POST_READ_TIMEOUT_SECS)


def test_post_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEC_LIVE_POST_TIMEOUT_SECS", "90")
    assert post_timeout() == (POST_CONNECT_TIMEOUT_SECS, 90.0)


def test_http_poster_uses_tuple_timeout_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[object] = []

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {"id": 1}

    class _Session:
        def post(self, url, json, timeout):  # type: ignore[no-untyped-def]
            captured.append(timeout)
            return _Resp()

        def close(self) -> None:
            pass

    poster = HTTPPoster("https://example.com", "tok", 1)
    monkeypatch.setattr(poster, "_session", _Session())
    ok, ev_id = poster.send(
        __import__("spec_cli.realtime.events", fromlist=["OutgoingEvent"]).OutgoingEvent(
            session_id="s",
            source="manual",
            role="user",
            summary="hi",
            text="hi",
        )
    )
    assert ok is True
    assert ev_id == 1
    assert captured == [post_timeout()]
