"""Tests for ``spec team watch`` Q/A coalescing (user immediate + paired block)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from spec_cli.commands.team import (
    _TeamWatchQAState,
    _resolve_assistant_quiet_secs,
)
from spec_cli.realtime.events import IncomingEvent


def _ev(
    *,
    id: int,
    role: str,
    text: str | None,
    summary: str | None = None,
    closes_event_id: int | None = None,
    project_id: int = 1,
    session_id: str = "sess",
) -> IncomingEvent:
    ts = datetime.now(timezone.utc)
    return IncomingEvent(
        id=id,
        project_id=project_id,
        session_id=session_id,
        source="cursor",
        role=role,
        branch="main",
        commit_sha=None,
        model="default",
        summary=summary,
        text=text,
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=1,
        author_handle="alice",
        author_name="Alice",
        author_avatar_url=None,
        closes_event_id=closes_event_id,
    )


def test_merge_assistant_chunks_prefers_longest_text_and_latest_meta() -> None:
    a = _ev(id=10, role="assistant", text="partial")
    b = _ev(id=11, role="assistant", text="partial\n\nfull body here")
    merged = _TeamWatchQAState._merge_assistant_chunks([a, b])
    assert merged.id == 11
    assert "full body here" in (merged.text or "")


def test_merge_assistant_chunks_tie_length_prefers_higher_id() -> None:
    """When two bodies tie on length, the newer row wins (streaming snapshot)."""
    a = _ev(id=10, role="assistant", text="12345")
    b = _ev(id=11, role="assistant", text="abcde")
    merged = _TeamWatchQAState._merge_assistant_chunks([a, b])
    assert merged.text == "abcde"
    assert merged.id == 11


def test_merge_assistant_chunks_single() -> None:
    a = _ev(id=3, role="assistant", text="only")
    merged = _TeamWatchQAState._merge_assistant_chunks([a])
    assert merged.text == "only"


def test_merge_assistant_chunks_empty_raises() -> None:
    with pytest.raises(ValueError):
        _TeamWatchQAState._merge_assistant_chunks([])


def test_resolve_assistant_quiet_secs_cli_overrides_env(monkeypatch) -> None:
    monkeypatch.setenv("SPEC_TEAM_WATCH_ASSISTANT_QUIET_SECS", "99")
    assert _resolve_assistant_quiet_secs(12.0) == 12.0


def test_resolve_assistant_quiet_secs_env(monkeypatch) -> None:
    monkeypatch.setenv("SPEC_TEAM_WATCH_ASSISTANT_QUIET_SECS", "3600")
    assert _resolve_assistant_quiet_secs(None) == 3600.0


def test_resolve_assistant_quiet_secs_zero_means_timer_off(monkeypatch) -> None:
    monkeypatch.delenv("SPEC_TEAM_WATCH_ASSISTANT_QUIET_SECS", raising=False)
    assert _resolve_assistant_quiet_secs(0.0) == 0.0


def test_resolve_assistant_quiet_secs_default_from_constant(monkeypatch) -> None:
    monkeypatch.delenv("SPEC_TEAM_WATCH_ASSISTANT_QUIET_SECS", raising=False)
    assert _resolve_assistant_quiet_secs(None) == 120.0


def test_buffer_assistant_requires_same_project_and_session() -> None:
    qa = _TeamWatchQAState()
    qa.pending_user = _ev(id=1, role="user", text="hi", session_id="aaa")
    assert not qa.buffer_assistant(
        _ev(id=2, role="assistant", text="wrong", session_id="bbb")
    )
    assert qa.assistant_chunks == []


def test_flush_pair_drops_cross_session_chunks_before_merge() -> None:
    qa = _TeamWatchQAState()
    n = MagicMock()
    qa.pending_user = _ev(id=1, role="user", text="hey", session_id="aaa")
    qa.assistant_chunks = [
        _ev(id=2, role="assistant", text="stray", session_id="bbb"),
        _ev(id=3, role="assistant", text="ok", session_id="aaa"),
    ]
    assert qa.flush_pair(n)
    n.show_completed_pair.assert_called_once()
    u, a = n.show_completed_pair.call_args[0]
    assert u.session_id == "aaa"
    assert a.session_id == "aaa"
    assert "ok" in (a.text or "")


def test_flush_on_assistant_closed_matches_session() -> None:
    qa = _TeamWatchQAState()
    n = MagicMock()
    qa.pending_user = _ev(id=1, role="user", text="hi")
    qa.assistant_chunks = [_ev(id=2, role="assistant", text="a")]
    assert qa.flush_on_assistant_closed(
        _ev(id=5, role="assistant_closed", text=None, closes_event_id=2),
        n,
        [0.0],
    )
    n.show_completed_pair.assert_called_once()


def test_flush_on_assistant_closed_wrong_closes_id_skips() -> None:
    qa = _TeamWatchQAState()
    n = MagicMock()
    qa.pending_user = _ev(id=1, role="user", text="hi")
    qa.assistant_chunks = [_ev(id=2, role="assistant", text="a")]
    assert not qa.flush_on_assistant_closed(
        _ev(id=5, role="assistant_closed", text=None, closes_event_id=99),
        n,
        [0.0],
    )
    n.show_completed_pair.assert_not_called()


def test_flush_on_assistant_closed_none_closes_id_still_flushes() -> None:
    qa = _TeamWatchQAState()
    n = MagicMock()
    qa.pending_user = _ev(id=1, role="user", text="hi")
    qa.assistant_chunks = [_ev(id=2, role="assistant", text="a")]
    assert qa.flush_on_assistant_closed(
        _ev(id=5, role="assistant_closed", text=None, closes_event_id=None),
        n,
        [0.0],
    )
    n.show_completed_pair.assert_called_once()


def test_tick_quiet_flush_skipped_when_quiet_secs_zero(monkeypatch) -> None:
    """``quiet_secs=0`` never flushes on idle — only user/error/shutdown."""
    monkeypatch.setattr("time.monotonic", lambda: 1e12)
    qa = _TeamWatchQAState()
    qa.pending_user = _ev(id=1, role="user", text="hi")
    qa.assistant_chunks = [_ev(id=2, role="assistant", text="yo")]
    qa.last_assistant_mono = 0.0
    n = MagicMock()
    qa.tick_quiet_flush(n, [0.0], quiet_secs=0.0)
    n.show_completed_pair.assert_not_called()
