"""Каркасные тесты: реальная сборка AvitoParse и сквозной parse()."""
import threading
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import parser_cls
from db_service import SQLiteDBHandler
from dto import AvitoConfig
from models import Item
from parser.http.client import GOVERNOR


def make_config(**overrides) -> AvitoConfig:
    defaults = dict(
        urls=["https://www.avito.ru/test"],
        count=1,
        save_xlsx=False,
        pause_between_links=0,
        parse_views=False,
        parse_phone=False,
        max_age=0,
    )
    defaults.update(overrides)
    return AvitoConfig(**defaults)


@contextmanager
def parser_context(tmp_path, config=None, stop_event=None):
    """Настоящий __init__ AvitoParse с БД-заглушкой во временной папке.

    Патчи действуют и на создание парсера, и на время вызова parse().
    """
    SQLiteDBHandler._instance = None

    def db_factory(*args, **kwargs):
        SQLiteDBHandler._instance = None
        return SQLiteDBHandler(db_name=str(tmp_path / "wiring.db"))

    with (
        patch("parser_cls.SQLiteDBHandler", db_factory),
        patch("parser_cls.AvitoUrlConverter", MagicMock()),
        patch("parser_cls.build_notifier") as notifier_builder,
        patch("parser_cls.build_result_storage") as storage_builder,
    ):
        notifier_builder.return_value.notify_many.side_effect = lambda ads: list(ads)
        parser = parser_cls.AvitoParse(config or make_config(), stop_event=stop_event)
        yield parser, storage_builder.return_value


class TestWiring:
    def test_stop_event_reaches_http_and_proxy(self, tmp_path):
        event = threading.Event()
        with parser_context(tmp_path, stop_event=event) as (parser, _):
            assert parser.http.stop_event is event
            assert parser.proxy.stop_event is event

    def test_http_without_stop_event(self, tmp_path):
        with parser_context(tmp_path) as (parser, _):
            assert parser.http.stop_event is None
            assert parser.proxy.stop_event is None

    def test_batch_viewed_filter_wired(self, tmp_path):
        with parser_context(tmp_path) as (parser, _):
            assert parser.ads_filter.is_viewed_many_fn == parser._many_viewed

    def test_many_viewed_real_db(self, tmp_path):
        with parser_context(tmp_path) as (parser, _):
            seen = Item(id=1, title="already")
            fresh = Item(id=2, title="fresh")
            parser.db_handler.add_record_from_page([seen])

            flags = parser._many_viewed([seen, fresh])
            assert flags == [True, False]

    def test_filter_ads_uses_batch_db_check(self, tmp_path):
        with parser_context(tmp_path) as (parser, _):
            seen = Item(id=7, title="seen")
            fresh = Item(id=8, title="fresh")
            parser.db_handler.add_record_from_page([seen])

            result = parser.filter_ads([seen, fresh])
            assert [ad.id for ad in result] == [8]


class TestParseE2E:
    def test_parse_full_cycle(self, tmp_path):
        with parser_context(tmp_path) as (parser, storage_mock):
            ads = [Item(id=11, title="a"), Item(id=12, title="b")]
            parser.fetch_api_data = lambda api_url, page: {"items": list(ads)}
            parser.parse()

            # объявления сохранены и отмечены в БД
            assert parser.db_handler.record_exists(11, 0) is True
            assert parser.db_handler.record_exists(12, 0) is True
            # excel-хранилище получил отфильтрованный список
            storage_mock.save.assert_called_once()
            saved_ads = storage_mock.save.call_args.args[0]
            assert [ad.id for ad in saved_ads] == [11, 12]

    def test_parse_stops_midway_by_stop_event(self, tmp_path):
        event = threading.Event()
        config = make_config(count=3)

        with parser_context(tmp_path, config=config, stop_event=event) as (
            parser,
            storage_mock,
        ):
            pages_data = {
                1: {"items": [Item(id=21, title="p1")]},
                2: {"items": [Item(id=22, title="p2")]},
                3: {"items": [Item(id=23, title="p3")]},
            }

            def pages(api_url, page):
                if page > 1:
                    event.set()  # «Стоп» срабатывает прямо во время загрузки страницы 2
                return pages_data[page]

            parser.fetch_api_data = pages
            parser.parse()

            # страницы 1 и 2 обработаны и сохранены («Стоп» не теряет данные),
            # страница 3 не начиналась: проверка стоп-сигнала стоит до fetch
            assert parser.db_handler.record_exists(21, 0) is True
            assert parser.db_handler.record_exists(22, 0) is True
            assert parser.db_handler.record_exists(23, 0) is False
            assert storage_mock.save.call_count == 2

    def test_fetch_error_propagates(self, tmp_path):
        with parser_context(tmp_path) as (parser, storage_mock):

            def broken(api_url, page):
                raise RuntimeError("network down")

            parser.fetch_api_data = broken
            # ошибка запроса всплывает из parse() (как и раньше), при этом
            # предыдущие страницы уже сохранены
            with pytest.raises(RuntimeError, match="network down"):
                parser.parse()
            storage_mock.save.assert_not_called()


class TestEnrichAds:
    @pytest.fixture(autouse=True)
    def reset_governor(self):
        GOVERNOR._penalty = 0.0
        yield
        GOVERNOR._penalty = 0.0

    @contextmanager
    def _parser_for_enrich(self, tmp_path, parse_views=True):
        config = make_config(parse_views=parse_views)
        with parser_context(tmp_path, config=config) as (parser, _):
            parser.http.fetch_item_data = MagicMock(return_value={
                "success": {"mobile": {"stats": {"views": {"total": 10, "today": 3}}}}
            })
            yield parser

    def test_views_enriched(self, tmp_path):
        with self._parser_for_enrich(tmp_path) as parser:
            ads = [Item(id=31, title="x"), Item(id=32, title="y")]
            with patch("parser_cls.time.sleep") as sleep_mock:
                result = parser.parse_views(ads)

            assert result[0].total_views == 10
            assert result[0].today_views == 3
            assert result[1].total_views == 10
            # пауза между карточками: n-1 раз (без хвостовой паузы)
            assert sleep_mock.call_count == len(ads) - 1

    def test_tail_sleep_skipped_when_governor_active(self, tmp_path):
        with self._parser_for_enrich(tmp_path) as parser:
            GOVERNOR._penalty = 5.0  # регулятор сам ставит паузы перед запросами
            ads = [Item(id=41, title="x"), Item(id=42, title="y")]
            with patch("parser_cls.time.sleep") as sleep_mock:
                parser.parse_views(ads)
            sleep_mock.assert_not_called()

    def test_failed_card_skipped(self, tmp_path):
        with self._parser_for_enrich(tmp_path) as parser:
            parser.http.fetch_item_data.side_effect = RuntimeError("dead")
            ads = [Item(id=51, title="x")]
            with patch("parser_cls.time.sleep"):
                result = parser.parse_views(ads)
            assert result[0].total_views is None  # карточка не получена, объявление живо

    def test_parse_views_disabled(self, tmp_path):
        with self._parser_for_enrich(tmp_path, parse_views=False) as parser:
            parser.http.fetch_item_data = MagicMock()
            ads = [Item(id=61, title="x")]
            result = parser.parse_views(ads)
            parser.http.fetch_item_data.assert_not_called()
            assert result[0].total_views is None
