from loguru import logger

from models import Item
from .base import Notifier


class CompositeNotifier(Notifier):
    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    def notify(self, ad: Item = None, message: str = None) -> bool:
        success = False
        for notifier in self.notifiers:
            try:
                result = notifier.notify(ad=ad, message=message)
                if result is not False:
                    success = True
            except Exception as e:
                logger.exception(
                    f"Ошибка {e} отправки уведомления через {notifier.__class__.__name__}"
                )

        if self.notifiers and not success:
            logger.error("Не удалось отправить уведомление ни через один канал")
        return success or not self.notifiers


class NullNotifier(Notifier):
    def notify(self, ad: Item = None, message: str = None) -> bool:
        return True
