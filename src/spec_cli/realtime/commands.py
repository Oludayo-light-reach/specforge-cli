"""
Interactive slash-command layer for ``spec team watch``.

The watcher is, by default, a read-only firehose. This module adds a
small in-pane command surface so a reviewer can do the obvious
review things — flag a teammate, focus on one engineer, replay the
last few minutes — without leaving the terminal.

The dispatcher is pure: it takes a parsed command and a
:class:`CommandContext`, mutates the state, prints via the
:class:`Notifier`, and returns. All I/O (reading stdin, posting
flags) lives in the caller. That keeps the dispatcher trivially
testable: instantiate a context with a ``MagicMock`` notifier and
assert on method calls.

Commands
========

* ``/summarize <n>{h,m}`` — dump the last N hours/minutes of user
  prompt events as a structured context block. No API call: the
  agent already running in the terminal (Cursor, Claude Code,
  Codex) reads the output and synthesises.
* ``/flag <event_id> <kind> [note…]`` — post a flag (``warning``,
  ``question``, ``block``, ``ack``) via the existing CloudClient.
* ``/focus <handle>`` / ``/focus off`` — show only events from one
  teammate.
* ``/mute <handle>`` / ``/unmute <handle>`` — suppress events from
  one teammate (additive).
* ``/replay <n>{h,m}`` — re-emit the last N minutes from the
  in-memory buffer through the same Notifier, so critic and flag
  rendering still apply.
* ``/pair`` — flush the buffered user+assistant ``paired reply`` block
  immediately (``spec team watch`` only; for when idle detection has
  not fired yet or you use ``--assistant-quiet-secs 0``).
* ``/turn [<session-chip>]`` — **Latest turn only:** after loading the full
  session from Cloud (paginated ``session_id`` + ``since_id``), print the
  last user row and the merged assistant reply for that prompt only.
* ``/full [<session-chip>]`` — **Entire session:** same Cloud fetch, but print
  **every** user→assistant turn in order (subject to row / print caps;
  override with ``SPEC_LIVE_THREAD_MAX_ROWS`` / ``SPEC_LIVE_THREAD_PRINT_MAX_CHARS``).
* ``/critic on|off`` — toggle the auto-critic at runtime.
* ``/status`` (``/who``) — print who is active and on which source.
* ``/push <handle> [message…]`` / ``/push@handle [message…]`` — from a
  bundle cwd, append a git-push handoff row to
  ``.spec/team-push-requests.yaml`` (merged into ``team-presence.json`` /
  ``team-editing-brief.md`` by ``spec watch``).
* ``/help`` (``/h``, ``/?``) — list the commands.

**Aliases:** ``/summary`` → ``/summarize``; ``/grep``, ``/find`` → ``/search``.

The buffer is a bounded ``deque`` and lives in
:class:`CommandContext.buffer`; the live watcher appends to it on
every incoming event. We keep ``maxlen = 500`` — enough for the
last hour of an active team without bloating memory.
"""
from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Protocol

from rich.markup import escape

from ..api import ApiError
from ..config import load_credentials
from ..git import read_git_context
from .events import IncomingEvent
from .merge_turns import merge_assistant_snapshots
from .notifier import Notifier, _format_tool_call_line
from .team_push_requests import record_push_request


# Bounded event memory for /summarize / /replay / /status. ~300 rows
# keeps RAM flat while still covering long /summarize windows.
EVENT_BUFFER_MAX = 300


@dataclass
class WatchState:
    """Mutable per-session settings the dispatcher reads and writes.

    Lives for the lifetime of one ``spec team watch`` invocation.
    Reset cleanly on Ctrl+C / reconnect — none of this is persisted.
    """

    focus: str | None = None
    mutes: set[str] = field(default_factory=set)
    critic_enabled: bool = True

    def is_visible(self, event: IncomingEvent) -> bool:
        """Whether ``event`` should reach the terminal under the
        current focus / mute filters. Author identity matches against
        both ``@handle`` and the display name so users can mute by
        either."""
        handle = (event.author_handle or "").lower()
        name = (event.author_name or "").lower()
        display = event.author_display.lstrip("@").lower()
        if self.focus:
            f = self.focus.lstrip("@").lower()
            if f != handle and f != name and f != display:
                return False
        for m in self.mutes:
            mn = m.lstrip("@").lower()
            if mn == handle or mn == name or mn == display:
                return False
        return True


class _FlagClient(Protocol):
    """Minimal protocol the dispatcher needs to post a flag. Lets us
    type-check the dispatcher against either the real
    :class:`spec_cli.api.CloudClient` or a stub in tests."""

    def create_prompt_event_flag(
        self,
        *,
        project_id: int,
        event_id: int,
        kind: str,
        note: str | None = None,
    ) -> dict:
        ...


class _NotifierProtocol(Protocol):
    """Subset of :class:`Notifier` the dispatcher calls."""

    def show(self, event: IncomingEvent) -> None: ...
    def show_command_result(
        self, body: str, *, kind: str = "info"
    ) -> None: ...

    def show_in_system_pager(self, body: str, *, banner: str) -> None: ...

    def last_turn_digest(
        self,
    ) -> tuple[str, int, int, int] | None: ...


