"""
``spec live`` — control Spec Live (real-time prompt sharing) toggles.

Two layers of opt-out, both surfaced through this one command group:

* **Per-bundle** (``cloud.prompt_stream`` in ``spec.yaml``) — what the
  team agrees on. ``spec live on`` / ``spec live off`` flip this.
  Edited as plain YAML by anyone with the manifest open; this command
  is the friendly path that doesn't require remembering the key.
* **Per-user** (``~/.spec/preferences.json``) — what *you*, on this
  machine, want regardless of the bundle. ``spec live mute`` silences
  broadcasting on this laptop for every bundle; ``spec live unmute``
  removes the override. Useful for laptops with NDA work, demos, or
  side projects you don't want bleeding into the team feed.

``spec live status`` prints both layers and the resolved final state
("broadcasting: on/off") so the user can see why a setting is what it
is without opening two different files.

Spec Live broadcasting is **on by default** the moment the CLI is
installed. The opt-outs are the affordance; nothing else needs to
happen for new teammates to start sharing prompts.
"""
from __future__ import annotations

import click

from ..config import (
    BundleNotFoundError,
    Manifest,
    dump_manifest,
    find_bundle_root,
    load_manifest,
)
from ..preferences import load_preferences
from ..ui import console, dim, fatal, info, ok, warn


def _try_load_manifest() -> Manifest | None:
    """Tolerant manifest load — returns ``None`` outside a bundle.

    The user-level commands (``mute`` / ``unmute`` / ``status``) work
    everywhere on the machine, so we don't fail when the user runs
    them outside a bundle root. The bundle-level ``on`` / ``off``
    commands re-check and bail loudly themselves.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError:
        return None
    try:
        return load_manifest(root)
    except Exception:  # noqa: BLE001 — best-effort
        return None


@click.group("live")
def live_group() -> None:
    """Spec Live — real-time prompt sharing toggles.

    Broadcasting is on by default once the CLI is installed. Use this
    group to opt the bundle out, or to silence broadcasting on your
    machine without affecting teammates.

    \b
    Subcommands:
      spec live status   — show current state (bundle + user, resolved)
      spec live on       — enable broadcasting for this bundle
      spec live off      — disable broadcasting for this bundle
      spec live mute     — silence broadcasting on this machine (all bundles)
      spec live unmute   — remove the per-machine mute
    """


# ── per-bundle controls ──────────────────────────────────────────


@live_group.command("on")
@click.option(
    "--verbose",
    "verbose_flag",
    is_flag=True,
    help=(
        "Also enable verbose mode — broadcasts assistant *full text* "
        "(not just summaries). Off by default; assistant bodies are "
        "big and often sensitive."
    ),
)
def live_on_cmd(verbose_flag: bool) -> None:
    """Enable Spec Live broadcasting for this bundle.

    Writes ``cloud.prompt_stream: enabled`` to ``spec.yaml``. The
    setting is committed to git like any other manifest change, so
    teams agreeing on a policy do so visibly.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return
    manifest = load_manifest(root)
    manifest.set_cloud_prompt_stream(enabled=True, verbose=verbose_flag or None)
    dump_manifest(manifest)
    ok(
        "Spec Live ON for this bundle "
        + ("(verbose: assistant full text)" if verbose_flag else "(summary-only)")
    )
    dim("  written to spec.yaml — commit it so teammates inherit the setting.")


@live_group.command("off")
def live_off_cmd() -> None:
    """Disable Spec Live broadcasting for this bundle.

    Writes ``cloud.prompt_stream: disabled`` to ``spec.yaml``.
    Receivers — anyone running ``spec watch`` — still see incoming
    peer events; this only stops the bundle from broadcasting.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return
    manifest = load_manifest(root)
    manifest.set_cloud_prompt_stream(enabled=False)
    dump_manifest(manifest)
    ok("Spec Live OFF for this bundle")
    dim("  written to spec.yaml — commit it so teammates inherit the setting.")


# ── per-user controls ───────────────────────────────────────────


@live_group.command("mute")
def live_mute_cmd() -> None:
    """Silence Spec Live broadcasting on **this machine** for every bundle.

    The user-level kill-switch. Use this on a laptop with NDA / private
    work that you'd prefer not to mix into any team feed regardless of
    per-bundle settings. Receiving is unaffected.

    Stored in ``~/.spec/preferences.json``; revert with ``spec live unmute``.
    """
    prefs = load_preferences()
    if prefs.prompt_stream_muted:
        info("already muted")
        return
    prefs.prompt_stream = "muted"
    path = prefs.save()
    ok("Spec Live muted on this machine — broadcasting off for every bundle.")
    dim(f"  ({path})")
    dim("  receivers still see incoming peer events; this only stops your outgoing share.")


@live_group.command("unmute")
def live_unmute_cmd() -> None:
    """Remove the per-machine mute — defer to per-bundle settings again."""
    prefs = load_preferences()
    if not prefs.prompt_stream_muted:
        info("not muted — nothing to do")
        return
    prefs.prompt_stream = "default"
    prefs.save()
    ok("Spec Live unmuted — broadcasting follows each bundle's spec.yaml setting again.")


# ── inspection ──────────────────────────────────────────────────


@live_group.command("status")
def live_status_cmd() -> None:
    """Show the resolved Spec Live state for the current shell.

    Prints both the per-bundle setting and the per-user mute, plus the
    final resolved answer to "is broadcasting on right now?". The
    resolution rule is simple: per-user mute always wins; otherwise
    the per-bundle setting decides.
    """
    prefs = load_preferences()
    manifest = _try_load_manifest()

    console.print("[sf.label]Spec Live[/]")

    if manifest is None:
        dim("  bundle: (not in a Spec bundle — run from a directory with spec.yaml)")
        bundle_enabled = None
        bundle_verbose = False
    else:
        ps = manifest.prompt_stream
        bundle_enabled = bool(ps.get("enabled"))
        bundle_verbose = bool(ps.get("verbose"))
        state = "ON" if bundle_enabled else "OFF"
        verbose_tag = " · verbose: assistant full text" if bundle_verbose else " · summary-only"
        bundle_origin = "default" if not _has_explicit_prompt_stream(manifest) else "explicit"
        dim(
            f"  bundle (spec.yaml):     {state}{verbose_tag}  ({bundle_origin})"
        )

    if prefs.prompt_stream_muted:
        dim("  machine (~/.spec):      MUTED — overrides any bundle setting")
    else:
        dim("  machine (~/.spec):      default — defers to bundle setting")

    if bundle_enabled is None:
        dim("  resolved broadcasting:  ?  (not in a bundle)")
        return

    resolved = bundle_enabled and not prefs.prompt_stream_muted
    if resolved:
        ok(f"  → broadcasting ON {'(verbose)' if bundle_verbose else ''}".rstrip())
    else:
        warn(
            "  → broadcasting OFF"
            + (" (machine mute)" if prefs.prompt_stream_muted else " (bundle off)")
        )
    dim("  receiving:              always available to project members.")


def _has_explicit_prompt_stream(manifest: Manifest) -> bool:
    """Did ``cloud.prompt_stream`` appear in the manifest, or is the
    current state purely the default? Used by ``status`` to label the
    bundle line so users know whether the team has agreed on a policy
    or is just inheriting the default-on behaviour."""
    cloud = manifest.data.get("cloud") or {}
    if not isinstance(cloud, dict):
        return False
    return "prompt_stream" in cloud
