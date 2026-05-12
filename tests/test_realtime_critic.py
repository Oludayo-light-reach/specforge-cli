"""Tests for the rule-based prompt auto-critic.

The critic is the first line of defence against "AI is about to do
something dangerous". Coverage focuses on:

* every rule fires on its happy-path phrase
* every rule does **not** fire on a close-but-safe phrase (false
  positives are what kill trust in a critic)
* severities map to the right suggested flag kind
* assistant turns and empty prompts are skipped entirely
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from spec_cli.realtime.critic import (
    SEV_HIGH,
    SEV_INFO,
    SEV_WARN,
    Critique,
    critique_event,
    suggested_flag_command,
)
from spec_cli.realtime.events import IncomingEvent


def _user_event(text: str, *, eid: int = 1) -> IncomingEvent:
    """Build a minimal user IncomingEvent carrying ``text``. Only the
    fields the critic actually reads are populated — the rest of the
    dataclass is filled with safe defaults so tests don't drift when
    new fields are added upstream."""
    return IncomingEvent(
        id=eid,
        project_id=42,
        session_id="s",
        source="claude_code",
        role="user",
        branch="main",
        commit_sha=None,
        model=None,
        summary=None,
        text=text,
        title=None,
        cwd=None,
        paths_touched=[],
        turn_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        author_user_id=1,
        author_handle="alice",
        author_name="Alice",
        author_avatar_url=None,
    )


def _rules(event: IncomingEvent) -> set[str]:
    return {c.rule for c in critique_event(event)}


# ── destructive-verb ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "please run rm -rf node_modules",
        "let's drop table users",
        "git reset --hard origin/main",
        "git push --force to staging",
        "DELETE FROM customers where id=1",
        "use shutil.rmtree to clean dist/",
    ],
)
def test_destructive_verb_fires(phrase: str):
    assert "destructive-verb" in _rules(_user_event(phrase))


@pytest.mark.parametrize(
    "phrase",
    [
        "please remove unused imports from utils.py",
        # 'delete' without 'from' (SQL) and not part of test-bypass
        "delete the comment on line 14",
        "format the file with black",
        "drop the menu state into context",
    ],
)
def test_destructive_verb_does_not_fire_on_safe_phrases(phrase: str):
    rules = _rules(_user_event(phrase))
    assert "destructive-verb" not in rules


# ── test-bypass ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "let's disable the failing tests for now",
        "skip the integration test",
        "just comment out the test",
        "remove the unit tests so CI passes",
        "commit with --no-verify",
        "disable CI on this branch",
    ],
)
def test_test_bypass_fires(phrase: str):
    assert "test-bypass" in _rules(_user_event(phrase))


def test_test_bypass_does_not_fire_on_legitimate_test_work():
    rules = _rules(_user_event("add a unit test for the parser"))
    assert "test-bypass" not in rules
    rules = _rules(_user_event("fix the failing assertion in test_parser.py"))
    assert "test-bypass" not in rules


# ── secret-in-prompt ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "deploy with sk_test_abcdef0123456789abcdef",
        "header: Authorization Bearer ghp_aaaaaaaaaaaaaaaaaaaaaaaa",
        "AKIAABCDEFGHIJKLMNOP is the key",
        # JWT-ish — three base64 chunks separated by '.', each long
        # enough to clear the {10,} minimum on the chunks following the
        # eyJ prefix.
        "token = eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhYmNkZWY.SflKxwRJSMeKKFabcdef",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_secret_in_prompt_fires(phrase: str):
    assert "secret-in-prompt" in _rules(_user_event(phrase))


def test_secret_in_prompt_ignores_plain_text():
    rules = _rules(_user_event("rotate the API key in vault and redeploy"))
    assert "secret-in-prompt" not in rules


# ── vague-intent ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "fix this",
        "improve",
        "clean up the code",
        "polish",
        "refactor things",
    ],
)
def test_vague_intent_fires(phrase: str):
    assert "vague-intent" in _rules(_user_event(phrase))


def test_vague_intent_does_not_fire_when_scope_is_given():
    rules = _rules(
        _user_event("fix the off-by-one in payments.py:apply_discount")
    )
    assert "vague-intent" not in rules


# ── trust-handoff ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "just do whatever you think is best",
        "you decide on the schema",
        "make it good",
        "build something cool",
        "surprise me",
    ],
)
def test_trust_handoff_fires(phrase: str):
    assert "trust-handoff" in _rules(_user_event(phrase))


# ── multi-task ────────────────────────────────────────────────────


def test_multi_task_fires_on_long_compound_prompt():
    phrase = (
        "Please add the OAuth callback endpoint, write unit tests for "
        "the token exchange, and also update the README with usage "
        "examples and screenshots."
    )
    assert "multi-task" in _rules(_user_event(phrase))


def test_multi_task_does_not_fire_on_short_prompts():
    # Below the 60-char threshold — short "and also" is too ambiguous
    # to flag without becoming noisy.
    assert "multi-task" not in _rules(_user_event("rename foo and also bar"))


# ── role / empty filters ──────────────────────────────────────────


def test_assistant_turns_are_critiqued_for_blast_radius():
    """Assistant turns now run through a narrower rule set focused on
    *what the AI is about to do*, since the synthesized tool summary
    (``ran 1 tool: Bash "rm -rf …"``) carries the dangerous command
    onto the wire. This is the receiver's last chance to stop a
    teammate's agent before the file system change lands."""
    ev = _user_event(text=None, eid=1)
    ev.role = "assistant"
    ev.summary = 'ran 1 tool: Bash "rm -rf node_modules"'
    rules = {c.rule for c in critique_event(ev)}
    assert "destructive-verb" in rules


