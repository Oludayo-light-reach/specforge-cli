"""Tests for ``.spec/team-push-requests.yaml`` handoff helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml

from spec_cli.realtime.team_push_requests import (
    attach_push_requests_to_body,
    list_active_push_requests,
    normalize_target_handle,
    record_push_request,
    team_push_requests_path,
)


def test_normalize_target_handle():
    assert normalize_target_handle("JC") == "jc"
    assert normalize_target_handle("@alice-1") == "alice-1"


def test_normalize_target_handle_rejects_invalid():
    with pytest.raises(ValueError):
        normalize_target_handle("Bad_Handle")


def test_record_and_list_roundtrip(tmp_path):
    p = record_push_request(
        tmp_path,
        to_handle="jc",
        from_handle="alice",
        from_display="@alice",
        branch="main",
        message="please push",
        ttl_secs=3600,
    )
    assert p == team_push_requests_path(tmp_path)
    active = list_active_push_requests(tmp_path)
    assert len(active) == 1
    assert active[0]["to_handle"] == "jc"
    assert active[0]["from_handle"] == "alice"
    assert active[0]["branch"] == "main"
    assert active[0]["message"] == "please push"


def test_attach_push_requests_to_body(tmp_path):
    record_push_request(
        tmp_path,
        to_handle="bob",
        from_handle="carol",
        from_display="@carol",
        branch=None,
        message=None,
        ttl_secs=3600,
    )
    body: dict = {"schema": 1, "members": []}
    attach_push_requests_to_body(tmp_path, body)
    assert "push_requests" in body
    assert body["push_requests"][0]["to_handle"] == "bob"


def test_expired_row_removed_from_disk(tmp_path):
    path = team_push_requests_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    path.write_text(
        yaml.safe_dump(
            {
                "schema": 1,
                "requests": [
                    {
                        "id": "dead",
                        "to_handle": "jc",
                        "from_handle": "alice",
                        "from_display": "@alice",
                        "branch": "main",
                        "message": None,
                        "requested_at": past,
                        "expires_at": past,
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    active = list_active_push_requests(tmp_path)
    assert active == []
    body = path.read_text(encoding="utf-8")
    assert "requests: []" in body or "requests: null" in body
