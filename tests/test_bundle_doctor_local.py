"""Local-only bundle doctor + post-merge hint emission."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from spec_cli.cli import cli
from spec_cli.commands.bundle import emit_bundle_doctor_post_merge_hints


def _git_init_with_origin(root: Path, *, origin_path: str = "https://github.com/acme/RightRepo.git") -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", origin_path],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_emit_post_merge_hints_cloud_project_parse_error(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = tmp_path / "b"
    root.mkdir()
    (root / "spec.yaml").write_text(
        "name: test\n"
        "cloud:\n"
        "  project: \"!!!/bad\"\n",
        encoding="utf-8",
    )
    emit_bundle_doctor_post_merge_hints(root)
    err = capsys.readouterr().err
    assert "spec: bundle doctor (after merge)" in err
    assert "cloud.project invalid" in err


def test_emit_post_merge_hints_name_mismatch_origin(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = tmp_path / "b"
    root.mkdir()
    _git_init_with_origin(root)
    (root / "spec.yaml").write_text(
        "name: WrongName\n"
        "cloud:\n"
        "  project: alice/slug-one\n",
        encoding="utf-8",
    )
    emit_bundle_doctor_post_merge_hints(root)
    err = capsys.readouterr().err
    assert "spec: bundle doctor (after merge)" in err
    assert "sync-name" in err


def test_emit_post_merge_hints_silent_when_aligned(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = tmp_path / "b"
    root.mkdir()
    _git_init_with_origin(root, origin_path="https://github.com/acme/Aligned.git")
    (root / "spec.yaml").write_text(
        "name: Aligned\n"
        "cloud:\n"
        "  project: alice/slug-one\n",
        encoding="utf-8",
    )
    emit_bundle_doctor_post_merge_hints(root)
    assert capsys.readouterr().err == ""


def test_bundle_doctor_local_only_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "spec.yaml").write_text(
        "name: test\n"
        "cloud:\n"
        "  project: bob/slug\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(cli, ["bundle", "doctor", "--local-only"])
    assert r.exit_code == 0
    assert "no issues detected" in r.output
