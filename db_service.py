import sqlite3

from models import Item


class SQLiteDBHandler:
    """Работа с БД sqlite"""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(SQLiteDBHandler, cls).__new__(cls)
        return cls._instance

    def __init__(self, db_name="database.db"):
        if not hasattr(self, "_initialized"):
            self.db_name = db_name
            self._create_table()
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_name, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _create_table(self):
        """Создает таблицу viewed и индексы, удаляет старые дубли."""
        with self._connect() as conn:
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
    def _record(ad: Item) -> tuple:
        price = (
            ad.priceDetailed.value
            if (ad.priceDetailed and getattr(ad.priceDetailed, "value", None) is not None)
            else 0
        )
        return ad.id, price

    def add_record(self, ad: Item):
        """Добавляет новую запись в таблицу viewed."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO viewed (id, price) VALUES (?, ?)",
                self._record(ad),
            )
            conn.commit()

    def add_record_from_page(self, ads: list[Item]):
        """Добавляет несколько записей в таблицу viewed."""
        records = [self._record(ad) for ad in ads]

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT OR IGNORE INTO viewed (id, price)
                VALUES (?, ?)
                """,
                records,
            )
            conn.commit()

    def record_exists(self, record_id, price):
        """Проверяет, существует ли запись с заданными id и price."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM viewed WHERE id = ? AND price = ?",
                (record_id, price),
            )
            return cursor.fetchone() is not None
