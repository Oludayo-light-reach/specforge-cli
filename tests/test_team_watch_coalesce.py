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
) -> IncomingEvent:
    ts = datetime.now(timezone.utc)
    return IncomingEvent(
        id=id,
        project_id=1,
        session_id="sess",
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
    )


def test_merge_assistant_chunks_prefers_longest_text_and_latest_meta() -> None:
    a = _ev(id=10, role="assistant", text="partial")
    b = _ev(id=11, role="assistant", text="partial\n\nfull body here")
    merged = _TeamWatchQAState._merge_assistant_chunks([a, b])
    assert merged.id == 11
    assert "full body here" in (merged.text or "")


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
