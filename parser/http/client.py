"""
Клиент для запросов парсера (curl_cffi)

Включает адаптивный «регулятор» темпа запросов (RateGovernor):
после каждого 429/403/439 пауза перед следующими запросами плавно
растёт (с учётом Retry-After), а после успешных ответов — снижается.
Это снижает вероятность блокировок без изменения настроек пользователя.
"""
import random
import threading

from curl_cffi import requests
from loguru import logger

from parser.cookies.base import CookiesProvider
from parser.proxies.proxy import Proxy
from utils.pacing import interruptible_sleep


class RateGovernor:
    """Адаптивное ограничение темпа запросов против 429/439.

    Чем чаще Avito отвечает кодами блокировки, тем длиннее пауза перед
    каждым следующим запросом. После серии успешных ответов штраф
    уменьшается, и парсер снова работает на полной скорости.
    """

    MIN_PENALTY = 1.0   # первая блокировка -> +1 сек к каждому запросу
    MAX_PENALTY = 45.0  # потолок дополнительной паузы
    DECAY = 0.5         # штраф уменьшается вдвое после каждого успеха

    def __init__(self):
        self._lock = threading.Lock()
        self._penalty = 0.0

    @property
    def penalty(self) -> float:
        with self._lock:
            return self._penalty

    def on_block(self, retry_after: float | None = None) -> float:
        """Зарегистрировать блокировку и вернуть новую паузу."""
        with self._lock:
            new_penalty = (
                self.MIN_PENALTY if self._penalty <= 0 else self._penalty * 2
            )
            if retry_after:
                new_penalty = max(new_penalty, float(retry_after))
            self._penalty = min(new_penalty, self.MAX_PENALTY)
            return self._penalty

    def on_success(self) -> None:
        """После успешного ответа постепенно снимаем ограничения."""
        with self._lock:
            self._penalty = 0.0 if self._penalty <= 0.25 else self._penalty * self.DECAY

    def wait(self, stop_event=None) -> None:
        """Пауза перед запросом, если антибот недавно отвечал блокировкой."""
        penalty = self.penalty
        if penalty <= 0:
            return
        delay = penalty * random.uniform(0.85, 1.15)
        interruptible_sleep(delay, stop_event)


# Единый регулятор на процесс: адаптация сохраняется между циклами парсера.
GOVERNOR = RateGovernor()


