"""
``spec team`` — snapshot of recent team prompt activity.

A one-shot read of the most recent N prompt events for the bundle's
project, or (with ``--org``) across every bundle in your Spec workspace
via ``GET /api/me/prompt-events``. Same data the SSE stream serves,
just frozen in time.
"""
from __future__ import annotations

import re
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


# GitHub-style handle — server ``author_handle`` filter is exact match.
_HANDLE_STYLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,37}$")


def _event_matches_user_filter(ev: IncomingEvent, needle: str) -> bool:
    n = needle.lower().strip().lstrip("@")
    if not n:
        return True
    handle = (ev.author_handle or "").lower()
    name = (ev.author_name or "").lower()
    display = ev.author_display.lower()
    return n in handle or n in name or n in display


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
    help="Override `cloud.project` from spec.yaml (ignored with `--org`).",
)
@click.option(
    "--org",
    "org_wide",
    is_flag=True,
    help=(
        "Workspace-wide feed: all bundles you can see on Spec Cloud, "
        "one API round trip (`GET /api/me/prompt-events`). Does not "
        "require standing inside a bundle directory."
    ),
)
@click.option(
    "--user",
    "user_filter",
    default=None,
    help=(
        "Only events from this teammate — matches handle (substring), "
        "display name, or @handle (case-insensitive)."
    ),
)
def team_cmd(
    limit: int,
    branch_filter: str | None,
    role_filter: str | None,
    project: str | None,
    org_wide: bool,
    user_filter: str | None,
) -> None:
    """Print recent Spec Live prompt activity for this bundle or your whole workspace.

    Reads from ``/api/projects/{id}/prompt-events`` or, with ``--org``,
    ``/api/me/prompt-events``. Use ``--user alice`` to narrow the list to
    one teammate's turns (client-side filter, substring-friendly).

    \b
    Examples:
      spec team
      spec team --org --limit 50
      spec team --user alice
      spec team --org --user bob
    """
    creds = load_credentials()
    if not creds or not creds.access_token:
        fatal("Not signed in. Run `spec login` first.")
        return

    client = CloudClient(creds)
    label: str

    if org_wide:
        api_author = None
        if user_filter:
            cand = user_filter.strip().lstrip("@").lower()
            if cand and _HANDLE_STYLE.match(cand):
                api_author = cand
        try:
            rows = client.list_my_prompt_events(
                limit=limit,
                author_handle=api_author,
                role=role_filter,
                include_presence=False,
            )
        except ApiError as e:
            fatal(str(e))
            return
        label = "workspace (all your bundles)"
    else:
        try:
            root = find_bundle_root()
        except BundleNotFoundError as e:
            fatal(str(e))
            return
        manifest = load_manifest(root)
        raw = project or manifest.cloud_project
        if not raw:
            fatal(
                "No cloud project configured. Add `cloud.project: <handle>/<slug>` "
                "to spec.yaml, pass --project, or use --org for a workspace-wide feed."
            )
            return
        try:
            handle, slug = parse_cloud_project(raw, default_handle=creds.user_handle)
        except RemoteUrlError as e:
            fatal(str(e))
            return
        try:
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
        label = f"{handle}/{slug}"

    events = [IncomingEvent.from_json(r) for r in rows if isinstance(r, dict)]

    if branch_filter:
        needle = branch_filter.lower()
        events = [e for e in events if e.branch and needle in e.branch.lower()]
    if role_filter and not org_wide:
        # Org path already applied role server-side when set.
        events = [e for e in events if e.role == role_filter]
    if user_filter:
        events = [e for e in events if _event_matches_user_filter(e, user_filter)]

    if not events:
        dim(f"no recent activity for {label} — waiting for the team.")
        return

    scope = "[sf.label]team activity (org)[/]" if org_wide else "[sf.label]team activity[/]"
    console.print(
        f"{scope} [bold]{label}[/] [sf.muted]· {len(events)} event(s)[/]"
    )
    for event in events:
        when = _ago(event.turn_at or event.received_at)
        author = event.author_display
        branch = event.branch or "-"
        role_color = "sf.point" if event.role == "user" else "sf.label"
        bundle = ""
        if event.bundle_label:
            bundle = f" [sf.muted]· {event.bundle_label}[/]"
        head = (
            f"  [{role_color}]{event.role:<9}[/] "
            f"[sf.label]{author}[/] [sf.muted]· {branch} · {when} · {event.source}[/]"
            f"{bundle}"
        )
        console.print(head)
        text = (event.summary or event.text or "").strip()
        if text:
            short = text.splitlines()[0]
            if len(short) > 200:
                short = short[:200].rstrip() + "…"
            console.print(f"      [sf.muted]{short}[/]")
