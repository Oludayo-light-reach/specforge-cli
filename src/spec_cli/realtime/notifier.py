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

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rich.markup import escape

from ..ui import console
from .critic import SEV_HIGH, Critique, critique_event, suggested_flag_command
from .events import IncomingEvent, IncomingFlag


def _short_cwd(cwd: str | None) -> str | None:
    """Render a teammate's working directory compactly for the header.

    Strips the user's own ``$HOME`` to ``~`` (universal shell ergonomics)
    and collapses very long paths to ``…/last-two-segments`` so the
    header line never wraps. Returns ``None`` for missing / empty
    input so callers can compose the chip conditionally.
    """
    if not isinstance(cwd, str):
        return None
    path = cwd.strip()
    if not path:
        return None
    home = os.path.expanduser("~")
    if home and (path == home or path.startswith(home + os.sep)):
        path = "~" + path[len(home):]
    # Hard cap on chip width — pick the last two segments when the
    # path is longer than 40 chars so the eye still recognises the
    # repo name on the right.
    if len(path) > 40:
        try:
            parts = Path(path).parts
            if len(parts) > 2:
                path = "…/" + "/".join(parts[-2:])
        except Exception:  # noqa: BLE001
            pass
    return path


def _short_session(session_id: str | None) -> str | None:
    """Render a short, stable session badge. We use the first 6 chars
    of the upstream session id — long enough to be unique across the
    handful of concurrent sessions a reviewer is likely to be
    watching, short enough not to dominate the header line."""
    if not isinstance(session_id, str):
        return None
    sid = session_id.strip()
    if not sid:
        return None
    return sid[:6]


def _paths_chip(paths: list[str] | None) -> str | None:
    """Render a compact chip listing the first couple of files an
    event touched, with an overflow marker. Cheap proxy for "what
    did this turn change" without shipping a full diff over the
    wire. Returns ``None`` when there is nothing to show."""
    if not paths:
        return None
    seen = [p for p in paths if isinstance(p, str) and p][:2]
    if not seen:
        return None
    extra = max(0, len(paths) - len(seen))
    basenames = [p.rsplit("/", 1)[-1] for p in seen]
    body = ", ".join(basenames)
    if extra:
        body += f", +{extra} more"
    return body


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
# Red ERROR badge used when an adapter ships ``role = "error"`` —
# agent timeout, refused request, tool failure. Lights up the pane so
# a reviewer notices an agent in trouble without having to read text.
_ERROR_BADGE = "[bold white on #d63a4e] ERROR [/]"

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


