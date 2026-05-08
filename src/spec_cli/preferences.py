"""
Per-user, machine-local preferences for the Spec CLI.

Lives at ``~/.spec/preferences.json`` (or ``$SPEC_HOME/preferences.json``).
Distinct from credentials on purpose — credentials are *who you are*;
preferences are *how you want the CLI to behave on this machine*.

Shape (small, forward-compat — unknown keys are preserved verbatim):

    {
      "schema": 1,
      "prompt_stream": "default" | "muted"
    }

Why JSON, not YAML: the credentials file already uses JSON, so users
who poke at ``~/.spec/`` aren't suddenly switching format. Why a
separate file from credentials: signing in and out should not nuke
behavioural preferences (and credentials get rotated; preferences
don't).

The current keys:

* ``prompt_stream`` — ``"muted"`` silences Spec Live broadcasting on
  this machine even when the bundle's manifest opts in. ``"default"``
  defers to the manifest. The CLI never broadcasts when this is
  ``"muted"`` regardless of any ``spec.yaml`` setting; this is the
  individual-engineer kill-switch.

Atomic writes (write-temp + rename) and tolerant reads (missing or
malformed file = defaults). Same hygiene as ``LiveCursor`` so a kill
in flight can't corrupt the file.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PREFERENCES_FILENAME = "preferences.json"
PREFERENCES_SCHEMA_VERSION = 1


def _prefs_dir() -> Path:
    override = os.environ.get("SPEC_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".spec"


def _prefs_path() -> Path:
    return _prefs_dir() / PREFERENCES_FILENAME


@dataclass
class Preferences:
    """User-controlled CLI behaviour for this machine.

    Use :meth:`load` to read; mutate the dataclass; persist with
    :meth:`save`. ``raw`` carries any unknown keys forward so an older
    CLI doesn't silently drop a newer CLI's settings on round-trip.
    """

    prompt_stream: str = "default"  # "default" | "muted"
    raw: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.raw is None:
            self.raw = {}

    # ── reads ────────────────────────────────────────────────

    @property
    def prompt_stream_muted(self) -> bool:
        """Should Spec Live *broadcasting* be silenced on this machine?

        ``True`` overrides any per-bundle ``cloud.prompt_stream:
        enabled``. Receiving (incoming peer events) is unaffected —
        muting is the broadcasting kill-switch only.
        """
        return self.prompt_stream == "muted"

    # ── factories ────────────────────────────────────────────

    @classmethod
    def load(cls) -> "Preferences":
        path = _prefs_path()
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.info("spec-prefs: ignoring malformed prefs at %s: %s", path, e)
            return cls()
        if not isinstance(data, dict):
            return cls()
        prompt_stream_raw = data.get("prompt_stream")
        if prompt_stream_raw in ("muted", "default"):
            ps = prompt_stream_raw
        else:
            ps = "default"
        return cls(prompt_stream=ps, raw=data)

    # ── writes ────────────────────────────────────────────────

    def save(self) -> Path:
        path = _prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, stat.S_IRWXU)  # 0700
        except OSError:
            pass

        merged = dict(self.raw or {})
        merged["schema"] = PREFERENCES_SCHEMA_VERSION
        merged["prompt_stream"] = self.prompt_stream

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f"{PREFERENCES_FILENAME}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_name, path)
        except OSError as e:
            log.info("spec-prefs: save failed: %s", e)
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            return path
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass
        return path


def load_preferences() -> Preferences:
    """Module-level convenience — used by ``spec watch`` and the
    ``spec live`` command group so a single import covers the whole
    surface."""
    return Preferences.load()


__all__ = [
    "PREFERENCES_FILENAME",
    "PREFERENCES_SCHEMA_VERSION",
    "Preferences",
    "load_preferences",
]