@dataclass
class CommandContext:
    """Shared state + collaborators passed to every command handler."""

    notifier: _NotifierProtocol
    state: WatchState
    buffer: Deque[IncomingEvent]
    flag_client: _FlagClient | None = None
    # Optional resolver from author handle → project_id so /flag can
    # post against the right project when the user has multiple
    # bundles in the workspace stream. Returns ``None`` when the event
    # id is not known to the dispatcher.
    project_for_event: Callable[[int], int | None] | None = None
    # ``spec team watch`` only: flush Q/A coalescing buffer on ``/pair``.
    qa_pair_now: Callable[[], None] | None = None
    # ``spec team watch`` only: same CloudClient used for /flag — also
    # powers ``/turn`` and ``/full`` thread expansion from storage.
    cloud_client: Any | None = None
    # ``spec team watch`` only: one-line digest of SSE / layout for ``/status``.
    team_watch_receiver_brief: str | None = None
    # When set (cwd is inside a bundle), ``/push`` / ``/push@…`` can write
    # ``.spec/team-push-requests.yaml``.
    bundle_root: Path | None = None


@dataclass(frozen=True)
class ParsedCommand:
    """One parsed ``/command`` line. ``name`` excludes the leading slash."""

    name: str
    args: tuple[str, ...]
    raw: str


# Valid flag kinds — mirrors the server's ``PromptEventFlagCreate``
# enum so we can validate before we hit the network.
FLAG_KINDS = ("warning", "question", "block", "ack")


_TIME_WINDOW_RE = re.compile(r"^(\d+)\s*([hm])$", re.IGNORECASE)


def parse_command(line: str) -> ParsedCommand | None:
    """Parse one input line into a :class:`ParsedCommand`.

    Returns ``None`` for blank lines, non-slash lines, or syntactically
    broken commands (e.g. ``/``). The dispatcher treats ``None`` as a
    no-op — the user's text is *not* echoed elsewhere, since the line
    already appeared in the terminal via the user's own typing.
    """
    if not isinstance(line, str):
        return None
    stripped = line.strip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:].strip()
    if not body:
        return None
    parts = body.split(None, 1)
    name = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    # ``/push@alice …`` → same as ``/push alice …`` (team handoff ping).
    if name.startswith("push@"):
        target = name[5:].lstrip("@").lower()
        name = "push"
        rest = f"{target} {rest}".strip() if target else rest.strip()
    # Most commands take whitespace-separated args except /flag and
    # /summarize-like commands that have a free-form note tail. We
    # keep the args simple here and let the handler decide how to
    # split rest. ``raw`` carries the full original text so handlers
    # that need it (notes) can use it.
    args = tuple(rest.split()) if rest else ()
    return ParsedCommand(name=name, args=args, raw=rest)


