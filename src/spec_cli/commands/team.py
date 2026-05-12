"""
``spec team`` — recent prompt activity (snapshot) plus subcommands for
the live workspace-wide stream and flagging teammates' prompts.

Subcommands:

* ``spec team`` — print recent prompt events (snapshot from the REST
  list endpoints). Bundle-scoped by default; ``--org`` falls back to
  ``GET /api/me/prompt-events`` for a workspace-wide listing.
* ``spec team watch`` — long-lived SSE tail across every bundle the
  caller can see (``GET /api/me/prompt-stream``). Receive-only;
  designed to live in a dedicated terminal so engineers can watch
  every running agent on every project from one screen.
* ``spec team flag <event_id> --kind …`` — post a flag (reaction /
  warning / question / ack) on a prompt event. The flag fans out
  over the same SSE channel so peers see it within an RTT.
"""
from __future__ import annotations

import re
import signal
import sys
import threading
import time
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
from ..realtime.commands import (
    CommandContext,
    WatchState,
    dispatch,
    make_buffer,
    parse_command,
)
from ..realtime.critic import (
    critique_event,
    is_tool_only_summary,
    suggested_flag_command,
)
from ..realtime.events import IncomingEvent, IncomingFlag
from ..realtime.notifier import Notifier
from ..realtime.transport import SSEConsumer, SSEStreamError, run_consumer_in_thread
from ..ui import console, dim, fatal, ok


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

# Closed enum (mirrors the server's ``PromptEventFlagCreate``).
_FLAG_KINDS = ("warning", "question", "block", "ack")


def _event_matches_user_filter(ev: IncomingEvent, needle: str) -> bool:
    n = needle.lower().strip().lstrip("@")
    if not n:
        return True
    handle = (ev.author_handle or "").lower()
    name = (ev.author_name or "").lower()
    display = ev.author_display.lower()
    return n in handle or n in name or n in display


def _run_team_snapshot(
    limit: int,
    branch_filter: str | None,
    role_filter: str | None,
    project: str | None,
    org_wide: bool,
    user_filter: str | None,
) -> None:
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
        bundle = ""
        if event.bundle_label:
            bundle = f" [sf.muted]· {event.bundle_label}[/]"
        # Same role-badge convention as the live watcher so muscle
        # memory transfers between `spec team` and `spec team watch`.
        # Source color tags help separate concurrent claude_code /
        # codex / cursor activity in the snapshot too.
        if event.role == "user":
            badge = "[bold black on #3ddab4] USER [/]"
            who = f"[bold #3ddab4]{author}[/]"
        else:
            badge = "[bold black on #7de3ff]  AI  [/]"
            model = event.model or "assistant"
            who = (
                f"[bold #7de3ff]{model}[/] [sf.muted]→[/] "
                f"[bold #3ddab4]{author}[/]"
            )
        src_color = {
            "claude_code": "#c79bff",
            "codex": "#9ee37d",
            "cursor": "#7de3ff",
            "manual": "#c7c9d1",
        }.get(event.source, "#9aa3b2")
        src = f"[bold {src_color}]{event.source}[/]"
        head = (
            f"  {badge} [sf.muted]#{event.id}[/] {who} "
            f"[sf.muted]· {branch} · {when} · in[/] {src}"
            f"{bundle}"
        )
        console.print(head)
        text = (event.summary or event.text or "").strip()
        if text:
            short = text.splitlines()[0]
            if len(short) > 200:
                short = short[:200].rstrip() + "…"
            console.print(f"      [sf.muted]{short}[/]")
        # Apply the same auto-critic in the snapshot view so a `spec
        # team` glance flags the same risky prompts the live watcher
        # would. Cheap (pure regex) and only fires on user turns.
        for c in critique_event(event):
            console.print(
                f"      [{c.color}]{c.glyph} AUTO {c.rule}[/] "
                f"[sf.muted]{c.msg}[/]"
            )
            console.print(
                f"        [sf.muted]→ {suggested_flag_command(event.id, c)}[/]"
            )


