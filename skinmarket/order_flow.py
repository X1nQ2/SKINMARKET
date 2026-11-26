"""Состояния заказа: отправка, подтверждение получения и споры.

Сделка проходит так:

    Оплачен → Отправлен → Выполнен
                 ↓
               Спор → Выполнен (выплата продавцу) или Отменён (возврат покупателю)

Деньги удерживаются платформой, пока покупатель не подтвердит получение.
Если подтверждения нет 24 часа, заказ закрывается автоматически.
"""
from datetime import datetime, timedelta

PAID = "Оплачен"
SENT = "Отправлен"
DISPUTE = "Спор"
DONE = "Выполнен"
CANCELLED = "Отменён"

STATUSES = ("Создан", PAID, SENT, DISPUTE, DONE, CANCELLED)
AUTO_CONFIRM_HOURS = 24

_SCHEMA_READY = set()


def ensure_order_schema(db):
    """Добавляет поля сделки и недостающие статусы в старую базу."""
    key = str(db.path)
    if key in _SCHEMA_READY:
        return
    columns = db.columns("Заказы")
    if "ДатаОтправки" not in columns:
        db.execute("ALTER TABLE Заказы ADD COLUMN ДатаОтправки DATETIME")
        # Access применяет DDL только после commit.
        db.commit()
    if "ПричинаСпора" not in columns:
        db.execute("ALTER TABLE Заказы ADD COLUMN ПричинаСпора "
                   + ("TEXT" if db.sqlite else "MEMO"))
        db.commit()
    for name in STATUSES:
        exists = db.one(
            "SELECT КодСтатуса AS code FROM СтатусыЗаказов WHERE НазваниеСтатуса = ?", (name,))
        if not exists:
            db.execute("INSERT INTO СтатусыЗаказов (НазваниеСтатуса) VALUES (?)", (name,))
    db.commit()
    _SCHEMA_READY.add(key)


def as_datetime(value):
    """Приводит дату из Access (datetime) или SQLite (строка) к datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def confirm_deadline(order):
    """Момент автоподтверждения для отправленного заказа."""
    sent_at = as_datetime(order.get("sent_at"))
    if not sent_at:
        return None
    return sent_at + timedelta(hours=AUTO_CONFIRM_HOURS)


def is_due(order, now=None):
    """Пора ли закрывать заказ автоматически (24 часа без подтверждения)."""
    deadline = confirm_deadline(order)
    return bool(deadline and (now or datetime.now()) >= deadline)