def parse_window(spec: str) -> timedelta | None:
    """Parse strings like ``"2h"`` / ``"45m"`` into a ``timedelta``.

    Returns ``None`` for any malformed input so the caller can show
    a clean usage hint instead of stack-tracing on a typo.
    """
    if not isinstance(spec, str):
        return None
    m = _TIME_WINDOW_RE.match(spec.strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if n <= 0:
        return None
    if unit == "h":
        return timedelta(hours=n)
    return timedelta(minutes=n)


# ── dispatch ──────────────────────────────────────────────────────


def dispatch(cmd: ParsedCommand, ctx: CommandContext) -> None:
    """Route ``cmd`` to the matching handler. Unknown commands print
    a hint rather than raising — the watcher is a long-lived process
    and a typo should never crash the pane."""
    handler = _HANDLERS.get(cmd.name)
    if handler is None:
        ctx.notifier.show_command_result(
            f"unknown command: /{cmd.name}. Try /help for the list.",
            kind="error",
        )
        return
    try:
        handler(cmd, ctx)
    except Exception as e:  # noqa: BLE001
        # A bug in a handler must not take the watcher down. Surface
        # the error to the reviewer and keep streaming.
        ctx.notifier.show_command_result(
            f"/{cmd.name} failed: {e}", kind="error"
        )


# ── individual handlers ──────────────────────────────────────────


def _cmd_help(_cmd: ParsedCommand, ctx: CommandContext) -> None:
    body = "\n".join(
        [
            "available commands:",
            "  aliases: /h /? = /help   /summary = /summarize   "
            "/grep /find = /search   /who = /status",
            "  /summarize <n>{h,m}            dump last window for the agent to synthesise",
            "  /flag <event_id> <kind> [note] post a flag (kinds: warning, question, block, ack)",
            "  /focus <handle> | /focus off   show events only from this teammate",
            "  /mute <handle> | /unmute <handle>  suppress events from a teammate",
            "  /replay <n>{h,m}               re-emit last window through the notifier",
            "  /pair                          flush buffered user+assistant paired block now",
            "  /turn [<session-chip>]         last user + merged AI reply in",
            "                                 system pager (q = back to live);",
            "                                 no arg = last ● turn; 50k row cap",
            "  /full [<session-chip>]         full session in pager (q = live);",
            "                                 print cap ~180k chars",
            "  /search <term>                 grep the in-memory buffer (handle / file / body)",
            "  /critic on | /critic off       toggle the auto-critic at runtime",
            "  /status                        visibility (/focus, /mutes) + who was active",
            "  /push <handle> [message…]        from bundle cwd: request @handle git-push",
            "  /push@handle [message…]        same (shorthand)",
            "  /help                          this list",
            "",
            "  output glyphs: · info   ✓ ok   ✗ error   ≡ large blocks (/summarize /turn /full)",
            "  full reference: spec-cli/docs/team-watch-slash-commands.md",
        ]
    )
    ctx.notifier.show_command_result(body, kind="info")


# Hard cap on /search results so a too-broad query doesn't dump 500
# events into the pane. ``_RECENT`` lets the search still feel "live"
# by walking newest-first.
_SEARCH_MAX_RESULTS = 25


def _cmd_search(cmd: ParsedCommand, ctx: CommandContext) -> None:
    """Grep the in-memory event buffer for a term and report matches.

    Useful for the "I remember seeing something 20 minutes ago"
    workflow. The buffer is in-process; no API call. Matches against
    body text, summary, paths touched, handles, and event id so the
    reviewer can usually find the row they want with whatever shred
    of memory they still have."""
    if not cmd.args:
        ctx.notifier.show_command_result(
            "usage: /search <term>   (matches body, summary, paths, "
            "handles, event id)",
            kind="error",
        )
        return
    term = cmd.raw.strip()
    if not term:
        ctx.notifier.show_command_result(
            "search term must not be empty.", kind="error"
        )
        return
    needle = term.lower()
    # Walk newest-first so the most recent hit is at the top of the
    # printout — matches how a reviewer thinks about "find that thing
    # I just saw".
    hits: list[tuple[IncomingEvent, str]] = []
    for ev in reversed(ctx.buffer):
        if len(hits) >= _SEARCH_MAX_RESULTS:
            break
        # Build a flat haystack from every field worth searching. We
        # include the event id as a string so `/search 12345` finds
        # by id.
        haystack_parts = [
            str(ev.id),
            (ev.author_handle or ""),
            ev.author_name or "",
            ev.summary or "",
            ev.text or "",
            ev.title or "",
            ev.source or "",
            ev.branch or "",
            ev.cwd or "",
            ev.session_id or "",
            " ".join(ev.paths_touched or []),
        ]
        haystack = "\n".join(haystack_parts).lower()
        if needle not in haystack:
            continue
        # Pull the first matching snippet — body first, then summary.
        snippet = ""
        for src in (ev.text, ev.summary, ev.title):
            if not src:
                continue
            idx = src.lower().find(needle)
            if idx < 0:
                continue
            start = max(0, idx - 30)
            end = min(len(src), idx + len(needle) + 30)
            snippet = src[start:end].replace("\n", " ").strip()
            if start > 0:
                snippet = "…" + snippet
            if end < len(src):
                snippet = snippet + "…"
            break
        hits.append((ev, snippet))
    if not hits:
        ctx.notifier.show_command_result(
            f"no matches for {term!r} in the last {len(ctx.buffer)} events.",
            kind="info",
        )
        return
    lines = [
        f"{len(hits)} match{'es' if len(hits) != 1 else ''} for "
        f"{term!r} (newest first):"
    ]
    for ev, snippet in hits:
        ts = ev.turn_at or ev.received_at
        when = ts.astimezone().strftime("%H:%M:%S") if ts else "??:??:??"
        body = snippet or (ev.summary or "(no body)")
        lines.append(
            f"  #{ev.id:<6} {when}  {ev.role:<5} {ev.author_display:<22}  {body}"
        )
    ctx.notifier.show_command_result("\n".join(lines), kind="info")


def _cmd_push_request(cmd: ParsedCommand, ctx: CommandContext) -> None:
    """Record a teammate push handoff in ``.spec/team-push-requests.yaml``."""
    if ctx.bundle_root is None:
        ctx.notifier.show_command_result(
            "/push needs a Spec bundle as cwd — `cd` to the bundle root first "
            "(workspace-wide `spec team watch` has no single `.spec/` to write).",
            kind="error",
        )
        return
    if not cmd.args:
        ctx.notifier.show_command_result(
            "usage: /push@handle [optional message]  or  /push handle [message]",
            kind="error",
        )
        return
    target = cmd.args[0].lstrip("@").lower()
    note = " ".join(cmd.args[1:]).strip() or None
    creds = load_credentials()
    fh = (creds.user_handle or "").strip().lstrip("@").lower() if creds else None
    disp: str | None = None
    if creds:
        if creds.user_handle:
            disp = f"@{creds.user_handle.lstrip('@')}"
        if creds.user_name:
            disp = f"{creds.user_name} ({disp})" if disp else creds.user_name
    git = read_git_context(ctx.bundle_root)
    branch = git.branch
    try:
        path = record_push_request(
            ctx.bundle_root,
            to_handle=target,
            from_handle=fh or None,
            from_display=disp,
            branch=branch,
            message=note,
        )
    except ValueError as e:
        ctx.notifier.show_command_result(str(e), kind="error")
        return
    except OSError as e:
        ctx.notifier.show_command_result(f"write failed: {e}", kind="error")
        return
    ctx.notifier.show_command_result(
        f"push handoff recorded for @{target} → {path}\n"
        "(merged into team-presence / editing-brief on the next `spec watch` tick, "
        "≤30s if the daemon is running.)",
        kind="ok",
    )


def _cmd_pair(_cmd: ParsedCommand, ctx: CommandContext) -> None:
    """Force-print the coalesced user+assistant block (team watch only)."""
    if ctx.qa_pair_now is None:
        ctx.notifier.show_command_result(
            "/pair only works in `spec team watch` (paired-reply mode).",
            kind="error",
        )
        return
    ctx.qa_pair_now()


# Page size for ``GET /prompt-events`` — must stay within server
# ``PROMPT_EVENT_LIST_LIMIT_MAX`` (1000). ``/full`` pages with
# ``since_id`` + ``session_id`` until the session is exhausted.
_THREAD_PAGE_SIZE = 1000
_THREAD_FETCH_TIMEOUT = 25.0


def _thread_max_session_rows() -> int:
    """Upper bound on rows pulled for one ``/full`` / ``/turn`` session.

    Env ``SPEC_LIVE_THREAD_MAX_ROWS`` overrides (clamped 1k–2M). Default
    raised from 50k so long agent threads truncate less often.
    """
    raw = (os.environ.get("SPEC_LIVE_THREAD_MAX_ROWS") or "").strip()
    if raw.isdigit():
        return max(1000, min(int(raw), 2_000_000))
    return 120_000


def _thread_print_max_chars() -> int:
    """Pager body cap for ``/full`` (and symmetrically sized ``/turn``).

    Env ``SPEC_LIVE_THREAD_PRINT_MAX_CHARS`` overrides (clamped).
    """
    raw = (os.environ.get("SPEC_LIVE_THREAD_PRINT_MAX_CHARS") or "").strip()
    if raw.isdigit():
        return max(50_000, min(int(raw), 10_000_000))
    return 900_000


def _session_matches_prefix(prefix: str, session_id: str) -> bool:
    ps = prefix.strip()
    sid = (session_id or "").strip()
    if not ps or not sid:
        return False
    return sid == ps or sid.startswith(ps)


def _resolve_session_for_thread(
    ctx: CommandContext,
    prefix: str | None,
) -> tuple[int, str] | None:
    raw = (prefix or "").strip()
    if not raw:
        try:
            d = ctx.notifier.last_turn_digest()
        except Exception:  # noqa: BLE001
            d = None
        if (
            isinstance(d, tuple)
            and len(d) == 4
            and isinstance(d[0], str)
            and d[0].strip()
            and isinstance(d[1], int)
        ):
            sid, pid, _, _ = d
            return (pid, sid.strip())
        return None
    candidates: set[tuple[int, str]] = set()
    for ev in ctx.buffer:
        if ev.role not in ("user", "assistant", "error"):
            continue
        sid = (ev.session_id or "").strip()
        if not _session_matches_prefix(raw, sid):
            continue
        candidates.add((ev.project_id, sid))
    if len(candidates) > 1:
        ctx.notifier.show_command_result(
            f"`{raw}` matches {len(candidates)} different sessions in the "
            "buffer — type a longer chip.",
            kind="error",
        )
        return None
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _fetch_entire_session_events(
    client: Any,
    project_id: int,
    session_id: str,
) -> tuple[list[IncomingEvent], bool]:
    """All user/assistant/error rows for ``session_id``, ascending by ``id``.

    Pages with ``since_id`` + optional ``session_id`` (Cloud >= this release).
    Rows are always filtered to ``session_id`` client-side so older servers
    that ignore the query param still return correct data (at the cost of
    more round-trips on busy projects).

    Returns ``(events, truncated)`` where ``truncated`` is True when the
    hard row cap was hit mid-session.
    """
    target = (session_id or "").strip()
    if not target:
        return [], False
    out: list[IncomingEvent] = []
    since = 0
    truncated = False
    while len(out) < _thread_max_session_rows():
        rows = client.list_prompt_events(
            project_id,
            since_id=since,
            limit=_THREAD_PAGE_SIZE,
            session_id=target,
            timeout=_THREAD_FETCH_TIMEOUT,
        )
        if not rows:
            break
        raw_ids: list[int] = []
        for r in rows:
            if not isinstance(r, dict) or r.get("id") is None:
                continue
            try:
                raw_ids.append(int(r["id"]))
            except (TypeError, ValueError):
                continue
        if not raw_ids:
            break
        batch: list[IncomingEvent] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                ev = IncomingEvent.from_json(r)
            except Exception:  # noqa: BLE001
                continue
            if (ev.session_id or "").strip() != target:
                continue
            if ev.role not in ("user", "assistant", "error"):
                continue
            batch.append(ev)
        out.extend(batch)
        if len(out) >= _thread_max_session_rows():
            truncated = True
            del out[_thread_max_session_rows() :]
            break
        page_max = max(raw_ids)
        if len(rows) < _THREAD_PAGE_SIZE:
            break
        since = page_max
    out.sort(key=lambda e: e.id)
    return out, truncated


def _iter_user_assistant_turns(
    evs: list[IncomingEvent],
) -> list[tuple[IncomingEvent, list[IncomingEvent]]]:
    turns: list[tuple[IncomingEvent, list[IncomingEvent]]] = []
    cur_u: IncomingEvent | None = None
    cur_a: list[IncomingEvent] = []
    for ev in evs:
        if ev.role == "user":
            if cur_u is not None:
                turns.append((cur_u, cur_a))
            cur_u = ev
            cur_a = []
        elif ev.role in ("assistant", "error") and cur_u is not None:
            cur_a.append(ev)
    if cur_u is not None:
        turns.append((cur_u, cur_a))
    return turns


def _format_thread_turn_block(
    label: str,
    user: IncomingEvent,
    reply_chunks: list[IncomingEvent],
) -> str:
    """One user prompt plus ordered assistant/error segments for ``/turn`` / ``/full``."""
    chip = (user.session_id or "").strip()[:8] or "?"
    ubody = (user.text or user.summary or "").strip()
    ordered = sorted(reply_chunks, key=lambda e: e.id)
    lines: list[str] = [
        f"── {label} · session {chip}… · user #{user.id} ──",
        "",
        "USER",
        ubody,
    ]
    if not ordered:
        lines.extend(["", "(no assistant or error rows in this window)"])
        return "\n".join(lines)

    i = 0
    while i < len(ordered):
        ev = ordered[i]
        if ev.role == "error":
            err_body = Notifier._assistant_visible_prose(
                ev.text, ev.summary
            ).strip()
            lines.extend(
                [
                    "",
                    f"ERROR (#{ev.id}) — {ev.model or 'agent'}",
                    err_body or "(no message)",
                ]
            )
            i += 1
            continue
        if ev.role == "assistant":
            j = i
            while j < len(ordered) and ordered[j].role == "assistant":
                j += 1
            group = ordered[i:j]
            merged = merge_assistant_snapshots(group)
            abody = Notifier._assistant_visible_prose(
                merged.text, merged.summary
            ).strip()
            lines.extend(
                [
                    "",
                    f"ASSISTANT (#{group[0].id}–#{group[-1].id})",
                    abody or "(empty body)",
                ]
            )
            if merged.tool_calls:
                lines.extend(
                    [
                        "",
                        f"TOOL RUNS ({len(merged.tool_calls)})",
                    ]
                )
                shown = merged.tool_calls[:50]
                for tc in shown:
                    lines.append(f"  · {_format_tool_call_line(tc)}")
                extra = len(merged.tool_calls) - len(shown)
                if extra > 0:
                    lines.append(f"  · …+{extra} more tools")
            i = j
            continue
        i += 1
    return "\n".join(lines)


def _cmd_turn(cmd: ParsedCommand, ctx: CommandContext) -> None:
    if ctx.cloud_client is None:
        ctx.notifier.show_command_result(
            "/turn needs `spec team watch` with cloud commands enabled "
            "(signed in).",
            kind="error",
        )
        return
    prefix = " ".join(cmd.args).strip() if cmd.args else ""
    resolved = _resolve_session_for_thread(ctx, prefix or None)
    if resolved is None:
        if prefix:
            ctx.notifier.show_command_result(
                f"no session in the buffer matches `{prefix}` — check the "
                "session chip, or wait until that session appears.",
                kind="error",
            )
        else:
            ctx.notifier.show_command_result(
                "no session — pass a chip from the buffer "
                "(e.g. `/turn a6c8b1`) or complete a digest turn first "
                "(● turn complete).",
                kind="error",
            )
        return
    pid, sid = resolved
    row_cap = _thread_max_session_rows()
    try:
        evs, truncated = _fetch_entire_session_events(ctx.cloud_client, pid, sid)
    except ApiError as e:
        ctx.notifier.show_command_result(f"/turn fetch failed: {e}", kind="error")
        return
    except Exception as e:  # noqa: BLE001
        ctx.notifier.show_command_result(f"/turn fetch failed: {e}", kind="error")
        return
    notes: list[str] = []
    if truncated:
        notes.append(
            f"[note] session fetch stopped at {row_cap:,} rows — earliest "
            "events may be missing; the last turn may be incomplete.\n"
        )
    turns = _iter_user_assistant_turns(evs)
    if not turns:
        ctx.notifier.show_command_result(
            f"no user rows for session `{sid[:8]}…` in Cloud storage.",
            kind="info",
        )
        return
    user, chunks = turns[-1]
    if not chunks:
        ctx.notifier.show_command_result(
            f"no assistant rows after the latest user prompt "
            f"(#{user.id}) — the model may still be streaming.",
            kind="info",
        )
        return
    body = "".join(notes) + _format_thread_turn_block("/turn", user, chunks)
    chip = (user.session_id or "").strip()[:8] or (sid[:8] if sid else "?")
    ctx.notifier.show_in_system_pager(body, banner=f"/turn · session {chip}…")


def _cmd_full(cmd: ParsedCommand, ctx: CommandContext) -> None:
    if ctx.cloud_client is None:
        ctx.notifier.show_command_result(
            "/full needs `spec team watch` with cloud commands enabled "
            "(signed in).",
            kind="error",
        )
        return
    prefix = " ".join(cmd.args).strip() if cmd.args else ""
    resolved = _resolve_session_for_thread(ctx, prefix or None)
    if resolved is None:
        if prefix:
            ctx.notifier.show_command_result(
                f"no session in the buffer matches `{prefix}` — check the "
                "session chip, or wait until that session appears.",
                kind="error",
            )
        else:
            ctx.notifier.show_command_result(
                "no session — pass a chip from the buffer "
                "(e.g. `/full a6c8b1`) or complete a digest turn first "
                "(● turn complete).",
                kind="error",
            )
        return
    pid, sid = resolved
    row_cap = _thread_max_session_rows()
    print_cap = _thread_print_max_chars()
    try:
        evs, truncated = _fetch_entire_session_events(ctx.cloud_client, pid, sid)
    except ApiError as e:
        ctx.notifier.show_command_result(f"/full fetch failed: {e}", kind="error")
        return
    except Exception as e:  # noqa: BLE001
        ctx.notifier.show_command_result(f"/full fetch failed: {e}", kind="error")
        return
    turns = _iter_user_assistant_turns(evs)
    if not turns:
        ctx.notifier.show_command_result(
            f"no user rows for session `{sid[:8]}…` in Cloud storage.",
            kind="info",
        )
        return
    blocks: list[str] = []
    if truncated:
        blocks.append(
            f"[note] session fetch stopped at {row_cap:,} rows "
            "— older turns may be missing.\n"
        )
    for idx, (user, chunks) in enumerate(turns, start=1):
        if not chunks:
            chip = (user.session_id or "").strip()[:8] or "?"
            blocks.append(
                f"── turn {idx} · session {chip}… · #{user.id} "
                f"(no assistant rows in window) ──\n\n"
                f"USER\n{(user.text or user.summary or '').strip()}"
            )
            continue
        blocks.append(
            _format_thread_turn_block(f"/full · turn {idx}", user, chunks)
        )
    body = "\n\n".join(blocks)
    if len(body) > print_cap:
        body = (
            body[:print_cap]
            + "\n\n… [truncated at "
            f"{print_cap:,} chars — raise SPEC_LIVE_THREAD_PRINT_MAX_CHARS "
            "or inspect Cloud / REST export]\n"
        )
    chip = sid[:8] if sid else "?"
    ctx.notifier.show_in_system_pager(
        body,
        banner=f"/full · session {chip}… · {len(turns)} turn(s)",
    )


def _cmd_focus(cmd: ParsedCommand, ctx: CommandContext) -> None:
    if not cmd.args:
        if ctx.state.focus:
            ctx.notifier.show_command_result(
                f"focus is on @{ctx.state.focus.lstrip('@')}. "
                "Use `/focus off` to clear.",
                kind="info",
            )
        else:
            ctx.notifier.show_command_result(
                "usage: /focus <handle> or /focus off",
                kind="error",
            )
        return
    target = cmd.args[0]
    if target.lower() == "off":
        ctx.state.focus = None
        ctx.notifier.show_command_result("focus cleared.", kind="ok")
        return
    handle = target.lstrip("@")
    ctx.state.focus = handle
    ctx.notifier.show_command_result(
        f"focus → @{handle}. All other teammates are hidden until "
        "`/focus off`.",
        kind="ok",
    )


def _cmd_mute(cmd: ParsedCommand, ctx: CommandContext) -> None:
    if not cmd.args:
        ctx.notifier.show_command_result(
            "usage: /mute <handle>", kind="error"
        )
        return
    handle = cmd.args[0].lstrip("@")
    ctx.state.mutes.add(handle)
    ctx.notifier.show_command_result(
        f"muted @{handle}. Use `/unmute {handle}` to reverse.",
        kind="ok",
    )


def _cmd_unmute(cmd: ParsedCommand, ctx: CommandContext) -> None:
    if not cmd.args:
        ctx.notifier.show_command_result(
            "usage: /unmute <handle>", kind="error"
        )
        return
    handle = cmd.args[0].lstrip("@")
    if handle not in ctx.state.mutes:
        ctx.notifier.show_command_result(
            f"@{handle} was not muted.", kind="info"
        )
        return
    ctx.state.mutes.discard(handle)
    ctx.notifier.show_command_result(
        f"@{handle} unmuted.", kind="ok"
    )


def _cmd_critic(cmd: ParsedCommand, ctx: CommandContext) -> None:
    if not cmd.args or cmd.args[0].lower() not in {"on", "off"}:
        state = "on" if ctx.state.critic_enabled else "off"
        ctx.notifier.show_command_result(
            f"auto-critic is currently {state}. Use `/critic on` or "
            "`/critic off` to toggle.",
            kind="info",
        )
        return
    new_state = cmd.args[0].lower() == "on"
    ctx.state.critic_enabled = new_state
    ctx.notifier.show_command_result(
        f"auto-critic {'enabled' if new_state else 'disabled'}.",
        kind="ok",
    )


def _cmd_status(_cmd: ParsedCommand, ctx: CommandContext) -> None:
    """Per-teammate "last seen" digest, derived from the in-memory
    buffer. No API call — what's in the pane is what we report."""
    st = ctx.state
    vis: list[str] = []
    if ctx.team_watch_receiver_brief:
        vis.append(ctx.team_watch_receiver_brief)

    if st.focus:
        vis.append(
            f"visibility: /focus @{st.focus.lstrip('@')} "
            "(only this teammate's events are printed)"
        )
    elif st.mutes:
        joined = ", ".join(sorted(f"@{m.lstrip('@')}" for m in st.mutes))
        vis.append(f"visibility: muted {joined} (everyone else still shows)")
    else:
        vis.append(
            "visibility: all teammates (workspace prompt stream; "
            "no /focus, no /mutes)"
        )

    if not ctx.buffer:
        vis.append("no activity yet in this session.")
        ctx.notifier.show_command_result("\n".join(vis), kind="info")
        return
    # Map (handle, source) → (last_event_time, bundle_label)
    seen: dict[tuple[str, str], tuple[datetime, str | None]] = {}
    for ev in ctx.buffer:
        if ev.role == "presence":
            continue
        key = (ev.author_display, ev.source or "?")
        ts = ev.turn_at or ev.received_at
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        prev = seen.get(key)
        if prev is None or ts > prev[0]:
            seen[key] = (ts, ev.bundle_label)
    if not seen:
        vis.append("no non-presence events seen yet.")
        ctx.notifier.show_command_result("\n".join(vis), kind="info")
        return
    rows: list[str] = [*vis, "active teammates (last seen, source, bundle):"]
    now = datetime.now(timezone.utc)
    for (author, source), (ts, bundle) in sorted(
        seen.items(), key=lambda kv: kv[1][0], reverse=True
    ):
        age = max(0, int((now - ts).total_seconds()))
        ago = _format_age(age)
        bundle_part = f" · {bundle}" if bundle else ""
        rows.append(f"  {author:<24} {source:<14} {ago:<10}{bundle_part}")
    ctx.notifier.show_command_result("\n".join(rows), kind="info")


def _format_age(secs: int) -> str:
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86_400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86_400}d ago"


