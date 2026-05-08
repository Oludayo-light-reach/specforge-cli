"""
``spec team`` — snapshot of recent team prompt activity.

A one-shot read of the most recent N prompt events for the bundle's
project. Same data the SSE stream serves, just frozen in time. Useful
for "I just sat down at my desk, what has my team been doing?" without
firing up the long-running ``spec watch`` daemon.
"""
from __future__ import annotations

from datetime import datetime, timezone

import click

from ..api import ApiError, CloudClient
from ..config import (
    BundleNotFoundError,
    RemoteUrlError,
    find_bundle_root,
    load_credentials,
    load_manifest,
    parse_cloud_project,
)
from ..realtime.events import IncomingEvent
from ..ui import console, dim, fatal


def _ago(value: datetime | None) -> str:
    if value is None:
        return "?"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = max(0, (datetime.now(timezone.utc) - value).total_seconds())
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86_400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86_400)}d ago"


@click.command("team")
@click.option(
    "--limit",
    "-n",
    "limit",
    default=20,
    show_default=True,
    type=click.IntRange(1, 200),
    help="Number of recent events to show.",
)
@click.option(
    "--branch",
    "branch_filter",
    default=None,
    help="Only show events on this branch. Substring match (case-insensitive).",
)
@click.option(
    "--role",
    "role_filter",
    type=click.Choice(["user", "assistant"]),
    default=None,
    help="Only show user or assistant turns.",
)
@click.option(
    "--project",
    "-p",
    default=None,
    help="Override `cloud.project` from spec.yaml.",
)
def team_cmd(
    limit: int,
    branch_filter: str | None,
    role_filter: str | None,
    project: str | None,
) -> None:
    """Print the most recent team prompt activity for this bundle.

    Reads from `/api/projects/{id}/prompt-events` (no SSE — one-shot).
    Honors the same auth and project-resolution rules as `spec watch`
    and `spec push`.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    manifest = load_manifest(root)
    creds = load_credentials()
    if not creds or not creds.access_token:
        fatal("Not signed in. Run `spec login` first.")
        return

    raw = project or manifest.cloud_project
    if not raw:
        fatal(
            "No cloud project configured. Add `cloud.project: <handle>/<slug>` "
            "to spec.yaml or pass --project <handle>/<slug>."
        )
        return
    try:
        handle, slug = parse_cloud_project(raw, default_handle=creds.user_handle)
    except RemoteUrlError as e:
        fatal(str(e))
        return

    try:
        client = CloudClient(creds)
        project_info = client.resolve_project(handle, slug)
    except ApiError as e:
        fatal(str(e))
        return
    project_id = int(project_info["id"])

    try:
        rows = client.list_prompt_events(project_id, limit=limit)
    except ApiError as e:
        fatal(str(e))
        return

    events = [IncomingEvent.from_json(r) for r in rows if isinstance(r, dict)]

    if branch_filter:
        needle = branch_filter.lower()
        events = [e for e in events if e.branch and needle in e.branch.lower()]
    if role_filter:
        events = [e for e in events if e.role == role_filter]

    label = f"{handle}/{slug}"
    if not events:
        dim(f"no recent activity on {label} — waiting for the team.")
        return

    console.print(
        f"[sf.label]team activity[/] [bold]{label}[/] "
        f"[sf.muted]· last {len(events)} event(s)[/]"
    )
    for event in events:
        when = _ago(event.turn_at or event.received_at)
        author = event.author_display
        branch = event.branch or "-"
        role_color = "sf.point" if event.role == "user" else "sf.label"
        head = (
            f"  [{role_color}]{event.role:<9}[/] "
            f"[sf.label]{author}[/] [sf.muted]· {branch} · {when} · {event.source}[/]"
        )
        console.print(head)
        text = (event.summary or event.text or "").strip()
        if text:
            short = text.splitlines()[0]
            if len(short) > 200:
                short = short[:200].rstrip() + "…"
            console.print(f"      [sf.muted]{short}[/]")
