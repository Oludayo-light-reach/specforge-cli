"""
Disk mirror of the live ``PresenceCache`` — ``.spec/team-presence.json``.

The whole point of this file is to give *every* AI tool a single,
boring, parseable place to read "who is editing what" without having
to know about Spec's wire protocol or hold an SSE connection. Three
known consumers today:

* The Claude Code ``PreToolUse`` hook (``spec hooks
  claude-pre-tool-use``) — runs in a fresh subprocess for every tool
  call, so it cannot hold any in-memory state and must read from disk.
* ``.cursor/rules/spec-team-presence.md`` — points Cursor at this file
  so the model can voluntarily check it before file edits.
* Future consumers (LSP, MCP server, dashboards) — same file, same
  shape.

The schema is intentionally tiny and stable. We treat
``.spec/team-presence.json`` as a public contract: any external tool
should be able to ``cat`` it, ``jq`` it, or read it from a Python
script without our help. Versioned via ``schema`` so future shape
changes are detectable without mystery.

Atomic writes (write-temp + rename) match the pattern in
``LiveCursor`` / ``Preferences`` so a kill mid-write can't leave a
half-written JSON file that crashes the hook.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .presence import LocalPresence, PeerPresence, PresenceCache

log = logging.getLogger(__name__)

TEAM_PRESENCE_FILENAME = "team-presence.json"
TEAM_PRESENCE_DIR = ".spec"
TEAM_PRESENCE_SCHEMA_VERSION = 1


def _team_presence_path(bundle_root: Path) -> Path:
    return bundle_root / TEAM_PRESENCE_DIR / TEAM_PRESENCE_FILENAME


class TeamPresenceMirror:
    """Writes ``.spec/team-presence.json`` whenever the cache changes.

    The mirror owns the file: ``spec watch`` is the only writer, and
    the file's contents are derived purely from the in-memory
    ``PresenceCache`` plus the local snapshot the watcher computed
    most recently. Everything else (hooks, rules, dashboards) is
    read-only.

    Debounced via the cache's ``apply_event`` return — the watcher
    only calls :meth:`write_if_dirty` when something actually
    changed, so we don't tax the SSD with rewrites every tick.
    """

    def __init__(self, bundle_root: Path) -> None:
        self._bundle_root = bundle_root.resolve()
        self._path = _team_presence_path(self._bundle_root)
        self._lock = threading.Lock()
        self._last_payload: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def write(
        self,
        cache: PresenceCache,
        *,
        local: LocalPresence | None,
        self_handle: str | None,
        self_name: str | None,
        branch: str | None,
    ) -> bool:
        """Render the cache + local presence into a single JSON file.

        Returns ``True`` if the file changed on disk, ``False`` if the
        contents matched what we wrote last time. Atomic on success;
        on failure logs and returns ``False`` without disturbing the
        existing file.
        """
        peers = cache.current()
        body = _render(
            peers=peers,
            local=local,
            self_handle=self_handle,
            self_name=self_name,
            branch=branch,
        )

        # Idempotency check: compare on everything *except* ``updated_at``
        # (which is a wall-clock timestamp regenerated on every render and
        # would otherwise force a rewrite even when nothing else moved).
        # Stable, sorted JSON serialisation so key ordering / whitespace
        # don't introduce false diffs.
        body_for_equality = {k: v for k, v in body.items() if k != "updated_at"}
        equality_key = json.dumps(body_for_equality, sort_keys=True)
        with self._lock:
            if equality_key == self._last_payload:
                return False
            encoded = json.dumps(body, indent=2, sort_keys=True)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp_fd, tmp_name = tempfile.mkstemp(
                    prefix=f"{TEAM_PRESENCE_FILENAME}.",
                    suffix=".tmp",
                    dir=str(self._path.parent),
                )
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                        f.write(encoded)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except OSError:
                            pass
                    os.replace(tmp_name, self._path)
                except OSError:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
            except OSError as e:
                log.info(
                    "spec-live: team-presence mirror write failed: %s", e
                )
                return False
            self._last_payload = equality_key
            return True


def _render(
    *,
    peers: list[PeerPresence],
    local: LocalPresence | None,
    self_handle: str | None,
    self_name: str | None,
    branch: str | None,
) -> dict:
    """Build the ``team-presence.json`` body.

    The shape is small on purpose. Top-level layout:

    .. code-block:: json

       {
         "schema": 1,
         "updated_at": "<iso>",
         "self": {"handle": "...", "name": "...", "branch": "...",
                  "files": [...], "head_commit": "..."},
         "members": [
            {"user_id": int, "handle": "...", "name": "...",
             "branch": "...", "files": [{path, lines_added,
             lines_removed, untracked}], "last_seen": "<iso>",
             "head_commit": "..."}
         ],
         "files_index": {
            "auth.py": [{"handle": "alice", "lines_added": 12,
                         "lines_removed": 3, "untracked": false,
                         "self": false}]
         }
       }

    ``files_index`` is the secret weapon for hooks: a hook gets a
    file path and needs the answer in O(1), not by walking every
    member. We pre-build the inverted index here so consumers don't
    have to reinvent it.
    """
    members_block = []
    files_index: dict[str, list[dict]] = {}

    for peer in peers:
        member = {
            "user_id": peer.user_id,
            "handle": peer.handle,
            "name": peer.name,
            "branch": peer.branch,
            "head_commit": peer.head_commit,
            "files": [
                {
                    "path": f.path,
                    "lines_added": int(f.lines_added),
                    "lines_removed": int(f.lines_removed),
                    "untracked": bool(f.untracked),
                }
                for f in peer.files
            ],
            "last_seen": _iso(peer.last_seen),
        }
        members_block.append(member)
        for f in peer.files:
            files_index.setdefault(f.path, []).append(
                {
                    "handle": peer.handle,
                    "name": peer.name,
                    "lines_added": int(f.lines_added),
                    "lines_removed": int(f.lines_removed),
                    "untracked": bool(f.untracked),
                    "self": False,
                }
            )

    self_block = None
    if local is not None:
        self_files = [
            {
                "path": f.path,
                "lines_added": int(f.lines_added),
                "lines_removed": int(f.lines_removed),
                "untracked": bool(f.untracked),
            }
            for f in local.files
        ]
        self_block = {
            "handle": self_handle,
            "name": self_name,
            "branch": branch,
            "head_commit": local.head_commit,
            "files": self_files,
        }
        # Include the local user in the inverted index too, marked
        # ``self: true`` so a hook can choose to ignore self-overlaps.
        for f in local.files:
            files_index.setdefault(f.path, []).append(
                {
                    "handle": self_handle,
                    "name": self_name,
                    "lines_added": int(f.lines_added),
                    "lines_removed": int(f.lines_removed),
                    "untracked": bool(f.untracked),
                    "self": True,
                }
            )

    # Sort each file's holders by "non-self first" then by handle so
    # the consumer can render the most relevant holder up top.
    for entries in files_index.values():
        entries.sort(key=lambda e: (e.get("self") or False, str(e.get("handle") or "")))

    return {
        "schema": TEAM_PRESENCE_SCHEMA_VERSION,
        "updated_at": _iso(datetime.now(timezone.utc)),
        "self": self_block,
        "members": members_block,
        "files_index": files_index,
    }


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def read_team_presence(bundle_root: Path) -> dict | None:
    """Read ``.spec/team-presence.json`` and return its parsed body,
    or ``None`` if the file is missing / malformed / outside the
    bundle. Callers (hooks, ``spec presence check``) treat absence
    as "no live data, fail open" rather than crashing."""
    path = _team_presence_path(bundle_root.resolve())
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


__all__ = [
    "TEAM_PRESENCE_DIR",
    "TEAM_PRESENCE_FILENAME",
    "TEAM_PRESENCE_SCHEMA_VERSION",
    "TeamPresenceMirror",
    "read_team_presence",
]