@click.group(name="team", invoke_without_command=True)
@click.pass_context
@click.option(
    "--limit",
    "-n",
    "limit",
    default=20,
    show_default=True,
    type=click.IntRange(1, 200),
    help="Number of recent events to show (snapshot only).",
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
def team_group(
    ctx: click.Context,
    limit: int,
    branch_filter: str | None,
    role_filter: str | None,
    project: str | None,
    org_wide: bool,
    user_filter: str | None,
) -> None:
    """Print recent Spec Live prompt activity, or stream the whole workspace.

    Default (no subcommand): snapshot from the REST list endpoints.

    \b
    Examples:
      spec team
      spec team --org --limit 50
      spec team --user alice
      spec team watch
      spec team flag 4711 --kind warning --note "race condition risk"
    """
    if ctx.invoked_subcommand is not None:
        return
    _run_team_snapshot(
        limit,
        branch_filter,
        role_filter,
        project,
        org_wide,
        user_filter,
    )


# Idle interval (seconds) between visible "still watching" heartbeats
# in `spec team watch`. Chosen so a quiet workspace still feels alive
# without ever competing with real events for screen real estate.
_TEAM_WATCH_HEARTBEAT_SECS = 60.0
# Workspace SSE only replays when ``Last-Event-ID`` is set. On a fresh
# connect that cursor is empty — so we prime the pane from REST once
# (same bundles as the stream) and then resume the socket from the
# newest id so reviewers still see the user prompt that kicked off a
# thread they joined mid-flight.
_TEAM_WATCH_BOOTSTRAP_LIMIT = 40


def _stdin_is_interactive() -> bool:
    """Whether ``sys.stdin`` looks like an interactive TTY.

    We refuse to start the slash-command reader otherwise — piping a
    log file into ``spec team watch`` should not silently start
    interpreting log lines as commands. Anything that fails the
    isatty() check (CI runners, ``< /dev/null``, ``screen -L``
    rotated buffers) falls back to read-only mode.
    """
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def _stdin_reader(ctx: "CommandContext", stop_event: threading.Event) -> None:
    """Read slash-commands from stdin until ``stop_event`` fires.

    Lives in a daemon thread so a hung ``readline()`` does not block
    process exit after Ctrl+C — the kernel reaps stdin when the main
    thread tears down. Non-command lines are silently ignored, which
    means a reviewer who fat-fingers their editor open in the same
    pane doesn't accidentally trigger anything destructive.
    """
    while not stop_event.is_set():
        try:
            line = sys.stdin.readline()
        except (KeyboardInterrupt, ValueError):
            return
        if not line:
            # EOF on stdin (Ctrl+D, or piped input exhausted) — let
            # the watcher continue running purely as a stream
            # consumer; we just stop accepting commands.
            return
        cmd = parse_command(line)
        if cmd is None:
            continue
        dispatch(cmd, ctx)


@team_group.command("watch")
@click.option(
    "--compact",
    is_flag=True,
    help="One line per event instead of the multi-line default.",
)
@click.option(
    "--include-presence",
    is_flag=True,
    help="Include presence pings in the stream (very noisy).",
)
@click.option(
    "--heartbeat/--no-heartbeat",
    "heartbeat",
    default=True,
    show_default=True,
    help=(
        "Print a single `· still watching ·` line on idle so the terminal "
        "never looks frozen. Disable in dashboards / CI to keep the log clean."
    ),
)
@click.option(
    "--heartbeat-interval",
    type=click.IntRange(15, 3600),
    default=int(_TEAM_WATCH_HEARTBEAT_SECS),
    show_default=True,
    help="Seconds between heartbeat lines when --heartbeat is on.",
)
@click.option(
    "--critic/--no-critic",
    "critic_enabled",
    default=True,
    show_default=True,
    help=(
        "Run the rule-based auto-critic against every user prompt and "
        "print suggestions inline. Each suggestion includes the exact "
        "`spec team flag` command to escalate it into a team-visible flag."
    ),
)
@click.option(
    "--verbose/--no-verbose",
    "verbose",
    default=True,
    show_default=True,
    help=(
        "Receive full assistant ``text`` bodies (default). Use "
        "``--no-verbose`` for a summary-only feed: assistant turns "
        "show only the short summary line, user prompts are still "
        "shipped in full so reviewers can see what triggered each "
        "response."
    ),
)
@click.option(
    "--show-tool-runs/--no-tool-runs",
    "show_tool_runs",
    default=False,
    show_default=True,
    help=(
        "Show synthetic assistant turns whose only content is "
        "``ran N tools: …``. Off by default because tool-only turns "
        "are noisy; the auto-critic still inspects them and surfaces "
        "any that match a destructive / test-bypass rule."
    ),
)
@click.option(
    "--commands/--no-commands",
    "commands_enabled",
    default=True,
    show_default=True,
    help=(
        "Enable the in-pane slash-command layer (default). Disable for "
        "fully passive read-only mode. Commands include /summarize, "
        "/flag, /focus, /mute, /replay, /search, /critic, /status, /help."
    ),
)
@click.option(
    "--notify/--no-notify",
    "notify",
    default=False,
    show_default=True,
    help=(
        "Ring the terminal bell and (on macOS) fire a system "
        "notification banner when the auto-critic catches a "
        "block-severity hit on a teammate's turn — destructive "
        "command, leaked secret, test bypass. Off by default; turn "
        "on when you can't keep eyes on the pane."
    ),
)
def team_watch_cmd(
    compact: bool,
    include_presence: bool,
    heartbeat: bool,
    heartbeat_interval: int,
    critic_enabled: bool,
    verbose: bool,
    show_tool_runs: bool,
    commands_enabled: bool,
    notify: bool,
) -> None:
    """Live SSE tail across every bundle you can see (workspace-wide).

    Connects to ``GET /api/me/prompt-stream``. Receive-only — does not
    require a bundle directory. Reconnects with exponential backoff on
    transient drops; ``Ctrl+C`` once asks for a graceful exit, a
    second press forces.

    Two reviewer aids run automatically and can be disabled if the
    output ever gets noisy:

    * **Auto-critic** — every user prompt is matched against a small
      catalogue of "AI is about to do something dangerous" rules
      (destructive verbs, test-bypass language, vague intent, leaked
      secrets). Each firing rule prints one suggestion line plus the
      exact ``spec team flag`` command to escalate it. Turn off with
      ``--no-critic``.

    * **No-reply hint** — if a user prompt has been visible for 90s+
      and no assistant turn from the same session has arrived, the
      watcher surfaces a `⏳ no-reply` line. Catches the common case
      where a teammate's broadcaster is sharing prompts but not
      assistant text (the AI looks "silent" when really we just
      aren't getting the reply).

    \b
    Examples:
      spec team watch
      spec team watch --compact
      spec team watch --no-heartbeat
      spec team watch --no-critic   # silence rule-based suggestions
    """
    creds = load_credentials()
    if not creds or not creds.access_token:
        fatal("Not signed in. Run `spec login` first.")
        return

    notifier = Notifier(
        compact=compact,
        critic_enabled=critic_enabled,
        notify=notify,
    )
    stop_event = threading.Event()
    # Tracks the timestamp of the last *visible* output so the idle
    # heartbeat printer doesn't fire on top of fresh content.
    last_output_at = [time.monotonic()]

    # Bounded in-memory event memory shared with the command layer:
    # /summarize, /replay, /status all read from this. Updated in
    # the consumer callback, so command handlers see exactly what
    # has been received in this session.
    event_buffer = make_buffer()
    watch_state = WatchState(critic_enabled=critic_enabled)

    # Map event_id → project_id so /flag can post against the right
    # project when the workspace stream covers multiple bundles. We
    # populate this from the in-memory buffer; older events that
    # have aged out of the buffer are not flaggable via /flag (a
    # reviewer can always fall back to `spec team flag` outside the
    # pane).
    event_to_project: dict[int, int] = {}

    flag_client: CloudClient | None = None
    if commands_enabled:
        try:
            flag_client = CloudClient(creds)
        except Exception:  # noqa: BLE001
            flag_client = None

    cmd_ctx = CommandContext(
        notifier=notifier,
        state=watch_state,
        buffer=event_buffer,
        flag_client=flag_client,
        project_for_event=event_to_project.get,
    )

    def _on_connect() -> None:
        # First successful handshake — print the "connected" banner
        # only now, so auth failures stay silent on stdout and the
        # user sees the real error from the SSE consumer instead.
        notifier.announce_connected("workspace (all bundles)")
        if commands_enabled:
            notifier.show_command_result(
                "interactive commands enabled — type /help for the list. "
                "Two-stage Ctrl+C still exits.",
                kind="info",
            )
        last_output_at[0] = time.monotonic()

    consumer = SSEConsumer(
        creds.api_base,
        creds.access_token,
        None,
        workspace=True,
        include_presence=include_presence,
        verbose=verbose,
        on_connect=_on_connect,
    )

    def _deliver(ev: IncomingEvent, *, tick_clock: bool = True) -> None:
        """Shared path for live SSE frames and the one-shot REST warm."""
        event_buffer.append(ev)
        event_to_project[ev.id] = ev.project_id
        if not include_presence and ev.role == "presence":
            return
        if not watch_state.is_visible(ev):
            return
        notifier.set_critic_enabled(watch_state.critic_enabled)
        if (
            ev.role == "assistant"
            and not show_tool_runs
            and is_tool_only_summary(ev.summary)
        ):
            critiques = (
                critique_event(ev) if watch_state.critic_enabled else []
            )
            if not critiques:
                return
        notifier.show(ev)
        if tick_clock:
            last_output_at[0] = time.monotonic()

    try:
        hist_client = CloudClient(creds)
        boot_rows = hist_client.list_my_prompt_events(
            limit=_TEAM_WATCH_BOOTSTRAP_LIMIT,
            include_presence=include_presence,
        )
        boot_events = sorted(
            (
                IncomingEvent.from_json(r)
                for r in boot_rows
                if isinstance(r, dict)
            ),
            key=lambda e: e.id,
        )
        max_boot_id: int | None = None
        for ev in boot_events:
            if max_boot_id is None or ev.id > max_boot_id:
                max_boot_id = ev.id
            _deliver(ev, tick_clock=False)
        if max_boot_id is not None:
            consumer.set_resume_cursor(max_boot_id)
    except ApiError:
        pass

    def on_fatal(err: SSEStreamError) -> None:
        notifier.announce_fatal(str(err))
        stop_event.set()

    def on_event(ev: IncomingEvent) -> None:
        _deliver(ev, tick_clock=True)

    def on_flag(flag: IncomingFlag) -> None:
        notifier.show_flag(flag)
        last_output_at[0] = time.monotonic()

    consumer_thread = run_consumer_in_thread(
        consumer, on_event, on_fatal, on_flag=on_flag
    )

    # Background stdin reader: read one line at a time, parse, and
    # dispatch. Daemonised so a hung readline() does not prevent the
    # process from exiting after the two-stage Ctrl+C completes. We
    # deliberately do not draw a pinned input prompt or use
    # ``rich.live`` — keeping the watcher in a normal scrolling pane
    # preserves terminal scrollback, multiplexer integration, and
    # mouse-copy of past events.
    stdin_thread: threading.Thread | None = None
    if commands_enabled and _stdin_is_interactive():
        stdin_thread = threading.Thread(
            target=_stdin_reader,
            args=(cmd_ctx, stop_event),
            name="spec-team-watch-stdin",
            daemon=True,
        )
        stdin_thread.start()

    # Two-stage Ctrl+C: first press asks the consumer to stop; second
    # raises KeyboardInterrupt (default handler) and bails out of any
    # blocking cleanup. Matches the convention in `spec watch`.
    pressed_once = threading.Event()

    def _stop(_signum: int, _frame: object | None) -> None:
        if not pressed_once.is_set():
            pressed_once.set()
            try:
                dim("spec team watch: shutting down… (press Ctrl+C again to force)")
            except Exception:  # noqa: BLE001
                pass
            consumer.stop()
            stop_event.set()
            try:
                signal.signal(signal.SIGINT, signal.default_int_handler)
            except (AttributeError, ValueError):
                pass

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):
        pass

    notifier.announce_reconnecting("connecting…")

    # Main thread surfaces the idle heartbeat so the consumer thread
    # stays a clean network reader. Tick rate is 1Hz which is well
    # below any reasonable interval.
    try:
        while consumer_thread.is_alive() and not stop_event.is_set():
            # Surface "AI has not replied" hints on every loop tick.
            # Cheap (O(open_sessions)) and only prints on the first
            # transition past the no-reply threshold per session.
            notifier.check_open_sessions()
            if heartbeat:
                idle_for = time.monotonic() - last_output_at[0]
                if idle_for >= heartbeat_interval:
                    notifier.announce_heartbeat()
                    last_output_at[0] = time.monotonic()
            # Sleep in small slices so Ctrl+C is responsive even when
            # we are otherwise quiet.
            stop_event.wait(timeout=1.0)
    finally:
        consumer.stop()
        consumer_thread.join(timeout=2.0)


