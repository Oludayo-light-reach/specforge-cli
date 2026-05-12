"""End-to-end tests for the watcher's handling of tool-only assistant turns.

Both supported live sources — Claude Code and Codex — emit assistant
``Turn`` rows that carry only ``tool_calls`` (no prose) whenever the
agent runs a chain of Edit / Read / Bash with no narration. The
watcher used to drop those turns entirely, which made the team feed
look like prompts going into a void.

These tests confirm:

1. ``_synthesize_tool_summary`` produces a stable one-liner naming the
   tools and primary file paths.
2. ``_build_outgoing`` no longer returns ``None`` for tool-only
   assistant turns — it emits an :class:`OutgoingEvent` with the
   synthetic summary, so the wire carries an "AI was busy" signal
   regardless of whether the broadcaster is in verbose mode.
3. The same code path works for both Claude Code and Codex sources,
   since the adapter only sets ``Turn.source`` indirectly through the
   session — the watcher routes on role + tool_calls, not on source.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from spec_cli.prompts.schema import Session, ToolCall, Turn
from spec_cli.realtime.watcher import (
    WatcherOptions,
    _build_outgoing,
    _synthesize_tool_summary,
)


def _opts(verbose_assistant: bool = False) -> WatcherOptions:
    """Bare-minimum WatcherOptions for ``_build_outgoing`` to run.

    The function only reads ``opts.verbose_assistant`` from the
    options dataclass, so the rest of the fields can be defaulted.
    """
    return WatcherOptions(
        project_id=1,
        project_label="acme/widgets",
        api_base="https://example.invalid",
        access_token="t",
        self_user_id=42,
        verbose_assistant=verbose_assistant,
    )


def _session(source: str) -> Session:
    return Session(
        id="sess-123",
        source=source,
        turns=[],
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        model="claude-sonnet-4" if source == "claude_code" else "gpt-5",
        cwd="/tmp/bundle",
        title="working on auth",
        paths_touched=["auth.py"],
    )


def _git_ctx(branch: str = "main", sha: str = "abc1234") -> MagicMock:
    g = MagicMock()
    g.commit_sha = sha
    return g


# ── _synthesize_tool_summary ──────────────────────────────────────


def test_synthesize_handles_single_tool():
    calls = [ToolCall(name="Edit", args={"file_path": "auth.py"})]
    out = _synthesize_tool_summary(calls)
    assert out is not None
    assert "ran 1 tool:" in out
    assert "Edit auth.py" in out


def test_synthesize_handles_multi_tool_with_overflow_marker():
    calls = [
        ToolCall(name="Read", args={"file_path": "main.py"}),
        ToolCall(name="Edit", args={"path": "auth.py"}),
        ToolCall(name="Bash", args={"command": "pytest"}),
        ToolCall(name="Write", args={"file_path": "out.md"}),
        ToolCall(name="Grep", args={"pattern": "TODO"}),
    ]
    out = _synthesize_tool_summary(calls)
    assert out is not None
    assert "ran 5 tools:" in out
    # First three appear with names; the rest are summarised.
    assert "Read main.py" in out
    assert "Edit auth.py" in out
    # Bash command snippet is now included so the auto-critic on the
    # receiver side can spot destructive verbs before the tool lands.
    assert 'Bash "pytest"' in out
    assert "(+2 more)" in out


def test_synthesize_includes_bash_command_for_critic_to_inspect():
    """The synthesized summary must include enough of the Bash
    command for the receiver's auto-critic to recognise destructive
    verbs like ``rm -rf``. Without this, a teammate's agent could
    nuke a directory and the team feed would only see ``Bash``."""
    calls = [ToolCall(name="Bash", args={"command": "rm -rf node_modules && rebuild"})]
    out = _synthesize_tool_summary(calls)
    assert out is not None
    assert "rm -rf" in out


def test_synthesize_quotes_grep_and_glob_patterns():
    calls = [
        ToolCall(name="Grep", args={"pattern": "TODO"}),
        ToolCall(name="Glob", args={"pattern": "**/*.py"}),
    ]
    out = _synthesize_tool_summary(calls)
    assert out is not None
    assert 'Grep "TODO"' in out
    assert 'Glob "**/*.py"' in out


def test_synthesize_strips_directory_components():
    calls = [ToolCall(name="Edit", args={"file_path": "src/services/auth/login.py"})]
    out = _synthesize_tool_summary(calls)
    assert out is not None
    # Long path collapses to basename so the summary stays one line.
    assert "login.py" in out
    assert "services" not in out


def test_synthesize_returns_none_when_no_tools():
    assert _synthesize_tool_summary([]) is None


def test_synthesize_skips_unnamed_tool_entries():
    # A weird upstream row with no .name shouldn't crash the watcher.
    bogus = ToolCall(name="", args={})  # type: ignore[arg-type]
    assert _synthesize_tool_summary([bogus]) is None


# ── _build_outgoing ───────────────────────────────────────────────


@pytest.mark.parametrize("source", ["claude_code", "codex"])
def test_assistant_tool_only_turn_is_streamed_with_synthetic_summary(
    source: str,
):
    """Regression for the silent-AI bug. Before the fix, a tool-only
    assistant turn returned ``None`` and the team feed showed only
    user prompts. We now synthesise a summary so the receiver always
    sees that an AI turn landed."""
    session = _session(source)
    turn = Turn(
        role="assistant",
        text=None,
        summary=None,
        at=datetime.now(timezone.utc),
        model=session.model,
        tool_calls=[
            ToolCall(name="Edit", args={"file_path": "auth.py"}),
            ToolCall(name="Bash", args={"command": "pytest -q"}),
        ],
    )
    out = _build_outgoing(
        session, turn, branch="main", git=_git_ctx(), opts=_opts()
    )
    assert out is not None, "tool-only assistant turn must not be dropped"
    assert out.role == "assistant"
    assert out.source == source
    assert out.summary is not None
    assert "ran 2 tools" in out.summary
    assert "Edit auth.py" in out.summary
    # No prose was available, so even in non-verbose mode we don't
    # invent a ``text`` body — the summary alone carries the signal.
    assert out.text is None


@pytest.mark.parametrize("source", ["claude_code", "codex"])
def test_assistant_with_prose_does_not_get_overridden_by_tool_summary(
    source: str,
):
    """When the adapter does provide a summary, the tool-summary
    fallback must stay out of the way — we don't want to clobber a
    perfectly good first-sentence preview."""
    session = _session(source)
    turn = Turn(
        role="assistant",
        text=None,
        summary="Let me start by reading the relevant files.",
        at=datetime.now(timezone.utc),
        tool_calls=[ToolCall(name="Read", args={"file_path": "auth.py"})],
    )
    out = _build_outgoing(
        session, turn, branch="main", git=_git_ctx(), opts=_opts()
    )
    assert out is not None
    assert out.summary == "Let me start by reading the relevant files."


def test_assistant_with_empty_prose_and_no_tools_is_still_dropped():
    """A truly empty assistant turn (no prose, no tools) is an
    upstream artefact and should not reach the wire."""
    session = _session("claude_code")
    turn = Turn(
        role="assistant",
        text=None,
        summary=None,
        at=datetime.now(timezone.utc),
        tool_calls=[],
    )
    out = _build_outgoing(
        session, turn, branch="main", git=_git_ctx(), opts=_opts()
    )
    assert out is None


def test_user_turns_unchanged_by_tool_summary_logic():
    """The tool-summary path lives in the assistant branch — user
    turns must behave identically to before."""
    session = _session("claude_code")
    turn = Turn(
        role="user",
        text="please refactor auth.py",
        at=datetime.now(timezone.utc),
    )
    out = _build_outgoing(
        session, turn, branch="main", git=_git_ctx(), opts=_opts()
    )
    assert out is not None
    assert out.role == "user"
    assert out.text == "please refactor auth.py"


def test_assistant_verbose_mode_still_passes_through_text():
    """Verbose broadcaster keeps shipping the full assistant body."""
    session = _session("codex")
    turn = Turn(
        role="assistant",
        text="Here is the plan: first read the file, then patch it.",
        summary="Here is the plan: first read the file, then patch it.",
        at=datetime.now(timezone.utc),
        tool_calls=[ToolCall(name="Edit", args={"file_path": "main.py"})],
    )
    out = _build_outgoing(
        session, turn, branch="main", git=_git_ctx(), opts=_opts(verbose_assistant=True)
    )
    assert out is not None
    assert out.text is not None
    assert "Here is the plan" in out.text