def _cmd_replay(cmd: ParsedCommand, ctx: CommandContext) -> None:
    if not cmd.args:
        ctx.notifier.show_command_result(
            "usage: /replay <n>{h,m} (e.g. /replay 10m)", kind="error"
        )
        return
    window = parse_window(cmd.args[0])
    if window is None:
        ctx.notifier.show_command_result(
            f"unrecognised window: {cmd.args[0]!r}. "
            "Use formats like 10m, 2h.",
            kind="error",
        )
        return
    cutoff = datetime.now(timezone.utc) - window
    # ``ctx.buffer`` is append-ordered, so iterating it gives us the
    # chronological order we want for replay.
    selected: list[IncomingEvent] = []
    for ev in ctx.buffer:
        ts = ev.turn_at or ev.received_at
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            selected.append(ev)
    if not selected:
        ctx.notifier.show_command_result(
            "nothing to replay in that window.", kind="info"
        )
        return
    ctx.notifier.show_command_result(
        f"replaying {len(selected)} event(s) from the last {cmd.args[0]}…",
        kind="info",
    )
    for ev in selected:
        ctx.notifier.show(ev)


def _cmd_summarize(cmd: ParsedCommand, ctx: CommandContext) -> None:
    """Dump a structured context block for the agent already running
    in this terminal (Cursor / Claude Code / Codex) to synthesise.

    The block is intentionally machine-readable: a clear header and
    footer wrapped around a per-event record. The agent then sees
    one giant prompt that says "here is the team's recent activity,
    please summarise" — no spec-cli API call required, no LLM cost
    inside spec-cli."""
    if not cmd.args:
        ctx.notifier.show_command_result(
            "usage: /summarize <n>{h,m} (e.g. /summarize 2h)",
            kind="error",
        )
        return
    window = parse_window(cmd.args[0])
    if window is None:
        ctx.notifier.show_command_result(
            f"unrecognised window: {cmd.args[0]!r}.", kind="error"
        )
        return
    cutoff = datetime.now(timezone.utc) - window
    rows: list[IncomingEvent] = []
    for ev in ctx.buffer:
        if ev.role == "presence":
            continue
        ts = ev.turn_at or ev.received_at
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            rows.append(ev)
    if not rows:
        ctx.notifier.show_command_result(
            f"no team activity in the last {cmd.args[0]}.",
            kind="info",
        )
        return

    lines: list[str] = []
    # Rich markup: ``\\[`` / ``\\]`` render as literal brackets so tags in
    # user prompts cannot break the pane (body lines are still escaped).
    lines.append(
        rf"\[spec summarize request — past {escape(cmd.args[0])}\]"
    )
    lines.append("[sf.muted]" + "─" * 64 + "[/]")
    lines.append("")
    for ev in rows:
        ts = ev.turn_at or ev.received_at or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        when = escape(ts.astimezone().strftime("%H:%M:%S"))
        role_u = ev.role.upper()
        role_style = {
            "USER": "bold #3ddab4",
            "ASSISTANT": "bold #7de3ff",
            "ERROR": "bold #ff8a98",
            "ASSISTANT_CLOSED": "dim #9aa3b2",
        }.get(role_u, "bold #c7c9d1")
        bundle = escape(f" · {ev.bundle_label}") if ev.bundle_label else ""
        model = (
            escape(f"/{ev.model}")
            if ev.role == "assistant" and ev.model
            else ""
        )
        lines.append(
            f"[sf.muted]{when}[/] [bold #c7c9d1]{escape(ev.author_display)}[/] "
            f"· [{role_style}]{role_u}[/] · "
            f"[sf.label]{escape(ev.source)}[/]{model} · "
            f"[sf.muted]{escape(ev.branch or '-')}{bundle}[/]"
        )
        body = (ev.text or ev.summary or "").strip()
        if body:
            for body_line in body.splitlines():
                lines.append(f"  [sf.muted]{escape(body_line)}[/]")
        else:
            lines.append("  [sf.muted](no body)[/]")
        lines.append("")
    lines.append("[sf.muted]" + "─" * 64 + "[/]")
    lines.append(
        r"\[end of summarize request — please synthesise the above into "
        r"(1) what each engineer is currently working on, (2) any "
        r"patterns or risks across the team, (3) one concrete action "
        r"the team should take next.\]"
    )
    ctx.notifier.show_command_result("\n".join(lines), kind="summarize")