@team_group.command("flag")
@click.argument("event_id", type=int)
@click.option(
    "--kind",
    "-k",
    type=click.Choice(list(_FLAG_KINDS)),
    default="warning",
    show_default=True,
    help="Flag kind (warning · question · block · ack).",
)
@click.option(
    "--note",
    "-m",
    default=None,
    help="Optional short note (max 500 chars).",
)
@click.option(
    "--project",
    "-p",
    default=None,
    help=(
        "Project `handle/slug`. Defaults to `cloud.project` from "
        "spec.yaml when run inside a bundle. Required outside a bundle."
    ),
)
def team_flag_cmd(
    event_id: int, kind: str, note: str | None, project: str | None
) -> None:
    """Flag a teammate's prompt event in near-real-time.

    The flag is delivered to every connected ``spec watch`` /
    ``spec team watch`` over SSE on the ``flag`` channel, so peers
    see it next to the prompt within an RTT. Idempotent: posting the
    same kind for the same event twice yields 409.

    \b
    Examples:
      spec team flag 4711 --kind warning --note "race condition risk"
      spec team flag 4712 --kind ack
      spec team flag 4713 --kind block --note "do not run this"
    """
    creds = load_credentials()
    if not creds or not creds.access_token:
        fatal("Not signed in. Run `spec login` first.")
        return

    raw = project
    if not raw:
        try:
            root = find_bundle_root()
        except BundleNotFoundError:
            fatal(
                "No project specified. Pass `--project <handle>/<slug>` or "
                "run `spec team flag` from inside a Spec bundle."
            )
            return
        manifest = load_manifest(root)
        raw = manifest.cloud_project
        if not raw:
            fatal(
                "No `cloud.project` in spec.yaml. Pass --project <handle>/<slug>."
            )
            return

    try:
        handle, slug = parse_cloud_project(raw, default_handle=creds.user_handle)
    except RemoteUrlError as e:
        fatal(str(e))
        return

    client = CloudClient(creds)
    try:
        project_info = client.resolve_project(handle, slug)
    except ApiError as e:
        fatal(str(e))
        return
    project_id = int(project_info["id"])
    try:
        out = client.create_prompt_event_flag(
            project_id=project_id,
            event_id=event_id,
            kind=kind,
            note=note,
        )
    except ApiError as e:
        fatal(str(e))
        return

    flag_id = out.get("id") if isinstance(out, dict) else None
    ok(
        f"flagged #{event_id} as {kind}"
        + (f" (flag id {flag_id})" if flag_id is not None else "")
    )


# Backwards-compatible export name for cli.py
team_cmd = team_group
