"""Strip terminal noise and disallowed controls from text that becomes TOML.

Claude Code log lines sometimes contain ANSI escape sequences (``\\x1b...``).
Those are fine in a terminal but break Python's ``tomllib`` when they appear
inside TOML **multiline literal** strings (``'''...'''``), which is how we
render long ``text`` fields. We normalize before render and at the capture
adapter so ``spec prompts submit`` can parse the file.
"""

from __future__ import annotations

import re

# CSI and common 2-byte sequences (sgr, cursor, etc.)
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[\][()#%][^\n\x1b]*|[@-Z\\-_])"
)
# OSC can end in BEL or ST
_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")
# Cursor Agent wraps the human prompt in XML-like tags in workspaceStorage.
_CURSOR_TIMESTAMP_RE = re.compile(
    r"<timestamp>\s*[\s\S]*?\s*</timestamp>\s*",
    re.IGNORECASE,
)
_CURSOR_USER_QUERY_RE = re.compile(
    r"<user_query>\s*([\s\S]*?)\s*</user_query>",
    re.IGNORECASE,
)

# Cursor Agent transcripts replace tool-heavy prose with this literal
# string while keeping structured ``tool_use`` blocks in the same row.
CURSOR_REDACTED_PLACEHOLDER = "[REDACTED]"


def is_cursor_redacted_placeholder(text: str | None) -> bool:
    """True when ``text`` is empty or only Cursor's redaction placeholder(s)."""
    if text is None:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    if stripped == CURSOR_REDACTED_PLACEHOLDER:
        return True
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    return bool(lines) and all(ln == CURSOR_REDACTED_PLACEHOLDER for ln in lines)


def prose_without_redacted_placeholders(text: str) -> str:
    """Drop placeholder-only lines; join the rest."""
    kept: list[str] = []
    for line in text.splitlines():
        if line.strip() and line.strip() != CURSOR_REDACTED_PLACEHOLDER:
            kept.append(line)
    return "\n".join(kept).strip()


def strip_ansi_escapes(s: str) -> str:
    """Remove ANSI/VT escape sequences; leave newlines and tabs intact."""
    s = _OSC_RE.sub("", s)
    s = _ANSI_ESCAPE_RE.sub("", s)
    return s.replace("\x1b", "")


def unwrap_cursor_user_message(text: str) -> str:
    """Return the human prompt from Cursor's Agent envelope markup.

    Cursor stores user bubbles as ``<timestamp>…</timestamp>`` plus
    ``<user_query>…</user_query>`` (and sometimes more system context).
    Spec Live should show and broadcast only what the teammate typed.
    """
    if not text or not text.strip():
        return text
    s = _CURSOR_TIMESTAMP_RE.sub("", text)
    parts = [
        m.strip()
        for m in _CURSOR_USER_QUERY_RE.findall(s)
        if isinstance(m, str) and m.strip()
    ]
    if parts:
        return "\n\n".join(parts)
    return s.strip()


def sanitize_for_toml_text(s: str) -> str:
    """Make string safe for our TOML emit + ``tomllib`` parse (multiline literals).

    Strips ANSI, then drops other C0 control characters except ``\\n``, ``\\t``,
    and ``\\r`` (so CRLF is preserved).
    """
    s = strip_ansi_escapes(s)
    out: list[str] = []
    for ch in s:
        o = ord(ch)
        if o < 0x20 and ch not in "\n\t\r":
            continue
        if o == 0x7F:  # DEL
            continue
        out.append(ch)
    return "".join(out)
