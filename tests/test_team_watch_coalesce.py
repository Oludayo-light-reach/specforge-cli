"""Tests for ``spec team watch`` Q/A coalescing (user immediate + paired block)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from spec_cli.commands.team import (
    _TeamWatchQAState,
    _assistant_has_reviewable_prose,
    _resolve_assistant_quiet_secs,
    _user_from_rest_before_assistant,
)
from spec_cli.api import ApiError
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


def test_merge_assistant_chunks_tie_length_disjoint_keeps_both() -> None:
    """Equal-length bodies that are not cumulative must not drop the first row."""
    a = _ev(id=10, role="assistant", text="12345")
    b = _ev(id=11, role="assistant", text="abcde")
    merged = _TeamWatchQAState._merge_assistant_chunks([a, b])
    assert "12345" in (merged.text or "")
    assert "abcde" in (merged.text or "")
    assert merged.id == 11


def test_merge_assistant_chunks_single() -> None:
    a = _ev(id=3, role="assistant", text="only")
    merged = _TeamWatchQAState._merge_assistant_chunks([a])
    assert merged.text == "only"


def test_merge_assistant_chunks_joins_disjoint_segments() -> None:
    """Non-cumulative rows (e.g. separate paragraphs per snapshot) must not be dropped."""
    a = _ev(id=10, role="assistant", text="First paragraph alpha.")
    b = _ev(id=11, role="assistant", text="Second paragraph beta.")
    merged = _TeamWatchQAState._merge_assistant_chunks([a, b])
    assert "First paragraph alpha" in (merged.text or "")
    assert "Second paragraph beta" in (merged.text or "")
    assert "\n\n" in (merged.text or "")


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
    assert _resolve_assistant_quiet_secs(None) == 60.0


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


def test_merge_unions_tool_calls_across_chunks() -> None:
    from dataclasses import replace

    from spec_cli.realtime.events import ToolCallPayload

    r = ToolCallPayload(name="Read", args={"path": "a.py"}, status=None)
    e = ToolCallPayload(name="Edit", args={"path": "b.py"}, status=None)
    c1 = replace(_ev(id=10, role="assistant", text="a"), tool_calls=[r])
    c2 = replace(_ev(id=11, role="assistant", text="ab"), tool_calls=[e])
    merged = _TeamWatchQAState._merge_assistant_chunks([c1, c2])
    names = [t.name for t in merged.tool_calls]
    assert names == ["Read", "Edit"]


def test_merge_tool_calls_dedupes_identical_entries() -> None:
    from dataclasses import replace

    from spec_cli.realtime.events import ToolCallPayload

    r = ToolCallPayload(name="Read", args={"path": "a.py"}, status=None)
    c1 = replace(_ev(id=10, role="assistant", text="a"), tool_calls=[r])
    c2 = replace(_ev(id=11, role="assistant", text="ab"), tool_calls=[r])
    merged = _TeamWatchQAState._merge_assistant_chunks([c1, c2])
    assert len(merged.tool_calls) == 1


def test_flush_pair_prefers_cloud_tail_over_partial_sse_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qa = _TeamWatchQAState()
    n = MagicMock()
    qa.pending_user = _ev(id=10, role="user", text="question")
    qa.assistant_chunks = [_ev(id=11, role="assistant", text="partial")]
    qa.pair_cloud = MagicMock()

    def _fake_tail(_client: object, pending: IncomingEvent) -> list[IncomingEvent]:
        assert pending.id == 10
        return [
            _ev(id=11, role="assistant", text="partial"),
            _ev(
                id=12,
                role="assistant",
                text="much longer assistant body from stored snapshots",
            ),
        ]

    monkeypatch.setattr(
        "spec_cli.commands.team._assistant_tail_from_rest_after_user",
        _fake_tail,
    )
    assert qa.flush_pair(n)
    _u, merged = n.show_completed_pair.call_args[0]
    assert "much longer assistant" in (merged.text or "")


def test_bootstrap_merge_reserves_user_slots() -> None:
    from spec_cli.commands.team import _build_team_watch_bootstrap_events

    client = MagicMock()
    client.list_my_prompt_events.side_effect = [
        [{"id": 3, "role": "user", "session_id": "s", "project_id": 1,
          "source": "cursor", "text": "q3"}],
        [
            {"id": 10, "role": "assistant", "session_id": "s", "project_id": 1,
             "source": "codex", "text": "a10"},
            {"id": 9, "role": "assistant", "session_id": "s", "project_id": 1,
             "source": "codex", "text": "a9"},
        ],
    ]
    events = _build_team_watch_bootstrap_events(
        client, limit=3, include_presence=False
    )
    assert len(events) == 3
    assert any(e.role == "user" for e in events)


def test_on_user_skips_same_cloud_row_id_replay() -> None:
    """SSE warm-up can redeliver the same user row id — show once."""
    qa = _TeamWatchQAState()
    n = MagicMock()
    u = _ev(id=1, role="user", text="same question", session_id="s1")
    qa.on_user(u, n, [0.0])
    assert n.show.call_count == 1
    qa.on_user(u, n, [0.0])
    assert n.show.call_count == 1


def test_on_user_distinct_ids_same_body_shows_twice() -> None:
    """Two Cloud rows with different ids are two prompts — never drop."""
    qa = _TeamWatchQAState()
    n = MagicMock()
    u1 = _ev(id=1, role="user", text="same question", session_id="s1")
    u2 = _ev(id=99, role="user", text="same question", session_id="s1")
    qa.on_user(u1, n, [0.0])
    qa.on_user(u2, n, [0.0])
    assert n.show.call_count == 2


def test_on_user_bootstrap_still_shows_duplicate_text() -> None:
    qa = _TeamWatchQAState()
    n = MagicMock()
    u1 = _ev(id=1, role="user", text="same question", session_id="s1")
    u2 = _ev(id=99, role="user", text="same question", session_id="s1")
    qa.on_user(u1, n, [0.0], is_bootstrap=True)
    qa.on_user(u2, n, [0.0], is_bootstrap=True)
    assert n.show.call_count == 2


def test_flush_pair_allows_same_body_distinct_user_rows() -> None:
    """Two real prompts with identical text must not be collapsed."""
    qa = _TeamWatchQAState()
    n = MagicMock()
    u = _ev(id=1, role="user", text="hey", session_id="s1")
    a = _ev(id=2, role="assistant", text="reply", session_id="s1")
    qa.pending_user = u
    qa.assistant_chunks = [a]
    assert qa.flush_pair(n)
    assert n.show_completed_pair.call_count == 1
    qa.pending_user = _ev(id=3, role="user", text="hey", session_id="s1")
    qa.assistant_chunks = [_ev(id=4, role="assistant", text="reply", session_id="s1")]
    assert qa.flush_pair(n)
    assert n.show_completed_pair.call_count == 2


def test_flush_pair_skips_reflush_same_user_event_id() -> None:
    qa = _TeamWatchQAState()
    n = MagicMock()
    u = _ev(id=1, role="user", text="hey", session_id="s1")
    a = _ev(id=2, role="assistant", text="reply", session_id="s1")
    qa.pending_user = u
    qa.assistant_chunks = [a]
    assert qa.flush_pair(n)
    qa.pending_user = u
    qa.assistant_chunks = [a]
    assert not qa.flush_pair(n)
    assert n.show_completed_pair.call_count == 1


def test_flush_on_assistant_closed_uses_cloud_when_sse_buffer_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qa = _TeamWatchQAState()
    n = MagicMock()
    qa.pending_user = _ev(id=1, role="user", text="hi")
    qa.assistant_chunks = []
    qa.pair_cloud = MagicMock()
    monkeypatch.setattr(
        "spec_cli.commands.team._assistant_tail_from_rest_after_user",
        lambda _c, _p: [_ev(id=2, role="assistant", text="from cloud")],
    )
    assert qa.flush_on_assistant_closed(
        _ev(id=5, role="assistant_closed", text=None, closes_event_id=2),
        n,
        [0.0],
    )
    n.show_completed_pair.assert_called_once()


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


def test_tick_quiet_flush_cloud_only_assistant_without_sse_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle flush runs when Cloud has assistant rows but SSE buffer is empty."""
    monkeypatch.setattr("time.monotonic", lambda: 500.0)
    qa = _TeamWatchQAState()
    n = MagicMock()
    qa.pending_user = _ev(id=1, role="user", text="hi")
    qa.assistant_chunks = []
    qa.pending_since_mono = 0.0
    qa.pair_cloud = MagicMock()
    monkeypatch.setattr(
        "spec_cli.commands.team._assistant_tail_from_rest_after_user",
        lambda _c, _p: [_ev(id=2, role="assistant", text="from cloud only")],
    )
    qa.tick_quiet_flush(n, [0.0], quiet_secs=10.0)
    n.show_completed_pair.assert_called_once()


