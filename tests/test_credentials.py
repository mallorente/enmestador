"""Tests for password-manager credential loading."""

import subprocess
from unittest.mock import Mock

import pytest

from auth.credentials import CredentialError, get_credentials


def test_get_credentials_from_explicit_commands(monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_USERNAME_CMD", "bw get username linkedin")
    monkeypatch.setenv("LINKEDIN_PASSWORD_CMD", "bw get password linkedin")

    def fake_run(args, **_kwargs):
        if args == ["bw", "get", "username", "linkedin"]:
            return Mock(stdout="user@example.com\n")
        if args == ["bw", "get", "password", "linkedin"]:
            return Mock(stdout="secret\n")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)

    credentials = get_credentials("linkedin")

    assert credentials.username == "user@example.com"
    assert credentials.password == "secret"


def test_get_credentials_from_direct_env(monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_USERNAME", "user@example.com")
    monkeypatch.setenv("LINKEDIN_PASSWORD", "secret")

    credentials = get_credentials("linkedin")

    assert credentials.username == "user@example.com"
    assert credentials.password == "secret"


def test_get_credentials_from_bitwarden_item(monkeypatch) -> None:
    monkeypatch.setenv("X_BITWARDEN_ITEM", "enmestador/x")

    def fake_run(args, **_kwargs):
        if args == ["bw", "get", "username", "enmestador/x"]:
            return Mock(stdout="x-user\n")
        if args == ["bw", "get", "password", "enmestador/x"]:
            return Mock(stdout="x-pass\n")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)

    credentials = get_credentials("x")

    assert credentials.username == "x-user"
    assert credentials.password == "x-pass"


def test_get_credentials_requires_configuration(monkeypatch) -> None:
    monkeypatch.delenv("LINKEDIN_USERNAME_CMD", raising=False)
    monkeypatch.delenv("LINKEDIN_PASSWORD_CMD", raising=False)
    monkeypatch.delenv("LINKEDIN_BITWARDEN_ITEM", raising=False)

    with pytest.raises(CredentialError):
        get_credentials("linkedin")


def test_get_credentials_does_not_use_shell(monkeypatch) -> None:
    monkeypatch.setenv("LINKEDIN_USERNAME_CMD", "bw get username linkedin")
    monkeypatch.setenv("LINKEDIN_PASSWORD_CMD", "bw get password linkedin")
    seen_shell_values = []

    def fake_run(_args, **kwargs):
        seen_shell_values.append(kwargs.get("shell"))
        return Mock(stdout="value\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    get_credentials("linkedin")

    assert seen_shell_values == [None, None]
