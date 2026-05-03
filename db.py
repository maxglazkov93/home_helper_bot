import os
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import mysql.connector


class Database:
    def __init__(self) -> None:
        self.host = os.getenv("MYSQL_HOST", "127.0.0.1")
        self.port = int(os.getenv("MYSQL_PORT", "3306"))
        self.user = os.getenv("MYSQL_USER", "root")
        self.password = os.getenv("MYSQL_PASSWORD", "")
        self.database = os.getenv("MYSQL_DATABASE", "tg_bot_helper")

    def _connect(self):
        return mysql.connector.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            autocommit=True,
        )

    def init_db(self) -> None:
        server_connection = mysql.connector.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            autocommit=True,
        )
        cursor = server_connection.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{self.database}`")
        cursor.close()
        server_connection.close()

        connection = self._connect()
        cursor = connection.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS replacements (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                chat_id BIGINT NOT NULL,
                item_name VARCHAR(255) NOT NULL,
                replaced_at DATE NOT NULL,
                usage_days INT NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_chat_item (chat_id, item_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        try:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME = 'replacements'
                  AND COLUMN_NAME = 'usage_days'
                """,
                (self.database,),
            )
            has_column = cursor.fetchone()[0] > 0
            if not has_column:
                cursor.execute(
                    """
                    ALTER TABLE replacements
                    ADD COLUMN usage_days INT NOT NULL DEFAULT 0
                    """
                )
        except mysql.connector.Error:
            # Если информация о schema недоступна, пытаемся добавить колонку напрямую.
            # Ошибку "Duplicate column" игнорируем, остальные пробрасываем дальше.
            try:
                cursor.execute(
                    """
                    ALTER TABLE replacements
                    ADD COLUMN usage_days INT NOT NULL DEFAULT 0
                    """
                )
            except mysql.connector.Error as exc:
                if exc.errno != 1060:
                    raise
        cursor.close()
        connection.close()

    def upsert_item(
        self, chat_id: int, item_name: str, replaced_at: date, usage_days: int
    ) -> None:
        connection = self._connect()
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO replacements (chat_id, item_name, replaced_at, usage_days)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                replaced_at = VALUES(replaced_at),
                usage_days = VALUES(usage_days),
                updated_at = CURRENT_TIMESTAMP
            """,
            (chat_id, item_name, replaced_at, usage_days),
        )
        cursor.close()
        connection.close()

    def get_items(self, chat_id: int) -> List[str]:
        connection = self._connect()
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT item_name
            FROM replacements
            WHERE chat_id = %s
            ORDER BY item_name ASC
            """,
            (chat_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        connection.close()
        return [row[0] for row in rows]

    def get_item_details(
        self, chat_id: int, item_name: str
    ) -> Optional[Tuple[date, int]]:
        connection = self._connect()
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT replaced_at, usage_days
            FROM replacements
            WHERE chat_id = %s AND item_name = %s
            LIMIT 1
            """,
            (chat_id, item_name),
        )
        row = cursor.fetchone()
        cursor.close()
        connection.close()
        if not row:
            return None
        return row[0], row[1]

    def update_item_replaced_at(
        self, chat_id: int, item_name: str, replaced_at: date
    ) -> bool:
        connection = self._connect()
        cursor = connection.cursor()
        cursor.execute(
            """
            UPDATE replacements
            SET replaced_at = %s, updated_at = CURRENT_TIMESTAMP
            WHERE chat_id = %s AND item_name = %s
            """,
            (replaced_at, chat_id, item_name),
        )
        updated = cursor.rowcount > 0
        cursor.close()
        connection.close()
        return updated

    def get_expired_items_by_chat(self, today: date) -> Dict[int, List[Tuple[str, date, int]]]:
        connection = self._connect()
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT chat_id, item_name, replaced_at, usage_days
            FROM replacements
            WHERE DATE_ADD(replaced_at, INTERVAL usage_days DAY) < %s
            ORDER BY chat_id, item_name
            """,
            (today,),
        )
        rows = cursor.fetchall()
        cursor.close()
        connection.close()

        grouped: Dict[int, List[Tuple[str, date, int]]] = {}
        for chat_id, item_name, replaced_at, usage_days in rows:
            if chat_id not in grouped:
                grouped[chat_id] = []
            grouped[chat_id].append((item_name, replaced_at, usage_days))
        return grouped

    def delete_item(self, chat_id: int, item_name: str) -> bool:
        connection = self._connect()
        cursor = connection.cursor()
        cursor.execute(
            """
            DELETE FROM replacements
            WHERE chat_id = %s AND item_name = %s
            """,
            (chat_id, item_name),
        )
        deleted = cursor.rowcount > 0
        cursor.close()
        connection.close()
        return deleted


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%d.%m.%Y").date()


def parse_usage_days(value: str) -> int:
    raw = value.strip().lower().replace(",", ".")
    match = re.match(
        r"^\s*(\d+)\s*(дн(?:я|ей)?|день|нед(?:еля|ели|ель)?|мес(?:яц|яца|яцев)?)\s*$",
        raw,
    )
    if not match:
        raise ValueError("Некорректный формат срока")

    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        raise ValueError("Срок должен быть больше 0")

    if unit.startswith("д"):
        return amount
    if unit.startswith("нед"):
        return amount * 7
    if unit.startswith("мес"):
        return amount * 30
    raise ValueError("Неизвестная единица срока")


def calculate_due_date(replaced_at: date, usage_days: int) -> date:
    return replaced_at + timedelta(days=usage_days)