def _cmd_flag(cmd: ParsedCommand, ctx: CommandContext) -> None:
    if len(cmd.args) < 2:
        ctx.notifier.show_command_result(
            "usage: /flag <event_id> <kind> [note…]    "
            f"(kinds: {', '.join(FLAG_KINDS)})",
            kind="error",
        )
        return
    if ctx.flag_client is None or ctx.project_for_event is None:
        ctx.notifier.show_command_result(
            "/flag is not wired up in this watcher (no API client).",
            kind="error",
        )
        return
    try:
        event_id = int(cmd.args[0])
    except ValueError:
        ctx.notifier.show_command_result(
            f"event_id must be an integer, got {cmd.args[0]!r}.",
            kind="error",
        )
        return
    kind = cmd.args[1].lower()
    if kind not in FLAG_KINDS:
        ctx.notifier.show_command_result(
            f"unknown flag kind: {kind!r}. "
            f"Use one of: {', '.join(FLAG_KINDS)}.",
            kind="error",
        )
        return
    # Anything after the kind is a free-form note. We use cmd.raw
    # (the unsplit tail) to preserve internal whitespace / quotes the
    # reviewer typed.
    note: str | None = None
    tail = cmd.raw.strip()
    # Strip "<event_id> <kind> " from the front of raw to leave the
    # note. Done by token count rather than character count so weird
    # whitespace in the raw string doesn't trip us.
    tokens = tail.split(None, 2)
    if len(tokens) >= 3:
        note = tokens[2].strip() or None
    pid = ctx.project_for_event(event_id)
    if pid is None:
        ctx.notifier.show_command_result(
            f"event #{event_id} is not in the current buffer — "
            "scroll up or use `spec team flag` from outside the watcher.",
            kind="error",
        )
        return
    try:
        ctx.flag_client.create_prompt_event_flag(
            project_id=pid, event_id=event_id, kind=kind, note=note
        )
    except Exception as e:  # noqa: BLE001
        ctx.notifier.show_command_result(
            f"flag failed: {e}", kind="error"
        )
        return
    note_part = f" — {note}" if note else ""
    ctx.notifier.show_command_result(
        f"flagged #{event_id} as {kind}{note_part}", kind="ok"
    )


