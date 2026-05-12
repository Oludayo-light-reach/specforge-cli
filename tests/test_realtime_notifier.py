"""Tests for the live-stream Notifier render helpers.

The Notifier itself prints to a Rich console (hard to assert against
in unit tests), so we focus on the pure formatting helpers that drive
the new header chips — cwd shortening, session id rendering, paths
collapsing — and the opt-in alert path. The alert tests stub out
``subprocess`` / ``sys.stderr`` so nothing leaks into the test runner.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import MagicMock

import pytest
from rich.console import Console
from rich.theme import Theme

from spec_cli.realtime.critic import SEV_HIGH, SEV_WARN, Critique
from spec_cli.realtime.events import IncomingEvent
from spec_cli.realtime.notifier import (
    Notifier,
    _paths_chip,
    _short_cwd,
    _short_session,
)


# ── _short_cwd ────────────────────────────────────────────────────


def test_short_cwd_strips_home_to_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/alice")
    assert _short_cwd("/Users/alice/code/widgets") == "~/code/widgets"


def test_short_cwd_returns_path_unchanged_when_not_in_home(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/alice")
    assert _short_cwd("/srv/repos/billing") == "/srv/repos/billing"


def test_short_cwd_collapses_very_long_paths_to_last_two_segments(
    monkeypatch,
):
    monkeypatch.setenv("HOME", "/Users/alice")
    long_path = "/Users/alice/very/deeply/nested/long/path/to/finally/repo"
    out = _short_cwd(long_path)
    # Either the tilde form is short enough, or it collapsed; in both
    # cases the result must end with the actual repo name so the
    # reviewer recognises the bundle.
    assert out is not None
    assert out.endswith("/repo")
    assert len(out) <= 41  # leaves room for one trailing character


def test_short_cwd_handles_none_and_empty():
    assert _short_cwd(None) is None
    assert _short_cwd("") is None
    assert _short_cwd("   ") is None


# ── _short_session ────────────────────────────────────────────────


def test_short_session_truncates_to_six_chars():
    assert _short_session("abc12345678") == "abc123"


def test_short_session_handles_short_or_missing_input():
    assert _short_session("abc") == "abc"
    assert _short_session(None) is None
    assert _short_session("") is None


# ── _paths_chip ────────────────────────────────────────────────────


def test_paths_chip_renders_basenames_with_overflow():
    out = _paths_chip(["a/b/c.py", "d/e/f.py", "g/h/i.py", "j/k/l.py"])
    assert out is not None
    assert "c.py" in out
    assert "f.py" in out
    # First two basenames only; remaining two summarised as +2 more.
    assert "+2 more" in out


def test_paths_chip_drops_empty_input():
    assert _paths_chip(None) is None
    assert _paths_chip([]) is None
    assert _paths_chip(["", None, ""]) is None  # type: ignore[list-item]


# ── pending user prompt (assistant context) ───────────────────────


def _recording_console() -> Console:
    """Console compatible with :mod:`spec_cli.ui` theme tokens."""
    return Console(
        record=True,
        width=120,
        theme=Theme(
            {
                "sf.mint": "bold #3ddab4",
                "sf.reject": "bold #ff5a6a",
                "sf.warn": "bold #f0b86e",
                "sf.muted": "dim #9aa3b2",
                "sf.point": "bold #7de3ff",
                "sf.label": "bold #c7c9d1",
            }
        ),
        highlight=False,
    )


def test_assistant_shows_pending_user_prompt_line(monkeypatch):
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    sid = "composer-shared"
    pid = 42
    user = IncomingEvent(
        id=101,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="user",
        branch="main",
        commit_sha=None,
        model=None,
        summary=None,
        text="Where is the hero section and the install curl snippet?",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    assistant = IncomingEvent(
        id=102,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="default",
        summary="Searching the codebase for the landing page hero.",
        text="Searching the codebase for the landing page hero.",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    n = Notifier(critic_enabled=False)
    n.show(user)
    n.show(assistant)
    out = cap.export_text()
    assert "Where is the hero section" in out
    assert "⤷ prompt" in out


def test_second_assistant_does_not_repeat_stale_prompt(monkeypatch):
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    sid = "composer-shared-2"
    pid = 43
    user = IncomingEvent(
        id=201,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="user",
        branch="main",
        commit_sha=None,
        model=None,
        summary=None,
        text="First question only",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    a1 = IncomingEvent(
        id=202,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="m",
        summary="reply one",
        text="reply one",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    a2 = IncomingEvent(
        id=203,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="m",
        summary="reply two",
        text="reply two",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=7,
        author_handle="jon",
        author_name="Jon",
        author_avatar_url=None,
    )
    n = Notifier(critic_enabled=False)
    n.show(user)
    n.show(a1)
    n.show(a2)
    assert cap.export_text().count("⤷ prompt") == 1


def test_assistant_prefers_full_text_over_one_line_summary(monkeypatch):
    import spec_cli.realtime.notifier as notifier_mod

    cap = _recording_console()
    monkeypatch.setattr(notifier_mod, "console", cap)
    ts = datetime.now(timezone.utc)
    sid = "sess-detail"
    pid = 99
    long_tail = "X" * 800
    assistant = IncomingEvent(
        id=501,
        project_id=pid,
        session_id=sid,
        source="cursor",
        role="assistant",
        branch="main",
        commit_sha=None,
        model="default",
        summary="Short headline only.",
        text=f"Short headline only.\n\nExpanded reasoning and code.\n{long_tail}",
        title=None,
        cwd="/tmp",
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=3,
        author_handle="pat",
        author_name="Pat",
        author_avatar_url=None,
    )
    n = Notifier(critic_enabled=False)
    n.show(assistant)
    out = cap.export_text()
    assert "Expanded reasoning and code." in out
    # ``export_text`` inserts hard wraps — count chars instead of a
    # contiguous 800-``X`` substring.
    assert out.count("X") >= 800


# ── _alert (notify) ───────────────────────────────────────────────


def _ev(role: str = "user", text: str | None = "hello") -> IncomingEvent:
    ts = datetime.now(timezone.utc)
    return IncomingEvent(
        id=99,
        project_id=1,
        session_id="abcdef123456",
        source="claude_code",
        role=role,
        branch="main",
        commit_sha=None,
        model=None,
        summary=None,
        text=text,
        title=None,
        cwd=None,
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=1,
        author_handle="alice",
        author_name="Alice",
        author_avatar_url=None,
    )


def test_notify_off_by_default_does_not_alert(monkeypatch):
    n = Notifier()
    called = {"bell": False, "osa": False}

    monkeypatch.setattr(
        sys, "stderr", _stub_stderr_with_bell_callback(called)
    )
    monkeypatch.setattr(
        "shutil.which", lambda _: "/usr/bin/osascript"
    )

    # Critic hit but notify is off → no bell, no osascript.
    hits = [
        Critique(
            rule="destructive-verb",
            severity=SEV_HIGH,
            msg="rm -rf detected",
            suggested_flag_kind="block",
        )
    ]
    n._render_critiques(_ev(), hits)
    assert called["bell"] is False


def test_notify_on_rings_bell_on_block_severity(monkeypatch):
    n = Notifier(notify=True)
    called = {"bell": False}
    monkeypatch.setattr(
        sys, "stderr", _stub_stderr_with_bell_callback(called)
    )
    # No osascript available — we still want the bell to ring so
    # cross-platform users get the audible cue.
    monkeypatch.setattr("shutil.which", lambda _: None)

    hits = [
        Critique(
            rule="destructive-verb",
            severity=SEV_HIGH,
            msg="rm -rf detected",
            suggested_flag_kind="block",
        )
    ]
    n._render_critiques(_ev(), hits)
    assert called["bell"] is True


def test_notify_does_not_fire_for_warn_severity(monkeypatch):
    n = Notifier(notify=True)
    called = {"bell": False}
    monkeypatch.setattr(
        sys, "stderr", _stub_stderr_with_bell_callback(called)
    )
    hits = [
        Critique(
            rule="vague-intent",
            severity=SEV_WARN,
            msg="vague",
            suggested_flag_kind="warning",
        )
    ]
    n._render_critiques(_ev(), hits)
    assert called["bell"] is False


# ── helpers ───────────────────────────────────────────────────────


def _stub_stderr_with_bell_callback(state: dict) -> object:
    """Replacement stderr that records when the BEL character is
    written. Used to verify the opt-in --notify path without polluting
    the actual test runner stderr."""

    class _Sink:
        def write(self, s: str) -> None:
            if "\a" in s:
                state["bell"] = True

        def flush(self) -> None:
            pass

    return _Sink()
