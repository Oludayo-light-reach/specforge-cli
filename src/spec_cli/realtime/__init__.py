"""
Spec Live — real-time prompt sharing.

The CLI side of the live prompt feed. ``spec watch`` runs a small
in-process orchestrator that does two things concurrently:

* **Broadcast** — polls Cursor / Codex / Claude Code transcripts every
  few seconds, redacts each new turn, and posts it to Cloud's
  ``POST /api/projects/{id}/prompt-events`` endpoint.
* **Receive** — holds a long-lived SSE connection on
  ``GET /api/projects/{id}/prompt-stream`` and surfaces every teammate's
  turn in the user's terminal (and optionally on disk).

This package is the wiring; ``spec_cli.commands.watch`` is the user-
facing surface. See ``spec/PROMPT-LIVE-PLAN.md`` for the full design.
"""

from .events import IncomingEvent, OutgoingEvent, PresenceFile, PresencePayload
from .mirror import PeerMirror
from .notifier import Notifier
from .presence import (
    LocalPresence,
    PeerPresence,
    PresenceCache,
    compute_local_presence,
)
from .presence_mirror import (
    TEAM_PRESENCE_DIR,
    TEAM_PRESENCE_FILENAME,
    TeamPresenceMirror,
    read_team_presence,
)
from .tracker import LiveCursor
from .transport import HTTPPoster, SSEConsumer, SSEStreamError
from .watcher import WatcherOptions, run_watcher

__all__ = [
    "HTTPPoster",
    "IncomingEvent",
    "LiveCursor",
    "LocalPresence",
    "Notifier",
    "OutgoingEvent",
    "PeerMirror",
    "PeerPresence",
    "PresenceCache",
    "PresenceFile",
    "PresencePayload",
    "SSEConsumer",
    "SSEStreamError",
    "TEAM_PRESENCE_DIR",
    "TEAM_PRESENCE_FILENAME",
    "TeamPresenceMirror",
    "WatcherOptions",
    "compute_local_presence",
    "read_team_presence",
    "run_watcher",
]
