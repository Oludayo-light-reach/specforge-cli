"""Shutdown / Ctrl+C responsiveness tests for ``spec watch``.

The user-facing requirement: when someone hits Ctrl+C in the terminal
running ``spec watch``, the process must exit within a few seconds —
not 60 (the SSE read timeout) and definitely not "never".

Three things are tested:

1. ``SSEConsumer.stop()`` closes the active streaming response so the
   reader thread unblocks immediately, instead of waiting for the
   next read timeout. This is the bug that was causing the original
   "Ctrl+C does nothing" report — without it, ``stop()`` only set a
   flag the consumer didn't check until the next byte arrived.
2. ``_producer_tick`` honours ``stop_event`` between sessions and
   between turns, so a fresh ``spec watch`` against a long-quiet
   bundle (with a backlog of dozens of turns to broadcast) doesn't
   block the shutdown for tens of seconds.
3. ``run_watcher`` exits within a couple of seconds of ``stop_event``
   being set, even when the consumer is idle on a long-lived SSE
   connection.

We can't simply send a real ``SIGINT`` from the test — the watcher
runs in a background thread (so its signal handler isn't installed
on the main thread) and pytest's own ``KeyboardInterrupt`` handling
would tear down the test session. Instead we use the externally-
owned ``stop_event`` parameter on ``run_watcher`` (the same event
the in-process SIGINT handler sets) to drive shutdown.
"""
from __future__ import annotations

import threading
import time

import pytest

from spec_cli.realtime.transport import SSEConsumer
from spec_cli.realtime.watcher import (
    WatcherOptions,
    _producer_tick,
    run_watcher,
)


# ── stub transports ─────────────────────────────────────────────────


class _StubPoster:
    """In-memory ``HTTPPoster`` replacement.

    ``send`` records the event and returns True. With ``post_delay``
    > 0 it sleeps to simulate a slow network — useful for verifying
    the producer interrupts mid-tick when ``stop_event`` is set.
    """

    def __init__(self, *, post_delay: float = 0.0) -> None:
        self.events: list = []
        self.post_delay = post_delay
        self.closed = False

    def send(self, event, *, timeout=None):  # type: ignore[no-untyped-def]
        self.events.append(event)
        if self.post_delay > 0:
            time.sleep(self.post_delay)
        # Mirror real ``HTTPPoster.send`` — success plus monotonic ids.
        self._seq = getattr(self, "_seq", 0) + 1
        return True, self._seq

    def close(self) -> None:
        self.closed = True


