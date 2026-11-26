"""Убирает из базы игры, которые больше не входят в каталог.

Запуск:  python cleanup_games.py
Скрипт безопасен: если в игре уже есть товары/заказы, он не удаляет историю,
а только снимает объявления с продажи.
"""
from db import Db
from games import RETIRED_GAMES
from server import app


def main():
    db = Db(app.config["DB_PATH"])
    try:
        for name in RETIRED_GAMES:
            game = db.one("SELECT КодИгры AS game_id FROM Игры WHERE НазваниеИгры = ?", (name,))
            if not game:
                print(f"{name}: нет в базе")
                continue
            game_id = game["game_id"]
            has_items = db.one("SELECT КодТовара AS item_id FROM Товары WHERE КодИгры = ?", (game_id,))
            if has_items:
                db.execute(
                    "UPDATE Объявления SET КодСтатуса = ? WHERE КодТовара IN "
                    "(SELECT КодТовара FROM Товары WHERE КодИгры = ?)",
                    (lookup_status(db, "Снято"), game_id))
                print(f"{name}: есть товары, объявления сняты с продажи (история сохранена)")
            else:
                db.execute("DELETE FROM Категории WHERE КодИгры = ?", (game_id,))
                db.execute("DELETE FROM Игры WHERE КодИгры = ?", (game_id,))
                print(f"{name}: удалено")
        db.commit()
        print("Готово. Осталось игр:")
        for row in db.query("SELECT КодИгры AS game_id, НазваниеИгры AS game_name FROM Игры ORDER BY КодИгры"):
            print(f"  {row['game_id']}: {row['game_name']}")
    finally:
        db.close()


def lookup_status(db, name):
    row = db.one("SELECT КодСтатуса AS code FROM СтатусыОбъявлений WHERE НазваниеСтатуса = ?", (name,))
    return row["code"] if row else 1


if __name__ == "__main__":
    main()