# Terminal preview limits (chars, excluding the trailing ellipsis).
# Defaults favour team review: long user prompts and large assistant
# replies stay readable without ``--compact``; compact mode stays
# bounded so one-line logging stays usable.
_PREVIEW_USER = (48_000, 2_000)  # (non-compact, compact)
_PREVIEW_ASSISTANT = (96_000, 8_000)  # (non-compact, compact)
_PREVIEW_ERROR = (24_000, 800)

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
        notify: bool = False,
        pairing_buffer: Any = None,
        viewer_handle: str | None = None,
    ) -> None:
        self._compact = compact
        self._lock = threading.Lock()
        self._critic_enabled = critic_enabled
        # Opt-in attention helper: ring the terminal bell + best-effort
        # OS notification (macOS only for now via ``osascript``) when
        # the critic fires at ``block`` severity, e.g. a teammate just
        # typed ``rm -rf`` or pasted a secret. Default off so noisy
        # team feeds don't beep constantly.
        self._notify = notify
        # session_key → (event_id, posted_at, author_display, warned)
        # Tracks open user prompts that have not yet seen an assistant
        # follow-up; lets ``check_open_sessions`` surface a hint when
        # the AI has been silent too long.
        self._open_sessions: dict[
            tuple[int, str], tuple[int, datetime, str, bool]
        ] = {}
        # (project_id, session_id) → (author_display, truncated prompt) so
        # the first assistant/error line after a user turn can echo what
        # question was being answered — even when the viewer only tuned
        # in after the USER badge scrolled away.
        self._pending_user_prompt: dict[
            tuple[int, str], tuple[str, str]
        ] = {}
        # Optional bounded deque of recent events (``spec team watch``).
        # When the warm-up window skipped the triggering USER row, we
        # still recover the prompt for ``⤷ prompt`` by scanning back.
        self._pairing_buffer = pairing_buffer
        # Signed-in viewer (``spec team watch`` only). Skip no-reply
        # tracking for your own user prompts — the hint is for teammates.
        self._viewer_handle = (viewer_handle or "").strip().lower() or None

    def set_critic_enabled(self, enabled: bool) -> None:
        """Toggle the auto-critic at runtime. Used by the ``/critic``
        slash command so a reviewer can silence the suggestion stream
        without restarting the watcher."""
        self._critic_enabled = bool(enabled)

    def record_pairing(self, event: IncomingEvent) -> None:
        """Update the user→AI pairing tracker without rendering.

        Called from the watcher's ``_deliver`` *before* the filter that
        drops noisy tool-only assistant frames. Without this hop, a
        tool-only assistant reply (synthesized summary, no critic hit)
        would be filtered out before ``show()`` runs — and the
        ``_remember_open_session`` entry left by the matching user
        prompt would never be cleared, causing the no-reply hint to
        fire 90 s later even though the AI *did* reply.

        Safe to call multiple times for the same event: the
        underlying maps are idempotent.
        """
        if event.role == "user":
            self._remember_open_session(event)
        elif event.role in ("assistant", "error"):
            self._mark_session_replied(event)

    @staticmethod
    def _session_pair_key(event: IncomingEvent) -> tuple[int, str]:
        sid = (event.session_id or "").strip()
        if not sid:
            # Extremely rare — keep keys stable per row so we never leak
            # one session's prompt into another on the same project.
            sid = f"_ev:{event.id}"
        return (event.project_id, sid)

    def _pairing_prompt_from_buffer(
        self, event: IncomingEvent
    ) -> tuple[str, str] | None:
        """Find the most recent user turn for this session with ``id``
        strictly before ``event`` — used when ``_pending_user_prompt``
        missed the USER frame (bootstrap gap, reconnect, or bursty
        assistant rows).

        Stops walking back if we encounter another assistant/error
        row for the same session first — that one already carried the
        echo, so this row is a continuation and should *not* repeat
        the prompt one-liner.
        """
        buf = self._pairing_buffer
        if buf is None:
            return None
        key = self._session_pair_key(event)
        try:
            tail = reversed(buf)
        except TypeError:
            return None
        for ev in tail:
            if ev.id >= event.id:
                continue
            if self._session_pair_key(ev) != key:
                continue
            # If a prior assistant/error in the same session is closer
            # to ``event`` than the user prompt, this row is a chain
            # continuation — the echo already happened, suppress it.
            if ev.role in ("assistant", "error"):
                return None
            if ev.role != "user":
                continue
            preview = (ev.text or ev.summary or "").strip()
            if not preview:
                continue
            lim_u, lim_uc = _PREVIEW_USER
            preview = _truncate(preview, lim_uc if self._compact else lim_u)
            return (ev.author_display, preview)
        return None

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

        # Composable context chips — cwd shortened to ``~``, paths the
        # turn touched, and a short session id for thread tracking.
        # All three are optional: we only render the divider before a
        # chip when it actually has content, so a quiet stream stays
        # clean and a busy stream still fits on one line.
        cwd_chip = _short_cwd(event.cwd)
        paths_chip = _paths_chip(event.paths_touched)
        session_chip = _short_session(event.session_id)
        ctx_parts: list[str] = []
        if cwd_chip:
            ctx_parts.append(f"[sf.muted]cwd[/] [sf.label]{cwd_chip}[/]")
        if paths_chip:
            ctx_parts.append(f"[sf.muted]touched[/] [sf.label]{paths_chip}[/]")
        if session_chip:
            ctx_parts.append(f"[sf.muted]session[/] [sf.label]{session_chip}[/]")
        ctx_line = "  ".join(ctx_parts) if ctx_parts else ""

        critiques: list[Critique] = []
        pair_key = self._session_pair_key(event)
        pending_prompt: tuple[str, str] | None = None
        if event.role == "user":
            preview = (event.text or event.summary or "").strip()
            lim_u, lim_uc = _PREVIEW_USER
            preview = _truncate(preview, lim_uc if self._compact else lim_u)
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
            if preview:
                self._pending_user_prompt[pair_key] = (author, preview)
            # Remember this prompt as "awaiting AI reply". Sessions
            # are pinned by (project_id, session_id) — the same
            # identity the server uses for dedupe.
            self._remember_open_session(event)
        elif event.role == "error":
            # Agent error: timeout / tool failure / refused request.
            # Red badge + short message in the header keeps the eye
            # snapping to it even on a busy pane.
            preview = (event.text or event.summary or "").strip()
            lim_e, lim_ec = _PREVIEW_ERROR
            preview = _truncate(preview, lim_ec if self._compact else lim_e)
            model = event.model or "agent"
            head = (
                f"{_ERROR_BADGE} [bold #ff8a98]{model}[/] "
                f"[sf.muted]· failed on[/] [bold #3ddab4]{author}[/] "
                f"[sf.muted]· in[/] {source_label} "
                f"[sf.muted]· {branch} · {time_label}[/]"
                f"{bundle}"
            )
            # An error closes the awaiting-reply tracker for this
            # session — we have a definitive answer, just not a happy
            # one.
            self._mark_session_replied(event)
            # Surface assistant-side critic on the error message too,
            # so e.g. a tool failure containing destructive text
            # still gets flagged.
            if self._critic_enabled:
                critiques = critique_event(event)
            pending_prompt = self._pending_user_prompt.pop(pair_key, None)
            if pending_prompt is None:
                pending_prompt = self._pairing_prompt_from_buffer(event)
        else:
            # Prefer full ``text`` over ``summary`` — both are usually set
            # for assistant turns, and the summary is only a headline.
            preview = (event.text or event.summary or "").strip()
            lim_a, lim_ac = _PREVIEW_ASSISTANT
            preview = _truncate(preview, lim_ac if self._compact else lim_a)
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
            # Assistant turns also run through the critic so
            # destructive Bash / test-bypass language in tool
            # summaries surfaces in the live stream.
            if self._critic_enabled:
                critiques = critique_event(event)
            pending_prompt = self._pending_user_prompt.pop(pair_key, None)
            if pending_prompt is None:
                pending_prompt = self._pairing_prompt_from_buffer(event)

        with self._lock:
            if self._compact:
                # Compact mode lives on one line — context chips ride
                # at the end so the row still parses even when piped
                # into ``grep`` for a handle / file / session id.
                tail = ""
                if preview:
                    flat = " ".join(preview.splitlines())
                    tail = f"  {escape(flat)}"
                if pending_prompt:
                    _, prev_txt = pending_prompt
                    tail = f"  [sf.muted]⤷ {prev_txt}[/]{tail}"
                ctx_compact = f"  {ctx_line}" if ctx_line else ""
                console.print(f"{head}{tail}{ctx_compact}")
                self._render_critiques(event, critiques)
                return
            console.print()
            console.print(head)
            if ctx_line:
                # One indented line below the badge with the muted
                # context chips. Always indented to the same column
                # as the body so a vertical scan groups header →
                # context → body cleanly.
                console.print(f"  {ctx_line}")
            if event.title and event.role == "user":
                console.print(f"  [sf.muted]title:[/] {_truncate(event.title, 200)}")
            if pending_prompt:
                _, prev_txt = pending_prompt
                console.print(
                    f"  [sf.muted]⤷ prompt ·[/] [sf.label]{prev_txt}[/]"
                )
            if preview:
                # Indent assistant / error bodies a bit further so
                # they read as a clear "reply" block underneath the
                # header.
                indent = "    " if event.role != "user" else "  "
                for line in preview.splitlines():
                    # Literal ``[...]`` in pasted logs / code must not be
                    # parsed as Rich markup (team watch often carries
                    # terminal scrollback with brackets and backticks).
                    console.print(
                        f"{indent}{line}", markup=False, highlight=False
                    )
            elif event.role == "assistant":
                # Empty-body assistant turn (broadcaster did not send
                # summary or text) — call it out so reviewers know
                # there *was* a reply, just not its content.
                console.print(
                    "    [sf.muted](assistant body not shared — broadcaster is "
                    "in summary-only mode)[/]"
                )
            self._render_critiques(event, critiques)

    # ── critic + session-pair plumbing ────────────────────────────

    def _render_critiques(
        self, event: IncomingEvent, critiques: list[Critique]
    ) -> None:
        """Print one indented suggestion per fired rule, plus the
        exact ``spec team flag`` command a reviewer would run if they
        agree with the critic. Single-quoted ``rule`` makes searching
        chat logs for a specific rule easy.

        When ``--notify`` was set on the watcher and *any* of the
        rules is ``block`` severity, we also ring the terminal bell
        and fire a best-effort macOS notification — for the case
        where the reviewer is not staring at the pane and a teammate
        just typed ``rm -rf`` or pasted a secret.
        """
        if not critiques:
            return
        block_hits: list[Critique] = []
        for c in critiques:
            console.print(
                f"  [{c.color}]{c.glyph} AUTO {c.rule:<16}[/] "
                f"[sf.muted]{c.msg}[/]"
            )
            console.print(
                f"     [sf.muted]→ {suggested_flag_command(event.id, c)}[/]"
            )
            if c.severity == SEV_HIGH:
                block_hits.append(c)
        if self._notify and block_hits:
            self._alert(event, block_hits)

    def _alert(
        self, event: IncomingEvent, hits: list[Critique]
    ) -> None:
        """Best-effort "look at the pane" alert for block-severity
        critic hits. Always rings the terminal bell (works in any
        terminal); on macOS we additionally fire ``osascript`` so the
        OS shows a banner.

        Failures are swallowed silently — alerting is a courtesy, not
        the contract of the watcher.
        """
        try:
            import sys

            # ``\a`` to stderr so it doesn't get caught by stdout
            # redirects piping the stream into a file.
            sys.stderr.write("\a")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            import shutil
            import subprocess

            osa = shutil.which("osascript")
            if not osa:
                return
            top = hits[0]
            title = f"Spec: block on {event.author_display}"
            # AppleScript quoting: escape double quotes and backslashes
            # inside the message so a stray quote in the critic text
            # doesn't break the call.
            msg = top.msg.replace("\\", "\\\\").replace('"', '\\"')
            sub_title = f"#{event.id} · {top.rule}"
            script = (
                f'display notification "{msg}" '
                f'with title "{title}" '
                f'subtitle "{sub_title}"'
            )
            subprocess.Popen(
                [osa, "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass

    def _remember_open_session(self, event: IncomingEvent) -> None:
        # Workspace stream includes your own USER rows; the no-reply hint
        # is written for teammates ("their watcher") and is noise on self.
        if self._viewer_handle and event.role == "user":
            ah = (event.author_handle or "").strip().lower()
            if ah and ah == self._viewer_handle:
                return
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

    def show_command_result(self, body: str, *, kind: str = "info") -> None:
        """Render the output of a slash-command (``/flag``, ``/summarize``,
        etc.) so it visually separates from streamed events. ``kind`` is
        one of ``info`` / ``ok`` / ``error`` / ``summarize``; each gets a
        distinct accent glyph so the eye can tell at a glance whether
        the watcher is acknowledging an action, raising an error, or
        emitting a large structured block for the agent.

        ``/summarize`` output, in particular, is meant to be read
        verbatim by the agent running in the terminal — we therefore
        render it with ``markup=False`` so any literal ``[…]`` text
        in a teammate's prompt does not get interpreted as a Rich
        tag and disappear from the agent's context.
        """
        glyph, color = {
            "ok": ("✓", "sf.mint"),
            "error": ("✗", "sf.reject"),
            "summarize": ("≡", "sf.point"),
            "info": ("·", "sf.point"),
        }.get(kind, ("·", "sf.point"))
        lines = body.splitlines() or [body]
        with self._lock:
            console.print()
            console.print(
                f"[{color}]{glyph}[/] [bold {color}]spec>[/] "
                f"{lines[0]}",
                markup=True,
                highlight=False,
            )
            for extra in lines[1:]:
                console.print(f"   {extra}", markup=False, highlight=False)

    def announce_connected(self, project_label: str) -> None:
        with self._lock:
            console.print(
                f"[sf.mint]●[/] connected · [sf.label]{project_label}[/] [sf.muted]· "
                f"streaming team prompts[/]"
            )

    def announce_connecting(self, detail: str) -> None:
        """Printed once before the SSE thread delivers live rows.

        Runs *after* the REST bootstrap replay so reviewers are not
        misled into thinking this was a mid-stream disconnect."""
        with self._lock:
            console.print(f"[sf.warn]…[/] connecting [sf.muted]({detail})[/]")

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

