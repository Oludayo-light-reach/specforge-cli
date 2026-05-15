"""Regression tests for missed user POSTs and premature assistant_closed."""

from __future__ import annotations

import threading

from spec_cli.prompts.schema import Session, Turn
from spec_cli.realtime.tracker import LiveCursor
from spec_cli.realtime.watcher import (
    WatcherOptions,
    _assistant_turn_fingerprint,
    _producer_tick,
    tail_stability_quiet_secs,
)


class _StubPoster:
    def __init__(self) -> None:
        self.events: list = []

    def send(self, event, *, timeout=None):  # type: ignore[no-untyped-def]
        self.events.append(event)
        return True, len(self.events)


class _StubGit:
    branch = "main"
    commit_sha = None
    author_name = "test"
    author_email = "test@example.com"


def _make_opts() -> WatcherOptions:
    return WatcherOptions(
        api_base="http://test",
        access_token="tok",
        project_id=1,
        project_label="test/test",
        poll_interval=2.0,
        presence_interval=15.0,
        broadcast=True,
        presence_enabled=False,
        mirror=False,
        verbose_assistant=False,
        broadcast_client_id="test-client",
        self_user_id=None,
    )


def test_clamp_prune_allows_repost_after_coalesce_shrink(
    tmp_path, monkeypatch
) -> None:
    """Shrinking transcripts must not mark every slot posted without POST."""

    sid = "shrink-session"
    session = Session(
        id=sid,
        source="cursor",
        title="t",
        turns=[
            Turn(role="user", text="prompt A", at=None),
            Turn(role="assistant", text="reply A", at=None),
        ],
        cwd=str(tmp_path),
        paths_touched=[],
        verbose=True,
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher._iter_local_sessions",
        lambda _paths: iter([session]),
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.historical_bundle_paths",
        lambda _root: [],
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.read_git_context", lambda _root: _StubGit()
    )

    poster = _StubPoster()
    cursor = LiveCursor.load(tmp_path, project_id=1)
    cursor.record_broadcast(sid, 0)
    with cursor._lock:
        cursor.posted_turn_keys[sid] = {"22:user", "23:assistant"}

    _producer_tick(
        bundle_root=tmp_path,
        cursor=cursor,
        poster=poster,
        opts=_make_opts(),
        stop_event=threading.Event(),
    )

    assert len(poster.events) >= 1
    assert poster.events[0].role == "user"
    assert poster.events[0].text == "prompt A"
    # Tail assistant stays on hold until stable — user POST is what mattered.
    assert cursor.turns_broadcast_for(sid) == 1
    # Content-hash keys: a different prompt at the same index is not "posted".
    assert not cursor.is_turn_posted(
        sid, 0, Turn(role="user", text="different prompt", at=None)
    )


def test_assistant_fingerprint_changes_when_tools_grow() -> None:
    from spec_cli.prompts.schema import ToolCall

    base = Turn(role="assistant", text="Working", summary="Working", at=None)
    with_tools = Turn(
        role="assistant",
        text="Working",
        summary="Working",
        at=None,
        tool_calls=[ToolCall(name="Read", args={"path": "x.py"})],
    )
    assert _assistant_turn_fingerprint(base) != _assistant_turn_fingerprint(
        with_tools
    )


def test_tail_stability_longer_when_tools_present() -> None:
    plain = tail_stability_quiet_secs(2.0, tool_count=0)
    tools = tail_stability_quiet_secs(2.0, tool_count=3)
    assert tools >= plain
    assert tools >= 20.0