_HANDLERS: dict[str, Callable[[ParsedCommand, CommandContext], None]] = {
    "help": _cmd_help,
    "h": _cmd_help,
    "?": _cmd_help,
    "summarize": _cmd_summarize,
    "summary": _cmd_summarize,
    "flag": _cmd_flag,
    "focus": _cmd_focus,
    "mute": _cmd_mute,
    "unmute": _cmd_unmute,
    "critic": _cmd_critic,
    "status": _cmd_status,
    "who": _cmd_status,
    "replay": _cmd_replay,
    "search": _cmd_search,
    "grep": _cmd_search,
    "find": _cmd_search,
    "pair": _cmd_pair,
    "turn": _cmd_turn,
    "full": _cmd_full,
    "push": _cmd_push_request,
}


def make_buffer() -> Deque[IncomingEvent]:
    """Construct the bounded event memory used by ``spec team watch``.

    Exposed so the caller can wire one ``deque`` into both the
    consumer thread (append on every event) and the command context
    (read by /summarize, /replay, /status)."""
    return deque(maxlen=EVENT_BUFFER_MAX)


__all__ = [
    "CommandContext",
    "EVENT_BUFFER_MAX",
    "FLAG_KINDS",
    "ParsedCommand",
    "WatchState",
    "dispatch",
    "make_buffer",
    "parse_command",
    "parse_window",
]