class _StubConsumer:
    """Replacement for ``SSEConsumer`` that idles forever (like a
    healthy SSE connection with only keepalives) until ``stop()`` is
    called. Mirrors the real interface exactly."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self.stopped = False

    def set_resume_cursor(self, _last_id) -> None:  # type: ignore[no-untyped-def]
        pass

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    def stream(self):
        # Block until stopped — mimics an idle stream (server pings,
        # no events). Yielding nothing is fine; the watcher only
        # checks for events, never demands them.
        self._stop.wait()
        return
        yield  # unreachable; keeps this a generator function


@pytest.fixture
def patched_transports(monkeypatch):
    """Replace network surfaces with stubs so the watcher runs
    entirely in-process. Returns the stub instances so tests can
    inspect them after ``run_watcher`` returns."""

    poster = _StubPoster()
    consumer = _StubConsumer()
    consumer_thread_box: list[threading.Thread] = []

    monkeypatch.setattr(
        "spec_cli.realtime.watcher.HTTPPoster",
        lambda *a, **kw: poster,
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.SSEConsumer",
        lambda *a, **kw: consumer,
    )

    # The real ``run_consumer_in_thread`` returns a daemon thread
    # that drives our stub's ``stream()``. We can use the real one —
    # the stub's ``stream()`` already idles correctly.
    real_run_in_thread = pytest.importorskip(
        "spec_cli.realtime.transport"
    ).run_consumer_in_thread

    def _capture(c, on_event, on_fatal):
        t = real_run_in_thread(c, on_event=on_event, on_fatal=on_fatal)
        consumer_thread_box.append(t)
        return t

    monkeypatch.setattr(
        "spec_cli.realtime.watcher.run_consumer_in_thread", _capture
    )

    class _StubGit:
        branch = "main"
        commit_sha = None
        author_name = "test"
        author_email = "test@example.com"

    monkeypatch.setattr(
        "spec_cli.realtime.watcher.read_git_context", lambda _root: _StubGit()
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher._iter_local_sessions", lambda _paths: iter([])
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.compute_local_presence",
        lambda _root: _empty_local_presence(),
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.historical_bundle_paths",
        lambda _root: [],
    )
    return poster, consumer, consumer_thread_box


def _empty_local_presence():
    from spec_cli.realtime.presence import LocalPresence

    return LocalPresence(files=[], head_commit=None, fingerprint="empty")


def _make_opts() -> WatcherOptions:
    return WatcherOptions(
        project_id=1,
        project_label="alice/demo",
        api_base="http://localhost",
        access_token="t",
        self_user_id=1,
        self_handle="alice",
        self_name="Alice",
        poll_interval=0.5,
        broadcast=True,
        receive=True,
        mirror=False,
        # presence + receive are exercised through the stub, but we
        # don't need them for the shutdown contract; turn off so the
        # presence interval doesn't compete with the test timing.
        presence_enabled=False,
    )


# ── Issue 1: SSEConsumer.stop() closes the active response ─────────


def test_sse_consumer_stop_closes_active_response():
    """The original bug: ``stop()`` only set a flag, leaving the
    consumer thread blocked in ``iter_lines`` until the next 60s
    read timeout. The fix tracks the active response and closes it
    from ``stop()`` so the iterator raises immediately."""
    consumer = SSEConsumer(
        api_base="http://example.invalid",
        access_token="t",
        project_id=1,
    )

    class _FakeResp:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    fake = _FakeResp()
    with consumer._resp_lock:  # noqa: SLF001
        consumer._active_response = fake  # noqa: SLF001

    consumer.stop()

    assert fake.closed is True
    assert consumer._active_response is None  # noqa: SLF001


def test_sse_consumer_stop_is_idempotent():
    """``stop()`` must be safe to call multiple times — once during
    ``run_watcher``'s normal shutdown, once again from a ``finally``
    block in embedding code, etc. Should never raise."""
    consumer = SSEConsumer(
        api_base="http://example.invalid",
        access_token="t",
        project_id=1,
    )

    consumer.stop()  # no active response
    consumer.stop()  # second call — also no-op

    closed_count = [0]

    class _FakeResp:
        def close(self):
            closed_count[0] += 1

    with consumer._resp_lock:  # noqa: SLF001
        consumer._active_response = _FakeResp()  # noqa: SLF001

    consumer.stop()
    assert closed_count[0] == 1
    # Calling again with no active response is still safe.
    consumer.stop()
    assert closed_count[0] == 1


# ── Issue 2: _producer_tick honours stop_event mid-tick ────────────


def test_producer_tick_bails_when_stop_event_pre_set(tmp_path, monkeypatch):
    """If the stop event is already set when ``_producer_tick`` is
    called, it should return immediately — not even start scanning
    transcripts."""
    poster = _StubPoster()
    stop_event = threading.Event()
    stop_event.set()

    monkeypatch.setattr(
        "spec_cli.realtime.watcher.read_git_context",
        lambda _root: pytest.fail("git read attempted after stop"),
    )

    from spec_cli.realtime.tracker import LiveCursor

    cursor = LiveCursor.load(tmp_path, project_id=1)
    _producer_tick(
        bundle_root=tmp_path,
        cursor=cursor,
        poster=poster,
        opts=_make_opts(),
        stop_event=stop_event,
    )
    assert poster.events == []


def test_producer_tick_bails_between_turns(tmp_path, monkeypatch):
    """When the producer is mid-way through broadcasting a backlog,
    setting ``stop_event`` between turns must abort the rest of the
    tick. Otherwise a 50-turn backlog at 15s timeout each could
    block shutdown for over a minute."""
    poster = _StubPoster()
    stop_event = threading.Event()

    # Build a fake session with 5 user turns. After the first send,
    # we set stop_event — the tick should send 1 turn and stop.
    from spec_cli.prompts.schema import Session, Turn

    turns = [
        Turn(role="user", text=f"hello {i}", at=None) for i in range(5)
    ]
    session = Session(
        id="s1",
        source="cursor",
        title="t",
        turns=turns,
        cwd=str(tmp_path),
        paths_touched=[],
    )

    monkeypatch.setattr(
        "spec_cli.realtime.watcher._iter_local_sessions",
        lambda _paths: iter([session]),
    )
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.historical_bundle_paths",
        lambda _root: [],
    )

    class _StubGit:
        branch = "main"
        commit_sha = None
        author_name = "test"
        author_email = "test@example.com"

    monkeypatch.setattr(
        "spec_cli.realtime.watcher.read_git_context", lambda _root: _StubGit()
    )

    sent_count = [0]
    original_send = poster.send

    def _send(event, *, timeout=None):
        sent_count[0] += 1
        if sent_count[0] == 1:
            # Mimic what SIGINT would do: set the stop event after
            # the first delivery. Subsequent turns must NOT fire.
            stop_event.set()
        return original_send(event, timeout=timeout)

    poster.send = _send  # type: ignore[assignment]

    from spec_cli.realtime.tracker import LiveCursor

    cursor = LiveCursor.load(tmp_path, project_id=1)
    _producer_tick(
        bundle_root=tmp_path,
        cursor=cursor,
        poster=poster,
        opts=_make_opts(),
        stop_event=stop_event,
    )
    # Exactly one turn should have made it onto the wire.
    assert sent_count[0] == 1


@pytest.mark.parametrize("source", ("cursor", "claude_code", "codex"))
def test_producer_tail_assistant_reposts_then_advances_when_stable(
    tmp_path, monkeypatch, source: str
):
    """Final assistant turn may grow on disk between polls (all sources).

    ``broadcast_turns`` stays pinned until the body fingerprint is
    quiet for ``max(5s, 3×poll_interval)``, so we can POST updates while
    text streams — same rules for cursor, Claude Code, and Codex."""
    from spec_cli.prompts.schema import Session, Turn
    from spec_cli.realtime.tracker import LiveCursor

    sid = f"s1-{source}"
    u = Turn(role="user", text="hello", at=None)
    a = Turn(role="assistant", text="part", at=None)
    session = Session(
        id=sid,
        source=source,
        title="t",
        turns=[u, a],
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

    class _StubGit:
        branch = "main"
        commit_sha = None
        author_name = "test"
        author_email = "test@example.com"

    monkeypatch.setattr(
        "spec_cli.realtime.watcher.read_git_context", lambda _root: _StubGit()
    )

    clock = [0.0]

    def _mono() -> float:
        return clock[0]

    monkeypatch.setattr("spec_cli.realtime.watcher.time.monotonic", _mono)
    monkeypatch.setattr(
        "spec_cli.realtime.watcher.tail_stability_quiet_secs",
        lambda poll_interval, tool_count=0: max(5.0, poll_interval * 3.0),
    )

    poster = _StubPoster()
    cursor = LiveCursor.load(tmp_path, project_id=1)
    holds: dict = {}
    stop_event = threading.Event()

    cloud_ids: dict[str, int] = {}
    _producer_tick(
        bundle_root=tmp_path,
        cursor=cursor,
        poster=poster,
        opts=_make_opts(),
        stop_event=stop_event,
        assistant_tail_holds=holds,
        last_assistant_cloud_ids=cloud_ids,
    )
    assert len(poster.events) == 2
    assert cursor.turns_broadcast_for(sid) == 1

    clock[0] = 0.1
    a.text = "part — full reply"
    _producer_tick(
        bundle_root=tmp_path,
        cursor=cursor,
        poster=poster,
        opts=_make_opts(),
        stop_event=stop_event,
        assistant_tail_holds=holds,
        last_assistant_cloud_ids=cloud_ids,
    )
    assert len(poster.events) == 3
    assert cursor.turns_broadcast_for(sid) == 1

    clock[0] = 10.0
    _producer_tick(
        bundle_root=tmp_path,
        cursor=cursor,
        poster=poster,
        opts=_make_opts(),
        stop_event=stop_event,
        assistant_tail_holds=holds,
        last_assistant_cloud_ids=cloud_ids,
    )
    assert len(poster.events) == 4
    assert poster.events[-1].role == "assistant_closed"
    assert poster.events[-1].closes_event_id == 3
    assert cursor.turns_broadcast_for(sid) == 2


@pytest.mark.parametrize("source", ("cursor", "claude_code", "codex"))
def test_producer_does_not_skip_empty_tail_assistant_slot(
    tmp_path, monkeypatch, source: str
):
    """Do not advance past an unshippable final assistant turn (all sources).

    If ``_build_outgoing`` returns ``None`` for that slot, retry next
    poll — otherwise ``spec team watch`` can sit on heartbeats with no
    AI row after the user prompt."""
    from spec_cli.prompts.schema import Session, Turn
    from spec_cli.realtime.tracker import LiveCursor

    sid = f"s2-{source}"
    u = Turn(role="user", text="hello", at=None)
    a = Turn(role="assistant", text="", at=None, summary=None)
    session = Session(
        id=sid,
        source=source,
        title="t",
        turns=[u, a],
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

    class _StubGit:
        branch = "main"
        commit_sha = None
        author_name = "test"
        author_email = "test@example.com"

    monkeypatch.setattr(
        "spec_cli.realtime.watcher.read_git_context", lambda _root: _StubGit()
    )

    poster = _StubPoster()
    cursor = LiveCursor.load(tmp_path, project_id=1)
    holds: dict = {}
    stop_event = threading.Event()

    _producer_tick(
        bundle_root=tmp_path,
        cursor=cursor,
        poster=poster,
        opts=_make_opts(),
        stop_event=stop_event,
        assistant_tail_holds=holds,
    )
    assert len(poster.events) == 1
    assert cursor.turns_broadcast_for(sid) == 1

    a.text = "finally"
    _producer_tick(
        bundle_root=tmp_path,
        cursor=cursor,
        poster=poster,
        opts=_make_opts(),
        stop_event=stop_event,
        assistant_tail_holds=holds,
    )
    assert len(poster.events) == 2
    assert cursor.turns_broadcast_for(sid) == 1


def test_is_turn_posted_skips_duplicate_user_repost(
    tmp_path, monkeypatch
) -> None:
    from datetime import datetime, timezone

    from spec_cli.prompts.schema import Session, Turn
    from spec_cli.realtime.tracker import LiveCursor

    sid = "dedup-session"
    at = datetime(2026, 5, 6, 8, 44, 52, tzinfo=timezone.utc)
    session = Session(
        id=sid,
        source="cursor",
        title="t",
        turns=[Turn(role="user", text="hello", at=at)],
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

    class _StubGit:
        branch = "main"
        commit_sha = None
        author_name = "test"
        author_email = "test@example.com"

    monkeypatch.setattr(
        "spec_cli.realtime.watcher.read_git_context", lambda _root: _StubGit()
    )

    poster = _StubPoster()
    cursor = LiveCursor.load(tmp_path, project_id=1)
    cursor.mark_turn_posted(sid, 0, session.turns[0])
    cursor.record_broadcast(sid, 1)

    _producer_tick(
        bundle_root=tmp_path,
        cursor=cursor,
        poster=poster,
        opts=_make_opts(),
        stop_event=threading.Event(),
    )
    assert poster.events == []


def test_producer_clamps_ahead_broadcast_cursor_without_repost(
    tmp_path, monkeypatch
):
    """When ``broadcast_turns`` overshoots local length, clamp — do not
    rewind and re-POST the whole transcript every poll."""
    from spec_cli.prompts.schema import Session, Turn
    from spec_cli.realtime.tracker import LiveCursor

    sid = "ahead-cursor-session"
    session = Session(
        id=sid,
        source="cursor",
        title="t",
        turns=[
            Turn(role="user", text="one", at=None),
            Turn(role="assistant", text="two", at=None),
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

    class _StubGit:
        branch = "main"
        commit_sha = None
        author_name = "test"
        author_email = "test@example.com"

    monkeypatch.setattr(
        "spec_cli.realtime.watcher.read_git_context", lambda _root: _StubGit()
    )

    poster = _StubPoster()
    cursor = LiveCursor.load(tmp_path, project_id=1)
    cursor.record_broadcast(sid, 10)

    _producer_tick(
        bundle_root=tmp_path,
        cursor=cursor,
        poster=poster,
        opts=_make_opts(),
        stop_event=threading.Event(),
    )
    assert poster.events == []
    assert cursor.turns_broadcast_for(sid) == 2


# ── Issue 3: run_watcher exits promptly on stop_event ──────────────


def test_run_watcher_exits_promptly_when_stop_event_set(
    tmp_path, patched_transports
):
    """Set the externally-owned ``stop_event`` mid-flight — the watcher
    must return within a couple of seconds. This covers the same code
    path the SIGINT handler triggers (``stop_event.set()``) without
    the test having to send a real signal."""
    poster, consumer, _threads = patched_transports
    opts = _make_opts()
    stop_event = threading.Event()

    result_box: list[int] = []

    def _run():
        result_box.append(run_watcher(tmp_path, opts, stop_event=stop_event))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # Give the loop a moment to enter steady-state.
    time.sleep(0.3)

    started = time.monotonic()
    stop_event.set()
    t.join(timeout=5.0)
    elapsed = time.monotonic() - started

    assert not t.is_alive(), (
        f"watcher did not exit within 5s of stop_event.set() "
        f"(elapsed={elapsed:.2f}s)"
    )
    assert result_box == [0], f"unexpected return code: {result_box}"
    # Tear-down side effects — these are the contract for a clean exit.
    assert consumer.stopped is True, "consumer.stop() not invoked"
    assert poster.closed is True, "poster.close() not invoked"
    # Sanity: the round-trip should have been quick (sub-second after
    # poll_interval=0.5s wakes). 3s is generous to absorb CI jitter.
    assert elapsed < 3.0, f"shutdown took too long: {elapsed:.2f}s"


def test_run_watcher_returns_zero_on_clean_stop(
    tmp_path, patched_transports
):
    """Stop-event-driven shutdown should yield exit code 0 — only a
    fatal stream error (auth rejected, project not found) returns 1."""
    _poster, _consumer, _threads = patched_transports
    opts = _make_opts()
    stop_event = threading.Event()
    stop_event.set()  # already stopped before we start
    result = run_watcher(tmp_path, opts, stop_event=stop_event)
    assert result == 0
