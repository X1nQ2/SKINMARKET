"""Заполняет базу демонстрационными пользователями, играми и категориями.

Запуск:  python seed_demo.py
Демо-пользователи: seller_demo и buyer_demo, пароль demo12345 (только для учебного стенда!).

Предметы и объявления этим скриптом не создаются: добавить их можно только
из реального инвентаря Steam в разделе «Инвентарь Steam».
"""
from decimal import Decimal

from db import Db
from server import app, hash_password

GAMES = {
    "CS2": ["Винтовки", "Пистолеты", "Ножи", "Перчатки"],
    "Dota 2": ["Герои", "Курьеры", "Наборы"],
    "Rust": ["Оружие", "Одежда"],
}


def get_or_create(db, select_sql, select_params, insert_sql, insert_params):
    row = db.one(select_sql, select_params)
    return row["x"] if row else db.insert(insert_sql, insert_params)


def main():
    db = Db(app.config["DB_PATH"])
    for game, categories in GAMES.items():
        game_id = get_or_create(db, "SELECT КодИгры AS x FROM Игры WHERE НазваниеИгры = ?", (game,),
                                "INSERT INTO Игры (НазваниеИгры) VALUES (?)", (game,))
        for category in categories:
            get_or_create(
                db, "SELECT КодКатегории AS x FROM Категории WHERE КодИгры = ? AND НазваниеКатегории = ?",
                (game_id, category),
                "INSERT INTO Категории (КодИгры, НазваниеКатегории) VALUES (?, ?)", (game_id, category))

    role = db.one("SELECT КодРоли AS x FROM Роли WHERE НазваниеРоли = ?", ("Продавец",))["x"]
    buyer_role = db.one("SELECT КодРоли AS x FROM Роли WHERE НазваниеРоли = ?", ("Покупатель",))["x"]
    password_hash = hash_password("demo12345")

    def user(login, role_id, balance):
        return get_or_create(
            db, "SELECT КодПользователя AS x FROM Пользователи WHERE Логин = ?", (login,),
            "INSERT INTO Пользователи (Логин, Email, ХешПароля, КодРоли, Баланс, ДатаРегистрации, Заблокирован) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (login, f"{login}@example.com", password_hash, role_id, Decimal(balance), db.now(), False))

    user("seller_demo", role, "0.00")
    user("buyer_demo", buyer_role, "100000.00")
    db.commit()
    db.close()
    print("Готово. Демо-вход: seller_demo / buyer_demo, пароль demo12345.")
    print("Предметы добавьте через «Инвентарь Steam» — вручную они больше не создаются.")


if __name__ == "__main__":
    main()
