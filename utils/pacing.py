"""Прерываемые паузы.

Обычный time.sleep блокирует поток на всё время ожидания, из-за чего
нажатие «Стоп» в GUI срабатывает с задержкой до нескольких минут
(например, при паузах сервиса cookies в 300 секунд). Здесь ожидание
разбито на короткие отрезки и проверяется stop_event.
"""
from time import monotonic, sleep


def interruptible_sleep(seconds: float, stop_event=None, chunk: float = 0.25) -> None:
    """Спать `seconds`, прерываясь сразу после установки stop_event."""
    if seconds <= 0:
        return

    if stop_event is None or not callable(getattr(stop_event, "is_set", None)):
        sleep(seconds)
        return

    deadline = monotonic() + seconds
    while not stop_event.is_set():
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(chunk, remaining))
