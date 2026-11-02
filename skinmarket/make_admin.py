"""Создаёт администратора или назначает им существующего пользователя.

Запуск:  python make_admin.py <логин>
"""
import getpass
import re
import sys
from decimal import Decimal

from db import Db
from server import app, hash_password


def main():
    if len(sys.argv) != 2:
        sys.exit("Использование: python make_admin.py <логин>")
    login = sys.argv[1].strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", login):
        sys.exit("Логин: 3–30 символов, латиница, цифры и «_».")

    db = Db(app.config["DB_PATH"])
    role = db.one("SELECT КодРоли AS role_id FROM Роли WHERE НазваниеРоли = ?", ("Администратор",))
    if not role:
        sys.exit("В таблице «Роли» нет роли «Администратор».")

    user = db.one("SELECT КодПользователя AS user_id FROM Пользователи WHERE Логин = ?", (login,))
    if user:
        db.execute("UPDATE Пользователи SET КодРоли = ? WHERE КодПользователя = ?", (role["role_id"], user["user_id"]))
        print(f"Пользователь «{login}» теперь администратор.")
    else:
        email = input("Email: ").strip().lower()
        password = getpass.getpass("Пароль (от 6 символов): ")
        if len(password) < 6 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            sys.exit("Некорректный email или слишком короткий пароль.")
        taken = db.one("SELECT Логин AS login FROM Пользователи WHERE Email = ?", (email,))
        if taken:
            sys.exit(f"Этот email уже занят логином «{taken['login']}». "
                     f"Сделайте его администратором: python make_admin.py {taken['login']}")
        db.insert(
            "INSERT INTO Пользователи (Логин, Email, ХешПароля, КодРоли, Баланс, ДатаРегистрации, Заблокирован) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (login, email, hash_password(password), role["role_id"], Decimal("0.00"), db.now(), False))
        print(f"Администратор «{login}» создан.")
    db.commit()
    db.close()


if __name__ == "__main__":
    main()