def test_assistant_has_reviewable_prose_rejects_redacted_only_body() -> None:
    assert not _assistant_has_reviewable_prose(
        _ev(id=1, role="assistant", text="[REDACTED]", summary="[REDACTED]")
    )
    assert _assistant_has_reviewable_prose(
        _ev(
            id=2,
            role="assistant",
            text="[REDACTED]",
            summary="Investigating the live feed.",
        )
    )


def _api_row(ev: IncomingEvent) -> dict:
    return {
        "id": ev.id,
        "project_id": ev.project_id,
        "session_id": ev.session_id,
        "source": ev.source,
        "role": ev.role,
        "branch": ev.branch,
        "text": ev.text,
        "summary": ev.summary,
        "turn_at": ev.turn_at.isoformat().replace("+00:00", "Z"),
        "received_at": ev.received_at.isoformat().replace("+00:00", "Z"),
        "author": {
            "user_id": ev.author_user_id,
            "handle": ev.author_handle,
            "name": ev.author_name,
        },
    }


def test_user_from_rest_before_assistant_finds_latest_user() -> None:
    client = MagicMock()
    user = _ev(id=10, role="user", text="my prompt")
    assistant = _ev(id=12, role="assistant", text="reply")
    client.list_prompt_events.return_value = [
        _api_row(user),
        _api_row(_ev(id=11, role="assistant", text="other thread", session_id="other")),
        _api_row(assistant),
    ]
    found = _user_from_rest_before_assistant(client, assistant)
    assert found is not None
    assert found.id == 10
    assert found.text == "my prompt"


def test_user_from_rest_before_assistant_api_error_returns_none() -> None:
    client = MagicMock()
    client.list_prompt_events.side_effect = ApiError("nope")
    assert _user_from_rest_before_assistant(
        client, _ev(id=2, role="assistant", text="x")
    ) is None


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
