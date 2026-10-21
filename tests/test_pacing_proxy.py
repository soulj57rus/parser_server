import threading
from time import monotonic
from unittest.mock import MagicMock, patch

from utils.pacing import interruptible_sleep
from parser.proxies.proxy import MobileProxy


class TestInterruptibleSleep:
    def test_sleeps_full_time_without_event(self):
        start = monotonic()
        interruptible_sleep(0.15)
        assert monotonic() - start >= 0.14

    def test_returns_immediately_when_event_set(self):
        event = threading.Event()
        event.set()
        start = monotonic()
        interruptible_sleep(5, event)
        assert monotonic() - start < 0.5

    def test_interrupted_midway(self):
        event = threading.Event()
        timer = threading.Timer(0.2, event.set)
        timer.start()
        start = monotonic()
        interruptible_sleep(5, event)
        elapsed = monotonic() - start
        timer.cancel()
        assert elapsed < 1.0

    def test_zero_is_noop(self):
        interruptible_sleep(0)
        interruptible_sleep(-1)


class TestMobileProxy:
    def _proxy(self, stop_event=None):
        return MobileProxy("user:pass@1.2.3.4:8000", "https://change.example", stop_event)

    def test_network_error_requestexception_no_sleep(self):
        import requests as std_requests

        proxy = self._proxy()
        with patch(
            "parser.proxies.proxy.requests.get",
            side_effect=std_requests.RequestException("timeout"),
        ) as get_mock:
            with patch("parser.proxies.proxy.interruptible_sleep") as sleep_mock:
                # не должно бросать исключение наверх
                proxy.handle_block()
        get_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_settles_after_ip_change(self):
        proxy = self._proxy()
        response = MagicMock(status_code=200)
        response.json.return_value = {"new_ip": "5.6.7.8"}
        with patch("parser.proxies.proxy.requests.get", return_value=response):
            with patch("parser.proxies.proxy.interruptible_sleep") as sleep_mock:
                proxy.handle_block()
        sleep_mock.assert_called_once_with(MobileProxy.IP_CHANGE_SETTLE_SECONDS, None)

    def test_bad_status_no_settle_sleep(self):
        proxy = self._proxy()
        response = MagicMock(status_code=500)
        with patch("parser.proxies.proxy.requests.get", return_value=response):
            with patch("parser.proxies.proxy.interruptible_sleep") as sleep_mock:
                proxy.handle_block()
        sleep_mock.assert_not_called()

    def test_bad_json_does_not_crash(self):
        proxy = self._proxy()
        response = MagicMock(status_code=200)
        response.json.side_effect = ValueError("no json")
        with patch("parser.proxies.proxy.requests.get", return_value=response):
            with patch("parser.proxies.proxy.interruptible_sleep"):
                proxy.handle_block()
