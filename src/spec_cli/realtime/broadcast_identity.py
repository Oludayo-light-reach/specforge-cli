"""Stable per-bundle id for Spec Live broadcast echo suppression.

``spec watch`` filters SSE replays of the *local* install's POSTs. The
id must survive process restarts (same path as ``LiveCursor``) so
reconnect replays still classify correctly, and must differ across
machines so the same account's other computers are not mistaken for
echoes.
"""
from __future__ import annotations

import uuid
from pathlib import Path

_CLIENT_ID_FILENAME = "live-broadcast-client-id"


def load_or_create_broadcast_client_id(bundle_root: Path) -> str:
    """Return a stable UUID string for this bundle directory.

    Persists under ``.spec/live-broadcast-client-id`` (gitignored with
    other machine-local Spec state). Creates parent dirs as needed.
    """
    spec_dir = bundle_root / ".spec"
    path = spec_dir / _CLIENT_ID_FILENAME
    try:
        if path.is_file():
            raw = path.read_text(encoding="utf-8").strip()
            if len(raw) >= 8:
                return raw[:128]
    except OSError:
        pass
    token = str(uuid.uuid4())
    try:
        spec_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
    except OSError:
        # Ephemeral bundle — still return a process token so POSTs work;
        # echo filtering may not match across restarts.
        return token
    return token


__all__ = ["load_or_create_broadcast_client_id"]
