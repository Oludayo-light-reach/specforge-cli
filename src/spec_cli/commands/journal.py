"""``spec journal`` — materialize team prompt activity as markdown files."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

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
from ..ui import console, dim, fatal, ok, warn


def _event_timestamp(ev: IncomingEvent) -> datetime:
    return ev.turn_at or ev.received_at


@click.group("journal")
def journal_group() -> None:
    """Write markdown journals from Spec Live prompt events."""


@journal_group.command("sync")
@click.option(
    "--days",
    default=7,
    show_default=True,
    type=click.IntRange(1, 30),
    help="Calendar days (UTC) ending today to cover in the output files.",
)
@click.option(
    "--output-dir",
    "-o",
    default="docs/spec-journal",
    show_default=True,
    type=click.Path(),
    help="Directory under the bundle root for per-day ``YYYY-MM-DD.md`` files.",
)
@click.option(
    "--limit",
    "-n",
    default=200,
    show_default=True,
    type=click.IntRange(1, 200),
    help="Max prompt events to pull from the API (newest first; may truncate).",
)
@click.option(
    "--project",
    "-p",
    default=None,
    help="Override `cloud.project` from spec.yaml.",
)
@click.option(
    "--include-presence",
    is_flag=True,
    help="Include ``role=presence`` events (very noisy).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print planned files without writing.",
)
def journal_sync_cmd(
    days: int,
    output_dir: str,
    limit: int,
    project: str | None,
    include_presence: bool,
    dry_run: bool,
) -> None:
    """Fetch recent team prompt events and write per-day markdown files.

    Each file is named ``YYYY-MM-DD.md`` under ``--output-dir`` (created
    if missing). Intended as a git-friendly team paper trail; combine
    with ``spec team`` for a quick terminal snapshot.

    Data comes from the same Cloud endpoint as ``spec team``; the
    ``--limit`` cap means very busy bundles may not reach the full
    ``--days`` window — a notice is written into affected files.
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
            "to spec.yaml or pass --project."
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
        rows = client.list_prompt_events(int(project_info["id"]), limit=limit)
    except ApiError as e:
        fatal(str(e))
        return

    events = [IncomingEvent.from_json(r) for r in rows if isinstance(r, dict)]
    if not include_presence:
        events = [e for e in events if e.role != "presence"]

    now = datetime.now(timezone.utc)
    today = now.date()
    start_date = today - timedelta(days=days - 1)

    by_day: dict[str, list[IncomingEvent]] = defaultdict(list)
    for ev in events:
        ts = _event_timestamp(ev)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        d = ts.astimezone(timezone.utc).date()
        if d < start_date or d > today:
            continue
        key = d.isoformat()
        by_day[key].append(ev)

    for key in by_day:
        by_day[key].sort(key=_event_timestamp)

    out_root = (root / output_dir).resolve()
    label = f"{handle}/{slug}"
    written = 0
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        key = day.isoformat()
        day_events = by_day.get(key, [])
        path = out_root / f"{key}.md"
        lines: list[str] = [
            f"# Spec journal — {key}",
            "",
            f"**Project:** `{label}`",
            f"**Generated (UTC):** {now.isoformat()}",
            f"**Source:** Spec Cloud prompt events (limit={limit} newest).",
            "",
        ]
        if not day_events:
            lines.append("_No events in the fetched window for this calendar day._")
            lines.append("")
        else:
            lines.append("## Activity")
            lines.append("")
            for ev in day_events:
                ts = _event_timestamp(ev)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                tss = ts.astimezone(timezone.utc).strftime("%H:%M:%S")
                author = ev.author_display
                branch = ev.branch or "-"
                lines.append(
                    f"### {tss} UTC · {author} · {ev.role} · {branch} · {ev.source}"
                )
                body = (ev.summary or ev.text or "").strip()
                if body:
                    first = body.splitlines()[0]
                    if len(first) > 400:
                        first = first[:400].rstrip() + "…"
                    lines.append(first)
                lines.append("")
            lines.append(
                f"_Event id range in this file: {day_events[0].id} … {day_events[-1].id}._"
            )
            lines.append("")

        lines.append(
            "> Note: the API returns at most `--limit` recent events (newest first); "
            "heavy traffic can omit older activity inside this window."
        )
        lines.append("")

        text = "\n".join(lines)
        if dry_run:
            console.print(f"[dry-run] would write {path} ({len(day_events)} events)")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written += 1

    if dry_run:
        dim("dry-run: no files written.")
        return
    ok(f"wrote {written} day file(s) under {out_root} ({label}).")
    if not events:
        warn("no events returned from the API — check `spec team` and Live setup.")


