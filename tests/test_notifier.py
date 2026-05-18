"""Tests for the Telegram notifier module."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from models import PipelineResult
from notifier import Notifier


def _make_response(status_code: int, text: str = "OK") -> httpx.Response:
    """Build a minimal httpx.Response for mocking."""
    return httpx.Response(status_code=status_code, text=text)


@pytest.fixture
def mock_client() -> AsyncMock:
    """Provide a mocked httpx.AsyncClient."""
    client = AsyncMock()
    with patch("notifier.httpx.AsyncClient", return_value=client):
        yield client


@pytest.fixture
def enabled_notifier(mock_client: AsyncMock) -> Notifier:
    """Notifier with env vars set."""
    with patch.dict("os.environ", {
        "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
        "TELEGRAM_CHAT_ID": "-1001234567890",
    }):
        notifier = Notifier()
        notifier._client = mock_client
        yield notifier


# --- Disabled notifier ---


def test_disabled_when_no_env_vars() -> None:
    with patch.dict("os.environ", {}, clear=True):
        notifier = Notifier()
        assert notifier._disabled is True


def test_disabled_when_only_token() -> None:
    with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "abc"}, clear=True):
        notifier = Notifier()
        assert notifier._disabled is True


def test_disabled_when_only_chat_id() -> None:
    with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "-100"}, clear=True):
        notifier = Notifier()
        assert notifier._disabled is True


# --- send() — alert ---


@pytest.mark.asyncio
async def test_send_success(enabled_notifier: Notifier, mock_client: AsyncMock) -> None:
    mock_client.post.return_value = _make_response(200)

    result = await enabled_notifier.send("X scraper", "Authentication failed")

    assert result is True
    mock_client.post.assert_awaited_once()
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[0][0].endswith("/sendMessage")
    payload = call_kwargs[1]["json"]
    assert payload["chat_id"] == "-1001234567890"
    assert "X scraper" in payload["text"]
    assert "Authentication failed" in payload["text"]
    assert "parse_mode" not in payload  # plain text, no Markdown


@pytest.mark.asyncio
async def test_send_includes_timestamp(enabled_notifier: Notifier, mock_client: AsyncMock) -> None:
    mock_client.post.return_value = _make_response(200)

    await enabled_notifier.send("LinkedIn", "API error")

    payload = mock_client.post.call_args[1]["json"]
    # Timestamp should be present in the message (plain text, no Markdown markers)
    assert "Time:" in payload["text"]


@pytest.mark.asyncio
async def test_send_silent_on_5xx(enabled_notifier: Notifier, mock_client: AsyncMock) -> None:
    error_resp = httpx.Response(500, text="Internal Server Error")
    mock_client.post.return_value = error_resp

    result = await enabled_notifier.send("X scraper", "boom")

    assert result is False
    # Notifier should NOT be disabled on 5xx (only on 401)
    assert enabled_notifier._disabled is False


@pytest.mark.asyncio
async def test_send_disables_on_401(enabled_notifier: Notifier, mock_client: AsyncMock) -> None:
    error_resp = httpx.Response(401, text="Unauthorized")
    mock_client.post.return_value = error_resp

    result = await enabled_notifier.send("X scraper", "bad token")

    assert result is False
    assert enabled_notifier._disabled is True
    # Subsequent calls should short-circuit
    result2 = await enabled_notifier.send("LinkedIn", "another")
    assert result2 is False


@pytest.mark.asyncio
async def test_send_disabled_returns_false() -> None:
    with patch.dict("os.environ", {}, clear=True):
        notifier = Notifier()
        result = await notifier.send("X", "error")
        assert result is False


# --- send_summary() ---


@pytest.mark.asyncio
async def test_send_summary_success(enabled_notifier: Notifier, mock_client: AsyncMock) -> None:
    mock_client.post.return_value = _make_response(200)
    result_obj = PipelineResult(processed=10, enriched=8, dead_letter=2)

    result = await enabled_notifier.send_summary(result_obj)

    assert result is True
    payload = mock_client.post.call_args[1]["json"]
    assert "10" in payload["text"]
    assert "8" in payload["text"]
    assert "2" in payload["text"]
    assert "Pipeline Run Summary" in payload["text"]


@pytest.mark.asyncio
async def test_send_summary_disabled(enabled_notifier: Notifier, mock_client: AsyncMock) -> None:
    enabled_notifier._disabled = True
    result_obj = PipelineResult(processed=0, enriched=0, dead_letter=0)

    result = await enabled_notifier.send_summary(result_obj)

    assert result is False
    mock_client.post.assert_not_awaited()


# --- Network errors ---


@pytest.mark.asyncio
async def test_send_network_error(enabled_notifier: Notifier, mock_client: AsyncMock) -> None:
    mock_client.post.side_effect = httpx.ConnectError("DNS failure")

    result = await enabled_notifier.send("X scraper", "network down")

    assert result is False
    # Should not disable on network errors
    assert enabled_notifier._disabled is False


@pytest.mark.asyncio
async def test_send_timeout(enabled_notifier: Notifier, mock_client: AsyncMock) -> None:
    mock_client.post.side_effect = httpx.ReadTimeout("timed out")

    result = await enabled_notifier.send("LinkedIn", "timeout")

    assert result is False


# --- close() ---


@pytest.mark.asyncio
async def test_close(enabled_notifier: Notifier, mock_client: AsyncMock) -> None:
    await enabled_notifier.close()
    mock_client.aclose.assert_awaited_once()


# --- send_bookmark_error() ---


@pytest.mark.asyncio
async def test_send_bookmark_error_success(enabled_notifier: Notifier, mock_client: AsyncMock) -> None:
    mock_client.post.return_value = _make_response(200)

    result = await enabled_notifier.send_bookmark_error("x", "https://example.com/article", "llm", "Model exhausted")

    assert result is True
    payload = mock_client.post.call_args[1]["json"]
    assert "x" in payload["text"]
    assert "https://example.com/article" in payload["text"]
    assert "llm" in payload["text"]


@pytest.mark.asyncio
async def test_send_bookmark_error_disabled() -> None:
    with patch.dict("os.environ", {}, clear=True):
        notifier = Notifier()
        result = await notifier.send_bookmark_error("x", "https://example.com", "extraction", "error")
        assert result is False


@pytest.mark.asyncio
async def test_send_bookmark_error_truncates_long_error(enabled_notifier: Notifier, mock_client: AsyncMock) -> None:
    mock_client.post.return_value = _make_response(200)

    long_error = "x" * 500
    result = await enabled_notifier.send_bookmark_error("x", "https://example.com", "llm", long_error)

    assert result is True
    payload = mock_client.post.call_args[1]["json"]
    assert long_error[:200] in payload["text"]
