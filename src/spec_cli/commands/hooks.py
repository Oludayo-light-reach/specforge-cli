"""
``spec hooks …`` — installable scripts that bridge Spec Live to AI IDEs.

Each command in this group is designed to be wired into another tool's
hook configuration and run in a fresh subprocess for every invocation.
That means:

* No assumptions about working directory — the AI IDE invokes them
  from whatever cwd it has.
* No long-lived in-memory state — everything we need to know lives in
  ``.spec/team-presence.json``.
* Failures are silent on stderr by default — a misbehaving hook should
  never block the user's work. The contract for blocking is the exit
  code; output is decoration.

The two surfaces that exist today:

* ``spec hooks claude-pre-tool-use`` — Claude Code ``PreToolUse``
  hook. Reads stdin (Claude's hook protocol), parses out the file
  path being edited, and warns when a teammate is currently editing
  it. Exit 0 by default (warn-only); ``--block`` exits non-zero so
  Claude refuses to proceed without an explicit override.

* ``spec hooks install-claude`` — write the per-bundle ``.claude/
  settings.json`` so the above is wired into Claude Code without the
  user touching JSON. Idempotent: re-running updates the same block.

The Cursor / Codex / generic-LSP integrations are *not* in this group
because they don't take stdin from the AI IDE — Cursor reads
``.cursor/rules/spec-team-presence.md`` directly (provisioned by
``spec init``), and AGENTS.md tells any model-driven agent to invoke
``spec presence check`` voluntarily. See
``PROMPT-LIVE-PLAN.md`` §5 for the matrix.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ..config import BundleNotFoundError, find_bundle_root
from ..realtime.presence_mirror import read_team_presence
from ..ui import dim


CLAUDE_HOOK_VERSION = 1
CLAUDE_SETTINGS_DIR = ".claude"
CLAUDE_SETTINGS_FILENAME = "settings.json"


@click.group("hooks")
def hooks_group() -> None:
    """Spec Live hooks for AI IDEs.

    \b
    Subcommands:
      spec hooks claude-pre-tool-use   — stdin-driven Claude Code hook
      spec hooks install-claude        — wire the hook into .claude/settings.json
    """


# ── Claude Code PreToolUse hook ───────────────────────────────────────


@hooks_group.command("claude-pre-tool-use")
@click.option(
    "--block",
    "block_mode",
    is_flag=True,
    help=(
        "Exit non-zero (refusing the tool call) when a teammate is "
        "editing the target file. Default behaviour is warn-only "
        "(exit 0 with a stderr message) — friendlier for first-time "
        "users; opt in here if your team wants firm coordination."
    ),
)
def claude_pre_tool_use_cmd(block_mode: bool) -> None:
    """Claude Code ``PreToolUse`` hook entry point.

    Reads Claude's hook payload from stdin. When the tool being
    invoked targets a file a teammate is currently editing, prints a
    warning to stderr (Claude's UI surfaces stderr to the user) and
    optionally exits non-zero to block the call.

    The hook is intentionally tolerant: any parse failure, missing
    presence file, missing bundle, or unrelated tool name is a no-op
    (exit 0, silent). We never want this hook to be the reason an
    edit fails — that path is the user explicitly opting in via
    ``--block``.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        # No input → nothing to check. Don't block.
        sys.exit(0)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        # Malformed input → fail open. Better to let an edit through
        # than to block on a Claude Code shape change we don't grok.
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)

    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    file_paths = _extract_file_paths(tool_name, tool_input)
    if not file_paths:
        sys.exit(0)

    # Find a bundle to consult. Prefer the cwd Claude is running from
    # (matches the user's intuition for "the project being edited"),
    # and fall back to the directory of the first edited file.
    bundle_root = _find_bundle_for_paths(file_paths)
    if bundle_root is None:
        sys.exit(0)
    body = read_team_presence(bundle_root)
    if body is None:
        sys.exit(0)

    conflicts: list[tuple[str, list[dict]]] = []
    for abs_path in file_paths:
        rel = _bundle_relative(abs_path, bundle_root)
        if rel is None:
            continue
        holders = _holders_for_path(body, rel)
        if holders:
            conflicts.append((rel, holders))

    if not conflicts:
        sys.exit(0)

    _emit_conflict_warning(tool_name, conflicts)
    if block_mode:
        # Non-zero exit blocks the tool call in Claude Code.
        sys.exit(2)
    sys.exit(0)


