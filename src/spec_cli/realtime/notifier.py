"""
Terminal output for incoming Spec Live events.

The Notifier is the surface a user actually *sees* when running
``spec watch``. It receives :class:`IncomingEvent` instances from the
SSE consumer (on a background thread) and prints each one in the
shared Rich console.

Output style mirrors the existing `spec` tone — color is signal,
density is low. One line per event in compact mode; one short block
per event in default mode. We deliberately don't draw boxes or tables
— the watcher window is meant to live alongside an editor and a chat,
not be the focal point.

For team review use (``spec team watch``), the Notifier can also
surface :mod:`spec_cli.realtime.critic` suggestions inline so a
reviewer can spot dangerous / vague prompts without reading every
word. The critic is opt-out, not opt-in: catching mistakes is the
whole point of having a stream open.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from ..ui import console
from .critic import Critique, critique_event, suggested_flag_command
from .events import IncomingEvent, IncomingFlag


# Per-kind glyph + color hint used both in the watcher and `spec team
# watch`. Kept small and stable so muscle memory transfers between
# screens.
_FLAG_GLYPH = {
    "warning": ("⚠", "sf.warn"),
    "question": ("?", "sf.point"),
    "block": ("⛔", "sf.reject"),
    "ack": ("✓", "sf.mint"),
}


# Role badges. The point is *zero ambiguity at a glance* — a reviewer
# scanning a busy pane should never have to read text to know whether
# a frame is a human prompt or the AI's reply. Rendered as a chunky
# colored badge with bright background; the role and the role color
# move together (green = human, cyan = AI) so even if a reviewer's
# terminal collapses spaces or wraps, the colour itself disambiguates.
#
# Background colors are inlined as hex so we don't depend on the
# theme having a "bg" variant — these need to render correctly in
# every Rich-capable terminal.
_USER_BADGE = "[bold black on #3ddab4] USER [/]"
_AI_BADGE = "[bold black on #7de3ff]  AI  [/]"

# Source → display color. Each adapter the watcher can stream from
# gets its own muted-but-distinct hue so a reviewer can tell which
# tool the engineer is using without parsing the label. Falls back
# to the generic muted style for any source we haven't tagged yet.
_SOURCE_COLOR = {
    "claude_code": "#c79bff",   # purple — Claude Code
    "codex": "#9ee37d",         # lime — Codex Desktop / Cursor agent
    "cursor": "#7de3ff",        # cyan — Cursor chat
    "manual": "#c7c9d1",        # neutral — `spec post` / scripted
}


def _source_label(source: str) -> str:
    color = _SOURCE_COLOR.get(source, "#9aa3b2")
    return f"[bold {color}]{source}[/]"


def _short_time(value: datetime | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    return value.astimezone().strftime("%H:%M:%S")


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


# How long we wait for an assistant follow-up to a user prompt before
# we surface a "no AI reply seen yet" hint. 90 seconds is a sweet
# spot in practice: short enough that a hung agent is noticed
# quickly, long enough that long-running tools (large file reads,
# Bash commands) don't trigger false alarms.
_NO_REPLY_AGE_SECS = 90.0

# Cap on tracked open sessions to keep memory bounded on a very busy
# workspace. We evict the oldest pending pair when over.
_OPEN_SESSIONS_MAX = 256


class Notifier:
    """Thread-safe printer for incoming Spec Live events.

    Rich's console serializes its own writes, so we don't need an
    explicit lock for ordering — but we do hold a lock around the
    multi-line composed output to keep events from interleaving when
    bursts arrive.

    Two opt-in review aids:

    * ``critic_enabled`` (default True) — runs the rule-based
      :mod:`spec_cli.realtime.critic` on every user turn and prints
      suggestions inline with the exact ``spec team flag`` command
      pre-filled. Disable in dashboards / CI to keep the log clean.

    * **No-reply hint** — when a user prompt has been visible for
      ``_NO_REPLY_AGE_SECS`` and no assistant turn from the same
      ``(project_id, session_id)`` has arrived, the next event from
      that author (or a ping from the heartbeat path) prints a
      ``waiting for AI reply`` warning. Catches the "I see prompts
      but not replies" scenario the team has been hitting.
    """

    def __init__(
        self,
        *,
        compact: bool = False,
        critic_enabled: bool = True,
    ) -> None:
        self._compact = compact
        self._lock = threading.Lock()
        self._critic_enabled = critic_enabled
        # session_key → (event_id, posted_at, author_display, warned)
        # Tracks open user prompts that have not yet seen an assistant
        # follow-up; lets ``check_open_sessions`` surface a hint when
        # the AI has been silent too long.
        self._open_sessions: dict[
            tuple[int, str], tuple[int, datetime, str, bool]
        ] = {}

    def show(self, event: IncomingEvent) -> None:
        time_label = _short_time(event.turn_at or event.received_at)
        author = event.author_display
        branch = event.branch or "-"
        source_label = _source_label(event.source)
        bundle = (
            f" [sf.muted]· {event.bundle_label}[/]"
            if event.bundle_label
            else ""
        )

        critiques: list[Critique] = []
        if event.role == "user":
            preview = (event.text or event.summary or "").strip()
            preview = _truncate(preview, 280 if not self._compact else 120)
            # USER badge (mint background) + author handle in the
            # source's accent color. A reviewer scanning a fast pane
            # sees the green block and knows immediately a human just
            # typed something.
            head = (
                f"{_USER_BADGE} [bold #3ddab4]{author}[/] "
                f"[sf.muted]· prompt to[/] {source_label} "
                f"[sf.muted]· {branch} · {time_label}[/]"
                f"{bundle}"
            )
            if self._critic_enabled:
                critiques = critique_event(event)
            # Remember this prompt as "awaiting AI reply". Sessions
            # are pinned by (project_id, session_id) — the same
            # identity the server uses for dedupe.
            self._remember_open_session(event)
        else:
            preview = (event.summary or event.text or "").strip()
            preview = _truncate(preview, 220 if not self._compact else 100)
            model = event.model or "assistant"
            # AI badge (cyan background). The model name carries the
            # source's accent color so "claude_code/claude-sonnet-4"
            # and "codex/gpt-5" read as cleanly separable identities.
            head = (
                f"{_AI_BADGE} [bold #7de3ff]{model}[/] "
                f"[sf.muted]· replying to[/] [bold #3ddab4]{author}[/] "
                f"[sf.muted]· in[/] {source_label} "
                f"[sf.muted]· {branch} · {time_label}[/]"
                f"{bundle}"
            )
            # Pair off the awaiting-reply tracker.
            self._mark_session_replied(event)

        with self._lock:
            if self._compact:
                line = f"{head}  {preview}" if preview else head
                console.print(line)
                self._render_critiques(event, critiques)
                return
            console.print()
            console.print(head)
            if event.title and event.role == "user":
                console.print(f"  [sf.muted]title:[/] {_truncate(event.title, 200)}")
            if preview:
                # Indent assistant bodies a bit further so they read
                # as a clear "reply" block underneath the header.
                indent = "    " if event.role != "user" else "  "
                for line in preview.splitlines():
                    console.print(f"{indent}{line}")
            elif event.role != "user":
                # Empty-body assistant turn (broadcaster did not send
                # summary or text) — call it out so reviewers know
                # there *was* a reply, just not its content.
                console.print(
                    "    [sf.muted](assistant body not shared — broadcaster is "
                    "in summary-only mode)[/]"
                )
            if event.paths_touched:
                paths = ", ".join(event.paths_touched[:5])
                if len(event.paths_touched) > 5:
                    paths += f", +{len(event.paths_touched) - 5} more"
                console.print(f"  [sf.muted]paths:[/] {paths}")
            self._render_critiques(event, critiques)

    # ── critic + session-pair plumbing ────────────────────────────

    def _render_critiques(
        self, event: IncomingEvent, critiques: list[Critique]
    ) -> None:
        """Print one indented suggestion per fired rule, plus the
        exact ``spec team flag`` command a reviewer would run if they
        agree with the critic. Single-quoted ``rule`` makes searching
        chat logs for a specific rule easy."""
        if not critiques:
            return
        for c in critiques:
            console.print(
                f"  [{c.color}]{c.glyph} AUTO {c.rule:<16}[/] "
                f"[sf.muted]{c.msg}[/]"
            )
            console.print(
                f"     [sf.muted]→ {suggested_flag_command(event.id, c)}[/]"
            )

    def _remember_open_session(self, event: IncomingEvent) -> None:
        key = (event.project_id, event.session_id or f"ev:{event.id}")
        # Evict oldest if the table gets too big — bounds memory at
        # the cost of losing one pairing on a freakishly busy host.
        if len(self._open_sessions) >= _OPEN_SESSIONS_MAX:
            try:
                oldest = min(self._open_sessions, key=lambda k: self._open_sessions[k][1])
                self._open_sessions.pop(oldest, None)
            except ValueError:
                pass
        ts = event.turn_at or event.received_at or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self._open_sessions[key] = (event.id, ts, event.author_display, False)

    def _mark_session_replied(self, event: IncomingEvent) -> None:
        key = (event.project_id, event.session_id or f"ev:{event.id}")
        self._open_sessions.pop(key, None)

    def check_open_sessions(self) -> None:
        """Surface a one-time "waiting for AI reply" hint per stale
        session. Called from the watcher's idle loop so an engineer
        notices when their teammate's agent has gone silent."""
        now = datetime.now(timezone.utc)
        threshold = timedelta(seconds=_NO_REPLY_AGE_SECS)
        to_warn: list[tuple[int, str, datetime]] = []
        with self._lock:
            for key, (ev_id, ts, author, warned) in list(
                self._open_sessions.items()
            ):
                if warned:
                    continue
                if now - ts >= threshold:
                    self._open_sessions[key] = (ev_id, ts, author, True)
                    to_warn.append((ev_id, author, ts))
        for ev_id, author, ts in to_warn:
            age = max(0, int((now - ts).total_seconds()))
            with self._lock:
                console.print(
                    f"  [sf.warn]⏳ no-reply[/]  "
                    f"[sf.muted]{author}'s prompt #{ev_id} is "
                    f"{age}s old with no AI reply yet — is their watcher "
                    f"sharing assistant turns?[/]"
                )

    def show_flag(self, flag: IncomingFlag) -> None:
        """Render an incoming flag frame inline with the prompt stream.

        Single line on purpose — flags are decorative annotations, not
        the main event. The glyph + role color encodes severity at a
        glance; the optional note is shown verbatim (truncated to a
        sensible width)."""
        glyph, color = _FLAG_GLYPH.get(flag.kind, ("⚑", "sf.warn"))
        author = flag.author_display
        note = (flag.note or "").strip()
        note_part = ""
        if note:
            note_short = _truncate(note, 220 if not self._compact else 100)
            note_part = f" [sf.muted]· {note_short}[/]"
        with self._lock:
            console.print(
                f"  [{color}]{glyph} {flag.kind:<8}[/] "
                f"[sf.label]{author}[/] [sf.muted]· flagged #{flag.prompt_event_id}[/]"
                f"{note_part}"
            )

    def announce_heartbeat(self) -> None:
        """Visible "I am still listening" tick. Surfaced periodically
        from idle workspace watchers so engineers can tell at a glance
        that the stream is alive even when the team is quiet."""
        ts = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
        with self._lock:
            console.print(
                f"[sf.muted]· still watching · {ts}[/]"
            )

    def announce_connected(self, project_label: str) -> None:
        with self._lock:
            console.print(
                f"[sf.mint]●[/] connected · [sf.label]{project_label}[/] [sf.muted]· "
                f"streaming team prompts[/]"
            )

    def announce_reconnecting(self, reason: str) -> None:
        with self._lock:
            console.print(f"[sf.warn]…[/] reconnecting [sf.muted]({reason})[/]")

    def announce_broadcast_disabled(self) -> None:
        with self._lock:
            console.print(
                "[sf.muted]·[/] receive-only mode "
                "(run [sf.label]spec live on[/] to share, or "
                "[sf.label]spec live status[/] to see why it's off)"
            )

    def announce_fatal(self, msg: str) -> None:
        with self._lock:
            console.print(f"[sf.reject]✗[/] {msg}")


__all__ = ["Notifier"]

