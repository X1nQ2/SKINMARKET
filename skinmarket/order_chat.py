"""Чат по заказу и Steam Trade URL.

Модуль держит схему, проверку ссылки обмена и запросы к сообщениям. Сами
страницы и права доступа описаны в server.py, чтобы не дублировать роли.
"""
import re
from urllib.parse import parse_qs, urlparse

from db import join_sql

TRADE_HOSTS = {"steamcommunity.com", "www.steamcommunity.com"}
TRADE_PATH = "/tradeoffer/new"
MAX_MESSAGE_LEN = 1000
MAX_TRADE_URL_LEN = 255  # в Access короткий текст ограничен 255 символами

_SCHEMA_READY = set()


def _columns(db, table):
    """Колонки таблицы без выборки данных (работает и в Access, и в SQLite)."""
    return db.columns(table)


def _table_exists(db, table):
    return db.has_table(table)


def ensure_chat_schema(db):
    """Добавляет SteamTradeURL и таблицу сообщений, если их ещё нет."""
    key = str(db.path)
    if key in _SCHEMA_READY:
        return
    if "SteamTradeURL" not in _columns(db, "Пользователи"):
        db.execute("ALTER TABLE Пользователи ADD COLUMN SteamTradeURL TEXT")
        # Access применяет DDL только после commit, а проверка таблицы ниже делает rollback.
        db.commit()
    if not _table_exists(db, "СообщенияЗаказов"):
        if db.sqlite:
            db.execute(
                """CREATE TABLE IF NOT EXISTS СообщенияЗаказов (
                    КодСообщения INTEGER PRIMARY KEY AUTOINCREMENT,
                    КодЗаказа INTEGER NOT NULL,
                    КодОтправителя INTEGER NOT NULL,
                    Текст TEXT NOT NULL,
                    ДатаОтправки TEXT,
                    Прочитано INTEGER NOT NULL DEFAULT 0
                )"""
            )
        else:
            db.execute(
                """CREATE TABLE СообщенияЗаказов (
                    КодСообщения COUNTER PRIMARY KEY,
                    КодЗаказа LONG NOT NULL,
                    КодОтправителя LONG NOT NULL,
                    Текст MEMO NOT NULL,
                    ДатаОтправки DATETIME,
                    Прочитано LONG
                )"""
            )
        db.commit()
    _SCHEMA_READY.add(key)


def normalize_trade_url(value):
    """Проверяет ссылку обмена Steam. Возвращает (ссылка, ошибка).

    Пустая строка — это «удалить ссылку»: (\"\", \"\") без ошибки.
    """
    raw = (value or "").strip()
    if not raw:
        return "", ""
    if len(raw) > MAX_TRADE_URL_LEN:
        return None, f"Ссылка слишком длинная (максимум {MAX_TRADE_URL_LEN} символов)."
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw.lstrip("/")
    parsed = urlparse(raw)
    if parsed.netloc.lower() not in TRADE_HOSTS:
        return None, "Ссылка должна вести на steamcommunity.com."
    if parsed.path.rstrip("/") != TRADE_PATH:
        return None, "Нужна ссылка на обмен: steamcommunity.com/tradeoffer/new/?partner=...&token=..."
    params = parse_qs(parsed.query)
    partner = (params.get("partner") or [""])[0].strip()
    token = (params.get("token") or [""])[0].strip()
    if not partner.isdigit():
        return None, "В ссылке нет параметра partner."
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,20}", token):
        return None, "В ссылке нет корректного параметра token."
    return (f"https://steamcommunity.com/tradeoffer/new/?partner={partner}&token={token}", "")


def send_message(db, order_id, user_id, text):
    """Сохраняет сообщение в чат заказа (без commit — его делает вызывающий)."""
    return db.insert(
        "INSERT INTO СообщенияЗаказов "
        "(КодЗаказа, КодОтправителя, Текст, ДатаОтправки, Прочитано) "
        "VALUES (?, ?, ?, ?, ?)",
        (order_id, user_id, text, db.now(), 0),
    )


def order_messages(db, order_id):
    """Сообщения одного заказа от старых к новым."""
    return db.query(
        "SELECT m.КодСообщения AS message_id, m.КодОтправителя AS sender_id, "
        "m.Текст AS msg_text, m.ДатаОтправки AS created_at, m.Прочитано AS is_read "
        "FROM СообщенияЗаказов AS m WHERE m.КодЗаказа = ? ORDER BY m.КодСообщения",
        (order_id,),
    )


def mark_order_read(db, order_id, user_id):
    """Отмечает прочитанными сообщения собеседника в этом заказе."""
    changed = db.execute(
        "UPDATE СообщенияЗаказов SET Прочитано = 1 "
        "WHERE КодЗаказа = ? AND КодОтправителя <> ? AND Прочитано = 0",
        (order_id, user_id),
    )
    if changed:
        db.commit()
    return changed


def unread_count(db, user_id):
    """Сколько непрочитанных сообщений у пользователя во всех заказах."""
    row = db.one(
        "SELECT COUNT(*) AS cnt FROM " + join_sql(
            "СообщенияЗаказов AS m",
            ("Заказы AS z", "m.КодЗаказа = z.КодЗаказа"),
            ("Объявления AS o", "z.КодОбъявления = o.КодОбъявления"),
        ) + " WHERE m.Прочитано = 0 AND m.КодОтправителя <> ? "
            "AND (z.КодПокупателя = ? OR o.КодПродавца = ?)",
        (user_id, user_id, user_id),
    )
    return int(row["cnt"]) if row else 0


def conversations(db, user_id):
    """Последнее сообщение и число непрочитанных по каждому заказу."""
    rows = db.query(
        "SELECT m.КодЗаказа AS order_id, m.КодОтправителя AS sender_id, "
        "m.Текст AS msg_text, m.ДатаОтправки AS created_at, m.Прочитано AS is_read, "
        "z.КодПокупателя AS buyer_id, o.КодПродавца AS seller_id, "
        "t.Название AS item_name, b.Логин AS buyer_login, s.Логин AS seller_login "
        "FROM " + join_sql(
            "СообщенияЗаказов AS m",
            ("Заказы AS z", "m.КодЗаказа = z.КодЗаказа"),
            ("Объявления AS o", "z.КодОбъявления = o.КодОбъявления"),
            ("Товары AS t", "o.КодТовара = t.КодТовара"),
            ("Пользователи AS b", "z.КодПокупателя = b.КодПользователя"),
            ("Пользователи AS s", "o.КодПродавца = s.КодПользователя"),
        ) + " WHERE z.КодПокупателя = ? OR o.КодПродавца = ? "
            "ORDER BY m.КодСообщения DESC",
        (user_id, user_id),
    )
    result = {}
    for row in rows:
        order_id = row["order_id"]
        is_buyer = row["buyer_id"] == user_id
        conversation = result.get(order_id)
        if conversation is None:
            conversation = {
                "order_id": order_id,
                "item_name": row["item_name"],
                "counterpart_id": row["seller_id"] if is_buyer else row["buyer_id"],
                "counterpart_login": row["seller_login"] if is_buyer else row["buyer_login"],
                "last_text": row["msg_text"],
                "last_at": row["created_at"],
                "unread": 0,
            }
            result[order_id] = conversation
        if row["sender_id"] != user_id and not row["is_read"]:
            conversation["unread"] += 1
    return list(result.values())