def test_assistant_prose_with_no_dangerous_content_yields_no_critique():
    ev = _user_event(text="Sure, I'll start by reading auth.py", eid=2)
    ev.role = "assistant"
    ev.summary = "Sure, I'll start by reading auth.py"
    assert critique_event(ev) == []


def test_assistant_critique_inspects_both_summary_and_text():
    """The assistant rule body is the union of summary + text — a
    broadcaster in summary-only mode is the more common case but
    verbose-mode broadcasters carry the full text and we should
    inspect that too."""
    from spec_cli.realtime.critic import critique_event as ce
    ev = _user_event(text="...full output... git reset --hard origin/main ...", eid=3)
    ev.role = "assistant"
    ev.summary = "applied the patch"  # innocent summary
    rules = {c.rule for c in ce(ev)}
    assert "destructive-verb" in rules


def test_is_tool_only_summary_detects_synthesized_prefix():
    from spec_cli.realtime.critic import is_tool_only_summary

    assert is_tool_only_summary("ran 1 tool: Edit auth.py")
    assert is_tool_only_summary("ran 12 tools: Read x, Bash, Edit y")
    assert is_tool_only_summary("Ran 3 Tools: foo")  # case-insensitive
    # False positives: a real prose reply that happens to start with
    # the word "ran" should not be misclassified.
    assert not is_tool_only_summary("ran into a bug in payments.py")
    assert not is_tool_only_summary("here is the plan: 1) read 2) patch")
    assert not is_tool_only_summary(None)
    assert not is_tool_only_summary("")


def test_empty_prompt_yields_no_critique():
    ev = _user_event("")
    assert critique_event(ev) == []


# ── severity → suggested flag kind ────────────────────────────────


def test_severity_maps_to_suggested_flag_kind():
    high = [c for c in critique_event(_user_event("rm -rf /")) if c.severity == SEV_HIGH]
    assert high and high[0].suggested_flag_kind == "block"

    warn = [c for c in critique_event(_user_event("clean up")) if c.severity == SEV_WARN]
    assert warn and warn[0].suggested_flag_kind == "warning"

    long_multi = (
        "Add OAuth, the unit tests, and also document the env vars in "
        "the README please."
    )
    info = [c for c in critique_event(_user_event(long_multi)) if c.severity == SEV_INFO]
    assert info and info[0].suggested_flag_kind == "question"


def test_suggested_flag_command_includes_event_id_and_kind():
    c = Critique(
        rule="destructive-verb",
        severity=SEV_HIGH,
        msg='do not run "rm -rf"',
        suggested_flag_kind="block",
    )
    cmd = suggested_flag_command(4711, c)
    assert "spec team flag 4711" in cmd
    assert "--kind block" in cmd
    assert "auto:destructive-verb" in cmd
    # Note must be quoted, and any inner double-quotes downgraded to
    # singles so paste-and-run survives the shell.
    assert '"' in cmd
    assert "'rm -rf'" in cmd
