"""Tests for the slash-command layer used by ``spec team watch``.

The dispatcher is pure (no I/O, no Rich, no network) so these tests
exercise it directly with a :class:`MagicMock` Notifier and a tiny
in-memory event buffer. Network calls go through a stubbed flag
client we instantiate per test.

Coverage matrix:

* :func:`parse_command` — slashes, blanks, garbage, multi-token rest.
* :func:`parse_window` — happy path + every malformed-input branch.
* :func:`WatchState.is_visible` — focus on / focus off, mute multi.
* Each handler: happy path, missing args, unknown command, and the
  side effects we care about (state mutation, notifier method calls,
  flag client invocation).
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from spec_cli.realtime.commands import (
    CommandContext,
    FLAG_KINDS,
    WatchState,
    dispatch,
    make_buffer,
    parse_command,
    parse_window,
)
from spec_cli.realtime.events import IncomingEvent


# ── fixtures ──────────────────────────────────────────────────────


def _event(
    eid: int,
    *,
    role: str = "user",
    text: str | None = "hello world",
    summary: str | None = None,
    handle: str = "alice",
    name: str = "Alice",
    source: str = "claude_code",
    bundle_label: str | None = "acme/widgets",
    project_id: int = 1,
    session_id: str = "s",
    minutes_ago: float = 0.0,
    model: str | None = None,
) -> IncomingEvent:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return IncomingEvent(
        id=eid,
        project_id=project_id,
        session_id=session_id,
        source=source,
        role=role,
        branch="main",
        commit_sha=None,
        model=model,
        summary=summary,
        text=text,
        title=None,
        cwd=None,
        paths_touched=[],
        turn_at=ts,
        received_at=ts,
        author_user_id=1,
        author_handle=handle,
        author_name=name,
        author_avatar_url=None,
        bundle_label=bundle_label,
    )


def _ctx(
    *,
    buffer_events: list[IncomingEvent] | None = None,
    bundle_root: Path | None = None,
) -> tuple[CommandContext, MagicMock, MagicMock, WatchState]:
    notifier = MagicMock()
    flag_client = MagicMock()
    buf = make_buffer()
    if buffer_events:
        for ev in buffer_events:
            buf.append(ev)
    state = WatchState()
    pid_map = {ev.id: ev.project_id for ev in (buffer_events or [])}
    ctx = CommandContext(
        notifier=notifier,
        state=state,
        buffer=buf,
        flag_client=flag_client,
        project_for_event=pid_map.get,
        bundle_root=bundle_root,
    )
    return ctx, notifier, flag_client, state


# ── parse_command ─────────────────────────────────────────────────


def test_parse_command_basic():
    cmd = parse_command("/flag 4711 warning race condition risk")
    assert cmd is not None
    assert cmd.name == "flag"
    assert cmd.args == ("4711", "warning", "race", "condition", "risk")
    assert cmd.raw == "4711 warning race condition risk"


def test_parse_command_ignores_non_slash_lines():
    assert parse_command("hello") is None
    assert parse_command("") is None
    assert parse_command("   ") is None
    assert parse_command("\n") is None


def test_parse_command_lone_slash_is_noop():
    assert parse_command("/") is None
    assert parse_command("/   ") is None


def test_parse_command_lowercases_name():
    cmd = parse_command("/FLAG 1 warning")
    assert cmd is not None
    assert cmd.name == "flag"


def test_parse_command_push_shorthand():
    cmd = parse_command("/push@alice please push your WIP")
    assert cmd is not None
    assert cmd.name == "push"
    assert cmd.args[0] == "alice"
    assert "please" in cmd.args


def test_dispatch_push_without_bundle_root_errors():
    ctx, notifier, _, _ = _ctx()
    dispatch(parse_command("/push alice"), ctx)
    assert notifier.show_command_result.called
    msg = notifier.show_command_result.call_args[0][0]
    assert "bundle" in msg.lower()


def test_dispatch_push_writes_yaml(tmp_path, monkeypatch):
    from spec_cli.realtime import commands as cmd_mod

    creds = MagicMock()
    creds.user_handle = "bob"
    creds.user_name = "Bob"
    monkeypatch.setattr(cmd_mod, "load_credentials", lambda: creds)
    monkeypatch.setattr(
        cmd_mod,
        "read_git_context",
        lambda _root: MagicMock(branch="feature/x"),
    )
    ctx, notifier, _, _ = _ctx(bundle_root=tmp_path)
    dispatch(parse_command("/push@alice need your commits"), ctx)
    yaml_path = tmp_path / ".spec" / "team-push-requests.yaml"
    assert yaml_path.is_file()
    text = yaml_path.read_text(encoding="utf-8")
    assert "alice" in text
    assert "bob" in text
    assert notifier.show_command_result.called
    assert notifier.show_command_result.call_args[1].get("kind") == "ok"


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("2h", timedelta(hours=2)),
        ("45m", timedelta(minutes=45)),
        ("1H", timedelta(hours=1)),
        (" 30 m ", timedelta(minutes=30)),
    ],
)
def test_parse_window_accepts_h_and_m_suffixes(spec, expected):
    assert parse_window(spec) == expected


@pytest.mark.parametrize(
    "spec", ["", "abc", "2d", "0h", "-3m", "h", "2", None]
)
def test_parse_window_rejects_garbage(spec):
    assert parse_window(spec) is None


# ── WatchState.is_visible ────────────────────────────────────────


def test_is_visible_with_no_filters():
    state = WatchState()
    assert state.is_visible(_event(1, handle="alice"))


def test_focus_hides_other_authors():
    state = WatchState(focus="alice")
    assert state.is_visible(_event(1, handle="alice"))
    assert not state.is_visible(_event(2, handle="bob", name="Bob"))


def test_focus_matches_at_handle_or_display_name():
    state = WatchState(focus="@alice")
    assert state.is_visible(_event(1, handle="alice"))
    state.focus = "Alice"
    assert state.is_visible(_event(1, handle=None, name="Alice"))  # type: ignore[arg-type]


def test_mute_suppresses_matching_authors():
    state = WatchState(mutes={"bob"})
    assert state.is_visible(_event(1, handle="alice"))
    assert not state.is_visible(_event(2, handle="bob", name="Bob"))


def test_mute_and_focus_compose_focus_wins_on_match():
    state = WatchState(focus="alice", mutes={"alice"})
    # If focus is alice but alice is muted, mute wins (events drop).
    assert not state.is_visible(_event(1, handle="alice"))


# ── /help ─────────────────────────────────────────────────────────


def test_help_lists_known_commands():
    ctx, notifier, _, _ = _ctx()
    dispatch(parse_command("/help"), ctx)  # type: ignore[arg-type]
    notifier.show_command_result.assert_called_once()
    body = notifier.show_command_result.call_args[0][0]
    for keyword in (
        "aliases",
        "summarize",
        "flag",
        "focus",
        "mute",
        "replay",
        "pair",
        "turn",
        "full",
        "search",
        "critic",
        "status",
        "help",
        "glyphs",
        "pager",
    ):
        assert keyword in body


def test_pair_without_qa_hook_shows_error():
    ctx, notifier, _, _ = _ctx()
    dispatch(parse_command("/pair"), ctx)  # type: ignore[arg-type]
    notifier.show_command_result.assert_called_once()
    assert "team watch" in notifier.show_command_result.call_args[0][0].lower()


def test_pair_invokes_qa_callback():
    calls: list[str] = []
    ctx, notifier, fc, state = _ctx()
    ctx2 = CommandContext(
        notifier=notifier,
        state=state,
        buffer=ctx.buffer,
        flag_client=fc,
        project_for_event=ctx.project_for_event,
        qa_pair_now=lambda: calls.append("x"),
    )
    dispatch(parse_command("/pair"), ctx2)  # type: ignore[arg-type]
    assert calls == ["x"]


# ── /focus + /mute ─────────────────────────────────────────────────


def test_focus_sets_state_and_announces():
    ctx, notifier, _, state = _ctx()
    dispatch(parse_command("/focus alice"), ctx)  # type: ignore[arg-type]
    assert state.focus == "alice"
    notifier.show_command_result.assert_called_once()
    assert (
        notifier.show_command_result.call_args.kwargs.get("kind") == "ok"
    )


def test_focus_off_clears_state():
    ctx, _, _, state = _ctx()
    state.focus = "alice"
    dispatch(parse_command("/focus off"), ctx)  # type: ignore[arg-type]
    assert state.focus is None


def test_focus_no_args_reports_current_or_usage():
    ctx, notifier, _, state = _ctx()
    state.focus = "alice"
    dispatch(parse_command("/focus"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    assert "@alice" in body


def test_mute_and_unmute_are_additive():
    ctx, _, _, state = _ctx()
    dispatch(parse_command("/mute alice"), ctx)  # type: ignore[arg-type]
    dispatch(parse_command("/mute bob"), ctx)  # type: ignore[arg-type]
    assert state.mutes == {"alice", "bob"}
    dispatch(parse_command("/unmute alice"), ctx)  # type: ignore[arg-type]
    assert state.mutes == {"bob"}


def test_unmute_unknown_handle_is_informational():
    ctx, notifier, _, state = _ctx()
    dispatch(parse_command("/unmute alice"), ctx)  # type: ignore[arg-type]
    assert state.mutes == set()
    body = notifier.show_command_result.call_args[0][0]
    assert "was not muted" in body


# ── /critic ────────────────────────────────────────────────────────


def test_critic_off_then_on():
    ctx, _, _, state = _ctx()
    dispatch(parse_command("/critic off"), ctx)  # type: ignore[arg-type]
    assert state.critic_enabled is False
    dispatch(parse_command("/critic on"), ctx)  # type: ignore[arg-type]
    assert state.critic_enabled is True


def test_critic_with_bad_arg_reports_current_state():
    ctx, notifier, _, _ = _ctx()
    dispatch(parse_command("/critic toggle"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    assert "currently" in body


# ── /status ───────────────────────────────────────────────────────


def test_status_includes_team_watch_receiver_brief():
    notifier = MagicMock()
    state = WatchState()
    buf = make_buffer()
    ctx = CommandContext(
        notifier=notifier,
        state=state,
        buffer=buf,
        team_watch_receiver_brief="receiver: SSE assistant prose on",
    )
    dispatch(parse_command("/status"), ctx)  # type: ignore[arg-type]
    text = notifier.show_command_result.call_args[0][0]
    assert "receiver: SSE assistant prose on" in text


def test_status_with_empty_buffer_says_no_activity():
    ctx, notifier, _, _ = _ctx()
    dispatch(parse_command("/status"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    assert "visibility" in body
    assert "all teammates" in body
    assert "no activity" in body


def test_status_lists_last_seen_per_author_source():
    events = [
        _event(1, handle="alice", source="claude_code", minutes_ago=10),
        _event(2, handle="alice", source="claude_code", minutes_ago=2),
        _event(3, handle="bob", source="codex", minutes_ago=1),
    ]
    ctx, notifier, _, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/status"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    assert "visibility" in body
    assert "all teammates" in body
    assert "@alice" in body
    assert "@bob" in body
    assert "claude_code" in body
    assert "codex" in body


def test_status_shows_focus_in_visibility_line():
    events = [_event(1, handle="alice", source="cursor")]
    ctx, notifier, _, state = _ctx(buffer_events=events)
    state.focus = "alice"
    dispatch(parse_command("/status"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    assert "/focus @alice" in body
    assert "only this teammate" in body


# ── /replay ────────────────────────────────────────────────────────


def test_replay_re_emits_events_in_window():
    events = [
        _event(1, minutes_ago=30),
        _event(2, minutes_ago=5),
        _event(3, minutes_ago=1),
    ]
    ctx, notifier, _, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/replay 10m"), ctx)  # type: ignore[arg-type]
    # 2 events fall within last 10 minutes; they must be replayed in
    # chronological order through Notifier.show().
    shown = [c.args[0] for c in notifier.show.call_args_list]
    assert [ev.id for ev in shown] == [2, 3]


def test_replay_with_no_events_in_window_is_informational():
    events = [_event(1, minutes_ago=60)]
    ctx, notifier, _, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/replay 1m"), ctx)  # type: ignore[arg-type]
    notifier.show.assert_not_called()
    body = notifier.show_command_result.call_args[0][0]
    assert "nothing to replay" in body


def test_replay_bad_window_is_error():
    ctx, notifier, _, _ = _ctx()
    dispatch(parse_command("/replay banana"), ctx)  # type: ignore[arg-type]
    notifier.show.assert_not_called()
    assert (
        notifier.show_command_result.call_args.kwargs.get("kind") == "error"
    )


# ── /summarize ────────────────────────────────────────────────────


def test_summarize_emits_structured_block_for_agent():
    events = [
        _event(1, role="user", text="add OAuth", minutes_ago=5),
        _event(
            2,
            role="assistant",
            text="here is the plan: read auth.py first",
            model="claude-sonnet-4",
            minutes_ago=4,
        ),
    ]
    ctx, notifier, _, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/summarize 1h"), ctx)  # type: ignore[arg-type]
    notifier.show_command_result.assert_called_once()
    args, kwargs = notifier.show_command_result.call_args
    body = args[0]
    assert kwargs.get("kind") == "summarize"
    # Header + footer must wrap the dump so the agent knows the
    # boundaries.
    assert "[spec summarize request" in body
    assert "[end of summarize request" in body
    # Author + text shows for each event so the agent has enough
    # context to synthesise.
    assert "@alice" in body
    assert "add OAuth" in body
    assert "here is the plan" in body
    # Role tags so the agent can tell who said what.
    assert "USER" in body
    assert "ASSISTANT" in body


def test_summarize_excludes_presence_rows():
    events = [
        _event(1, role="user", text="real prompt", minutes_ago=5),
        _event(2, role="presence", text=None, minutes_ago=5),
    ]
    ctx, notifier, _, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/summarize 1h"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    assert "real prompt" in body
    assert "PRESENCE" not in body


# ── /flag ──────────────────────────────────────────────────────────


def test_flag_posts_via_client_with_parsed_args():
    events = [_event(7, role="user", project_id=99)]
    ctx, notifier, flag_client, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/flag 7 warning race condition risk"), ctx)  # type: ignore[arg-type]
    flag_client.create_prompt_event_flag.assert_called_once_with(
        project_id=99,
        event_id=7,
        kind="warning",
        note="race condition risk",
    )
    assert (
        notifier.show_command_result.call_args.kwargs.get("kind") == "ok"
    )


def test_flag_rejects_unknown_kind():
    events = [_event(7, role="user")]
    ctx, notifier, flag_client, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/flag 7 superwarning"), ctx)  # type: ignore[arg-type]
    flag_client.create_prompt_event_flag.assert_not_called()
    body = notifier.show_command_result.call_args[0][0]
    assert "unknown flag kind" in body


def test_flag_rejects_non_integer_event_id():
    events = [_event(7, role="user")]
    ctx, notifier, flag_client, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/flag abc warning"), ctx)  # type: ignore[arg-type]
    flag_client.create_prompt_event_flag.assert_not_called()
    body = notifier.show_command_result.call_args[0][0]
    assert "event_id must be an integer" in body


def test_flag_rejects_event_outside_buffer():
    events = [_event(7, role="user")]
    ctx, notifier, flag_client, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/flag 99 warning"), ctx)  # type: ignore[arg-type]
    flag_client.create_prompt_event_flag.assert_not_called()
    body = notifier.show_command_result.call_args[0][0]
    assert "not in the current buffer" in body


def test_flag_swallows_client_errors_and_reports_them():
    events = [_event(7, role="user")]
    ctx, notifier, flag_client, _ = _ctx(buffer_events=events)
    flag_client.create_prompt_event_flag.side_effect = RuntimeError("boom")
    dispatch(parse_command("/flag 7 warning"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    assert "flag failed" in body
    assert "boom" in body


def test_flag_kinds_constant_matches_dispatcher():
    assert set(FLAG_KINDS) == {"warning", "question", "block", "ack"}


# ── unknown ────────────────────────────────────────────────────────


def test_unknown_command_prints_hint_without_crashing():
    ctx, notifier, _, _ = _ctx()
    dispatch(parse_command("/banana"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    assert "unknown command" in body
    assert "/help" in body


def test_handler_exception_is_caught_and_reported():
    ctx, notifier, _, _ = _ctx()
    # Force /focus to throw by passing args of an unexpected shape
    # only after we monkey-patch a handler. Easier path: corrupt state
    # so a handler trips and prove dispatch reports it cleanly.
    # We achieve this by giving the buffer a non-Event object so the
    # /status handler trips on the deque entry.
    ctx.buffer.append("not-an-event")  # type: ignore[arg-type]
    dispatch(parse_command("/status"), ctx)  # type: ignore[arg-type]
    # The dispatcher catches exceptions and reports them via
    # show_command_result with kind="error". The buffer corruption
    # is a stand-in for an unexpected handler bug.
    kinds = [
        c.kwargs.get("kind") for c in notifier.show_command_result.call_args_list
    ]
    assert "error" in kinds


# ── /search ────────────────────────────────────────────────────────


def test_search_finds_match_in_body():
    events = [
        _event(11, role="user", text="please refactor billing.py"),
        _event(12, role="user", text="add a test for auth"),
    ]
    ctx, notifier, _, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/search billing"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    assert "1 match" in body
    assert "#11" in body
    # The snippet preserves the matched substring so the reviewer
    # sees context, not just an id.
    assert "billing" in body


def test_search_is_case_insensitive_and_walks_newest_first():
    events = [
        _event(1, role="user", text="OAuth migration step 1", minutes_ago=10),
        _event(2, role="user", text="oauth refactor follow-up", minutes_ago=1),
    ]
    ctx, notifier, _, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/search oauth"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    # Both events matched; the most recent (id=2) should be first.
    assert "2 match" in body
    pos_2 = body.find("#2")
    pos_1 = body.find("#1")
    assert pos_2 != -1 and pos_1 != -1 and pos_2 < pos_1


def test_search_matches_handle_and_paths_and_event_id():
    events = [
        _event(50, role="user", handle="zoe", text="something else"),
        _event(
            51, role="assistant", text="",
            summary="ran 1 tool: Edit billing.py",
            handle="alice",
        ),
    ]
    # The second event lists no paths_touched in _event(); we set it
    # via direct construction so the search can find the file.
    events[1].paths_touched.append("services/billing.py")
    ctx, notifier, _, _ = _ctx(buffer_events=events)

    dispatch(parse_command("/search zoe"), ctx)  # type: ignore[arg-type]
    body_handle = notifier.show_command_result.call_args[0][0]
    assert "#50" in body_handle

    dispatch(parse_command("/search billing.py"), ctx)  # type: ignore[arg-type]
    body_path = notifier.show_command_result.call_args[0][0]
    assert "#51" in body_path

    dispatch(parse_command("/search 50"), ctx)  # type: ignore[arg-type]
    body_id = notifier.show_command_result.call_args[0][0]
    assert "#50" in body_id


def test_search_with_no_matches_is_informational():
    events = [_event(1, role="user", text="something")]
    ctx, notifier, _, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/search nonexistent-term"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    assert "no matches" in body


def test_search_without_term_shows_usage():
    ctx, notifier, _, _ = _ctx()
    dispatch(parse_command("/search"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    assert "usage" in body
    assert (
        notifier.show_command_result.call_args.kwargs.get("kind") == "error"
    )


def test_search_alias_grep_is_wired():
    events = [_event(7, role="user", text="hello refactor world")]
    ctx, notifier, _, _ = _ctx(buffer_events=events)
    dispatch(parse_command("/grep refactor"), ctx)  # type: ignore[arg-type]
    body = notifier.show_command_result.call_args[0][0]
    assert "#7" in body


def _prompt_event_row(
    eid: int,
    role: str,
    *,
    session_id: str,
    project_id: int = 1,
    text: str | None = "body",
    summary: str | None = None,
) -> dict:
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "id": eid,
        "project_id": project_id,
        "session_id": session_id,
        "source": "cursor",
        "role": role,
        "branch": "main",
        "commit_sha": None,
        "model": "default" if role == "assistant" else None,
        "summary": summary,
        "text": text,
        "title": None,
        "cwd": None,
        "paths_touched": [],
        "turn_at": ts,
        "received_at": ts,
        "author": {"user_id": 1, "handle": "jon", "name": "Jon"},
    }


def test_turn_without_cloud_shows_error():
    ctx, notifier, _, _ = _ctx()
    dispatch(parse_command("/turn"), ctx)  # type: ignore[arg-type]
    notifier.show_command_result.assert_called_once()
    assert "cloud" in notifier.show_command_result.call_args[0][0].lower()


def test_turn_ambiguous_prefix_does_not_hit_cloud():
    cloud = MagicMock()
    e1 = _event(1, role="user", session_id="abcd111")
    e2 = _event(2, role="user", session_id="abcd222")
    ctx, notifier, _, state = _ctx(buffer_events=[e1, e2])
    ctx2 = CommandContext(
        notifier=notifier,
        state=state,
        buffer=ctx.buffer,
        cloud_client=cloud,
        project_for_event=ctx.project_for_event,
    )
    dispatch(parse_command("/turn abcd"), ctx2)  # type: ignore[arg-type]
    assert "matches" in notifier.show_command_result.call_args[0][0]
    cloud.list_prompt_events.assert_not_called()


def test_turn_merges_last_assistant_chunks_from_cloud(monkeypatch):
    monkeypatch.setenv("SPEC_TEAM_WATCH_PAGER", "cat")
    cloud = MagicMock()
    sid = "sess123456789"
    cloud.list_prompt_events.return_value = [
        _prompt_event_row(1, "user", text="q1", session_id=sid),
        _prompt_event_row(2, "assistant", text="a1 short", session_id=sid),
        _prompt_event_row(3, "user", text="q2", session_id=sid),
        _prompt_event_row(4, "assistant", text="part", session_id=sid),
        _prompt_event_row(5, "assistant", text="part two longer", session_id=sid),
    ]
    buf_ev = _event(99, role="user", session_id=sid)
    ctx, notifier, _, state = _ctx(buffer_events=[buf_ev])
    ctx2 = CommandContext(
        notifier=notifier,
        state=state,
        buffer=ctx.buffer,
        cloud_client=cloud,
        project_for_event=ctx.project_for_event,
    )
    dispatch(parse_command("/turn sess"), ctx2)  # type: ignore[arg-type]
    cloud.list_prompt_events.assert_called_once_with(
        1,
        since_id=0,
        limit=1000,
        session_id=sid,
        timeout=25.0,
    )
    notifier.show_in_system_pager.assert_called_once()
    call_kw = notifier.show_in_system_pager.call_args
    assert "q2" in call_kw[0][0]
    assert "part two longer" in call_kw[0][0]
    assert "q1" not in call_kw[0][0]


def test_turn_uses_last_digest_when_no_arg(monkeypatch):
    monkeypatch.setenv("SPEC_TEAM_WATCH_PAGER", "cat")
    cloud = MagicMock()
    sid = "xyz999888777"
    cloud.list_prompt_events.return_value = [
        _prompt_event_row(1, "user", text="via digest", session_id=sid),
        _prompt_event_row(2, "assistant", text="reply", session_id=sid),
    ]
    notifier = MagicMock()
    notifier.last_turn_digest.return_value = (sid, 1, 1, 2)
    ctx = CommandContext(
        notifier=notifier,
        state=WatchState(),
        buffer=make_buffer(),
        cloud_client=cloud,
    )
    dispatch(parse_command("/turn"), ctx)  # type: ignore[arg-type]
    cloud.list_prompt_events.assert_called_once_with(
        1,
        since_id=0,
        limit=1000,
        session_id=sid,
        timeout=25.0,
    )
    notifier.show_in_system_pager.assert_called_once()
    body = notifier.show_in_system_pager.call_args[0][0]
    assert "via digest" in body and "reply" in body


def test_full_prints_multiple_turns_from_cloud(monkeypatch):
    monkeypatch.setenv("SPEC_TEAM_WATCH_PAGER", "cat")
    cloud = MagicMock()
    sid = "onefull999000"
    cloud.list_prompt_events.return_value = [
        _prompt_event_row(1, "user", text="a", session_id=sid),
        _prompt_event_row(2, "assistant", text="A", session_id=sid),
        _prompt_event_row(3, "user", text="b", session_id=sid),
        _prompt_event_row(4, "assistant", text="B", session_id=sid),
    ]
    notifier = MagicMock()
    notifier.last_turn_digest.return_value = (sid, 1, 3, 4)
    ctx = CommandContext(
        notifier=notifier,
        state=WatchState(),
        buffer=make_buffer(),
        cloud_client=cloud,
    )
    dispatch(parse_command("/full"), ctx)  # type: ignore[arg-type]
    notifier.show_in_system_pager.assert_called_once()
    body = notifier.show_in_system_pager.call_args[0][0]
    assert "turn 1" in body and "turn 2" in body
    assert "a" in body and "b" in body


# ── make_buffer ───────────────────────────────────────────────────


def test_make_buffer_is_bounded():
    buf = make_buffer()
    assert isinstance(buf, deque)
    # Spot-check the bound — exact value is module-level constant
    # but it is what guards memory growth in long-running watchers.
    assert buf.maxlen is not None
    assert buf.maxlen >= 100  # sanity floor
