import gc
import sqlite3
import tempfile
from pathlib import Path

from db_service import SQLiteDBHandler
from models import Item, PriceDetailed


def test_db_service_add_record_with_none_price():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(Path(tmp_dir) / "test_viewed.db")
        SQLiteDBHandler._instance = None
        handler = SQLiteDBHandler(db_name=db_path)

        ad_none_price = Item(id=12345, priceDetailed=None)
        handler.add_record(ad_none_price)

        assert handler.record_exists(record_id=12345, price=0) is True
        assert handler.record_exists(record_id=12345, price=100) is False
        gc.collect()


def test_db_service_add_record_from_page_with_none_price():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(Path(tmp_dir) / "test_page_viewed.db")
        SQLiteDBHandler._instance = None
        handler = SQLiteDBHandler(db_name=db_path)

        ad1 = Item(id=201, priceDetailed=None)
        ad2 = Item(
            id=202,
            priceDetailed=PriceDetailed(
                enabled=True,
                fullString="500 ₽",
                hasValue=True,
                postfix="",
                string="500",
                stringWithoutDiscount=None,
                title={},
                titleDative="",
                value=500,
                wasLowered=False,
                exponent=""
            )
        )

        handler.add_record_from_page([ad1, ad2])

        assert handler.record_exists(record_id=201, price=0) is True
        assert handler.record_exists(record_id=202, price=500) is True
        assert handler.record_exists(record_id=999, price=0) is False
        gc.collect()


def test_db_service_does_not_duplicate_records():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(Path(tmp_dir) / "test_dedup.db")
        SQLiteDBHandler._instance = None
        handler = SQLiteDBHandler(db_name=db_path)

        ad = Item(id=777, priceDetailed=None)
        handler.add_record(ad)
        handler.add_record(ad)
        handler.add_record_from_page([ad, ad])

        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM viewed WHERE id = 777"
            ).fetchone()[0]
        assert count == 1
        gc.collect()


def test_db_service_migrates_existing_duplicates():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(Path(tmp_dir) / "test_legacy.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE viewed (id INTEGER, price INTEGER)")
            conn.executemany(
                "INSERT INTO viewed (id, price) VALUES (?, ?)",
                [(1, 100), (1, 100), (2, 0)],
            )

        SQLiteDBHandler._instance = None
        handler = SQLiteDBHandler(db_name=db_path)

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, price FROM viewed ORDER BY id"
            ).fetchall()
        assert rows == [(1, 100), (2, 0)]
        gc.collect()