@journal_group.command("rollup")
@click.option(
    "--weeks",
    default=1,
    show_default=True,
    type=click.IntRange(1, 8),
    help="Include events whose timestamps fall within this many trailing weeks (UTC).",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    default="docs/spec-journal/weekly-rollup.md",
    show_default=True,
    type=click.Path(),
    help="Single markdown file to write under the bundle root.",
)
@click.option(
    "--limit",
    "-n",
    default=200,
    show_default=True,
    type=click.IntRange(1, 200),
    help="Max events to pull (newest first; may truncate busy bundles).",
)
@click.option(
    "--project",
    "-p",
    default=None,
    help="Override `cloud.project` (ignored with `--org`).",
)
@click.option(
    "--org",
    "org_wide",
    is_flag=True,
    help="Use workspace-wide ``GET /api/me/prompt-events`` (same bundles as ``spec team --org``).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print summary to stdout only; do not write the file.",
)
def journal_rollup_cmd(
    weeks: int,
    output_path: str,
    limit: int,
    project: str | None,
    org_wide: bool,
    dry_run: bool,
) -> None:
    """Write one markdown file grouping recent events by ISO week.

    Intended to be run from CI on a schedule (e.g. weekly) or by hand
    after ``spec journal sync``. Uses the same Cloud data as ``spec team``.
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

    client = CloudClient(creds)
    label: str
    if org_wide:
        try:
            rows = client.list_my_prompt_events(
                limit=limit, include_presence=False
            )
        except ApiError as e:
            fatal(str(e))
            return
        label = "workspace (all bundles)"
    else:
        raw = project or manifest.cloud_project
        if not raw:
            fatal(
                "No cloud project configured, or pass `--org` for a workspace rollup."
            )
            return
        try:
            handle, slug = parse_cloud_project(raw, default_handle=creds.user_handle)
        except RemoteUrlError as e:
            fatal(str(e))
            return
        try:
            project_info = client.resolve_project(handle, slug)
            rows = client.list_prompt_events(int(project_info["id"]), limit=limit)
        except ApiError as e:
            fatal(str(e))
            return
        label = f"{handle}/{slug}"

    events = [IncomingEvent.from_json(r) for r in rows if isinstance(r, dict)]
    events = [e for e in events if e.role != "presence"]

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=weeks)

    def _week_key(ts: datetime) -> str:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        d = ts.astimezone(timezone.utc).date()
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"

    filtered: list[IncomingEvent] = []
    for ev in events:
        ts = _event_timestamp(ev)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        if ts >= cutoff:
            filtered.append(ev)
    filtered.sort(key=_event_timestamp)

    by_week: dict[str, list[IncomingEvent]] = defaultdict(list)
    for ev in filtered:
        ts = _event_timestamp(ev)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        by_week[_week_key(ts)].append(ev)

    lines: list[str] = [
        "# Spec journal — weekly rollup",
        "",
        f"**Scope:** `{label}`",
        f"**Window:** last ~{weeks} week(s) UTC (from fetched events)",
        f"**Generated (UTC):** {now.isoformat()}",
        f"**Source:** Spec Cloud (limit={limit} newest rows).",
        "",
    ]
    if not by_week:
        lines.append("_No events in the fetched slice for this window._")
        lines.append("")
    else:
        for wk in sorted(by_week.keys()):
            lines.append(f"## Week {wk}")
            lines.append("")
            for ev in by_week[wk]:
                ts = _event_timestamp(ev)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                tss = ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                author = ev.author_display
                branch = ev.branch or "-"
                bl = f" · `{ev.bundle_label}`" if ev.bundle_label else ""
                lines.append(
                    f"### {tss} UTC · {author} · {ev.role} · {branch} · {ev.source}{bl}"
                )
                body = (ev.summary or ev.text or "").strip()
                if body:
                    first = body.splitlines()[0]
                    if len(first) > 500:
                        first = first[:500].rstrip() + "…"
                    lines.append(first)
                lines.append("")

    lines.append(
        "> For automation, run `spec journal rollup` from CI weekly; "
        "pair with `spec journal sync` for per-day files if you want both."
    )
    lines.append("")
    text = "\n".join(lines)
    out = (root / output_path).resolve()

    if dry_run:
        console.print(text)
        dim("dry-run: file not written.")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    ok(f"wrote {out} ({len(filtered)} event(s) across {len(by_week)} week bucket(s)).")
