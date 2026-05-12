"""Tests for ``spec team watch`` Q/A coalescing (user immediate + paired block)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from spec_cli.commands.team import _TeamWatchQAState
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
