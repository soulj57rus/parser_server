import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests as std_requests

from parser.cookies.external_api import ExternalApiCookiesProvider
from parser.cookies.own_cookies import OwnCookiesProvider
from parser.cookies.factory import build_cookies_provider


def make_config(**overrides):
    defaults = dict(
        cookies_api_key="test-key",
        proxy_string="user:pass@1.2.3.4:8080",
        purchase_cooldown=600,
        use_bypass_api=False,
        use_own_cookies=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_provider(tmp_path, stop_event=None, config=None):
    path = tmp_path / "cookies_external.json"
    return ExternalApiCookiesProvider(
        config or make_config(),
        storage_path=path,
        proxy=None,
        stop_event=stop_event,
    )


class FakeResponse:
    def __init__(self, status_code=200, cookies=None, json_data=None):
        self.status_code = status_code
        self.cookies = cookies or {}
        self.text = json.dumps(json_data or {})
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    @property
    def ok(self):
        return self.status_code < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as std_requests

            raise std_requests.HTTPError(f"HTTP {self.status_code}")


class TestExternalApiProvider:
    def test_stop_event_stored(self, tmp_path):
        event = threading.Event()
        provider = make_provider(tmp_path, stop_event=event)
        assert provider.stop_event is event

    def test_update_tracks_status_and_cookies(self, tmp_path):
        provider = make_provider(tmp_path)
        provider.update(FakeResponse(200, cookies={"k": "v"}))
        provider.update(FakeResponse(439, cookies={"k": "v2"}))
        assert provider.status_history == [200, 439]
        assert provider.last_cookies == {"k": "v2"}

        # история сберегается на диск
        data = json.loads(provider.storage_path.read_text(encoding="utf-8"))
        assert data["status_history"] == [200, 439]

    def test_update_bad_code_forces_save(self, tmp_path):
        provider = make_provider(tmp_path)
        provider.update(FakeResponse(429))
        data = json.loads(provider.storage_path.read_text(encoding="utf-8"))
        assert 429 in data["status_history"]

    def test_handle_block_without_cookies_purchases(self, tmp_path):
        provider = make_provider(tmp_path)
        with patch.object(provider, "_get_new_cookies") as purchase:
            provider.handle_block()
        purchase.assert_called_once()

    def test_handle_block_waits_for_running_unblock(self, tmp_path):
        provider = make_provider(tmp_path)
        provider.last_id = "cookie-id"
        provider.unblock_started_at = time.time() - 10  # внутри UNBLOCK_TIMEOUT
        with (
            patch("parser.cookies.external_api.requests.post") as post,
            patch.object(provider, "_get_new_cookies") as purchase,
            patch("parser.cookies.external_api.interruptible_sleep"),
        ):
            provider.handle_block()
        post.assert_not_called()
        purchase.assert_not_called()

    def test_handle_block_starts_unblock(self, tmp_path):
        provider = make_provider(tmp_path)
        provider.last_id = "cookie-id"
        with (
            patch("parser.cookies.external_api.requests.post") as post,
            patch("parser.cookies.external_api.interruptible_sleep") as sleep_mock,
        ):
            post.return_value = FakeResponse(202)
            provider.handle_block()

        assert provider.unblock_started_at is not None
        sleep_mock.assert_called_once_with(
            provider.WAIT_FIRST_FOR_UNBLOCK, provider.stop_event
        )

    def test_handle_block_409_keeps_waiting(self, tmp_path):
        provider = make_provider(tmp_path)
        provider.last_id = "cookie-id"
        with (
            patch("parser.cookies.external_api.requests.post") as post,
            patch.object(provider, "_get_new_cookies") as purchase,
            patch("parser.cookies.external_api.interruptible_sleep") as sleep_mock,
        ):
            post.return_value = FakeResponse(409)
            provider.handle_block()

        assert provider.unblock_started_at is not None
        sleep_mock.assert_called_once_with(provider.WAIT_FOR_UNBLOCK, provider.stop_event)
        purchase.assert_not_called()

    def test_handle_block_410_purchases_new(self, tmp_path):
        provider = make_provider(tmp_path)
        provider.last_id = "cookie-id"
        with (
            patch("parser.cookies.external_api.requests.post") as post,
            patch.object(provider, "_get_new_cookies") as purchase,
            patch("parser.cookies.external_api.interruptible_sleep"),
        ):
            post.return_value = FakeResponse(410)
            provider.handle_block()
        purchase.assert_called_once()
        assert provider.unblock_started_at is None

    def test_handle_block_403_pauses_before_purchase(self, tmp_path):
        provider = make_provider(tmp_path)
        provider.last_id = "cookie-id"
        with (
            patch("parser.cookies.external_api.requests.post") as post,
            patch.object(provider, "_get_new_cookies") as purchase,
            patch("parser.cookies.external_api.interruptible_sleep") as sleep_mock,
        ):
            post.return_value = FakeResponse(403)
            provider.handle_block()
        sleep_mock.assert_called_once_with(provider.NOT_BALANCE, provider.stop_event)
        purchase.assert_called_once()

    def test_handle_block_network_error_purchases(self, tmp_path):
        import requests as std_requests

        provider = make_provider(tmp_path)
        provider.last_id = "cookie-id"
        with (
            patch(
                "parser.cookies.external_api.requests.post",
                side_effect=std_requests.RequestException("down"),
            ),
            patch.object(provider, "_get_new_cookies") as purchase,
            patch("parser.cookies.external_api.interruptible_sleep"),
        ):
            provider.handle_block()
        purchase.assert_called_once()

    def _purchase_payload(self):
        return {
            "success": True,
            "results": {
                "id": "id-1",
                "cookies": {"avito": "1"},
                "fingerprint": {
                    "impersonate": "chrome120",
                    "headers": {"user-agent": "UA-test"},
                },
                "mobile": True,
                "user_agent": "UA-test",
            },
        }

    def test_purchase_success_stores_and_saves(self, tmp_path):
        provider = make_provider(tmp_path)
        with (
            patch("parser.cookies.external_api.requests.post") as post,
            patch("parser.cookies.external_api.interruptible_sleep") as sleep_mock,
        ):
            post.return_value = FakeResponse(200, json_data=self._purchase_payload())
            provider._get_new_cookies()

        assert provider.last_id == "id-1"
        assert provider.last_cookies == {"avito": "1"}
        assert provider.get_user_agent() == "UA-test"
        assert provider.get_fingerprint()["impersonate"] == "chrome120"
        assert provider.status_history == []
        sleep_mock.assert_called_once_with(provider.WAIT_FOR_NEW, provider.stop_event)
        assert provider.storage_path.exists()

    def test_purchase_failure_pauses(self, tmp_path):
        provider = make_provider(tmp_path)
        with patch("parser.cookies.external_api.requests.post") as post:
            post.return_value = FakeResponse(503)
            with patch("parser.cookies.external_api.interruptible_sleep") as sleep_mock:
                with pytest.raises(std_requests.HTTPError):
                    provider._get_new_cookies()
        sleep_mock.assert_called_once_with(provider.PAUSE_FOR_ERROR, provider.stop_event)

    def test_purchase_delayed_after_cooldown_503(self, tmp_path):
        provider = make_provider(tmp_path)
        provider.status_history = [439] * 20
        with patch("parser.cookies.external_api.requests.post") as post:
            post.return_value = FakeResponse(503)
            with patch("parser.cookies.external_api.interruptible_sleep") as sleep_mock:
                with pytest.raises(std_requests.HTTPError):
                    provider._get_new_cookies()
        sleep_mock.assert_called_once_with(provider.PAUSE_FOR_ERROR, provider.stop_event)
        # статус-история не очищена, так как покупка не удалась
        assert provider.status_history == [439] * 20

    def test_purchase_success_clears_status_history(self, tmp_path):
        provider = make_provider(tmp_path)
        provider.status_history = [439] * 20
        with (
            patch("parser.cookies.external_api.requests.post") as post,
            patch("parser.cookies.external_api.interruptible_sleep"),
        ):
            post.return_value = FakeResponse(200, json_data=self._purchase_payload())
            provider._get_new_cookies()
        # после успешной покупки старые «плохие» коды не провоцируют новую покупку
        assert provider.status_history == []


class TestOwnCookiesProvider:
    def test_handle_block_uses_interruptible_sleep(self, tmp_path):
        event = threading.Event()
        provider = OwnCookiesProvider(
            storage_path=tmp_path / "own.json", stop_event=event
        )
        with patch("parser.cookies.own_cookies.interruptible_sleep") as sleep_mock:
            provider.handle_block()
        assert provider.stop_event is event
        sleep_mock.assert_called_once_with(provider.UNBLOCK_TIMEOUT, event)

    def test_stop_event_stored(self, tmp_path):
        event = threading.Event()
        provider = OwnCookiesProvider(storage_path=tmp_path / "own.json", stop_event=event)
        assert provider.stop_event is event

    def test_get_missing_cookies_raises(self, tmp_path):
        provider = OwnCookiesProvider(storage_path=tmp_path / "own.json")
        with pytest.raises(Exception):
            provider.get()


class TestCookiesFactory:
    def test_factory_passes_stop_event_to_external(self):
        event = threading.Event()
        provider = build_cookies_provider(
            make_config(use_bypass_api=True), proxy=None, stop_event=event
        )
        assert isinstance(provider, ExternalApiCookiesProvider)
        assert provider.stop_event is event

    def test_factory_passes_stop_event_to_own(self):
        event = threading.Event()
        provider = build_cookies_provider(
            make_config(use_bypass_api=False, use_own_cookies=True), stop_event=event
        )
        assert isinstance(provider, OwnCookiesProvider)
        assert provider.stop_event is event

    def test_factory_returns_none_without_options(self):
        provider = build_cookies_provider(make_config())
        assert provider is None
