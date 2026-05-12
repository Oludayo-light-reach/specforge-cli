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
