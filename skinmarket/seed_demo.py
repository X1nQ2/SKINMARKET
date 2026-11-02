"""Заполняет базу демонстрационными данными: игры, категории, скины, продавца и объявления.

Запуск:  python seed_demo.py
Демо-пользователи: seller_demo и buyer_demo, пароль demo12345 (только для учебного стенда!).
"""
from decimal import Decimal

from db import Db
from server import app, hash_password

GAMES = {
    "CS2": ["Винтовки", "Пистолеты", "Ножи", "Перчатки"],
    "Dota 2": ["Герои", "Курьеры", "Наборы"],
    "Rust": ["Оружие", "Одежда"],
}
# (игра, категория, редкость, название, цена, количество)
ITEMS = [
    ("CS2", "Винтовки", "Эпическое", "AK-47 | Redline", "1450.00", 3),
    ("CS2", "Винтовки", "Редкое", "M4A4 | Asiimov", "2100.00", 2),
    ("CS2", "Пистолеты", "Обычное", "Glock-18 | Fade", "890.00", 5),
    ("CS2", "Ножи", "Легендарное", "Karambit | Doppler", "48000.00", 1),
    ("CS2", "Перчатки", "Легендарное", "Sport Gloves | Vice", "36500.00", 1),
    ("Dota 2", "Герои", "Эпическое", "Набор «Пылающий меч»", "720.00", 4),
    ("Dota 2", "Курьеры", "Редкое", "Курьер «Драконёнок»", "310.00", 6),
    ("Rust", "Оружие", "Редкое", "Tempered AK47", "540.00", 3),
    ("Rust", "Одежда", "Обычное", "Куртка «Тундра»", "120.00", 10),
]


def get_or_create(db, select_sql, select_params, insert_sql, insert_params):
    row = db.one(select_sql, select_params)
    return row["x"] if row else db.insert(insert_sql, insert_params)


def main():
    db = Db(app.config["DB_PATH"])
    game_ids, category_ids = {}, {}
    for game, categories in GAMES.items():
        game_ids[game] = get_or_create(db, "SELECT КодИгры AS x FROM Игры WHERE НазваниеИгры = ?", (game,),
                                       "INSERT INTO Игры (НазваниеИгры) VALUES (?)", (game,))
        for category in categories:
            category_ids[(game, category)] = get_or_create(
                db, "SELECT КодКатегории AS x FROM Категории WHERE КодИгры = ? AND НазваниеКатегории = ?",
                (game_ids[game], category),
                "INSERT INTO Категории (КодИгры, НазваниеКатегории) VALUES (?, ?)", (game_ids[game], category))

    role = db.one("SELECT КодРоли AS x FROM Роли WHERE НазваниеРоли = ?", ("Продавец",))["x"]
    buyer_role = db.one("SELECT КодРоли AS x FROM Роли WHERE НазваниеРоли = ?", ("Покупатель",))["x"]
    password_hash = hash_password("demo12345")

    def user(login, role_id, balance):
        return get_or_create(
            db, "SELECT КодПользователя AS x FROM Пользователи WHERE Логин = ?", (login,),
            "INSERT INTO Пользователи (Логин, Email, ХешПароля, КодРоли, Баланс, ДатаРегистрации, Заблокирован) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (login, f"{login}@example.com", password_hash, role_id, Decimal(balance), db.now(), False))

    seller_id = user("seller_demo", role, "0.00")
    user("buyer_demo", buyer_role, "100000.00")
    active = db.one("SELECT КодСтатуса AS x FROM СтатусыОбъявлений WHERE НазваниеСтатуса = ?", ("Активно",))["x"]

    added = 0
    for game, category, rarity, name, price, qty in ITEMS:
        rarity_id = db.one("SELECT КодРедкости AS x FROM Редкости WHERE НазваниеРедкости = ?", (rarity,))["x"]
        item_id = get_or_create(
            db, "SELECT КодТовара AS x FROM Товары WHERE КодИгры = ? AND Название = ?", (game_ids[game], name),
            "INSERT INTO Товары (КодИгры, КодКатегории, КодРедкости, Название, Описание, URLИзображения) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (game_ids[game], category_ids[(game, category)], rarity_id, name, "Демонстрационный товар.", None))
        if not db.one("SELECT КодОбъявления AS x FROM Объявления WHERE КодТовара = ? AND КодПродавца = ?",
                      (item_id, seller_id)):
            db.insert("INSERT INTO Объявления (КодПродавца, КодТовара, Цена, Количество, КодСтатуса, ДатаСоздания) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (seller_id, item_id, Decimal(price), qty, active, db.now()))
            added += 1
    db.commit()
    db.close()
    print(f"Готово. Новых объявлений: {added}. Демо-вход: seller_demo / buyer_demo, пароль demo12345.")


if __name__ == "__main__":
    main()
