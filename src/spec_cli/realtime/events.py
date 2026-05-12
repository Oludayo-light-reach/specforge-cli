"""
Wire shapes for Spec Live events.

Two dataclasses and the JSON (de)serialization that bridges them to the
server's ``PromptEventCreate`` / ``PromptEventOut`` Pydantic models.
The CLI never imports the server schemas directly (no shared package),
so everything that crosses the wire round-trips through these.

Keep these in lockstep with ``backend/app/schemas.py`` and
``PROMPT-LIVE-PLAN.md`` §4.1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).astimezone(timezone.utc)
    except ValueError:
        return None


@dataclass
class PresenceFile:
    """Per-file diff stat in a presence event.

    Wire-compatible with the server's ``PresenceFile`` Pydantic
    model — same field names, same JSON shape. Lives in both
    ``OutgoingEvent.presence`` and ``IncomingEvent.presence`` so
    every consumer (notifier, ``.spec/team-presence.json`` writer,
    Claude Code hook) reads the same dataclass.
    """

    path: str
    lines_added: int = 0
    lines_removed: int = 0
    untracked: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "lines_added": int(self.lines_added),
            "lines_removed": int(self.lines_removed),
            "untracked": bool(self.untracked),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "PresenceFile":
        return cls(
            path=str(payload.get("path") or ""),
            lines_added=int(payload.get("lines_added") or 0),
            lines_removed=int(payload.get("lines_removed") or 0),
            untracked=bool(payload.get("untracked") or False),
        )


@dataclass
class PresencePayload:
    """Full ``role = "presence"`` event body.

    Carries the structured "what is this teammate currently editing"
    data: the dirty file list and the head commit they're diffed
    from. ``is_clean`` is a convenience flag — sent when the working
    tree has no diffs at all so receivers can drop a teammate's
    presence row instead of waiting for the freshness window to
    expire.
    """

    files: list[PresenceFile] = field(default_factory=list)
    head_commit: str | None = None
    is_clean: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "files": [f.to_json() for f in self.files],
            "head_commit": self.head_commit,
            "is_clean": bool(self.is_clean),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any] | None) -> "PresencePayload | None":
        if not isinstance(payload, dict):
            return None
        files_raw = payload.get("files")
        files: list[PresenceFile] = []
        if isinstance(files_raw, list):
            for entry in files_raw:
                if isinstance(entry, dict):
                    files.append(PresenceFile.from_json(entry))
        return cls(
            files=files,
            head_commit=_str_or_none(payload.get("head_commit")),
            is_clean=bool(payload.get("is_clean") or False),
        )

    @property
    def total_lines_changed(self) -> int:
        return sum(f.lines_added + f.lines_removed for f in self.files)


@dataclass
class ToolCallPayload:
    """One agent-emitted tool invocation as shipped on the wire.

    Mirrors :class:`spec_cli.prompts.schema.ToolCall` but lives in
    ``realtime.events`` so the wire layer doesn't have to import the
    capture schema. ``name`` is the canonical spec tool name
    (``Read``, ``Edit``, ``Bash``, …) — adapter-specific identifiers
    are mapped before this dataclass is constructed so receivers can
    render every source uniformly.
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    status: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "args": dict(self.args or {})}
        if self.status is not None:
            out["status"] = self.status
        return out

    @classmethod
    def from_json(cls, payload: Any) -> "ToolCallPayload | None":
        if not isinstance(payload, dict):
            return None
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            return None
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        status = payload.get("status") if isinstance(payload.get("status"), str) else None
        return cls(name=name, args=dict(args), status=status)


@dataclass
class OutgoingEvent:
    """One observed turn we are about to broadcast.

    Mirrors ``PromptEventCreate`` on the server — the body of every
    ``POST /api/projects/{id}/prompt-events`` request. The client never
    sends an author block; the server stamps it from the bearer token.
    """

    session_id: str
    source: str
    role: str
    branch: str | None = None
    commit_sha: str | None = None
    model: str | None = None
    summary: str | None = None
    text: str | None = None
    title: str | None = None
    cwd: str | None = None
    paths_touched: list[str] = field(default_factory=list)
    presence: PresencePayload | None = None
    turn_at: datetime | None = None
    # Structured per-tool detail. Set on assistant turns for adapters
    # that surface tool invocations (Cursor agent runs, Claude Code,
    # Codex). Receivers expand these inline under the assistant body
    # when ``--show-tool-runs`` is on; the wire always carries them
    # so the toggle can be flipped without re-fetching history.
    tool_calls: list[ToolCallPayload] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source": self.source,
            "role": self.role,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "model": self.model,
            "summary": self.summary,
            "text": self.text,
            "title": self.title,
            "cwd": self.cwd,
            "paths_touched": list(self.paths_touched or []),
            "presence": self.presence.to_json() if self.presence else None,
            "turn_at": _isoformat(self.turn_at),
            "tool_calls": [c.to_json() for c in (self.tool_calls or [])],
        }


