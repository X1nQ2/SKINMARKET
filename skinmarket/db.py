"""Тонкий слой доступа к базе.

Основной режим: Microsoft Access (.accdb) через pyodbc.
Для автотестов поддерживается SQLite (файл .sqlite): SQL в проекте написан так,
чтобы работать в обоих движках.
"""
import os
import sqlite3
from datetime import datetime
from decimal import Decimal

try:
    import pyodbc
except ImportError:  # pyodbc нужен только для Access
    pyodbc = None

ACCESS_DRIVER = "Microsoft Access Driver (*.mdb, *.accdb)"

sqlite3.register_adapter(Decimal, float)


def join_sql(base, *joins):
    """Собирает цепочку INNER JOIN в синтаксисе Access (скобки вокруг каждого шага)."""
    sql = base
    for index, (table, on) in enumerate(joins):
        sql = f"{sql} INNER JOIN {table} ON {on}"
        if index < len(joins) - 1:
            sql = f"({sql})"
    return sql


class Db:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.sqlite = self.path.lower().endswith((".sqlite", ".sqlite3"))
        if self.sqlite:
            self.conn = sqlite3.connect(self.path)
            self.conn.execute("PRAGMA foreign_keys = ON")
        else:
            if pyodbc is None:
                raise RuntimeError("Не установлен pyodbc: pip install -r requirements.txt")
            self.conn = pyodbc.connect(
                f"DRIVER={{{ACCESS_DRIVER}}};DBQ={self.path};", autocommit=False
            )

    def now(self):
        value = datetime.now().replace(microsecond=0)
        return value.strftime("%Y-%m-%d %H:%M:%S") if self.sqlite else value

    def _run(self, sql, params):
        cursor = self.conn.cursor()
        cursor.execute(sql, tuple(params))
        return cursor

    def query(self, sql, params=()):
        cursor = self._run(sql, params)
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return rows

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        cursor = self._run(sql, params)
        count = cursor.rowcount
        cursor.close()
        return count

    def insert(self, sql, params=()):
        cursor = self._run(sql, params)
        if self.sqlite:
            new_id = cursor.lastrowid
        else:
            new_id = int(cursor.execute("SELECT @@IDENTITY").fetchone()[0])
        cursor.close()
        return new_id

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
