"""Постоянное соединение SQLite: восстановление, однопроцессность, потоки."""
import gc
import sqlite3
import threading
from pathlib import Path

import pytest

from db_service import SQLiteDBHandler
from models import Item


def make_db(tmp_path, name="t.db"):
    SQLiteDBHandler._instance = None
    return SQLiteDBHandler(db_name=str(Path(tmp_path) / name))


class TestPersistentConnection:
    def test_wal_mode_enabled(self, tmp_path):
        db = make_db(tmp_path)
        assert db._get_conn().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        gc.collect()

    def test_recovers_after_closed_connection(self, tmp_path):
        db = make_db(tmp_path)
        db.add_record(Item(id=1, priceDetailed=None))
        db._conn.close()  # соединение «умерло»

        # текущий вызов падает, но сбрасывает соединение
        with pytest.raises(sqlite3.Error):
            db.record_exists(1, 0)

        # следующий вызов автоматически работает с новым соединением
        assert db.record_exists(1, 0) is True
        gc.collect()

    def test_reopens_after_none(self, tmp_path):
        db = make_db(tmp_path)
        db._conn = None
        assert db.record_exists(12345, 0) is False
        gc.collect()

    def test_singleton_reused(self, tmp_path):
        db1 = make_db(tmp_path)
        db2 = SQLiteDBHandler()  # без сброса singleton
        assert db1 is db2
        gc.collect()


class TestThreadSafety:
    def test_concurrent_access_without_errors(self, tmp_path):
        db = make_db(tmp_path)
        errors = []

        def worker(thread_no: int):
            try:
                for i in range(50):
                    ad_id = thread_no * 1000 + i
                    db.record_exists(ad_id, 0)
                    db.add_record(Item(id=ad_id, priceDetailed=None))
                    db.existing_records([(ad_id, 0)])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(k,)) for k in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        gc.collect()


class TestBatchVsPersistence:
    def test_existing_records_roundtrip(self, tmp_path):
        db = make_db(tmp_path)
        ads = [Item(id=i, priceDetailed=None) for i in range(100)]
        db.add_record_from_page(ads)

        records = [SQLiteDBHandler.record_from_ad(ad) for ad in ads]
        existing = db.existing_records(records)
        assert existing == set(records)
        gc.collect()
