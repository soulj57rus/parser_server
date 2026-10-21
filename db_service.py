import sqlite3
from threading import Lock

from models import Item


class SQLiteDBHandler:
    """Работа с БД sqlite.

    Использует одно постоянное соединение (с WAL-режимом) вместо
    открытия нового на каждый запрос: фильтр «просмотрено» выполняется
    для десятков объявлений на каждой странице, и раньше каждое из них
    открывало и закрывало соединение отдельно.
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(SQLiteDBHandler, cls).__new__(cls)
        return cls._instance

    def __init__(self, db_name="database.db"):
        if not hasattr(self, "_initialized"):
            self.db_name = db_name
            self._lock = Lock()
            self._conn: sqlite3.Connection | None = None
            self._create_table()
            self._initialized = True

    def _get_conn(self) -> sqlite3.Connection:
        """Постоянное соединение; пересоздаётся после сбоя."""
        if self._conn is None:
            self._conn = sqlite3.connect(
                self.db_name, timeout=5, check_same_thread=False
            )
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _discard_conn(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except sqlite3.Error:
            pass
        self._conn = None

    def _create_table(self):
        """Создает таблицу viewed и индексы, удаляет старые дубли."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS viewed (
                    id INTEGER,
                    price INTEGER
                )
                """
            )
            cursor.execute(
                """
                DELETE FROM viewed
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM viewed GROUP BY id, price
                )
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_viewed_id_price
                ON viewed (id, price)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_viewed_id
                ON viewed (id)
                """
            )
            conn.commit()

    @staticmethod
    def record_from_ad(ad: Item) -> tuple:
        price = (
            ad.priceDetailed.value
            if (ad.priceDetailed and getattr(ad.priceDetailed, "value", None) is not None)
            else 0
        )
        return ad.id, price

    # Совместимость со старым именем
    _record = record_from_ad

    def add_record(self, ad: Item):
        """Добавляет новую запись в таблицу viewed."""
        with self._lock:
            try:
                conn = self._get_conn()
                conn.cursor().execute(
                    "INSERT OR IGNORE INTO viewed (id, price) VALUES (?, ?)",
                    self.record_from_ad(ad),
                )
                conn.commit()
            except sqlite3.Error:
                self._discard_conn()
                raise

    def add_record_from_page(self, ads: list[Item]):
        """Добавляет несколько записей в таблицу viewed."""
        records = [self.record_from_ad(ad) for ad in ads]
        with self._lock:
            try:
                conn = self._get_conn()
                conn.cursor().executemany(
                    """
                    INSERT OR IGNORE INTO viewed (id, price)
                    VALUES (?, ?)
                    """,
                    records,
                )
                conn.commit()
            except sqlite3.Error:
                self._discard_conn()
                raise

    def record_exists(self, record_id, price) -> bool:
        """Проверяет, существует ли запись с заданными id и price."""
        with self._lock:
            try:
                cursor = self._get_conn().cursor()
                cursor.execute(
                    "SELECT 1 FROM viewed WHERE id = ? AND price = ?",
                    (record_id, price),
                )
                return cursor.fetchone() is not None
            except sqlite3.Error:
                self._discard_conn()
                raise

    def existing_records(self, records: list[tuple]) -> set[tuple]:
        """Пакетная проверка: какие из записей (id, price) уже есть в БД.

        Вместо N запросов на каждое объявление — один запрос со списком
        пар. Существенно ускоряет фильтрацию на каждой странице.
        """
        if not records:
            return set()

        with self._lock:
            try:
                cursor = self._get_conn().cursor()
                placeholders = ", ".join(["(?, ?)"] * len(records))
                sql = f"SELECT id, price FROM viewed WHERE (id, price) IN ({placeholders})"
                flat = [value for record in records for value in record]
                cursor.execute(sql, flat)
                return {(row[0], row[1]) for row in cursor.fetchall()}
            except sqlite3.Error:
                self._discard_conn()
                raise
