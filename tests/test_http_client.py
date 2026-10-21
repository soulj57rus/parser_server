import threading
from unittest.mock import MagicMock, patch

import pytest
from curl_cffi import requests as cffi_requests

from parser.http.client import GOVERNOR, HttpClient, RateGovernor
from parser.proxies.proxy import NoProxy


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.cookies = {}
        self.text = ""

    def json(self):
        return {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise cffi_requests.RequestsError(
                f"HTTP Error {self.status_code}", 0, self
            )


def make_client(max_retries=5, retry_delay=1, block_threshold=3, stop_event=None,
                timeout=30):
    client = HttpClient(
        proxy=NoProxy(stop_event=stop_event),
        timeout=timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        block_threshold=block_threshold,
        stop_event=stop_event,
    )
    session = MagicMock()
    session.headers = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    client._client = session
    return client


@pytest.fixture(autouse=True)
def reset_governor():
    GOVERNOR._penalty = 0.0
    yield
    GOVERNOR._penalty = 0.0


class TestRateGovernor:
    def test_starts_without_penalty(self):
        assert RateGovernor().penalty == 0

    def test_penalty_grows_on_blocks(self):
        gov = RateGovernor()
        gov.on_block()
        first = gov.penalty
        gov.on_block()
        assert gov.penalty > first
        assert gov.penalty >= RateGovernor.MIN_PENALTY

    def test_penalty_capped(self):
        gov = RateGovernor()
        for _ in range(20):
            gov.on_block()
        assert gov.penalty <= RateGovernor.MAX_PENALTY

    def test_penalty_decays_on_success(self):
        gov = RateGovernor()
        gov.on_block()
        value = gov.penalty
        gov.on_success()
        assert gov.penalty < value
        for _ in range(30):
            gov.on_success()
        assert gov.penalty == 0

    def test_retry_after_raises_penalty(self):
        gov = RateGovernor()
        gov.on_block(retry_after=10)
        assert gov.penalty >= 10


class TestHttpClientRequest:
    def test_success_returns_response(self):
        client = make_client()
        client._client.request.return_value = FakeResponse(200)
        response = client.request("GET", "https://example.com")
        assert response.status_code == 200

    def test_404_raises_immediately_without_retries(self):
        client = make_client(max_retries=5)
        client._client.request.return_value = FakeResponse(404)
        with patch("parser.http.client.interruptible_sleep") as sleep_mock:
            with pytest.raises(cffi_requests.RequestsError):
                client.request("GET", "https://example.com")
        assert client._client.request.call_count == 1
        sleep_mock.assert_not_called()

    def test_blocked_status_retries_and_recovers_at_threshold(self):
        client = make_client(max_retries=5, retry_delay=1, block_threshold=3)
        client._client.request.return_value = FakeResponse(439)
        client._governor = MagicMock()  # отключаем паузы регулятора для чистоты теста
        recover_mock = MagicMock()
        # настоящий _recover_session сбрасывает счётчик блокировок,
        # мок должен делать то же самое
        recover_mock.side_effect = lambda: setattr(client, "_block_attempts", 0)
        with (
            patch("parser.http.client.interruptible_sleep") as sleep_mock,
            patch.object(client, "_recover_session", recover_mock),
        ):
            with pytest.raises(RuntimeError):
                client.request("GET", "https://example.com")

        # 5 попыток, восстановление один раз на 3-й блокировке
        assert client._client.request.call_count == 5
        recover_mock.assert_called_once()
        assert sleep_mock.call_count == 4  # перед последней попыткой не спим

    def test_governor_waits_before_requests_on_penalties(self):
        client = make_client()
        GOVERNOR._penalty = 2.0
        client._client.request.return_value = FakeResponse(200)
        with patch("parser.http.client.interruptible_sleep") as sleep_mock:
            client.request("GET", "https://example.com")
        sleep_mock.assert_called_once()

    def test_retry_after_respected(self):
        client = make_client(max_retries=2, retry_delay=1)
        client._client.request.return_value = FakeResponse(
            429, headers={"Retry-After": "17"}
        )
        with patch("parser.http.client.interruptible_sleep") as sleep_mock:
            with pytest.raises(RuntimeError):
                client.request("GET", "https://example.com")
        delay = sleep_mock.call_args_list[0].args[0]
        assert 17 * 0.8 <= delay <= 30

    def test_server_error_retries_then_raises(self):
        client = make_client(max_retries=5, retry_delay=0.4)
        client._client.request.return_value = FakeResponse(503)
        with patch("parser.http.client.interruptible_sleep") as sleep_mock:
            with pytest.raises(cffi_requests.RequestsError):
                client.request("GET", "https://example.com")
        # 1 попытка + 2 повтора 5xx, дальше сразу ошибка
        assert client._client.request.call_count == 3
        assert sleep_mock.call_count == 2

    def test_stop_event_aborts_before_request(self):
        stop_event = threading.Event()
        stop_event.set()
        client = make_client(stop_event=stop_event)
        with pytest.raises(RuntimeError, match="Остановка"):
            client.request("GET", "https://example.com")
        client._client.request.assert_not_called()

    def test_timeout_tuple_used(self):
        client = make_client(timeout=30)
        client._client.request.return_value = FakeResponse(200)
        client.request("GET", "https://example.com")
        kwargs = client._client.request.call_args.kwargs
        assert kwargs["timeout"] == (HttpClient.CONNECT_TIMEOUT, 30)


class TestItemApiHeaders:
    def test_headers_follow_session_user_agent(self):
        client = make_client()
        client._client.headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/150"
        }
        headers = client._item_api_headers()
        assert headers["sec-ch-ua-mobile"] == "?0"
        assert headers["sec-ch-ua-platform"] == '"Windows"'

        client._client.headers = {
            "user-agent": "Mozilla/5.0 (Linux; Android 14) Mobile Safari/537.36"
        }
        headers = client._item_api_headers()
        assert headers["sec-ch-ua-mobile"] == "?1"
        assert headers["sec-ch-ua-platform"] == '"Android"'
