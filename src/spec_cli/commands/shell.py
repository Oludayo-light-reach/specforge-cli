"""`spec shell` — manage Spec's shell integrations.

Two integrations live in the same rc-file block:

1. **``git init`` → ``spec init``** wrapper. Git has no post-init hook;
   wrapping the user's ``git`` shell function is the only way to make
   ``spec init`` run alongside ``git init`` for fresh repos.
2. **Autostart hook** for ``spec live``. Fires ``spec live ensure
   --quiet`` from the shell's prompt-render hook (zsh ``precmd``,
   bash ``PROMPT_COMMAND``, fish ``__fish_preexec``-class event)
   so the watcher daemon "flicks on" the moment the user is
   prompting inside a ``spec init``'d folder. Idempotent — the
   ensure command short-circuits in <10ms when a daemon is already
   running.

The curl installer (``install.sh``) runs ``spec shell install`` for you,
so most users never type these commands directly. They exist for users
on the manual-install path, for switching shells, for auditing the
wrapper text, and for clean uninstallation.

The combined block is bracketed by the same sentinels as before, so
``spec shell uninstall`` removes both integrations in one step. No
opt-out flag is needed for autostart at install time — set
``SPEC_NO_AUTOSTART=1`` in your environment, run ``spec live
autostart off``, or remove the rc-file block entirely.

The wrappers are deliberately conservative:

* ``git`` wrapper only acts when the first argument is ``init``,
  skips ``--bare`` / ``--shared=*``, skips when ``spec.yaml``
  already exists, and preserves git's exit code.
* Autostart hook bails on the fast path the moment ``$PWD`` doesn't
  contain a ``spec.yaml`` — saves ~50ms on every shell prompt for
  users in a non-Spec directory.
* Both fall back gracefully if the ``spec`` binary is missing.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from ..ui import dim, fatal, info, ok, pointer


SHELL_INTEGRATION_BEGIN: str = "# >>> spec shell integration >>>"
SHELL_INTEGRATION_END: str = "# <<< spec shell integration <<<"


# POSIX-ish wrapper. Works in bash and zsh (both have ``local``). We
# scope every helper variable behind the ``__spec_`` prefix so we don't
# clobber anything the user may have. The ``return $__spec_rc`` line
# preserves git's own exit code so callers like ``git init &&
# something`` keep behaving as before.
#
# The autostart hook below is wired into the right per-shell prompt
# event so it fires once per *prompt render*, not once per command.
# Walking up to find ``spec.yaml`` is bounded by the depth of the
# directory tree (~10 stat calls in the worst case) and we abort
# the moment we cross the home directory, so the fast path is fast.
SHELL_INTEGRATION_BODY_BASH_ZSH: str = f"""\
{SHELL_INTEGRATION_BEGIN}
# Auto-installed by `spec shell install`. Two integrations:
#   1. Wraps `git init` so a fresh Spec bundle is scaffolded
#      immediately. Skipped when `spec.yaml` already exists at the
#      target.
#   2. Auto-starts `spec watch` in the background the first time you
#      prompt inside a `spec init`'d bundle each shell session. The
#      daemon is idempotent and runs `spec live ensure --quiet`,
#      which is a no-op in 99% of prompts. Disable on this machine
#      with `spec live autostart off` or `export SPEC_NO_AUTOSTART=1`;
#      remove entirely with `spec shell uninstall`.
git() {{
  command git "$@"
  local __spec_rc=$?
  if [ "$1" = "init" ] && [ $__spec_rc -eq 0 ] && command -v spec >/dev/null 2>&1; then
    local __spec_target="$PWD"
    local __spec_skip=0
    local __spec_arg
    for __spec_arg in "$@"; do
      case "$__spec_arg" in
        --bare|--shared=*) __spec_skip=1 ;;
        init|-*) ;;
        *) __spec_target="$__spec_arg" ;;
      esac
    done
    if [ $__spec_skip -eq 0 ] && [ -d "$__spec_target" ] && [ ! -f "$__spec_target/spec.yaml" ]; then
      ( cd "$__spec_target" && spec init )
    fi
  fi
  return $__spec_rc
}}

