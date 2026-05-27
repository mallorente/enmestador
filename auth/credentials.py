"""Runtime credential loading from an external password manager.

This module deliberately does not know any secret values. It reads credentials
from commands configured in environment variables, so secrets stay in the
operator's password manager and out of the repo, logs, and prompts.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass


class CredentialError(RuntimeError):
    """Raised when credentials cannot be loaded safely."""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


def get_credentials(platform: str) -> Credentials:
    """Load credentials for a platform from configured commands.

    Supported env vars:
    - LINKEDIN_USERNAME_CMD / LINKEDIN_PASSWORD_CMD
    - X_USERNAME_CMD / X_PASSWORD_CMD
    - LINKEDIN_BITWARDEN_ITEM / X_BITWARDEN_ITEM

    The *_BITWARDEN_ITEM shorthand expands to:
    - bw get username <item>
    - bw get password <item>
    """
    prefix = _platform_prefix(platform)

    username = os.getenv(f"{prefix}_USERNAME")
    password = os.getenv(f"{prefix}_PASSWORD")
    if username and password:
        return Credentials(username=username, password=password)

    username_cmd = os.getenv(f"{prefix}_USERNAME_CMD")
    password_cmd = os.getenv(f"{prefix}_PASSWORD_CMD")

    item = os.getenv(f"{prefix}_BITWARDEN_ITEM")
    if item and not (username_cmd or password_cmd):
        bw = os.getenv("BITWARDEN_CLI", "bw")
        username_cmd = _quote_command([bw, "get", "username", item])
        password_cmd = _quote_command([bw, "get", "password", item])

    if not username_cmd or not password_cmd:
        raise CredentialError(
            f"Missing {prefix}_USERNAME/{prefix}_PASSWORD, "
            f"{prefix}_USERNAME_CMD/{prefix}_PASSWORD_CMD or {prefix}_BITWARDEN_ITEM"
        )

    username = _run_secret_command(username_cmd)
    password = _run_secret_command(password_cmd)
    if not username or not password:
        raise CredentialError(f"Credential command returned an empty value for {prefix}")
    return Credentials(username=username, password=password)


def _platform_prefix(platform: str) -> str:
    normalized = platform.strip().lower()
    if normalized == "linkedin":
        return "LINKEDIN"
    if normalized == "x":
        return "X"
    raise CredentialError(f"Unsupported credential platform: {platform}")


def _run_secret_command(command: str) -> str:
    """Run a configured command without shell expansion and return stdout."""
    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise CredentialError("Invalid credential command syntax") from exc
    if not args:
        raise CredentialError("Empty credential command")

    try:
        result = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=float(os.getenv("CREDENTIAL_CMD_TIMEOUT", "15")),
        )
    except FileNotFoundError as exc:
        raise CredentialError(f"Credential command not found: {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise CredentialError(f"Credential command failed: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CredentialError(f"Credential command timed out: {args[0]}") from exc

    return result.stdout.strip()


def _quote_command(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)
