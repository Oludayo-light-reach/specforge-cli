"""
``spec presence`` — read the local team-presence mirror.

Two subcommands:

* ``spec presence show`` — human-readable rollup of who's editing what.
  Walks ``.spec/team-presence.json`` and prints one teammate per
  block. Read-only; the daemon (`spec watch`) is what populates the
  file. Designed for "I'll glance at this in another terminal pane"
  rather than as a live dashboard — for live use, run ``spec watch``
  itself.

* ``spec presence check <path>`` — programmatic conflict probe. Exit
  code is the contract:
    - **0** → no teammate is currently editing the file.
    - **2** → a teammate is editing the file (warning printed).

  Tools (Claude Code ``PreToolUse`` hook, shell wrappers, CI gates)
  call this with the path they're about to modify and react to the
  exit code. ``--quiet`` suppresses stdout but keeps the exit code so
  scripts that just want the boolean don't have to swallow output.

The "no live data" case (``.spec/team-presence.json`` missing) is
treated as **fail open**: exit 0, print nothing. We never block edits
because the daemon isn't running.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ..config import BundleNotFoundError, find_bundle_root
from ..realtime.presence_mirror import read_team_presence
from ..ui import console, dim, fatal, info, ok, warn


@click.group("presence")
def presence_group() -> None:
    """File-level edit presence — who's editing what right now.

    Driven entirely by ``.spec/team-presence.json`` (kept fresh by
    ``spec watch``). When the daemon isn't running, the file is
    stale or missing — this group reports that honestly rather
    than guessing.
    """


@presence_group.command("show")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit machine-readable JSON instead of the rendered table.",
)
def presence_show_cmd(as_json: bool) -> None:
    """Print every teammate's current dirty-file list."""
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return
    body = read_team_presence(root)
    if body is None:
        if as_json:
            click.echo(json.dumps({"members": [], "self": None}))
            return
        warn("no live presence data — start `spec watch` to populate it.")
        return
    if as_json:
        click.echo(json.dumps(body, indent=2, sort_keys=True))
        return

    members = body.get("members") or []
    self_block = body.get("self")
    if self_block:
        files = self_block.get("files") or []
        handle = self_block.get("handle") or "(you)"
        branch = self_block.get("branch") or "?"
        if files:
            console.print(
                f"[sf.label]you[/] [sf.muted]@{handle} on {branch}[/] "
                f"({len(files)} dirty)"
            )
            for f in files:
                _print_file_line(f, prefix="  ")
        else:
            dim(f"you @{handle} on {branch}: working tree clean")
    if not members:
        info("")
        info("no other teammate is currently editing in this bundle.")
        return
    for m in members:
        files = m.get("files") or []
        handle = m.get("handle") or m.get("name") or "(unknown)"
        branch = m.get("branch") or "?"
        last_seen = m.get("last_seen") or ""
        console.print("")
        console.print(
            f"[sf.label]@{handle}[/] [sf.muted]on {branch} · last seen {last_seen}[/] "
            f"({len(files)} files)"
        )
        for f in files:
            _print_file_line(f, prefix="  ")


def _print_file_line(file_block: dict, *, prefix: str) -> None:
    path = file_block.get("path") or "?"
    added = int(file_block.get("lines_added") or 0)
    removed = int(file_block.get("lines_removed") or 0)
    untracked = bool(file_block.get("untracked") or False)
    suffix = " [sf.muted](new)[/]" if untracked else ""
    console.print(
        f"{prefix}[sf.path]{path}[/] [sf.muted](+{added}/-{removed})[/]{suffix}"
    )


@presence_group.command("check")
@click.argument("path", type=str)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Suppress stdout. Exit code is the contract.",
)
@click.option(
    "--include-self",
    is_flag=True,
    help=(
        "Also count overlap with your own currently-dirty files. Off "
        "by default — usually you don't want a hook warning *you* off "
        "your own file."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit machine-readable JSON instead of the rendered warning.",
)
def presence_check_cmd(
    path: str, quiet: bool, include_self: bool, as_json: bool
) -> None:
    """Check whether a teammate is currently editing PATH.

    Exit codes (the contract for hook callers):

    \b
      0  — clear, no teammate has the file dirty.
      2  — at least one teammate is editing the file.

    Path is resolved relative to the bundle root. Absolute paths are
    converted; paths outside the bundle exit 0 with a debug note —
    they're not Spec's business.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        # Hooks frequently run outside any bundle (Claude Code can be
        # invoked anywhere). Treat that as "not our problem" rather
        # than as an error so the hook is invisible there.
        if not quiet and not as_json:
            dim(f"not in a Spec bundle ({e}); skipping presence check.")
        if as_json:
            click.echo(json.dumps({"clear": True, "reason": "not_in_bundle"}))
        sys.exit(0)
    rel = _bundle_relative_path(path, root)
    if rel is None:
        if as_json:
            click.echo(json.dumps({"clear": True, "reason": "outside_bundle"}))
        sys.exit(0)

    body = read_team_presence(root)
    if body is None:
        if as_json:
            click.echo(json.dumps({"clear": True, "reason": "no_live_data"}))
        sys.exit(0)

    holders = _holders_for_path(body, rel, include_self=include_self)
    if not holders:
        if as_json:
            click.echo(json.dumps({"clear": True, "path": rel, "holders": []}))
        elif not quiet:
            ok(f"clear: no teammate is editing {rel}")
        sys.exit(0)

    if as_json:
        click.echo(json.dumps({"clear": False, "path": rel, "holders": holders}))
    elif not quiet:
        bullet_lines = []
        for h in holders:
            handle = h.get("handle") or h.get("name") or "(unknown)"
            added = int(h.get("lines_added") or 0)
            removed = int(h.get("lines_removed") or 0)
            untracked = " (new file)" if h.get("untracked") else ""
            bullet_lines.append(
                f"  · @{handle} (+{added}/-{removed}){untracked}"
            )
        warn(
            f"⚠ {rel} is currently being edited by {len(holders)} teammate(s):\n"
            + "\n".join(bullet_lines)
            + "\n  Pull / coordinate before making conflicting changes."
        )
    sys.exit(2)


def _bundle_relative_path(raw: str, bundle_root: Path) -> str | None:
    """Normalise ``raw`` to a bundle-relative path, or ``None`` when
    it falls outside the bundle. Hooks pass absolute paths; users
    typically pass bundle-relative ones; both should work."""
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        try:
            return str(p.resolve().relative_to(bundle_root.resolve()))
        except ValueError:
            return None
    candidate = (bundle_root / raw).resolve()
    try:
        return str(candidate.relative_to(bundle_root.resolve()))
    except ValueError:
        return None


def _holders_for_path(
    body: dict, rel_path: str, *, include_self: bool
) -> list[dict]:
    """Look up the inverted index in ``team-presence.json``. Returns
    every entry that holds ``rel_path``, optionally filtering out the
    local user."""
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
        if not include_self and bool(entry.get("self") or False):
            continue
        out.append(entry)
    return out
