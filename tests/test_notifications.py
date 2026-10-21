from unittest.mock import MagicMock

import requests

from integrations.notifications.utils import build_proxies, escape_markdown_v2
from integrations.notifications.vk import VKNotifier
from models import Item


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def test_build_proxies_normalizes_scheme():
    assert build_proxies(None) is None
    assert build_proxies("") is None
    assert build_proxies("1.2.3.4:8080") == {
        "http": "http://1.2.3.4:8080",
        "https": "http://1.2.3.4:8080",
    }
    assert build_proxies("socks5://1.2.3.4:1080") == {
        "http": "socks5://1.2.3.4:1080",
        "https": "socks5://1.2.3.4:1080",
    }


def test_escape_markdown_v2_escapes_asterisk_and_backslash():
    assert escape_markdown_v2("цена 100*200") == "цена 100\\*200"
    assert escape_markdown_v2("a\\b") == "a\\\\b"


def test_vk_check_response_raises_for_retryable_error():
    notifier = VKNotifier(vk_token="t", user_id=1)
    response = _Response(payload={"error": {"error_code": 6}})

    try:
        notifier._check_vk_response(response)
    except requests.HTTPError:
        pass
    else:
        raise AssertionError("retryable VK error must raise HTTPError")


def test_vk_check_response_ignores_client_error():
    notifier = VKNotifier(vk_token="t", user_id=1)
    response = _Response(payload={"error": {"error_code": 100}})
    notifier._check_vk_response(response)


def test_vk_random_id_is_positive():
    notifier = VKNotifier(vk_token="t", user_id=1)
    value = notifier._random_id()
    assert 1 <= value <= 2 ** 31 - 1


def test_telegram_only_text_sends_message(monkeypatch):
    from integrations.notifications import telegram as telegram_module

    post = MagicMock(return_value=_Response())
    monkeypatch.setattr(telegram_module.requests, "post", post)

    notifier = telegram_module.TelegramNotifier(
        bot_token="t", chat_id="1", only_text=True
    )
    notifier.notify_ad(Item(id=1, title="Тест"))

    assert post.call_count == 1
    url = post.call_args.args[0]
    assert url.endswith("/sendMessage")