def _extract_file_paths(tool_name: str, tool_input: dict) -> list[str]:
    """Pull every file-targeting argument out of a Claude tool call.

    Covers the tool names Claude Code uses for filesystem mutation
    today: ``Edit``, ``MultiEdit``, ``Write``, ``NotebookEdit``,
    ``StrReplace``, ``Delete``. Everything else (Bash, Grep, Read,
    web fetch, …) returns an empty list — we only care about
    edit-class tools, since reads and shell commands aren't where
    presence conflicts hurt.
    """
    edit_tools = {
        "Edit",
        "MultiEdit",
        "StrReplace",
        "Write",
        "NotebookEdit",
        "Delete",
    }
    if tool_name not in edit_tools:
        return []
    out: list[str] = []
    # Common: ``file_path`` (Edit, Write, Delete, MultiEdit).
    fp = tool_input.get("file_path")
    if isinstance(fp, str) and fp:
        out.append(fp)
    # NotebookEdit uses ``notebook_path``.
    np = tool_input.get("notebook_path")
    if isinstance(np, str) and np:
        out.append(np)
    # MultiEdit also accepts a list under ``edits``; rarely a file
    # list, but defensive.
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for e in edits:
            if isinstance(e, dict):
                p = e.get("file_path")
                if isinstance(p, str) and p:
                    out.append(p)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    dedup: list[str] = []
    for p in out:
        if p in seen:
            continue
        seen.add(p)
        dedup.append(p)
    return dedup


def _find_bundle_for_paths(paths: list[str]) -> Path | None:
    """Walk up from each path looking for a Spec bundle root.

    Identical algorithm to ``find_bundle_root`` but seeded at the
    file's directory rather than ``Path.cwd()`` — Claude Code may
    invoke the hook from a parent directory, so we have to start
    the search from where the edits actually live.
    """
    from ..constants import MANIFEST_FILENAME

    candidates: list[Path] = []
    for p in paths:
        try:
            start = Path(p).resolve()
        except OSError:
            continue
        if start.is_file():
            start = start.parent
        elif not start.exists():
            start = start.parent
        if start.is_dir():
            candidates.append(start)
    candidates.append(Path.cwd())

    for start in candidates:
        cur = start.resolve()
        for _ in range(64):  # bounded ascent — symlink-loop guard
            if (cur / MANIFEST_FILENAME).is_file():
                return cur
            parent = cur.parent
            if parent == cur:
                break
            cur = parent
    return None


def _bundle_relative(abs_path: str, bundle_root: Path) -> str | None:
    try:
        p = Path(abs_path).resolve()
    except OSError:
        return None
    try:
        return str(p.relative_to(bundle_root.resolve()))
    except ValueError:
        return None


def _holders_for_path(body: dict, rel_path: str) -> list[dict]:
    files_index = body.get("files_index")
    if not isinstance(files_index, dict):
        return []
    raw = files_index.get(rel_path)
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("self") is True:
            continue  # never warn the user about their own edits
        out.append(entry)
    return out


