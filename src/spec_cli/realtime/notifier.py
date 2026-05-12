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
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from ..ui import console
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


def _short_time(value: datetime | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    return value.astimezone().strftime("%H:%M:%S")


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


class Notifier:
    """Thread-safe printer for incoming Spec Live events.

    Rich's console serializes its own writes, so we don't need an
    explicit lock for ordering — but we do hold a lock around the
    multi-line composed output to keep events from interleaving when
    bursts arrive.
    """

    def __init__(self, *, compact: bool = False) -> None:
        self._compact = compact
        self._lock = threading.Lock()

    def show(self, event: IncomingEvent) -> None:
        time_label = _short_time(event.turn_at or event.received_at)
        author = event.author_display
        branch = event.branch or "-"
        source = event.source
        bundle = (
            f" [sf.muted]· {event.bundle_label}[/]"
            if event.bundle_label
            else ""
        )

        if event.role == "user":
            preview = (event.text or event.summary or "").strip()
            preview = _truncate(preview, 280 if not self._compact else 120)
            head = (
                f"[sf.point]{author}[/] [sf.muted]· {source} · {branch} · {time_label}[/]"
                f"{bundle}"
            )
        else:
            preview = (event.summary or event.text or "").strip()
            preview = _truncate(preview, 220 if not self._compact else 100)
            model = event.model or "assistant"
            head = (
                f"[sf.label]{author}[/] [sf.muted]· {model} · {branch} · {time_label}[/]"
                f"{bundle}"
            )

        with self._lock:
            if self._compact:
                line = f"{head}  {preview}" if preview else head
                console.print(line)
                return
            console.print()
            if event.role == "user":
                console.print(f"[sf.label]›[/] {head}")
            else:
                console.print(f"[sf.muted]‹[/] {head}")
            if event.title and event.role == "user":
                console.print(f"  [sf.muted]title:[/] {_truncate(event.title, 200)}")
            if preview:
                for line in preview.splitlines():
                    console.print(f"  {line}")
            if event.paths_touched:
                paths = ", ".join(event.paths_touched[:5])
                if len(event.paths_touched) > 5:
                    paths += f", +{len(event.paths_touched) - 5} more"
                console.print(f"  [sf.muted]paths:[/] {paths}")

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

