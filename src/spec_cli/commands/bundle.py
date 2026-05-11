"""``spec bundle`` — manifest alignment with git / GitHub."""
from __future__ import annotations

import click

from ..config import (
    BundleNotFoundError,
    dump_manifest,
    find_bundle_root,
    load_credentials,
    load_manifest,
    parse_cloud_project,
    RemoteUrlError,
)
from ..git import read_origin_url, repo_name_from_remote_url, repo_toplevel
from ..ui import console, dim, fatal, info, ok, warn


@click.group("bundle")
def bundle_group() -> None:
    """Inspect and align bundle metadata with your git remote."""


@bundle_group.command("doctor")
def bundle_doctor_cmd() -> None:
    """Show ``spec.yaml`` ``name``, directory name, ``origin`` repo name, and cloud slug.

    Surfaces mismatches so bundle labels stay aligned with the GitHub
    repo engineers expect.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    manifest = load_manifest(root)
    dir_name = root.name
    yaml_name = manifest.name
    origin_url = read_origin_url(root)
    origin_repo = repo_name_from_remote_url(origin_url)
    cloud_raw = manifest.cloud_project

    console.print("[sf.label]Bundle doctor[/]")
    console.print(f"  [sf.muted]root[/]       {root}")
    console.print(f"  [sf.muted]folder[/]    {dir_name}")
    console.print(f"  [sf.muted]name[/]      {yaml_name or '(unset)'}")
    if origin_url:
        console.print(f"  [sf.muted]origin[/]    {origin_url}")
        console.print(f"  [sf.muted]repo[/]      {origin_repo or '(unparsed)'}")
    else:
        dim("  origin: (no remote.origin.url — cannot infer repo name)")

    if cloud_raw:
        try:
            creds = load_credentials()
            dh = creds.user_handle if creds else None
            handle, slug = parse_cloud_project(cloud_raw, default_handle=dh)
            console.print(f"  [sf.muted]cloud[/]     {handle}/{slug}")
        except RemoteUrlError as e:
            warn(f"  cloud.project invalid: {e}")
    else:
        dim("  cloud: (cloud.project unset)")

    hints: list[str] = []
    if origin_repo and yaml_name and origin_repo != yaml_name:
        hints.append(
            f"manifest `name` ({yaml_name!r}) differs from origin repo ({origin_repo!r}) "
            "— run `spec bundle sync-name` to align, or set `name` by hand."
        )
    if origin_repo and dir_name != origin_repo and (not yaml_name or yaml_name == dir_name):
        hints.append(
            f"folder name ({dir_name!r}) differs from origin repo ({origin_repo!r}); "
            "the manifest `name` field is what Spec shows most often."
        )
    gt = repo_toplevel(root)
    if gt is not None and root.resolve() != gt.resolve():
        hints.append(
            f"bundle root ({root}) is not the git toplevel ({gt}) — unusual for a 1:1 repo."
        )

    if hints:
        console.print()
        for h in hints:
            warn(h)
    else:
        console.print()
        ok("no obvious naming mismatches detected.")


@bundle_group.command("sync-name")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the change without writing spec.yaml.",
)
def bundle_sync_name_cmd(dry_run: bool) -> None:
    """Set ``name`` in ``spec.yaml`` from ``git remote get-url origin``.

    Falls back with a clear error when origin is missing or unparsable.
    """
    try:
        root = find_bundle_root()
    except BundleNotFoundError as e:
        fatal(str(e))
        return

    origin_url = read_origin_url(root)
    inferred = repo_name_from_remote_url(origin_url)
    if not inferred:
        fatal(
            "Cannot infer a repo name from origin. "
            "Configure `git remote add origin …` or set `name` in spec.yaml manually."
        )
        return

    manifest = load_manifest(root)
    current = manifest.name
    if current == inferred:
        ok(f"`name` already matches origin ({inferred!r}). Nothing to do.")
        return

    if dry_run:
        info(f"Would set `name` to {inferred!r} (currently {current!r}).")
        return

    manifest.data["name"] = inferred
    dump_manifest(manifest)
    ok(f"Updated spec.yaml `name` → {inferred!r}")
