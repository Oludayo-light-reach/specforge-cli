"""
Rich-based output helpers.

The CLI stays quiet by default. Color is signal, not decoration:
  - mint  → a deliberate success (matches `site-button-mint` in the web UI)
  - red   → a reject (never a warning, always a hard no)
  - amber → a soft warning (the command still ran; read this)
  - dim   → metadata (paths, hashes, timestamps)
  - cyan  → pointer text (URLs, codes the user has to copy)
"""

from __future__ import annotations

import os
import sys

from rich.console import Console
from rich.theme import Theme

_theme = Theme(
    {
        "sf.mint": "bold #3ddab4",
        "sf.reject": "bold #ff5a6a",
        "sf.warn": "bold #f0b86e",
        "sf.muted": "dim #9aa3b2",
        "sf.point": "bold #7de3ff",
        "sf.label": "bold #c7c9d1",
    }
)

console = Console(theme=_theme, highlight=False, soft_wrap=False)
err_console = Console(theme=_theme, stderr=True, highlight=False, soft_wrap=False)


def configure_streaming_stdio() -> None:
    """Prefer line-buffered stdout/stderr for long-lived stream commands.

    When stdout is not a TTY (piped output, some IDE-integrated terminals),
    CPython uses block buffering. Rich output from ``spec watch`` and
    ``spec team watch`` can then look completely dead until ~8KiB fills
    or the process exits. Line buffering flushes after each line so
    banners, heartbeats, and events show up immediately.
    """
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    for stream in (sys.stdout, sys.stderr):
        try:
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(line_buffering=True, write_through=True)
        except (OSError, ValueError, TypeError, AttributeError):
            continue


def flush_streaming_output() -> None:
    """Best-effort flush after Rich prints in long-lived stream commands."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError, AttributeError):
            pass
    for con in (console, err_console):
        try:
            con.file.flush()
        except (OSError, ValueError, AttributeError):
            pass


def ok(msg: str) -> None:
    console.print(f"[sf.mint]✓[/] {msg}")


def info(msg: str) -> None:
    console.print(msg)


def dim(msg: str) -> None:
    console.print(f"[sf.muted]{msg}[/]")


def reject(msg: str) -> None:
    err_console.print(f"[sf.reject]✗[/] {msg}")


def warn(msg: str) -> None:
    """Soft warning — the command did something, the user should still read this."""
    err_console.print(f"[sf.warn]![/] {msg}")


def fatal(msg: str, code: int = 1) -> None:
    reject(msg)
    sys.exit(code)


def pointer(label: str, value: str) -> None:
    console.print(f"[sf.label]{label}[/] [sf.point]{value}[/]")