# Walk up from $PWD looking for spec.yaml; print the bundle root or
# nothing. Bounded at $HOME so a stray pwd inside / doesn't ever
# stat a thousand directories. Pure POSIX — no GNU-isms.
__spec_find_bundle_root() {{
  local __spec_dir="$PWD"
  local __spec_home="${{HOME:-/}}"
  local __spec_steps=0
  while [ -n "$__spec_dir" ] && [ "$__spec_dir" != "/" ]; do
    if [ -f "$__spec_dir/spec.yaml" ]; then
      printf '%s\\n' "$__spec_dir"
      return 0
    fi
    if [ "$__spec_dir" = "$__spec_home" ]; then
      return 1
    fi
    __spec_steps=$((__spec_steps + 1))
    if [ $__spec_steps -gt 32 ]; then
      return 1
    fi
    __spec_dir="$(dirname "$__spec_dir")"
  done
  return 1
}}

# The actual autostart kick. Runs once per *bundle root* per shell
# session — once you've kicked off the daemon for ~/work/foo, we
# never call ``spec live ensure`` for that root again unless you cd
# out and back in to a different bundle.
__SPEC_LIVE_ENSURED=""
__spec_live_autostart() {{
  # Honour env opt-out and sigil "shut up" knob without spawning
  # spec at all. Both checks are cheap.
  if [ "${{SPEC_NO_AUTOSTART:-0}}" = "1" ]; then return 0; fi
  command -v spec >/dev/null 2>&1 || return 0
  local __spec_root
  __spec_root="$(__spec_find_bundle_root)" || return 0
  if [ -z "$__spec_root" ]; then return 0; fi
  if [ "$__spec_root" = "$__SPEC_LIVE_ENSURED" ]; then return 0; fi
  __SPEC_LIVE_ENSURED="$__spec_root"
  ( cd "$__spec_root" && spec live ensure --quiet </dev/null >/dev/null 2>&1 & ) >/dev/null 2>&1
}}

# Wire into the right per-shell prompt event. zsh has a native
# precmd-functions hook; bash uses PROMPT_COMMAND. Both fire once per
# prompt render — exactly when the user is about to type / has just
# returned from running something. We append ourselves so user
# customisations are preserved.
if [ -n "${{ZSH_VERSION:-}}" ]; then
  if ! typeset -p precmd_functions >/dev/null 2>&1; then
    typeset -ga precmd_functions
  fi
  case " ${{precmd_functions[*]}} " in
    *' __spec_live_autostart '*) ;;
    *) precmd_functions+=( __spec_live_autostart ) ;;
  esac
elif [ -n "${{BASH_VERSION:-}}" ]; then
  case ";${{PROMPT_COMMAND:-}};" in
    *';__spec_live_autostart;'*) ;;
    *) PROMPT_COMMAND="${{PROMPT_COMMAND:+$PROMPT_COMMAND;}}__spec_live_autostart" ;;
  esac
fi
{SHELL_INTEGRATION_END}
"""


# Fish has its own grammar; we ship the same semantics rewritten in
# fish so users on fish aren't second-class. The autostart side uses
# fish's ``--on-event fish_prompt`` event so we hook the same render
# moment the bash/zsh ``PROMPT_COMMAND``/``precmd`` paths do.
SHELL_INTEGRATION_BODY_FISH: str = f"""\
{SHELL_INTEGRATION_BEGIN}
# Auto-installed by `spec shell install`. Two integrations:
#   1. Wraps `git init` so a fresh Spec bundle is scaffolded
#      in the same directory immediately after the git worktree.
#   2. Auto-starts `spec watch` in the background the first time you
#      prompt inside a `spec init`'d bundle each shell session.
# Run `spec shell uninstall` to remove. Disable autostart only with
# `spec live autostart off` or `set -x SPEC_NO_AUTOSTART 1`.
function git
    command git $argv
    set -l __spec_rc $status
    if test (count $argv) -ge 1; and test "$argv[1]" = init; and test $__spec_rc -eq 0; and type -q spec
        set -l __spec_target $PWD
        set -l __spec_skip 0
        for __spec_arg in $argv
            switch $__spec_arg
                case --bare '--shared=*'
                    set __spec_skip 1
                case init '-*'
                case '*'
                    set __spec_target $__spec_arg
            end
        end
        if test $__spec_skip -eq 0; and test -d $__spec_target; and not test -f $__spec_target/spec.yaml
            pushd $__spec_target
            spec init
            popd
        end
    end
    return $__spec_rc
end

function __spec_find_bundle_root
    set -l __spec_dir $PWD
    set -l __spec_home $HOME
    set -l __spec_steps 0
    while test -n "$__spec_dir"; and test "$__spec_dir" != "/"
        if test -f "$__spec_dir/spec.yaml"
            echo $__spec_dir
            return 0
        end
        if test "$__spec_dir" = "$__spec_home"
            return 1
        end
        set __spec_steps (math $__spec_steps + 1)
        if test $__spec_steps -gt 32
            return 1
        end
        set __spec_dir (dirname $__spec_dir)
    end
    return 1