def _emit_conflict_warning(
    tool_name: str, conflicts: list[tuple[str, list[dict]]]
) -> None:
    """Write the user-visible warning to stderr.

    Claude Code shows hook stderr inline with its tool-call output,
    which is exactly the surface we want — the user sees the warning
    next to the edit it's about to perform without us having to
    inject anything into the conversation.
    """
    lines: list[str] = []
    if len(conflicts) > 1:
        lines.append(
            f"⚠ Spec Live: {len(conflicts)} files have teammates editing them"
        )
    else:
        lines.append(
            "⚠ Spec Live: 1 file has a teammate currently editing it"
        )
    for rel, holders in conflicts:
        lines.append(f"  {rel}")
        for h in holders[:3]:
            handle = h.get("handle") or h.get("name") or "(unknown)"
            added = int(h.get("lines_added") or 0)
            removed = int(h.get("lines_removed") or 0)
            untracked = " (new file)" if h.get("untracked") else ""
            lines.append(
                f"    · @{handle} (+{added}/-{removed}){untracked}"
            )
        if len(holders) > 3:
            lines.append(f"    · …and {len(holders) - 3} more")
    lines.append(
        "  → consider `git pull` first or coordinate before overwriting."
    )
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()


# ── Claude settings install ──────────────────────────────────────────


@hooks_group.command("install-claude")
@click.option(
    "--block",
    "block_mode",
    is_flag=True,
    help="Configure the hook in --block mode (refuses tool calls on conflict).",
)
def install_claude_cmd(block_mode: bool) -> None:
    """Write/refresh ``.claude/settings.json`` so Claude Code in this
    bundle runs the Spec Live PreToolUse hook on every edit.

    Idempotent: re-running updates the Spec-managed entry in place
    without touching unrelated settings the user added by hand.
    Removing the file (or the Spec-managed entry inside it) opts out.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    install_claude_settings(root, block_mode=block_mode)
    dim(f".claude/settings.json updated ({'block' if block_mode else 'warn'} mode)")


def install_claude_settings(bundle_root: Path, *, block_mode: bool) -> Path:
    """Programmatic variant of ``install-claude`` — used by ``spec
    init`` to wire the hook on first scaffold without spawning the
    CLI again. Returns the settings path.

    Schema written:

    .. code-block:: json

       {
         "hooks": {
           "PreToolUse": [
             {
               "matcher": "Edit|MultiEdit|Write|NotebookEdit",
               "hooks": [
                 {
                   "type": "command",
                   "command": "spec hooks claude-pre-tool-use",
                   "spec_managed": true,
                   "spec_version": 1
                 }
               ]
             }
           ]
         }
       }

    The ``spec_managed`` / ``spec_version`` markers are how we identify
    the entry on subsequent runs — anything else under ``hooks`` is
    left alone. Older versions get replaced; entries from Spec are
    deduplicated.
    """
    settings_dir = bundle_root / CLAUDE_SETTINGS_DIR
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / CLAUDE_SETTINGS_FILENAME

    existing: dict = {}
    if settings_path.is_file():
        try:
            parsed = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                existing = parsed
        except (OSError, ValueError):
            existing = {}

    hooks_section = existing.get("hooks")
    if not isinstance(hooks_section, dict):
        hooks_section = {}
        existing["hooks"] = hooks_section

    pre_tool_use = hooks_section.get("PreToolUse")
    if not isinstance(pre_tool_use, list):
        pre_tool_use = []
        hooks_section["PreToolUse"] = pre_tool_use

    command = "spec hooks claude-pre-tool-use"
    if block_mode:
        command += " --block"

    spec_block = {
        "matcher": "Edit|MultiEdit|Write|NotebookEdit",
        "hooks": [
            {
                "type": "command",
                "command": command,
                "spec_managed": True,
                "spec_version": CLAUDE_HOOK_VERSION,
            }
        ],
    }

    # Replace any existing Spec-managed PreToolUse entry; leave the
    # rest. We identify our own by walking each ``matcher`` block's
    # ``hooks`` list for the marker.
    pruned: list = []
    for entry in pre_tool_use:
        if not isinstance(entry, dict):
            pruned.append(entry)
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, list):
            pruned.append(entry)
            continue
        is_spec = any(
            isinstance(h, dict) and h.get("spec_managed") is True for h in inner
        )
        if not is_spec:
            pruned.append(entry)
    pruned.append(spec_block)
    hooks_section["PreToolUse"] = pruned

    settings_path.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return settings_path
