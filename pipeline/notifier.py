"""Telegram notifier for the PKM ingestion pipeline."""

import logging
import os
import re
from datetime import UTC, datetime

import httpx

from models import PipelineResult

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org"
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"/bot[^/\s]+/sendMessage")
_HTTPX_REDACTION_FILTER_INSTALLED = False


def _redact_telegram_bot_tokens(value: object) -> object:
    """Redact Telegram bot tokens from log-record values."""
    if isinstance(value, str):
        return _TELEGRAM_BOT_TOKEN_RE.sub("/bot<redacted>/sendMessage", value)
    return value


class _TelegramTokenRedactionFilter(logging.Filter):
    """Redact Telegram bot tokens from httpx/httpcore records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_telegram_bot_tokens(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_telegram_bot_tokens(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: _redact_telegram_bot_tokens(value)
                for key, value in record.args.items()
            }
        return True


def _install_httpx_redaction_filter() -> None:
    """Install once so Telegram tokens cannot leak through httpx request logs."""
    global _HTTPX_REDACTION_FILTER_INSTALLED
    if _HTTPX_REDACTION_FILTER_INSTALLED:
        return
    redaction_filter = _TelegramTokenRedactionFilter()
    logging.getLogger("httpx").addFilter(redaction_filter)
    logging.getLogger("httpcore").addFilter(redaction_filter)
    _HTTPX_REDACTION_FILTER_INSTALLED = True


class Notifier:
    """Sends alerts and run summaries via Telegram Bot API.

    All failures are logged and silently swallowed — the notifier is
    best-effort and MUST NOT abort the pipeline.
    """

    def __init__(self) -> None:
        _install_httpx_redaction_filter()
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._client = httpx.AsyncClient(timeout=10.0)
        self._disabled = not self._token or not self._chat_id
        if self._disabled:
            logger.warning("Notifier disabled: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    async def send(self, domain: str, error_summary: str) -> bool:
        """Send a critical-failure alert for a specific domain."""
        if self._disabled:
            return False

        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        message = f"🚨 Pipeline Alert\n\nDomain: {domain}\nError: {error_summary}\nTime: {timestamp}"

        return await self._post(message)

    async def send_bookmark_error(self, source: str, url: str, stage: str, error: str) -> bool:
        if self._disabled:
            return False

        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        message = (
            f"⚠️ Bookmark Error\n\n"
            f"Source: {source}\n"
            f"URL: {url}\n"
            f"Stage: {stage}\n"
            f"Error: {error[:200]}\n"
            f"Time: {timestamp}"
        )

        return await self._post(message)

    async def send_auth_expired(self, platform: str) -> bool:
        """Notify that a platform session has expired and cookies need refreshing."""
        if self._disabled:
            return False

        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        message = (
            f"🔑 Auth Expired — {platform}\n\n"
            f"The {platform} session has expired. Run this to refresh:\n"
            f"  python cookie_refresher.py --platform {platform.lower()}\n\n"
            f"Time: {timestamp}"
        )
        return await self._post(message)

    async def send_summary(self, result: PipelineResult) -> bool:
        """Send an end-of-run summary with processed/failed counts."""
        if self._disabled:
            return False

        message = (
            f"📊 Pipeline Run Summary\n\n"
            f"Processed: {result.processed}\n"
            f"Enriched: {result.enriched}\n"
            f"Dead letters: {result.dead_letter}"
        )

        return await self._post(message)

    async def _post(self, message: str) -> bool:
        """POST a plain-text message to the Telegram Bot API."""
        url = f"{_TELEGRAM_API}/bot{self._token}/sendMessage"
        try:
            response = await self._client.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "text": message,
                },
            )
            if response.status_code == 401:
                logger.error("Telegram API: invalid bot token — disabling notifier for this run")
                self._disabled = True
                return False
            if response.status_code >= 400:
                logger.warning("Telegram API error (HTTP %d): %s", response.status_code, response.text)
                return False
            return True
        except httpx.RequestError as exc:
            logger.warning("Telegram request failed: %s", exc)
            return False

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