end

set -g __SPEC_LIVE_ENSURED ""
function __spec_live_autostart --on-event fish_prompt
    if test "$SPEC_NO_AUTOSTART" = "1"
        return 0
    end
    if not type -q spec
        return 0
    end
    set -l __spec_root (__spec_find_bundle_root)
    if test -z "$__spec_root"
        return 0
    end
    if test "$__spec_root" = "$__SPEC_LIVE_ENSURED"
        return 0
    end
    set -g __SPEC_LIVE_ENSURED $__spec_root
    fish -c "cd $__spec_root; and spec live ensure --quiet" </dev/null >/dev/null 2>&1 &
    disown
end
{SHELL_INTEGRATION_END}
"""


def _detect_shell_kind(explicit: str | None) -> str:
    """Resolve which shell flavour to install for.

    ``explicit`` (from ``--shell``) wins; otherwise we read ``$SHELL``.
    Falls back to ``zsh`` (the macOS default since Catalina) when we
    genuinely can't tell.
    """
    if explicit:
        kind = explicit.strip().lower()
        if kind not in ("bash", "zsh", "fish"):
            fatal(f"Unsupported shell: {explicit}. Use one of: bash, zsh, fish.")
        return kind
    shell = os.environ.get("SHELL", "").strip()
    base = Path(shell).name if shell else ""
    if base == "fish":
        return "fish"
    if base == "bash":
        return "bash"
    if base == "zsh":
        return "zsh"
    return "zsh"


def _default_rc_file(shell_kind: str) -> Path:
    """Pick a sensible rc file per shell flavour.

    For bash on macOS the conventional file is ``~/.bash_profile`` (login
    shell), but ``~/.bashrc`` is what people on Linux expect. We prefer
    whichever already exists; if neither does, default to ``~/.bashrc``
    so subsequent installs are idempotent.
    """
    home = Path.home()
    if shell_kind == "zsh":
        return home / ".zshrc"
    if shell_kind == "fish":
        return home / ".config" / "fish" / "config.fish"
    bashrc = home / ".bashrc"
    bash_profile = home / ".bash_profile"
    if bashrc.exists():
        return bashrc
    if bash_profile.exists():
        return bash_profile
    return bashrc


def _body_for_shell(shell_kind: str) -> str:
    if shell_kind == "fish":
        return SHELL_INTEGRATION_BODY_FISH
    return SHELL_INTEGRATION_BODY_BASH_ZSH


def _install_shell_block(rc_path: Path, body: str) -> tuple[str, Path]:
    """Install or update the Spec block in ``rc_path``.

    Returns ``(status, path)`` where ``status`` is one of:
        ``installed``  — fresh rc file written
        ``appended``   — block added to an existing user-authored file
        ``updated``    — replaced an existing Spec block in place
        ``unchanged``  — block already matches; no write performed
    """
    rc_path.parent.mkdir(parents=True, exist_ok=True)

    if not rc_path.exists():
        rc_path.write_text(body, encoding="utf-8")
        return "installed", rc_path

    existing = rc_path.read_text(encoding="utf-8")
    if SHELL_INTEGRATION_BEGIN in existing and SHELL_INTEGRATION_END in existing:
        start = existing.index(SHELL_INTEGRATION_BEGIN)
        end = existing.index(SHELL_INTEGRATION_END) + len(SHELL_INTEGRATION_END)
        updated = existing[:start] + body.rstrip() + existing[end:]
        if updated == existing:
            return "unchanged", rc_path
        rc_path.write_text(updated, encoding="utf-8")
        return "updated", rc_path

    separator = "" if existing.endswith("\n") else "\n"
    rc_path.write_text(existing + separator + "\n" + body, encoding="utf-8")
    return "appended", rc_path


def _uninstall_shell_block(rc_path: Path) -> tuple[str, Path]:
    """Strip the Spec block from ``rc_path``.

    Returns ``(status, path)`` where ``status`` is ``missing`` (file
    does not exist), ``no_spec_block`` (file present, nothing to remove),
    or ``stripped`` (block removed; rest of file preserved).
    """
    if not rc_path.is_file():
        return "missing", rc_path
    try:
        existing = rc_path.read_text(encoding="utf-8")
    except OSError:
        return "no_spec_block", rc_path
    if SHELL_INTEGRATION_BEGIN not in existing or SHELL_INTEGRATION_END not in existing:
        return "no_spec_block", rc_path
    start = existing.index(SHELL_INTEGRATION_BEGIN)
    end = existing.index(SHELL_INTEGRATION_END) + len(SHELL_INTEGRATION_END)
    before = existing[:start].rstrip()
    after = existing[end:].lstrip()
    if before and after:
        updated = before + "\n\n" + after
    elif before:
        updated = before
    elif after:
        updated = after
    else:
        updated = ""
    out = updated if (not updated or updated.endswith("\n")) else updated + "\n"
    rc_path.write_text(out, encoding="utf-8")
    return "stripped", rc_path


@click.group(
    "shell",
    help=(
        "Manage the `git init` → `spec init` shell wrapper "
        "(installed by the curl installer; commands here are for review, "
        "manual installs, switching shells, and uninstall)."
    ),
)
def shell_group() -> None:
    pass


@shell_group.command("install")
@click.option(
    "--shell",
    "shell_flag",
    type=click.Choice(["bash", "zsh", "fish"], case_sensitive=False),
    default=None,
    help="Force a specific shell flavour. Defaults to detection from $SHELL.",
)
@click.option(
    "--rc-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override which rc file to write to. Default: ~/.zshrc, ~/.bashrc, "
    "or ~/.config/fish/config.fish depending on the shell.",
)
def shell_install_cmd(shell_flag: str | None, rc_file: Path | None) -> None:
    """Install the `git init` → `spec init` shell wrapper into your rc file."""
    kind = _detect_shell_kind(shell_flag)
    rc_path = (rc_file.expanduser().resolve() if rc_file else _default_rc_file(kind))
    body = _body_for_shell(kind)

    try:
        status, path = _install_shell_block(rc_path, body)
    except OSError as e:
        fatal(f"Could not write {rc_path}: {e}")
        return

    ok(f"Spec shell integration {status} for {kind}.")
    pointer("rc file     ", str(path))
    info("")
    dim("New shells will pick this up automatically. To activate now:")
    if kind == "fish":
        dim(f"  source {path}")
    else:
        dim(f"  source {path}")
    info("")
    dim(
        "From now on, `git init` (or `git init <dir>`) also runs `spec init` "
        "in the new repo. Skipped when `spec.yaml` already exists or for "
        "`--bare` repos."
    )
    dim("Remove later with: spec shell uninstall")


@shell_group.command("uninstall")
@click.option(
    "--shell",
    "shell_flag",
    type=click.Choice(["bash", "zsh", "fish"], case_sensitive=False),
    default=None,
    help="Force a specific shell flavour. Defaults to detection from $SHELL.",
)
@click.option(
    "--rc-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override which rc file to strip. Default matches `spec shell install`.",
)
def shell_uninstall_cmd(shell_flag: str | None, rc_file: Path | None) -> None:
    """Remove the Spec shell integration block from your rc file."""
    kind = _detect_shell_kind(shell_flag)
    rc_path = (rc_file.expanduser().resolve() if rc_file else _default_rc_file(kind))

    try:
        status, path = _uninstall_shell_block(rc_path)
    except OSError as e:
        fatal(f"Could not write {rc_path}: {e}")
        return

    if status == "missing":
        dim(f"No rc file at {path} — nothing to remove.")
        return
    if status == "no_spec_block":
        dim(f"No Spec block found in {path} — nothing to remove.")
        return
    ok(f"Spec shell integration removed from {path}.")
    info("")
    dim("Open a new shell (or re-source the file) to drop the wrapper.")


@shell_group.command("snippet")
@click.option(
    "--shell",
    "shell_flag",
    type=click.Choice(["bash", "zsh", "fish"], case_sensitive=False),
    default=None,
    help="Print the snippet for a specific shell flavour. Defaults to "
    "detection from $SHELL.",
)
def shell_snippet_cmd(shell_flag: str | None) -> None:
    """Print the wrapper snippet to stdout (for manual installs / review)."""
    kind = _detect_shell_kind(shell_flag)
    click.echo(_body_for_shell(kind), nl=False)


__all__ = [
    "SHELL_INTEGRATION_BEGIN",
    "SHELL_INTEGRATION_BODY_BASH_ZSH",
    "SHELL_INTEGRATION_BODY_FISH",
    "SHELL_INTEGRATION_END",
    "shell_group",
]
