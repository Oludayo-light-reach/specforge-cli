"""``spec locks`` — coordination checks using the team-presence mirror.

``spec locks check`` matches ``spec presence check`` exit codes (0 clear,
2 conflict) but **ignores a stale** ``.spec/team-presence.json`` (when
``updated_at`` is older than a few minutes, ``spec watch`` is probably
not running — we fail open instead of trusting zombie data).

``.spec/team-editing-brief.md`` is a plain-language sibling file updated
with the JSON; agents can read it directly.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from ..config import BundleNotFoundError, find_bundle_root
from ..realtime.presence_mirror import read_team_presence
from ..realtime.team_editing_brief import (
    DEFAULT_LOCKS_MIRROR_STALE_SECS,
    TEAM_EDITING_BRIEF_FILENAME,
    team_presence_mirror_stale,
)
from ..ui import console, dim, fatal, ok, warn


def _locks_max_mirror_age_secs() -> float:
    raw = os.environ.get("SPEC_LOCKS_MAX_MIRROR_AGE_SECS", "").strip()
    if not raw:
        return DEFAULT_LOCKS_MIRROR_STALE_SECS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_LOCKS_MIRROR_STALE_SECS


@click.group("locks")
def locks_group() -> None:
    """Edit coordination using the Spec Live presence mirror."""


@locks_group.command("check")
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
    help="Also treat overlap with your own dirty files as a conflict.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit machine-readable JSON instead of a rendered warning.",
)
def locks_check_cmd(path: str, quiet: bool, include_self: bool, as_json: bool) -> None:
    """Like ``spec presence check``, but ignores a stale presence mirror.

    Exit **0** when the mirror is missing, you're outside a bundle, or
    ``updated_at`` is older than ``SPEC_LOCKS_MAX_MIRROR_AGE_SECS``
    (default: same as ``DEFAULT_LOCKS_MIRROR_STALE_SECS`` — 15 minutes).

    Exit **2** when at least one teammate (non-self) has the path dirty
    and the mirror is fresh.
    """
    max_age = _locks_max_mirror_age_secs()
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        if not quiet and not as_json:
            dim(f"not in a Spec bundle ({e}); skipping locks check.")
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

    if team_presence_mirror_stale(body, max_age_secs=max_age):
        if as_json:
            click.echo(json.dumps({"clear": True, "reason": "stale_mirror"}))
        elif not quiet:
            dim(
                "locks: team-presence mirror is stale or undated — "
                "treating as clear (start `spec watch` for live data)."
            )
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
            f"⚠ {rel} — teammate(s) may be editing (fresh mirror):\n"
            + "\n".join(bullet_lines)
            + "\n  Pull / coordinate before making conflicting changes."
        )
    sys.exit(2)


@locks_group.command("brief-path")
def locks_brief_path_cmd() -> None:
    """Print the absolute path to ``.spec/team-editing-brief.md``."""
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return
    p = (root / ".spec" / TEAM_EDITING_BRIEF_FILENAME).resolve()
    click.echo(str(p))


@locks_group.command("show-brief")
def locks_show_brief_cmd() -> None:
    """Display ``.spec/team-editing-brief.md`` when it exists."""
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return
    p = root / ".spec" / TEAM_EDITING_BRIEF_FILENAME
    if not p.is_file():
        dim(f"no {TEAM_EDITING_BRIEF_FILENAME} yet — run `spec watch`.")
        return
    console.print(p.read_text(encoding="utf-8"))


def _bundle_relative_path(raw: str, bundle_root: Path) -> str | None:
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
