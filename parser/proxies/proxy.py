from abc import ABC, abstractmethod

import requests
from loguru import logger

from utils.pacing import interruptible_sleep


class Proxy(ABC):
    @abstractmethod
    def get_httpx_proxy(self) -> dict | None:
        pass

    @abstractmethod
    def handle_block(self):
        pass


class NoProxy(Proxy):
    def __init__(self, stop_event=None):
        self.stop_event = stop_event

    def get_httpx_proxy(self):
        return None

    def handle_block(self):
        pass


class ServerProxy(Proxy):
    def __init__(self, proxy, stop_event=None):
        self.proxy = proxy
        self.stop_event = stop_event

    def get_httpx_proxy(self):
        return f"http://{self.proxy}"

    def handle_block(self):
        pass


class MobileProxy(Proxy):
    """Мобильный прокси со сменой IP по ссылке.

    После запроса на смену IP даём прокси время применить новый адрес:
    если сразу продолжить запросы, они уйдут со старого (уже
    заблокированного) IP и снова получат 403/429/439.
    """

    IP_CHANGE_SETTLE_SECONDS = 3

    def __init__(self, url, change_ip_url, stop_event=None):
        self.url = url
        self.change_ip_url = change_ip_url
        self.stop_event = stop_event

    def get_httpx_proxy(self):
        return f"http://{self.url}"

    def handle_block(self):
        try:
            res = requests.get(self.change_ip_url, params={"format": "json"}, timeout=10)
        except requests.RequestException as e:
            # Смена IP не удалась — старый IP ещё работает, не ждём зря
            logger.warning(f"Ошибка при запросе на смену IP: {e}")
            return

        if res.status_code != 200:
            logger.warning(f"Не удалось сменить IP: HTTP {res.status_code}")
            return

        try:
            new_ip = res.json().get("new_ip")
        except (ValueError, AttributeError):
            new_ip = None
        logger.success(f"новый IP {new_ip if new_ip else '(неизвестен)'}")

        interruptible_sleep(self.IP_CHANGE_SETTLE_SECONDS, self.stop_event)
