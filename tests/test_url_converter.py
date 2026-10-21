import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from parser.url_converter import AvitoUrlConverter


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    AvitoUrlConverter._last_request_at = 0.0
    yield
    AvitoUrlConverter._last_request_at = 0.0


def _make_converter(tmp_dir: Path) -> AvitoUrlConverter:
    return AvitoUrlConverter(
        cache_path=tmp_dir / "urls.json",
        min_request_interval=0,
        max_attempts=2,
    )


def test_convert_uses_cache(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        converter = _make_converter(Path(tmp))
        post = MagicMock(
            return_value=_Response(
                payload={"success": True, "api_url": "https://api.avito.ru/1"}
            )
        )
        monkeypatch.setattr("parser.url_converter.requests.post", post)

        assert converter.convert("https://avito.ru/x") == "https://api.avito.ru/1"
        assert converter.convert("https://avito.ru/x") == "https://api.avito.ru/1"
        assert post.call_count == 1

        cached = json.loads((Path(tmp) / "urls.json").read_text(encoding="utf-8"))
        assert cached["https://avito.ru/x"] == "https://api.avito.ru/1"


def test_convert_retries_on_retryable_status(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        converter = _make_converter(Path(tmp))
        responses = [
            _Response(status_code=502),
            _Response(payload={"success": True, "api_url": "https://api.avito.ru/2"}),
        ]
        post = MagicMock(side_effect=responses)
        monkeypatch.setattr("parser.url_converter.requests.post", post)
        monkeypatch.setattr("parser.url_converter.time.sleep", lambda _: None)

        assert converter.convert("https://avito.ru/y") == "https://api.avito.ru/2"
        assert post.call_count == 2


def test_convert_does_not_retry_client_error(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        converter = _make_converter(Path(tmp))
        post = MagicMock(return_value=_Response(status_code=400))
        monkeypatch.setattr("parser.url_converter.requests.post", post)
        monkeypatch.setattr("parser.url_converter.time.sleep", lambda _: None)

        with pytest.raises(RuntimeError):
            converter.convert("https://avito.ru/z")
        assert post.call_count == 1
