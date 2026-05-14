"""``spec bundle`` — manifest alignment with git / GitHub."""
from __future__ import annotations

import click

from ..api import ApiError, CloudClient
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
    """Show ``spec.yaml`` identity, git alignment, and cloud linkage.

    Resolves the bundle from ``cwd`` the same way as ``spec watch`` (walk-up,
    ``SPEC_BUNDLE_ROOT``, or git-tracked ``spec.yaml`` in a monorepo). When
    logged in, asks Cloud whether your account can access ``cloud.project`` —
    the usual blocker for team clones before ``spec team watch`` / ``spec watch``.
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
    bundle_id = manifest.cloud_bundle_id
    hints: list[str] = []

    console.print("[sf.label]Bundle doctor[/]")
    console.print(f"  [sf.muted]root[/]       {root}")
    console.print(f"  [sf.muted]folder[/]    {dir_name}")
    console.print(f"  [sf.muted]name[/]      {yaml_name or '(unset)'}")
    if origin_url:
        console.print(f"  [sf.muted]origin[/]    {origin_url}")
        console.print(f"  [sf.muted]repo[/]      {origin_repo or '(unparsed)'}")
    else:
        dim("  origin: (no remote.origin.url — cannot infer repo name)")

    cloud_handle: str | None = None
    cloud_slug: str | None = None
    if bundle_id:
        console.print(f"  [sf.muted]bundle_id[/] {bundle_id}")

    if cloud_raw:
        try:
            creds = load_credentials()
            dh = creds.user_handle if creds else None
            cloud_handle, cloud_slug = parse_cloud_project(cloud_raw, default_handle=dh)
            console.print(f"  [sf.muted]cloud[/]     {cloud_handle}/{cloud_slug}")
        except RemoteUrlError as e:
            warn(f"  cloud.project invalid: {e}")
    else:
        dim("  cloud: (cloud.project unset)")

    creds = load_credentials()
    if cloud_handle and cloud_slug and creds and creds.access_token:
        try:
            client = CloudClient(creds)
            proj = client.resolve_project(cloud_handle, cloud_slug)
            pid = proj.get("id")
            remote_bid = proj.get("bundle_id")
            tail = f", remote bundle_id={remote_bid}" if remote_bid else ""
            console.print(f"  [sf.muted]cloud access[/]  ok — project id {pid}{tail}")
            if (
                bundle_id
                and remote_bid
                and str(bundle_id).strip() != str(remote_bid).strip()
            ):
                hints.append(
                    f"Local `cloud.bundle_id` ({bundle_id!r}) differs from Cloud ({remote_bid!r}). "
                    "Resolve before `spec push`: bind to the tree that matches Cloud, or adopt "
                    "after a deliberate first push (see push command bundle mismatch error)."
                )
        except ApiError as e:
            st = getattr(e, "status", None)
            hints.append(
                f"Your Spec login cannot use `{cloud_handle}/{cloud_slug}` ({e}; status={st}). "
                f"Ask `@{cloud_handle}` (or a workspace admin) to grant access, then re-run this command."
            )
    elif cloud_handle and cloud_slug:
        dim("  cloud access: (run `spec login` to verify access)")

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
        if cloud_handle and cloud_slug and creds and creds.access_token:
            ok("no issues detected.")
        elif cloud_handle and cloud_slug:
            ok("no issues detected (run `spec login` to verify cloud access).")
        else:
            ok("no issues detected.")


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
