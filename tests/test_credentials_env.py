"""Environment override for ``load_credentials`` (CI / cron)."""

from __future__ import annotations

import pytest

from spec_cli.config import load_credentials


def test_load_credentials_prefers_spec_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEC_ACCESS_TOKEN", "jwt-test-token")
    monkeypatch.setenv("SPEC_API", "https://api.example.test")
    monkeypatch.setenv("SPEC_USER_HANDLE", "alice")
    monkeypatch.delenv("SPEC_REFRESH_TOKEN", raising=False)
    creds = load_credentials()
    assert creds is not None
    assert creds.access_token == "jwt-test-token"
    assert creds.api_base == "https://api.example.test"
    assert creds.user_handle == "alice"
