"""Stable per-bundle id for Spec Live broadcast echo suppression.

``spec watch`` filters SSE replays of the *local* install's POSTs. The
id must survive process restarts so reconnect replays still classify
correctly, and must differ across **physical machines** even when the
same git checkout (and ``.spec/``) is synced via iCloud, Dropbox, or
similar — a client id stored *inside* the bundle would be identical on
every clone and incorrectly suppress the other laptop's feed.

The id is therefore stored under the user's home directory
(``~/.spec/broadcast-client-ids/``), keyed by a hash of the bundle's
resolved path. Legacy ``.spec/live-broadcast-client-id`` in the bundle
is no longer read or written so synced copies cannot collide.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path


def _bundle_identity_key(bundle_root: Path) -> str:
    """Stable SHA-256 hex digest for ``bundle_root`` (resolved when possible)."""
    root = bundle_root.expanduser()
    try:
        resolved = root.resolve()
    except OSError:
        resolved = root
    raw = str(resolved).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _machine_client_id_path(bundle_root: Path) -> Path:
    return (
        Path.home()
        / ".spec"
        / "broadcast-client-ids"
        / f"{_bundle_identity_key(bundle_root)}.txt"
    )


def load_or_create_broadcast_client_id(bundle_root: Path) -> str:
    """Return a stable UUID string for this (machine, bundle directory) pair.

    Persists under ``~/.spec/broadcast-client-ids/<sha256>.txt``. Creates
    parent dirs as needed. Not tied to the repo so cloud-synced working
    trees do not share an id across laptops.
    """
    path = _machine_client_id_path(bundle_root)
    try:
        if path.is_file():
            raw = path.read_text(encoding="utf-8").strip()
            if len(raw) >= 8:
                return raw[:128]
    except OSError:
        pass
    token = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
    except OSError:
        # No home dir or read-only — still return a process token so POSTs
        # work; echo filtering may not match across restarts.
        return token
    return token


__all__ = ["load_or_create_broadcast_client_id"]