class HttpClient:
    ITEM_API_URL = "https://m.avito.ru/api/1/card/items/{item_id}"

    BLOCKED_STATUS_CODES = (403, 429, 439)
    BACKOFF_CAP = 30.0          # максимальная пауза между попытками
    CONNECT_TIMEOUT = 10.0      # отдельный таймаут на установку соединения
    MAX_SERVER_RETRIES = 2      # повторов для ошибок 5xx

    def __init__(
        self,
        proxy: Proxy,
        cookies: CookiesProvider | None = None,
        timeout: int = 30,
        max_retries: int = 5,
        retry_delay: int = 5,
        block_threshold: int = 3,
        stop_event=None,
        governor: RateGovernor | None = None,
    ):
        self.proxy = proxy
        self.cookies = cookies
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.block_threshold = block_threshold
        self.stop_event = stop_event
        self._governor = governor or GOVERNOR

        self._block_attempts = 0
        self._client = self._build_client()

    def _build_client(self) -> requests.Session:
        fingerprint = self.cookies.get_fingerprint() if self.cookies else None
        fingerprint = fingerprint if isinstance(fingerprint, dict) else {}
        fingerprint_headers = fingerprint.get("headers", {})
        fingerprint_headers = (
            fingerprint_headers if isinstance(fingerprint_headers, dict) else {}
        )
        user_agent = fingerprint_headers.get("user-agent")
        if not user_agent and self.cookies:
            user_agent = self.cookies.get_user_agent()
        user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        )
        is_mobile = " Mobile " in user_agent
        impersonate = fingerprint.get("impersonate") or "chrome"
        session = requests.Session(impersonate=impersonate)
        default_headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'accept-language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'cache-control': 'no-cache',
            'pragma': 'no-cache',
            "referer": "https://www.avito.ru/",
            'sec-ch-ua': '"Not=A?Brand";v="99", "Google Chrome";v="149", "Chromium";v="149"',
            'sec-ch-ua-mobile': '?1' if is_mobile else '?0',
            'sec-ch-ua-platform': '"Android"' if is_mobile else '"Windows"',
            'user-agent': user_agent,
        }
        headers = {**default_headers, **fingerprint_headers}
        headers["user-agent"] = user_agent
        session.headers.update(headers)
        if self.cookies:
            session.cookies.update(self.cookies.get())

        proxy = self.proxy.get_httpx_proxy()
        if proxy:
            session.proxies = {
                "http": proxy,
                "https": proxy,
            }

        return session

    def _reset_client(self) -> None:
        self._client.close()
        self._client = self._build_client()

    def _recover_session(self) -> None:
        """Сменить IP и обновить cookies в правильном порядке."""
        self.proxy.handle_block()
        if self.cookies:
            self.cookies.handle_block()
        self._reset_client()
        self._block_attempts = 0

    @staticmethod
    def _parse_retry_after(response) -> float | None:
        """Читает Retry-After (секунды). Некорректные значения игнорируются."""
        try:
            raw = (response.headers or {}).get("Retry-After")
        except Exception:
            return None
        if not raw:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return min(value, 120.0)

    def _backoff_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Задержка перед повтором: экспоненциальный рост + джиттер."""
        base = self.retry_delay * (1.5 ** max(0, attempt - 1))
        if retry_after:
            base = max(base, retry_after)
        base = min(base, self.BACKOFF_CAP)
        return base * random.uniform(0.8, 1.25)

    def request(self, method: str, url: str, **kwargs):
        last_exc = None
        server_retries = 0
        kwargs.pop("timeout", None)  # таймаут задаём сами
        timeout = (self.CONNECT_TIMEOUT, self.timeout)

        for attempt in range(1, self.max_retries + 1):
            if self.stop_event is not None and self.stop_event.is_set():
                raise RuntimeError(f"Остановка запрошена, запрос к {url} отменён")

            # Адаптивная пауза: растёт после блокировок, сходит на нет после успехов
            self._governor.wait(self.stop_event)

            try:
                response = self._client.request(
                    method,
                    url,
                    timeout=timeout,
                    **kwargs,
                )
            except requests.RequestsError as e:
                last_exc = e
                self._block_attempts += 1
                logger.warning(f"Request error (attempt {attempt}): {e}")
                if self._block_attempts >= self.block_threshold:
                    logger.warning(
                        "Достигнут лимит сетевых ошибок, "
                        "обновляем proxy и cookies"
                    )
                    self._recover_session()
                if attempt >= self.max_retries:
                    break  # дальше не спим — сразу наверх
                interruptible_sleep(self._backoff_delay(attempt), self.stop_event)
                continue

            if self.cookies:
                self.cookies.update(response)

            status = response.status_code

            if status in self.BLOCKED_STATUS_CODES:
                retry_after = self._parse_retry_after(response)
                self._governor.on_block(retry_after)
                self._block_attempts += 1
                logger.warning(
                    f"Запрос заблокирован ({status}) к {url}, "
                    f"попытка {self._block_attempts}"
                )

                if self._block_attempts >= self.block_threshold:
                    logger.warning("Достигнут лимит блокировок, запускается обработка")
                    self._recover_session()

                if attempt >= self.max_retries:
                    break  # дальше не спим — сразу наверх
                delay = self._backoff_delay(attempt, retry_after)
                logger.debug(f"Повтор запроса {url} через {delay:.1f} сек.")
                interruptible_sleep(delay, self.stop_event)
                continue

            self._block_attempts = 0
            self._governor.on_success()

            if 500 <= status < 600:
                server_retries += 1
                if server_retries > self.MAX_SERVER_RETRIES or attempt >= self.max_retries:
                    response.raise_for_status()
                delay = self._backoff_delay(server_retries)
                logger.warning(
                    f"Временная ошибка сервера ({status}) к {url}, "
                    f"повтор {server_retries}/{self.MAX_SERVER_RETRIES} через {delay:.1f} сек."
                )
                interruptible_sleep(delay, self.stop_event)
                continue

            if 400 <= status < 500:
                # Прочие 4xx (404, 410 и т.п.) — повторять бессмысленно
                response.raise_for_status()

            return response

        raise RuntimeError("HTTP запросы были неуспешными") from last_exc

    def _item_api_headers(self) -> dict:
        """Заголовки мобильного API, согласованные с UA текущей сессии.

        Раньше UA был жёстко зашит как Android-браузер, даже когда cookies
        были получены для десктопного браузера. Такое несовпадение —
        лишний сигнал для антибота, поэтому подстраиваемся под сессию.
        """
        user_agent = self._client.headers.get("user-agent", "")
        is_mobile = " Mobile " in user_agent or "Android" in user_agent
        return {
            "accept": "application/json, text/plain, */*",
            "referer": "https://m.avito.ru/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "sec-ch-ua-mobile": "?1" if is_mobile else "?0",
            "sec-ch-ua-platform": '"Android"' if is_mobile else '"Windows"',
        }

    def fetch_item_data(self, item_id: int) -> dict:
        """Загрузить JSON карточки объявления через mobile API."""
        response = self.request(
            "GET",
            self.ITEM_API_URL.format(item_id=item_id),
            headers=self._item_api_headers(),
        )
        logger.info(f"Карточка {item_id}: HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Ответ карточки не является JSON-объектом")
        return payload
