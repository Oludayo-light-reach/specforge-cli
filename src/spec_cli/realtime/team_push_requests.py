"""Teammate git-push handoff requests — ``.spec/team-push-requests.yaml``.

Humans run ``spec team request-push <handle>`` (or ``/push@handle`` inside
``spec team watch``) to record that they want a teammate's latest commits
on the remote *now*. The YAML is intentionally loud for AI tools; the
watcher merges the active rows into ``team-presence.json`` and
``team-editing-brief.md`` on every mirror tick so agents that already read
those files cannot miss the signal.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

TEAM_PUSH_REQUESTS_FILENAME = "team-push-requests.yaml"
TEAM_PUSH_REQUESTS_SCHEMA = 1
# Default window a request stays visible before it auto-expires.
DEFAULT_PUSH_REQUEST_TTL_SECS = 30 * 60
_MAX_REQUESTS = 50

# GitHub-style handle — aligned with ``spec team`` author filters.
_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,37}$")


def team_push_requests_path(bundle_root: Path) -> Path:
    return bundle_root.resolve() / ".spec" / TEAM_PUSH_REQUESTS_FILENAME


def normalize_target_handle(raw: str) -> str:
    """Lowercase handle without ``@``. Raises ``ValueError`` if invalid."""
    h = (raw or "").strip().lstrip("@").lower()
    if not h or not _HANDLE_RE.match(h):
        raise ValueError(
            "handle must match GitHub-style rules: "
            "start with a letter or digit, then letters, digits, or hyphens "
            "(max 38 chars)."
        )
    return h


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


def _load_raw(bundle_root: Path) -> dict[str, Any]:
    path = team_push_requests_path(bundle_root)
    if not path.is_file():
        return {"schema": TEAM_PUSH_REQUESTS_SCHEMA, "requests": []}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeError):
        return {"schema": TEAM_PUSH_REQUESTS_SCHEMA, "requests": []}
    if not isinstance(data, dict):
        return {"schema": TEAM_PUSH_REQUESTS_SCHEMA, "requests": []}
    reqs = data.get("requests")
    if not isinstance(reqs, list):
        reqs = []
    out = dict(data)
    out["requests"] = reqs
    return out


def _atomic_write_yaml(path: Path, payload: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(
            payload,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f"{TEAM_PUSH_REQUESTS_FILENAME}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(
                    "# Spec Live — git push handoff requests (auto-maintained).\n"
                    "# If your Spec handle appears under `to_handle`, push your branch\n"
                    "# to origin so teammates can `git pull`. This file is merged into\n"
                    "# `.spec/team-presence.json` and `.spec/team-editing-brief.md` by\n"
                    "# `spec watch`.\n\n"
                )
                f.write(text)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_name, path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as e:
        log.info("spec-live: team-push-requests.yaml write failed: %s", e)
        return False
    return True


def _normalize_request_row(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    to_h = entry.get("to_handle")
    if not isinstance(to_h, str) or not _HANDLE_RE.match(to_h.strip().lstrip("@").lower()):
        return None
    to_h = to_h.strip().lstrip("@").lower()
    exp = _parse_iso(entry.get("expires_at"))
    if exp is None:
        return None
    return {
        "id": str(entry.get("id") or ""),
        "to_handle": to_h,
        "from_handle": entry.get("from_handle"),
        "from_display": entry.get("from_display"),
        "branch": entry.get("branch"),
        "message": entry.get("message"),
        "requested_at": entry.get("requested_at"),
        "expires_at": entry.get("expires_at"),
    }


def list_active_push_requests(bundle_root: Path) -> list[dict[str, Any]]:
    """Return non-expired requests as JSON-friendly dicts.

    Drops expired rows from disk when the on-disk list shrinks so the
    YAML file cannot grow without bound.
    """
    bundle_root = bundle_root.resolve()
    raw = _load_raw(bundle_root)
    reqs_in = raw.get("requests") or []
    if not isinstance(reqs_in, list):
        reqs_in = []

    now = datetime.now(timezone.utc)
    kept: list[dict[str, Any]] = []
    for entry in reqs_in:
        row = _normalize_request_row(entry)
        if row is None:
            continue
        exp = _parse_iso(row.get("expires_at"))
        if exp is None or exp <= now:
            continue
        kept.append(row)

    # Compact if we dropped anything or fixed garbage rows.
    if len(kept) != len(reqs_in):
        raw_out = {
            "schema": TEAM_PUSH_REQUESTS_SCHEMA,
            "updated_at": _iso(now),
            "requests": kept,
        }
        _atomic_write_yaml(team_push_requests_path(bundle_root), raw_out)

    # Strip internal id from mirror copies optional — keep for dedupe.
    return [
        {
            "to_handle": r["to_handle"],
            "from_handle": r.get("from_handle"),
            "from_display": r.get("from_display"),
            "branch": r.get("branch"),
            "message": r.get("message"),
            "requested_at": r.get("requested_at"),
            "expires_at": r.get("expires_at"),
        }
        for r in kept
    ]


def record_push_request(
    bundle_root: Path,
    *,
    to_handle: str,
    from_handle: str | None,
    from_display: str | None,
    branch: str | None,
    message: str | None = None,
    ttl_secs: int = DEFAULT_PUSH_REQUEST_TTL_SECS,
) -> Path:
    """Append one request (prune expired + cap length). Returns yaml path.

    Raises ``ValueError`` for a bad target handle."""
    target = normalize_target_handle(to_handle)
    bundle_root = bundle_root.resolve()
    path = team_push_requests_path(bundle_root)

    now = datetime.now(timezone.utc)
    ttl = max(60, min(int(ttl_secs), 24 * 3600))
    expires = now + timedelta(seconds=ttl)

    raw = _load_raw(bundle_root)
    reqs_in = raw.get("requests") or []
    if not isinstance(reqs_in, list):
        reqs_in = []

    kept: list[dict[str, Any]] = []
    for entry in reqs_in:
        row = _normalize_request_row(entry)
        if row is None:
            continue
        exp = _parse_iso(row.get("expires_at"))
        if exp is None or exp <= now:
            continue
        kept.append(
            {
                "id": row["id"] or str(uuid.uuid4()),
                "to_handle": row["to_handle"],
                "from_handle": row.get("from_handle"),
                "from_display": row.get("from_display"),
                "branch": row.get("branch"),
                "message": row.get("message"),
                "requested_at": row.get("requested_at"),
                "expires_at": row.get("expires_at"),
            }
        )

    fh = (from_handle or "").strip().lstrip("@").lower() or None
    if fh and not _HANDLE_RE.match(fh):
        fh = None
    disp = (from_display or "").strip() or None
    if disp is None and fh:
        disp = f"@{fh}"
    msg = (message or "").strip() or None

    new_row = {
        "id": str(uuid.uuid4()),
        "to_handle": target,
        "from_handle": fh,
        "from_display": disp,
        "branch": (branch or "").strip() or None,
        "message": msg,
        "requested_at": _iso(now),
        "expires_at": _iso(expires),
    }
    kept.append(new_row)
    # Oldest first drop when over cap (FIFO).
    while len(kept) > _MAX_REQUESTS:
        kept.pop(0)

    out = {
        "schema": TEAM_PUSH_REQUESTS_SCHEMA,
        "updated_at": _iso(now),
        "requests": kept,
    }
    if not _atomic_write_yaml(path, out):
        raise OSError(f"could not write {path}")
    return path


def attach_push_requests_to_body(bundle_root: Path, body: dict[str, Any]) -> None:
    """Mutate ``body`` in place: set or clear ``push_requests`` from YAML."""
    reqs = list_active_push_requests(bundle_root)
    if reqs:
        body["push_requests"] = reqs
    else:
        body.pop("push_requests", None)


__all__ = [
    "DEFAULT_PUSH_REQUEST_TTL_SECS",
    "TEAM_PUSH_REQUESTS_FILENAME",
    "attach_push_requests_to_body",
    "list_active_push_requests",
    "normalize_target_handle",
    "record_push_request",
    "team_push_requests_path",
]