@dataclass
class IncomingFlag:
    """A teammate's flag (reaction / warning) on a prompt event.

    Carried over the SSE wire as ``event: flag`` frames and surfaced
    by the watcher's notifier next to the prompt the flag references.
    Mirrors the server's ``PromptEventFlagOut`` schema.
    """

    id: int
    prompt_event_id: int
    project_id: int
    kind: str
    note: str | None
    created_at: datetime
    author_user_id: int
    author_handle: str | None
    author_name: str
    author_avatar_url: str | None

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "IncomingFlag":
        author = payload.get("author") or {}
        if not isinstance(author, dict):
            author = {}
        return cls(
            id=int(payload["id"]),
            prompt_event_id=int(payload["prompt_event_id"]),
            project_id=int(payload["project_id"]),
            kind=str(payload.get("kind") or "warning"),
            note=_str_or_none(payload.get("note")),
            created_at=_parse_dt(payload.get("created_at"))
            or datetime.now(timezone.utc),
            author_user_id=int(author.get("user_id") or 0),
            author_handle=_str_or_none(author.get("handle")),
            author_name=str(author.get("name") or "(unknown)"),
            author_avatar_url=_str_or_none(author.get("avatar_url")),
        )

    @property
    def author_display(self) -> str:
        if self.author_handle:
            return f"@{self.author_handle}"
        return self.author_name


@dataclass
class IncomingEvent:
    """One event delivered by the server, via SSE or REST.

    Mirrors ``PromptEventOut``. ``author_*`` is flattened from the
    nested ``author`` block on the wire because the CLI doesn't use
    Pydantic — flat fields read more naturally from a dataclass.
    """

    id: int
    project_id: int
    session_id: str
    source: str
    role: str
    branch: str | None
    commit_sha: str | None
    model: str | None
    summary: str | None
    text: str | None
    title: str | None
    cwd: str | None
    paths_touched: list[str]
    turn_at: datetime | None
    received_at: datetime
    author_user_id: int
    author_handle: str | None
    author_name: str
    author_avatar_url: str | None
    presence: PresencePayload | None = None
    bundle_label: str | None = None
    tool_calls: list[ToolCallPayload] = field(default_factory=list)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "IncomingEvent":
        author = payload.get("author") or {}
        if not isinstance(author, dict):
            author = {}
        raw_tools = payload.get("tool_calls")
        tool_calls: list[ToolCallPayload] = []
        if isinstance(raw_tools, list):
            for entry in raw_tools:
                tc = ToolCallPayload.from_json(entry)
                if tc is not None:
                    tool_calls.append(tc)
        return cls(
            id=int(payload["id"]),
            project_id=int(payload["project_id"]),
            session_id=str(payload.get("session_id") or ""),
            source=str(payload.get("source") or ""),
            role=str(payload.get("role") or ""),
            branch=_str_or_none(payload.get("branch")),
            commit_sha=_str_or_none(payload.get("commit_sha")),
            model=_str_or_none(payload.get("model")),
            summary=_str_or_none(payload.get("summary")),
            text=_str_or_none(payload.get("text")),
            title=_str_or_none(payload.get("title")),
            cwd=_str_or_none(payload.get("cwd")),
            paths_touched=_str_list(payload.get("paths_touched")),
            turn_at=_parse_dt(payload.get("turn_at")),
            received_at=_parse_dt(payload.get("received_at"))
            or datetime.now(timezone.utc),
            author_user_id=int(author.get("user_id") or 0),
            author_handle=_str_or_none(author.get("handle")),
            author_name=str(author.get("name") or "(unknown)"),
            author_avatar_url=_str_or_none(author.get("avatar_url")),
            presence=PresencePayload.from_json(payload.get("presence")),
            bundle_label=_str_or_none(payload.get("bundle_label")),
            tool_calls=tool_calls,
        )

    @property
    def author_display(self) -> str:
        """Display name for terminal output. ``@handle`` if set, else
        the user's full name. Receivers using compact UIs prefer the
        handle; we fall back to the name so legacy users (no handle)
        still render something useful."""
        if self.author_handle:
            return f"@{self.author_handle}"
        return self.author_name


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value) or None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v]
