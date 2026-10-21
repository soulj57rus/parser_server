"""Тесты пакетной проверки «просмотрено ли» (см. оптимизацию 429/latency)."""
import gc
import tempfile
from pathlib import Path

from db_service import SQLiteDBHandler
from filters.ads_filter import AdsFilter
from models import Item
from dto import AvitoConfig


def _make_db(tmp_dir, name):
    SQLiteDBHandler._instance = None
    return SQLiteDBHandler(db_name=str(Path(tmp_dir) / name))


def test_existing_records_returns_subset():
    with tempfile.TemporaryDirectory() as tmp_dir:
        handler = _make_db(tmp_dir, "batch.db")
        handler.add_record_from_page(
            [
                Item(id=1, priceDetailed=None),
                Item(id=2, priceDetailed=None),
                Item(id=3, priceDetailed=None),
            ]
        )
        records = [(1, 0), (2, 5), (3, 0), (99, 0)]
        existing = handler.existing_records(records)
        assert existing == {(1, 0), (3, 0)}
        gc.collect()


def test_existing_records_empty_input():
    with tempfile.TemporaryDirectory() as tmp_dir:
        handler = _make_db(tmp_dir, "empty.db")
        assert handler.existing_records([]) == set()
        gc.collect()


def test_ads_filter_batch_viewed():
    config = AvitoConfig(urls=[], max_age=0)
    ads = [
        Item(id=1, title="a"),
        Item(id=2, title="b"),
        Item(id=3, title="c"),
    ]
    # id=2 считается просмотренным
    filter_ = AdsFilter(
        config=config,
        is_viewed_many_fn=lambda ads: [ad.id == 2 for ad in ads],
    )
    result = filter_.apply(ads)
    assert [ad.id for ad in result] == [1, 3]


def test_ads_filter_fallback_to_single_fn():
    config = AvitoConfig(urls=[], max_age=0)
    ads = [Item(id=1, title="a"), Item(id=2, title="b")]
    filter_ = AdsFilter(config=config, is_viewed_fn=lambda ad: ad.id == 1)
    assert [ad.id for ad in filter_.apply(ads)] == [2]
