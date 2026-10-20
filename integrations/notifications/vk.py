import random

import requests
from loguru import logger

from integrations.notifications.base import Notifier
from integrations.notifications.transport import send_with_retries
from integrations.notifications.utils import build_proxies, get_first_image
from models import Item

VK_RETRYABLE_ERROR_CODES = {6, 9, 10}


class VKNotifier(Notifier):
    def __init__(
        self,
        vk_token: str,
        user_id: str | int,
        proxy: str = None,
        only_text: bool = False,
    ):
        self.vk_token = vk_token
        self.user_id = user_id
        self.proxy = build_proxies(proxy)
        self.only_text = only_text

    @staticmethod
    def _random_id() -> int:
        return random.randint(1, 2 ** 31 - 1)

    @staticmethod
    def _check_vk_response(response: requests.Response) -> None:
        """Повторяем при временных ошибках VK API (200 + error_code)."""
        try:
            body = response.json()
        except ValueError:
            return
        if not isinstance(body, dict) or "error" not in body:
            return
        error_code = body["error"].get("error_code", 0)
        if error_code in VK_RETRYABLE_ERROR_CODES:
            raise requests.HTTPError(
                f"VK retryable error {error_code}", response=response
            )

    def notify_message(self, message: str):
        random_id = self._random_id()

        def _send():
            headers = {
                "Authorization": f"Bearer {self.vk_token}"
            }
            response = requests.post(
                "https://api.vk.com/method/messages.send",
                headers=headers,
                data={
                    "user_id": self.user_id,
                    "random_id": random_id,
                    "message": message,
                    "v": "5.199"
                },
                proxies=self.proxy,
                timeout=10,
            )
            self._check_vk_response(response)
            return response

        response = send_with_retries(_send)
        if response is None:
            return False
        body = response.json()
        if not isinstance(body, dict):
            return False
        if "error" in body:
            logger.warning(
                f"VK API error {body['error'].get('error_code', 0)}: "
                f"{body['error'].get('error_msg', 'Unknown error')}"
            )
            return False
        return True

    def notify_ad(self, ad: Item):
        random_id = self._random_id()
        message = self.format_ad(ad)

        attachment = None
        if not self.only_text:
            _image_url = get_first_image(ad=ad)
            if _image_url:
                attachment = self.__upload_photo_to_vk(_image_url, str(self.user_id))

        def _send():
            payload = {
                "user_id": self.user_id,
                "random_id": random_id,
                "message": message,
                "v": "5.199"
            }
            if attachment:
                payload["attachment"] = attachment

            headers = {
                "Authorization": f"Bearer {self.vk_token}"
            }

            response = requests.post(
                "https://api.vk.com/method/messages.send",
                data=payload,
                headers=headers,
                proxies=self.proxy,
                timeout=10,
            )
            self._check_vk_response(response)
            return response

        response = send_with_retries(_send)
        if response is None:
            logger.warning("VK: не удалось отправить сообщение")
            return False

        body = response.json()

        if not isinstance(body, dict):
            logger.warning("VK: неожиданный ответ API")
            return False

        if "error" in body:
            error_msg = body["error"].get("error_msg", "Unknown error")
            error_code = body["error"].get("error_code", 0)
            logger.warning(f"VK API error {error_code}: {error_msg}")
            return False

        logger.debug(f"Сообщение успешно отправлено")
        return True

    def notify(self, ad: Item = None, message: str = None):
        if ad:
            return self.notify_ad(ad=ad)
        return self.notify_message(message=message)

    def __upload_photo_to_vk(self, photo_url: str, user_id: str) -> str | None:
        """Загружает фото по URL и возвращает attachment для messages.send"""
        headers = {"Authorization": f"Bearer {self.vk_token}"}

        try:
            # Шаг 1: Получаем URL для загрузки
            upload_server_response = requests.post(
                "https://api.vk.com/method/photos.getMessagesUploadServer",
                headers=headers,
                params={"v": "5.199"},
                proxies=self.proxy,
                timeout=10,
            ).json()

            if "error" in upload_server_response:
                logger.warning(f"VK upload server error: {upload_server_response['error']}")
                return None

            upload_url = upload_server_response["response"]["upload_url"]

            # Шаг 2: Скачиваем фото и загружаем на VK
            photo_data = requests.get(
                photo_url, proxies=self.proxy, timeout=15
            ).content
            upload_response = requests.post(
                upload_url,
                files={"photo": ("photo.jpg", photo_data, "image/jpeg")},
                proxies=self.proxy,
                timeout=30,
            ).json()

            if not upload_response.get("photo") or upload_response.get("photo") == "[]":
                logger.warning("VK: фото не загружено")
                return None

            # Шаг 3: Сохраняем фото
            save_response = requests.post(
                "https://api.vk.com/method/photos.saveMessagesPhoto",
                headers=headers,
                params={
                    "photo": upload_response["photo"],
                    "server": upload_response["server"],
                    "hash": upload_response["hash"],
                    "v": "5.199"
                },
                proxies=self.proxy,
                timeout=10,
            ).json()

            if "error" in save_response:
                logger.warning(f"VK save photo error: {save_response['error']}")
                return None

            # Возвращаем attachment в формате photo{owner_id}_{id}
            photo_info = save_response["response"][0]
            attachment = f"photo{photo_info['owner_id']}_{photo_info['id']}"
            logger.debug(f"VK: фото загружено, attachment: {attachment}")
            return attachment

        except Exception as e:
            logger.warning(f"Error uploading photo to VK: {e}")
            return None

    @staticmethod
    def format_ad(ad: Item) -> str:
        """Форматирует объявление в простой текст для VK (без Markdown)"""

        def clean(text: str) -> str:
            if not text:
                return ""
            return str(text).replace("\xa0", " ")

        price = clean(
            getattr(ad, "priceDetailed", {}).get("value", "") if isinstance(getattr(ad, "priceDetailed", None),
                                                                            dict) else getattr(ad.priceDetailed,
                                                                                               "value",
                                                                                               getattr(ad,
                                                                                                       "priceDetailed",
                                                                                                       "")))
        title = clean(getattr(ad, "title", ""))
        short_url = f"https://avito.ru/{getattr(ad, 'id', '')}"
        seller = clean(str(getattr(ad, "sellerId", ""))) if getattr(ad, "sellerId", None) else ""

        parts = []
        if price:
            price_part = f"💰 {price}"
            if getattr(ad, "isPromotion", False):
                price_part += " 🔥"
            parts.append(price_part)

        if title:
            parts.append(f"📦 {title}")

        if seller:
            parts.append(f"👤 Продавец: {seller}")

        if short_url:
            parts.append(f"🔗 {short_url}")

        message = "\n".join(parts)
        return message
