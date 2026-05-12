"""Tests for the post-push presence broadcast.

These pin the wire shape and the failure handling:

* event role is ``presence`` and is_clean is forced True
* event session_id matches the watcher's stable presence stream
* the announce is best-effort and never raises on transport failure
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_cli.config import Credentials
from spec_cli.realtime import push_announce


@pytest.fixture
def creds() -> Credentials:
    return Credentials(
        access_token="test-token",
        api_base="https://example.test",
        user_handle="jon",
    )


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    (tmp_path / "spec.yaml").write_text("name: test\n", encoding="utf-8")
    return tmp_path


def test_announce_push_returns_false_when_no_credentials(bundle):
    """No usable credentials means we silently skip — never raise."""
    bad = Credentials(access_token=None, api_base=None, user_handle=None)
    assert push_announce.announce_push(bad, 42, bundle, branch="main") is False


def test_announce_push_posts_presence_event(creds, bundle):
    """Happy path: the helper computes a fresh presence snapshot,
    forces ``is_clean=True``, and POSTs to the project's
    prompt-events endpoint with the user's bearer token."""
    fake_response = MagicMock(status_code=200, text="")
    with patch(
        "spec_cli.realtime.push_announce.requests.post",
        return_value=fake_response,
    ) as mocked_post:
        ok = push_announce.announce_push(creds, 42, bundle, branch="main")

    assert ok is True
    mocked_post.assert_called_once()
    args, kwargs = mocked_post.call_args
    assert args[0] == "https://example.test/api/projects/42/prompt-events"
    body = kwargs["json"]
    assert body["role"] == "presence"
    # The stable session id mirrors the watcher's choice so server-
    # side dedupe (session + role + turn_at) keeps the post in the
    # same logical presence stream.
    assert body["session_id"] == "presence:42"
    assert body["source"] == "git"
    assert body["branch"] == "main"
    # ``is_clean=True`` is forced even if the working tree still has
    # stragglers — peers should hear "the push happened, drop the
    # row" immediately.
    assert body["presence"]["is_clean"] is True


def test_announce_push_swallows_network_errors(creds, bundle):
    """A network blip never propagates — the watcher's 15 s tick
    will reconcile the state, the push has already succeeded."""
    import requests

    with patch(
        "spec_cli.realtime.push_announce.requests.post",
        side_effect=requests.ConnectionError("boom"),
    ):
        ok = push_announce.announce_push(creds, 42, bundle, branch="main")
    assert ok is False


def test_announce_push_returns_false_on_server_rejection(creds, bundle):
    """A 4xx / 5xx response is logged + treated as best-effort skip."""
    fake_response = MagicMock(status_code=500, text="boom")
    with patch(
        "spec_cli.realtime.push_announce.requests.post",
        return_value=fake_response,
    ):
        ok = push_announce.announce_push(creds, 42, bundle, branch="main")
    assert ok is False
