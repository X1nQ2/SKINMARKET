"""SKINMARKET: маркетплейс игровых скинов (Flask + Access)."""
import json
import os
import re
import secrets
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from db import Db, join_sql

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.config.update(
    DB_PATH=os.getenv("DB_PATH", str(BASE_DIR / "database.accdb")),
    CARTS_PATH=os.getenv("CARTS_PATH", str(BASE_DIR / "carts.json")),
    MAX_TOPUP=Decimal(os.getenv("MAX_TOPUP", "100000")),
    PLAIN_PASSWORDS=os.getenv("PLAIN_PASSWORDS", "1") == "1",
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    UPLOAD_ROOT=str(BASE_DIR / "static"),
    PER_PAGE=12,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


def load_secret_key():
    path = BASE_DIR / "secret.key"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        key = secrets.token_hex(32)
        try:
            path.write_text(key, encoding="utf-8")
        except OSError:
            pass
        return key


app.secret_key = load_secret_key()

ROLE_BUYER, ROLE_SELLER, ROLE_ADMIN = "Покупатель", "Продавец", "Администратор"
RARITY_CLASS = {"Обычное": "common", "Редкое": "rare", "Эпическое": "epic", "Легендарное": "legendary"}
SORTS = {
    "new": "o.КодОбъявления DESC",
    "price_asc": "o.Цена ASC",
    "price_desc": "o.Цена DESC",
    "name": "t.Название ASC",
}


HASH_PREFIXES = ("scrypt:", "pbkdf2:")


def hash_password(password):
    """PLAIN_PASSWORDS=1 (по умолчанию): пароль хранится как есть, чтобы его было видно в базе.
    PLAIN_PASSWORDS=0: хранится безопасный хеш."""
    if app.config["PLAIN_PASSWORDS"]:
        return password
    return generate_password_hash(password)


def verify_password(stored, password):
    """Понимает оба формата: старые хеши и пароли открытым текстом."""
    if not stored:
        return False
    if stored.startswith(HASH_PREFIXES):
        return check_password_hash(stored, password)
    return secrets.compare_digest(stored.encode("utf-8"), password.encode("utf-8"))


class ShopError(Exception):
    """Ожидаемая ошибка пользователя (показывается во flash)."""


# ---------------------------------------------------------------- база данных
def get_db():
    if "db" not in g:
        g.db = Db(app.config["DB_PATH"])
    return g.db


@app.after_request
def commit_on_success(response):
    db = g.get("db")
    if db is not None and response.status_code < 400:
        db.commit()
    return response


@app.teardown_request
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        if exc is not None:
            db.rollback()
        db.close()


_LOOKUPS = {
    "listing": ("СтатусыОбъявлений", "КодСтатуса", "НазваниеСтатуса"),
    "order": ("СтатусыЗаказов", "КодСтатуса", "НазваниеСтатуса"),
    "role": ("Роли", "КодРоли", "НазваниеРоли"),
}
_lookup_cache = {}


def lookup(kind, name):
    """Код записи справочника по названию (с кэшем)."""
    cache_key = (app.config["DB_PATH"], kind, name)
    if cache_key not in _lookup_cache:
        table, id_col, name_col = _LOOKUPS[kind]
        row = get_db().one(f"SELECT {id_col} AS code FROM {table} WHERE {name_col} = ?", (name,))
        if not row:
            raise RuntimeError(f"В справочнике «{table}» нет значения «{name}».")
        _lookup_cache[cache_key] = row["code"]
    return _lookup_cache[cache_key]


# ----------------------------------------------------------------- утилиты
def D(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


def parse_money(text, hi=Decimal("1000000")):
    try:
        value = D(str(text).strip().replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ShopError("Введите сумму числом.")
    if not (Decimal("0.01") <= value <= hi):
        raise ShopError(f"Сумма должна быть от 0,01 до {hi:,.0f}.".replace(",", " "))
    return value


def to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("catalog")


def valid_image_url(url):
    return bool(re.match(r"^https?://[^\s<>\"']{3,240}$", url))


IMAGE_SIGNATURES = (("png", b"\x89PNG\r\n\x1a\n"), ("jpg", b"\xff\xd8\xff"), ("gif", b"GIF87a"), ("gif", b"GIF89a"))


def detect_image_ext(head):
    for ext, signature in IMAGE_SIGNATURES:
        if head.startswith(signature):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def save_image(storage, folder, name=None):
    """Сохраняет загруженную картинку в static/<folder>. Формат проверяется по содержимому файла.
    Возвращает веб-путь или None, если файл не выбран."""
    if storage is None or not storage.filename:
        return None
    head = storage.stream.read(16)
    storage.stream.seek(0)
    ext = detect_image_ext(head)
    if not ext:
        raise ShopError("Файл должен быть картинкой PNG, JPG, GIF или WEBP (до 5 МБ).")
    directory = Path(app.config["UPLOAD_ROOT"]) / folder
    directory.mkdir(parents=True, exist_ok=True)
    if name:  # логотип игры: старая версия заменяется новой
        for old_file in directory.glob(f"{name}.*"):
            old_file.unlink(missing_ok=True)
    filename = f"{name or secrets.token_hex(8)}.{ext}"
    storage.save(directory / filename)
    return url_for("static", filename=f"{folder}/{filename}")


def read_image(form, files):
    """Картинка товара: загруженный файл важнее ссылки."""
    uploaded = save_image(files.get("image_file"), "skin_images")
    if uploaded:
        return uploaded
    url = form.get("image_url", "").strip()
    if url and not valid_image_url(url):
        raise ShopError("Ссылка на изображение должна начинаться с http:// или https://.")
    return url or None


def slugify(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def game_logo(game):
    """Логотип игры: загруженный (game-<код>.*) или встроенный по названию (cs2.*, dota2.*)."""
    root = Path(app.config["UPLOAD_ROOT"]) / "game_images"
    for stem in (f"game-{game['game_id']}", slugify(game["game_name"])):
        if not stem:
            continue
        for found in sorted(root.glob(f"{stem}.*")):
            return url_for("static", filename=f"game_images/{found.name}") + f"?v={int(found.stat().st_mtime)}"
    return None


@app.template_filter("money")
def money_filter(value):
    text = f"{D(value or 0):,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} ₽"


@app.template_filter("dt")
def dt_filter(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%d.%m.%Y %H:%M") if value else ""


@app.template_filter("rarity_cls")
def rarity_cls(name):
    return RARITY_CLASS.get(name, "common")


@app.template_filter("initials")
def initials(name):
    letters = [w[0] for w in str(name).replace("|", " ").split() if w[:1].isalnum()]
    return "".join(letters[:2]).upper() or "?"


# ------------------------------------------------------------- корзина (JSON)
_cart_lock = threading.Lock()


def _read_carts():
    try:
        with open(app.config["CARTS_PATH"], encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_cart(user_id):
    with _cart_lock:
        return dict(_read_carts().get(str(user_id), {}))


def set_cart(user_id, cart):
    with _cart_lock:
        data = _read_carts()
        if cart:
            data[str(user_id)] = cart
        else:
            data.pop(str(user_id), None)
        path = app.config["CARTS_PATH"]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)


# ------------------------------------------------------ авторизация и CSRF
def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


@app.before_request
def load_user_and_check_csrf():
    if request.endpoint == "static":
        return
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf_token", "")):
            abort(400)
    g.user = None
    user_id = session.get("uid")
    if user_id:
        row = get_db().one(
            "SELECT u.КодПользователя AS user_id, u.Логин AS login, u.Email AS email, "
            "u.Баланс AS balance, u.Заблокирован AS blocked, u.КодРоли AS role_id, "
            "r.НазваниеРоли AS role_name "
            + "FROM " + join_sql("Пользователи AS u", ("Роли AS r", "u.КодРоли = r.КодРоли"))
            + " WHERE u.КодПользователя = ?",
            (user_id,),
        )
        if row and not row["blocked"]:
            g.user = row
        else:
            session.pop("uid", None)


@app.context_processor
def inject_globals():
    cart_count = 0
    if g.get("user"):
        cart_count = sum(get_cart(g.user["user_id"]).values())
    return {"csrf_token": csrf_token, "user": g.get("user"), "cart_count": cart_count,
            "ROLE_SELLER": ROLE_SELLER, "ROLE_ADMIN": ROLE_ADMIN}


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.user:
            flash("Войдите в аккаунт.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            if g.user["role_name"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------- общие запросы
LISTING_FROM = join_sql(
    "Объявления AS o",
    ("Товары AS t", "o.КодТовара = t.КодТовара"),
    ("Игры AS i", "t.КодИгры = i.КодИгры"),
    ("Категории AS k", "t.КодКатегории = k.КодКатегории"),
    ("Редкости AS r", "t.КодРедкости = r.КодРедкости"),
    ("Пользователи AS u", "o.КодПродавца = u.КодПользователя"),
    ("СтатусыОбъявлений AS s", "o.КодСтатуса = s.КодСтатуса"),
)
LISTING_COLS = (
    "o.КодОбъявления AS listing_id, o.Цена AS price, o.Количество AS qty, "
    "o.КодПродавца AS seller_id, o.КодСтатуса AS status_id, o.ДатаСоздания AS created_at, "
    "s.НазваниеСтатуса AS status_name, t.КодТовара AS item_id, t.Название AS item_name, "
    "t.Описание AS item_desc, t.URLИзображения AS image_url, t.КодИгры AS game_id, "
    "t.КодКатегории AS category_id, t.КодРедкости AS rarity_id, i.НазваниеИгры AS game_name, "
    "k.НазваниеКатегории AS category_name, r.НазваниеРедкости AS rarity_name, "
    "u.Логин AS seller_login"
)
ORDER_FROM = join_sql(
    "Заказы AS z",
    ("СтатусыЗаказов AS st", "z.КодСтатуса = st.КодСтатуса"),
    ("Пользователи AS b", "z.КодПокупателя = b.КодПользователя"),
    ("Объявления AS o", "z.КодОбъявления = o.КодОбъявления"),
    ("Товары AS t", "o.КодТовара = t.КодТовара"),
    ("Пользователи AS u", "o.КодПродавца = u.КодПользователя"),
)
ORDER_COLS = (
    "z.КодЗаказа AS order_id, z.Количество AS qty, z.Сумма AS order_sum, "
    "z.ДатаЗаказа AS created_at, z.КодСтатуса AS status_id, st.НазваниеСтатуса AS status_name, "
    "z.КодПокупателя AS buyer_id, b.Логин AS buyer_login, o.КодПродавца AS seller_id, "
    "u.Логин AS seller_login, o.КодОбъявления AS listing_id, t.Название AS item_name"
)


def fetch_listing(db, listing_id):
    return db.one(f"SELECT {LISTING_COLS} FROM {LISTING_FROM} WHERE o.КодОбъявления = ?", (listing_id,))


def reference_data(db):
    games = db.query("SELECT КодИгры AS game_id, НазваниеИгры AS game_name FROM Игры ORDER BY НазваниеИгры")
    for game in games:
        game["logo"] = game_logo(game)
    return {
        "games": games,
        "categories": db.query(
            "SELECT КодКатегории AS category_id, КодИгры AS game_id, НазваниеКатегории AS category_name "
            "FROM Категории ORDER BY НазваниеКатегории"),
        "rarities": db.query("SELECT КодРедкости AS rarity_id, НазваниеРедкости AS rarity_name FROM Редкости ORDER BY КодРедкости"),
    }


def add_transaction(db, user_id, order_id, kind, amount):
    if order_id is None:
        db.execute(
            "INSERT INTO Транзакции (КодПользователя, Тип, Сумма, Дата) VALUES (?, ?, ?, ?)",
            (user_id, kind, amount, db.now()))
    else:
        db.execute(
            "INSERT INTO Транзакции (КодПользователя, КодЗаказа, Тип, Сумма, Дата) VALUES (?, ?, ?, ?, ?)",
            (user_id, order_id, kind, amount, db.now()))


# ----------------------------------------------------------- бизнес-логика
def place_order(db, buyer, listing_id, qty):
    """Покупка: деньги списываются у покупателя и удерживаются до выполнения заказа."""
    if qty < 1 or qty > 9999:
        raise ShopError("Некорректное количество.")
    listing = db.one(
        "SELECT КодПродавца AS seller_id, Цена AS price, Количество AS qty, КодСтатуса AS status_id "
        "FROM Объявления WHERE КодОбъявления = ?", (listing_id,))
    if not listing:
        raise ShopError("Объявление не найдено.")
    active = lookup("listing", "Активно")
    if listing["status_id"] != active or listing["qty"] < 1:
        raise ShopError("Объявление недоступно.")
    if listing["seller_id"] == buyer["user_id"]:
        raise ShopError("Нельзя купить собственный товар.")
    if listing["qty"] < qty:
        raise ShopError(f"В наличии только {listing['qty']} шт.")
    total = D(listing["price"]) * qty

    if db.execute("UPDATE Пользователи SET Баланс = Баланс - ? WHERE КодПользователя = ? AND Баланс >= ?",
                  (total, buyer["user_id"], total)) != 1:
        raise ShopError("Недостаточно средств на балансе. Пополните баланс в профиле.")
    if db.execute("UPDATE Объявления SET Количество = Количество - ? "
                  "WHERE КодОбъявления = ? AND Количество >= ? AND КодСтатуса = ?",
                  (qty, listing_id, qty, active)) != 1:
        raise ShopError("Товар уже раскупили.")
    if listing["qty"] - qty == 0:
        db.execute("UPDATE Объявления SET КодСтатуса = ? WHERE КодОбъявления = ?",
                   (lookup("listing", "Продано"), listing_id))
    order_id = db.insert(
        "INSERT INTO Заказы (КодПокупателя, КодОбъявления, Количество, Сумма, ДатаЗаказа, КодСтатуса) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (buyer["user_id"], listing_id, qty, total, db.now(), lookup("order", "Оплачен")))
    add_transaction(db, buyer["user_id"], order_id, "Покупка", -total)
    return order_id


def fetch_order(db, order_id):
    return db.one(f"SELECT {ORDER_COLS} FROM {ORDER_FROM} WHERE z.КодЗаказа = ?", (order_id,))


def complete_order(db, order):
    if order["status_id"] != lookup("order", "Оплачен"):
        raise ShopError("Заказ уже обработан.")
    total = D(order["order_sum"])
    db.execute("UPDATE Заказы SET КодСтатуса = ? WHERE КодЗаказа = ?",
               (lookup("order", "Выполнен"), order["order_id"]))
    db.execute("UPDATE Пользователи SET Баланс = Баланс + ? WHERE КодПользователя = ?",
               (total, order["seller_id"]))
    add_transaction(db, order["seller_id"], order["order_id"], "Продажа", total)


def cancel_order(db, order):
    if order["status_id"] != lookup("order", "Оплачен"):
        raise ShopError("Отменить можно только оплаченный заказ.")
    total = D(order["order_sum"])
    db.execute("UPDATE Заказы SET КодСтатуса = ? WHERE КодЗаказа = ?",
               (lookup("order", "Отменён"), order["order_id"]))
    db.execute("UPDATE Пользователи SET Баланс = Баланс + ? WHERE КодПользователя = ?",
               (total, order["buyer_id"]))
    add_transaction(db, order["buyer_id"], order["order_id"], "Возврат", total)
    db.execute("UPDATE Объявления SET Количество = Количество + ? WHERE КодОбъявления = ?",
               (order["qty"], order["listing_id"]))
    db.execute("UPDATE Объявления SET КодСтатуса = ? WHERE КодОбъявления = ? AND КодСтатуса = ?",
               (lookup("listing", "Активно"), order["listing_id"], lookup("listing", "Продано")))


# ------------------------------------------------------------------ каталог
@app.get("/")
def catalog():
    db = get_db()
    q = request.args.get("q", "").strip()[:60]
    game_id = to_int(request.args.get("game"))
    category_id = to_int(request.args.get("category"))
    rarity_id = to_int(request.args.get("rarity"))
    sort = request.args.get("sort", "new")
    if sort not in SORTS:
        sort = "new"

    where, params = ["o.КодСтатуса = ?", "o.Количество > 0"], [lookup("listing", "Активно")]
    for column, value in (("t.КодИгры", game_id), ("t.КодКатегории", category_id), ("t.КодРедкости", rarity_id)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    for column, key, op in (("o.Цена", "pmin", ">="), ("o.Цена", "pmax", "<=")):
        raw = request.args.get(key, "").strip()
        if raw:
            try:
                params.append(D(raw.replace(",", ".")))
                where.append(f"{column} {op} ?")
            except (InvalidOperation, ValueError):
                pass
    rows = db.query(
        f"SELECT {LISTING_COLS} FROM {LISTING_FROM} WHERE {' AND '.join(where)} ORDER BY {SORTS[sort]}",
        params)
    if q:
        needle = q.casefold()
        rows = [r for r in rows if needle in r["item_name"].casefold() or needle in r["game_name"].casefold()]

    per_page = app.config["PER_PAGE"]
    pages = max(1, -(-len(rows) // per_page))
    page = min(max(to_int(request.args.get("page"), 1), 1), pages)
    ref = reference_data(db)
    args = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template("catalog.html", listings=rows[(page - 1) * per_page:page * per_page],
                           total=len(rows), page=page, pages=pages, args=args, q=q, sort=sort,
                           f_game=game_id, f_category=category_id, f_rarity=rarity_id,
                           pmin=request.args.get("pmin", ""), pmax=request.args.get("pmax", ""), **ref)


@app.get("/listing/<int:listing_id>")
def listing_page(listing_id):
    db = get_db()
    listing = fetch_listing(db, listing_id)
    if not listing:
        abort(404)
    reviews = db.query(
        "SELECT v.Оценка AS rating, v.Комментарий AS review_text, v.ДатаОтзыва AS created_at, b.Логин AS buyer_login "
        "FROM " + join_sql("Отзывы AS v", ("Заказы AS z", "v.КодЗаказа = z.КодЗаказа"),
                           ("Объявления AS o", "z.КодОбъявления = o.КодОбъявления"),
                           ("Пользователи AS b", "z.КодПокупателя = b.КодПользователя"))
        + " WHERE o.КодПродавца = ? ORDER BY v.КодОтзыва DESC", (listing["seller_id"],))
    rating = round(sum(r["rating"] for r in reviews) / len(reviews), 1) if reviews else None
    available = listing["status_id"] == lookup("listing", "Активно") and listing["qty"] > 0
    return render_template("listing.html", l=listing, reviews=reviews[:10], rating=rating,
                           reviews_count=len(reviews), available=available)


# ------------------------------------------------------ регистрация и вход
@app.route("/register", methods=["GET", "POST"])
def register():
    """Создание новых аккаунтов доступно только через подтверждённый Steam OpenID."""
    if g.user:
        return redirect(url_for("catalog"))
    return redirect(url_for("steam_login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("catalog"))
    next_url = request.values.get("next", "")
    if request.method == "POST":
        db = get_db()
        login_name = request.form.get("login", "").strip()
        row = db.one("SELECT КодПользователя AS user_id, ХешПароля AS pw, Заблокирован AS blocked "
                     "FROM Пользователи WHERE Логин = ?", (login_name,))
        if not row or not verify_password(row["pw"], request.form.get("password", "")):
            flash("Неверный логин или пароль.", "error")
            return render_template("login.html", login=login_name, next=next_url), 400
        if row["blocked"]:
            flash("Аккаунт заблокирован администратором.", "error")
            return render_template("login.html", login=login_name, next=next_url), 403
        session.clear()
        session["uid"] = row["user_id"]
        return redirect(safe_next(next_url))
    return render_template("login.html", login="", next=next_url)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("catalog"))


# ------------------------------------------------------------------ профиль
@app.get("/profile")
@login_required
def profile():
    transactions = get_db().query(
        "SELECT Тип AS trans_type, Сумма AS amount, Дата AS created_at, КодЗаказа AS order_id "
        "FROM Транзакции WHERE КодПользователя = ? ORDER BY КодТранзакции DESC", (g.user["user_id"],))
    return render_template("profile.html", transactions=transactions[:50])


@app.post("/profile/topup")
@login_required
def topup():
    db = get_db()
    try:
        amount = parse_money(request.form.get("amount", ""), hi=app.config["MAX_TOPUP"])
    except ShopError as error:
        flash(str(error), "error")
        return redirect(url_for("profile"))
    db.execute("UPDATE Пользователи SET Баланс = Баланс + ? WHERE КодПользователя = ?", (amount, g.user["user_id"]))
    add_transaction(db, g.user["user_id"], None, "Пополнение", amount)
    flash(f"Баланс пополнен на {money_filter(amount)} (демо-режим).", "success")
    return redirect(url_for("profile"))


@app.post("/profile/withdraw")
@login_required
def withdraw():
    db = get_db()
    try:
        amount = parse_money(request.form.get("amount", ""))
        if db.execute("UPDATE Пользователи SET Баланс = Баланс - ? WHERE КодПользователя = ? AND Баланс >= ?",
                      (amount, g.user["user_id"], amount)) != 1:
            raise ShopError("Недостаточно средств для вывода.")
    except ShopError as error:
        db.rollback()
        flash(str(error), "error")
        return redirect(url_for("profile"))
    add_transaction(db, g.user["user_id"], None, "Вывод", -amount)
    flash(f"Заявка на вывод {money_filter(amount)} выполнена (демо-режим).", "success")
    return redirect(url_for("profile"))


@app.post("/profile/become-seller")
@login_required
def become_seller():
    if g.user["role_name"] == ROLE_BUYER:
        get_db().execute("UPDATE Пользователи SET КодРоли = ? WHERE КодПользователя = ?",
                         (lookup("role", ROLE_SELLER), g.user["user_id"]))
        flash("Теперь вы продавец: можно размещать объявления.", "success")
    return redirect(url_for("sell"))


# ------------------------------------------------------------------- корзина
@app.get("/cart")
@login_required
def cart():
    db = get_db()
    items, total = [], Decimal("0")
    active = lookup("listing", "Активно")
    for key, qty in get_cart(g.user["user_id"]).items():
        listing = fetch_listing(db, int(key))
        if not listing:
            continue
        line = D(listing["price"]) * qty
        ok = listing["status_id"] == active and listing["qty"] >= qty
        listing.update(cart_qty=qty, line=line, ok=ok)
        if ok:
            total += line
        items.append(listing)
    return render_template("cart.html", items=items, total=total, all_ok=all(i["ok"] for i in items))


@app.post("/cart/add/<int:listing_id>")
@login_required
def cart_add(listing_id):
    listing = fetch_listing(get_db(), listing_id)
    qty = to_int(request.form.get("qty"), 1)
    if not listing or listing["status_id"] != lookup("listing", "Активно") or listing["qty"] < 1:
        flash("Объявление недоступно.", "error")
        return redirect(url_for("catalog"))
    if listing["seller_id"] == g.user["user_id"]:
        flash("Нельзя добавить в корзину собственный товар.", "error")
        return redirect(url_for("listing_page", listing_id=listing_id))
    cart_data = get_cart(g.user["user_id"])
    new_qty = min(cart_data.get(str(listing_id), 0) + max(qty, 1), listing["qty"])
    cart_data[str(listing_id)] = new_qty
    set_cart(g.user["user_id"], cart_data)
    flash("Товар добавлен в корзину.", "success")
    return redirect(url_for("cart"))


@app.post("/cart/remove/<int:listing_id>")
@login_required
def cart_remove(listing_id):
    cart_data = get_cart(g.user["user_id"])
    cart_data.pop(str(listing_id), None)
    set_cart(g.user["user_id"], cart_data)
    return redirect(url_for("cart"))


@app.post("/cart/checkout")
@login_required
def cart_checkout():
    db = get_db()
    cart_data = get_cart(g.user["user_id"])
    if not cart_data:
        flash("Корзина пуста.", "error")
        return redirect(url_for("cart"))
    try:
        for key, qty in cart_data.items():
            place_order(db, g.user, int(key), qty)
    except ShopError as error:
        db.rollback()
        flash(f"Заказ не оформлен: {error}", "error")
        return redirect(url_for("cart"))
    set_cart(g.user["user_id"], {})
    flash("Заказ оплачен. Продавец передаст товар, а вы подтвердите получение в разделе «Мои заказы».", "success")
    return redirect(url_for("orders"))


@app.post("/buy/<int:listing_id>")
@login_required
def buy(listing_id):
    db = get_db()
    try:
        place_order(db, g.user, listing_id, to_int(request.form.get("qty"), 1))
    except ShopError as error:
        db.rollback()
        flash(str(error), "error")
        return redirect(url_for("listing_page", listing_id=listing_id))
    flash("Заказ оплачен. Ожидайте передачи товара продавцом.", "success")
    return redirect(url_for("orders"))


# -------------------------------------------------------------------- заказы
@app.get("/orders")
@login_required
def orders():
    db = get_db()
    rows = db.query(f"SELECT {ORDER_COLS} FROM {ORDER_FROM} WHERE z.КодПокупателя = ? ORDER BY z.КодЗаказа DESC",
                    (g.user["user_id"],))
    reviewed = {r["order_id"] for r in db.query("SELECT КодЗаказа AS order_id FROM Отзывы")}
    paid, done = lookup("order", "Оплачен"), lookup("order", "Выполнен")
    for row in rows:
        row.update(can_cancel=row["status_id"] == paid,
                   can_review=row["status_id"] == done and row["order_id"] not in reviewed,
                   reviewed=row["order_id"] in reviewed)
    return render_template("orders.html", orders=rows)


@app.get("/sales")
@roles_required(ROLE_SELLER, ROLE_ADMIN)
def sales():
    rows = get_db().query(f"SELECT {ORDER_COLS} FROM {ORDER_FROM} WHERE o.КодПродавца = ? ORDER BY z.КодЗаказа DESC",
                          (g.user["user_id"],))
    paid = lookup("order", "Оплачен")
    for row in rows:
        row["open"] = row["status_id"] == paid
    return render_template("sales.html", orders=rows)


def order_action(order_id, who):
    db = get_db()
    order = fetch_order(db, order_id)
    if not order:
        abort(404)
    allowed = {"seller": [order["seller_id"]], "any": [order["seller_id"], order["buyer_id"]]}[who]
    if g.user["user_id"] not in allowed:
        abort(403)
    return db, order


@app.post("/orders/<int:order_id>/complete")
@roles_required(ROLE_SELLER, ROLE_ADMIN)
def order_complete(order_id):
    db, order = order_action(order_id, "seller")
    try:
        complete_order(db, order)
        flash("Заказ выполнен, деньги зачислены на ваш баланс.", "success")
    except ShopError as error:
        db.rollback()
        flash(str(error), "error")
    return redirect(url_for("sales"))


@app.post("/orders/<int:order_id>/cancel")
@login_required
def order_cancel(order_id):
    db, order = order_action(order_id, "any")
    try:
        cancel_order(db, order)
        flash("Заказ отменён, деньги возвращены покупателю.", "success")
    except ShopError as error:
        db.rollback()
        flash(str(error), "error")
    return redirect(url_for("orders" if g.user["user_id"] == order["buyer_id"] else "sales"))


@app.post("/orders/<int:order_id>/review")
@login_required
def order_review(order_id):
    db = get_db()
    order = fetch_order(db, order_id)
    if not order:
        abort(404)
    if order["buyer_id"] != g.user["user_id"]:
        abort(403)
    rating = to_int(request.form.get("rating"))
    text = request.form.get("text", "").strip()[:500]
    if order["status_id"] != lookup("order", "Выполнен"):
        flash("Отзыв можно оставить только после выполнения заказа.", "error")
    elif rating is None or not 1 <= rating <= 5:
        flash("Поставьте оценку от 1 до 5.", "error")
    elif db.one("SELECT КодОтзыва AS review_id FROM Отзывы WHERE КодЗаказа = ?", (order_id,)):
        flash("Отзыв на этот заказ уже оставлен.", "error")
    else:
        db.execute("INSERT INTO Отзывы (КодЗаказа, Оценка, Комментарий, ДатаОтзыва) VALUES (?, ?, ?, ?)",
                   (order_id, rating, text or None, db.now()))
        flash("Спасибо за отзыв!", "success")
    return redirect(url_for("orders"))


# ------------------------------------------------------------ кабинет продавца
@app.get("/sell")
@roles_required(ROLE_SELLER, ROLE_ADMIN)
def sell():
    rows = get_db().query(
        f"SELECT {LISTING_COLS} FROM {LISTING_FROM} WHERE o.КодПродавца = ? ORDER BY o.КодОбъявления DESC",
        (g.user["user_id"],))
    return render_template("sell.html", listings=rows, active=lookup("listing", "Активно"))


def create_listing(db, seller_id, item_id, price, qty):
    return db.insert("INSERT INTO Объявления (КодПродавца, КодТовара, Цена, Количество, КодСтатуса, ДатаСоздания) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (seller_id, item_id, price, qty, lookup("listing", "Активно"), db.now()))


def parse_item_form(form, ref):
    """Проверяет поля товара (игра, категория, редкость, название, описание)."""
    game_id, category_id, rarity_id = (to_int(form.get(k)) for k in ("game", "category", "rarity"))
    name = " ".join(form.get("name", "").split())
    description = form.get("description", "").strip()[:1000]
    category = next((c for c in ref["categories"] if c["category_id"] == category_id), None)
    if not any(x["game_id"] == game_id for x in ref["games"]) or not category or category["game_id"] != game_id:
        raise ShopError("Выберите игру и подходящую категорию.")
    if not any(x["rarity_id"] == rarity_id for x in ref["rarities"]):
        raise ShopError("Выберите редкость.")
    if not 2 <= len(name) <= 120:
        raise ShopError("Название: от 2 до 120 символов.")
    return game_id, category_id, rarity_id, name, description


def find_or_create_item(db, game_id, category_id, rarity_id, name, description, image):
    """Один и тот же скин в одной игре — одна запись каталога (с общей картинкой)."""
    item = db.one("SELECT КодТовара AS item_id, URLИзображения AS image_url, Описание AS descr "
                  "FROM Товары WHERE КодИгры = ? AND Название = ?", (game_id, name))
    if item:
        if image and not item["image_url"]:
            db.execute("UPDATE Товары SET URLИзображения = ? WHERE КодТовара = ?", (image, item["item_id"]))
        if description and not item["descr"]:
            db.execute("UPDATE Товары SET Описание = ? WHERE КодТовара = ?", (description, item["item_id"]))
        return item["item_id"], False
    item_id = db.insert(
        "INSERT INTO Товары (КодИгры, КодКатегории, КодРедкости, Название, Описание, URLИзображения) "
        "VALUES (?, ?, ?, ?, ?, ?)", (game_id, category_id, rarity_id, name, description or None, image))
    return item_id, True


def item_names(db):
    return [r["name"] for r in db.query("SELECT DISTINCT Название AS name FROM Товары ORDER BY Название")][:500]


@app.route("/sell/new", methods=["GET", "POST"])
@roles_required(ROLE_SELLER, ROLE_ADMIN)
def sell_new():
    db = get_db()
    ref = reference_data(db)
    ref["item_names"] = item_names(db)
    form = request.form
    if request.method == "POST":
        try:
            game_id, category_id, rarity_id, name, description = parse_item_form(form, ref)
            qty = to_int(form.get("qty"))
            price = parse_money(form.get("price", ""))
            if qty is None or not 1 <= qty <= 9999:
                raise ShopError("Количество: от 1 до 9999.")
            image = read_image(form, request.files)
        except ShopError as error:
            flash(str(error), "error")
            return render_template("sell_new.html", **ref), 400
        item_id, _created = find_or_create_item(db, game_id, category_id, rarity_id, name, description, image)
        create_listing(db, g.user["user_id"], item_id, price, qty)
        flash("Объявление опубликовано.", "success")
        return redirect(url_for("sell"))
    return render_template("sell_new.html", **ref)


def own_listing(db, listing_id):
    row = db.one("SELECT КодПродавца AS seller_id, КодСтатуса AS status_id FROM Объявления WHERE КодОбъявления = ?",
                 (listing_id,))
    if not row:
        abort(404)
    if row["seller_id"] != g.user["user_id"] and g.user["role_name"] != ROLE_ADMIN:
        abort(403)
    return row


@app.post("/sell/<int:listing_id>/update")
@roles_required(ROLE_SELLER, ROLE_ADMIN)
def sell_update(listing_id):
    db = get_db()
    own_listing(db, listing_id)
    try:
        price = parse_money(request.form.get("price", ""))
        qty = to_int(request.form.get("qty"))
        if qty is None or not 0 <= qty <= 9999:
            raise ShopError("Количество: от 0 до 9999.")
    except ShopError as error:
        flash(str(error), "error")
        return redirect(url_for("sell"))
    status = lookup("listing", "Активно") if qty > 0 else lookup("listing", "Продано")
    db.execute("UPDATE Объявления SET Цена = ?, Количество = ?, КодСтатуса = ? WHERE КодОбъявления = ?",
               (price, qty, status, listing_id))
    flash("Объявление обновлено.", "success")
    return redirect(url_for("sell"))


@app.post("/sell/<int:listing_id>/remove")
@roles_required(ROLE_SELLER, ROLE_ADMIN)
def sell_remove(listing_id):
    db = get_db()
    own_listing(db, listing_id)
    db.execute("UPDATE Объявления SET КодСтатуса = ? WHERE КодОбъявления = ?",
               (lookup("listing", "Снято"), listing_id))
    flash("Объявление снято с продажи.", "success")
    return redirect(url_for("sell"))


# --------------------------------------------------------------- админ-панель
@app.get("/admin")
@roles_required(ROLE_ADMIN)
def admin():
    db = get_db()
    users = db.query(
        "SELECT u.КодПользователя AS user_id, u.Логин AS login, u.Email AS email, u.Баланс AS balance, "
        "u.Заблокирован AS blocked, u.ДатаРегистрации AS created_at, u.КодРоли AS role_id, r.НазваниеРоли AS role_name "
        "FROM " + join_sql("Пользователи AS u", ("Роли AS r", "u.КодРоли = r.КодРоли"))
        + " ORDER BY u.КодПользователя")
    listings = db.query(f"SELECT {LISTING_COLS} FROM {LISTING_FROM} ORDER BY o.КодОбъявления DESC")
    orders_all = db.query(f"SELECT {ORDER_COLS} FROM {ORDER_FROM} ORDER BY z.КодЗаказа DESC")
    done = lookup("order", "Выполнен")
    stats = {
        "users": len(users),
        "listings": sum(1 for x in listings if x["status_name"] == "Активно"),
        "orders": len(orders_all),
        "turnover": sum((D(o["order_sum"]) for o in orders_all if o["status_id"] == done), Decimal("0")),
    }
    roles = db.query("SELECT КодРоли AS role_id, НазваниеРоли AS role_name FROM Роли ORDER BY КодРоли")
    return render_template("admin.html", users=users, listings=listings[:50], orders=orders_all[:50],
                           stats=stats, roles=roles)


@app.post("/admin/users/<int:user_id>/block")
@roles_required(ROLE_ADMIN)
def admin_block(user_id):
    db = get_db()
    row = db.one("SELECT Заблокирован AS blocked FROM Пользователи WHERE КодПользователя = ?", (user_id,))
    if not row:
        abort(404)
    if user_id == g.user["user_id"]:
        flash("Нельзя заблокировать самого себя.", "error")
    else:
        db.execute("UPDATE Пользователи SET Заблокирован = ? WHERE КодПользователя = ?", (not row["blocked"], user_id))
        flash("Статус пользователя изменён.", "success")
    return redirect(url_for("admin"))


@app.post("/admin/users/<int:user_id>/role")
@roles_required(ROLE_ADMIN)
def admin_role(user_id):
    db = get_db()
    role_id = to_int(request.form.get("role_id"))
    if user_id == g.user["user_id"]:
        flash("Свою роль менять нельзя.", "error")
    elif not db.one("SELECT КодРоли AS role_id FROM Роли WHERE КодРоли = ?", (role_id,)):
        flash("Неизвестная роль.", "error")
    else:
        db.execute("UPDATE Пользователи SET КодРоли = ? WHERE КодПользователя = ?", (role_id, user_id))
        flash("Роль изменена.", "success")
    return redirect(url_for("admin"))


@app.post("/admin/listings/<int:listing_id>/remove")
@roles_required(ROLE_ADMIN)
def admin_listing_remove(listing_id):
    get_db().execute("UPDATE Объявления SET КодСтатуса = ? WHERE КодОбъявления = ?",
                     (lookup("listing", "Снято"), listing_id))
    flash("Объявление снято.", "success")
    return redirect(url_for("admin"))


def clean_name(limit=60):
    name = " ".join(request.form.get("name", "").split())
    if not 2 <= len(name) <= limit:
        raise ShopError(f"Название: от 2 до {limit} символов.")
    return name


@app.route("/admin/catalog", methods=["GET", "POST"])
@roles_required(ROLE_ADMIN)
def admin_catalog():
    db = get_db()
    form = request.form
    if request.method == "POST":
        kind = form.get("kind")
        try:
            if kind == "game":
                name = clean_name()
                if db.one("SELECT КодИгры AS x FROM Игры WHERE НазваниеИгры = ?", (name,)):
                    raise ShopError("Такая игра уже есть.")
                logo = request.files.get("logo")
                if logo is not None and logo.filename and not detect_image_ext(logo.stream.read(16)):
                    raise ShopError("Логотип должен быть картинкой PNG, JPG, GIF или WEBP (до 5 МБ).")
                if logo is not None:
                    logo.stream.seek(0)
                game_id = db.insert("INSERT INTO Игры (НазваниеИгры) VALUES (?)", (name,))
                save_image(logo, "game_images", name=f"game-{game_id}")
                flash(f"Игра «{name}» добавлена.", "success")
            elif kind == "game_logo":
                game_id = to_int(form.get("game"))
                if not db.one("SELECT КодИгры AS x FROM Игры WHERE КодИгры = ?", (game_id,)):
                    raise ShopError("Выберите игру.")
                if not save_image(request.files.get("logo"), "game_images", name=f"game-{game_id}"):
                    raise ShopError("Выберите файл с логотипом.")
                flash("Логотип обновлён.", "success")
            elif kind == "category":
                name = clean_name()
                game_id = to_int(form.get("game"))
                if not db.one("SELECT КодИгры AS x FROM Игры WHERE КодИгры = ?", (game_id,)):
                    raise ShopError("Выберите игру.")
                if db.one("SELECT КодКатегории AS x FROM Категории WHERE КодИгры = ? AND НазваниеКатегории = ?",
                          (game_id, name)):
                    raise ShopError("Такая категория в этой игре уже есть.")
                db.execute("INSERT INTO Категории (КодИгры, НазваниеКатегории) VALUES (?, ?)", (game_id, name))
                flash("Категория добавлена.", "success")
            elif kind == "rarity":
                name = clean_name(40)
                if db.one("SELECT КодРедкости AS x FROM Редкости WHERE НазваниеРедкости = ?", (name,)):
                    raise ShopError("Такая редкость уже есть.")
                db.execute("INSERT INTO Редкости (НазваниеРедкости) VALUES (?)", (name,))
                flash("Редкость добавлена.", "success")
            elif kind == "item":
                ref = reference_data(db)
                game_id, category_id, rarity_id, name, description = parse_item_form(form, ref)
                price_raw = form.get("price", "").strip()
                price = parse_money(price_raw) if price_raw else None
                qty = to_int(form.get("qty"), 1)
                if price is not None and not 1 <= qty <= 9999:
                    raise ShopError("Количество: от 1 до 9999.")
                image = read_image(form, request.files)
                item_id, created = find_or_create_item(db, game_id, category_id, rarity_id, name, description, image)
                if price is not None:
                    create_listing(db, g.user["user_id"], item_id, price, qty)
                    flash(f"Скин {'добавлен в каталог и ' if created else ''}выставлен на продажу за {money_filter(price)}.",
                          "success")
                else:
                    flash("Скин добавлен в каталог, но на витрине его нет: выставьте его на продажу с ценой."
                          if created else "Такой скин уже есть: картинка и описание дополнены.", "success")
            elif kind == "item_listing":
                item_id = to_int(form.get("item_id"))
                if not db.one("SELECT КодТовара AS x FROM Товары WHERE КодТовара = ?", (item_id,)):
                    raise ShopError("Товар не найден.")
                price = parse_money(form.get("price", ""))
                qty = to_int(form.get("qty"), 1)
                if qty is None or not 1 <= qty <= 9999:
                    raise ShopError("Количество: от 1 до 9999.")
                create_listing(db, g.user["user_id"], item_id, price, qty)
                flash(f"Скин выставлен на продажу за {money_filter(price)}.", "success")
            elif kind == "item_image":
                item_id = to_int(form.get("item_id"))
                if not db.one("SELECT КодТовара AS x FROM Товары WHERE КодТовара = ?", (item_id,)):
                    raise ShopError("Товар не найден.")
                image = read_image(form, request.files)
                if not image:
                    raise ShopError("Выберите файл или вставьте ссылку на картинку.")
                db.execute("UPDATE Товары SET URLИзображения = ? WHERE КодТовара = ?", (image, item_id))
                flash("Картинка товара обновлена.", "success")
            else:
                abort(400)
        except ShopError as error:
            db.rollback()
            flash(str(error), "error")
        return redirect(url_for("admin_catalog", game=form.get("filter_game") or None))

    f_game = to_int(request.args.get("game"))
    where, params = "", []
    if f_game:
        where, params = " WHERE t.КодИгры = ?", [f_game]
    items = db.query(
        "SELECT t.КодТовара AS item_id, t.Название AS item_name, t.URLИзображения AS image_url, "
        "i.НазваниеИгры AS game_name, k.НазваниеКатегории AS category_name, r.НазваниеРедкости AS rarity_name FROM "
        + join_sql("Товары AS t", ("Игры AS i", "t.КодИгры = i.КодИгры"),
                   ("Категории AS k", "t.КодКатегории = k.КодКатегории"),
                   ("Редкости AS r", "t.КодРедкости = r.КодРедкости"))
        + where + " ORDER BY t.КодТовара DESC", params)
    sale = {}
    for row in db.query("SELECT КодТовара AS item_id, Цена AS price, Количество AS qty FROM Объявления "
                        "WHERE КодСтатуса = ? AND Количество > 0", (lookup("listing", "Активно"),)):
        entry = sale.setdefault(row["item_id"], {"qty": 0, "min": None})
        entry["qty"] += row["qty"]
        entry["min"] = row["price"] if entry["min"] is None else min(entry["min"], row["price"])
    for item in items:
        item["sale"] = sale.get(item["item_id"])
    ref = reference_data(db)
    for game in ref["games"]:
        game["cats"] = [c["category_name"] for c in ref["categories"] if c["game_id"] == game["game_id"]]
    return render_template("admin_add.html", items=items[:60], items_total=len(items), f_game=f_game, **ref)


# ------------------------------------------------------- информационные страницы
@app.get("/delivery")
def delivery():
    return render_template("delivery.html")


@app.get("/contacts")
def contacts():
    return render_template("contacts.html")


@app.get("/terms")
def terms():
    return render_template("terms.html")


@app.get("/privacy")
def privacy():
    return render_template("privacy.html")


# ---------------------------------------------------------- администратор по умолчанию
DEFAULT_ADMIN = ("rrrr", "rrrr@skinmarket.local", "123456")

DEFAULT_CATALOG = {
    "CS2": ["Винтовки", "Снайперские винтовки", "Пистолеты", "Пистолеты-пулемёты", "Дробовики", "Ножи", "Перчатки",
            "Наклейки", "Кейсы"],
    "Dota 2": ["Герои", "Курьеры", "Наборы", "Эффекты", "Сундуки"],
    "Rust": ["Оружие", "Одежда", "Постройки", "Инструменты"],
    "Team Fortress 2": ["Оружие", "Шляпы", "Косметика", "Наборы"],
    "PUBG: Battlegrounds": ["Оружие", "Одежда", "Наборы", "Транспорт"],
    "Apex Legends": ["Легенды", "Оружие", "Предметы"],
}


def ensure_catalog_defaults(db):
    """Добавляет CS2 и Dota 2 с категориями, если их ещё нет. Повторный запуск ничего не дублирует."""
    changed = False
    for game, categories in DEFAULT_CATALOG.items():
        row = db.one("SELECT КодИгры AS x FROM Игры WHERE НазваниеИгры = ?", (game,))
        if row:
            game_id = row["x"]
        else:
            game_id = db.insert("INSERT INTO Игры (НазваниеИгры) VALUES (?)", (game,))
            changed = True
        for category in categories:
            if not db.one("SELECT КодКатегории AS x FROM Категории WHERE КодИгры = ? AND НазваниеКатегории = ?",
                          (game_id, category)):
                db.execute("INSERT INTO Категории (КодИгры, НазваниеКатегории) VALUES (?, ?)", (game_id, category))
                changed = True
    if changed:
        db.commit()
    return changed


def ensure_admin(db, login, email, password):
    """Создаёт администратора (или назначает им существующего пользователя). Возвращает True, если что-то изменено."""
    role = db.one("SELECT КодРоли AS role_id FROM Роли WHERE НазваниеРоли = ?", (ROLE_ADMIN,))
    if not role:
        raise RuntimeError("В таблице «Роли» нет роли «Администратор».")
    user = db.one("SELECT КодПользователя AS user_id, КодРоли AS role_id, Заблокирован AS blocked, "
                  "ХешПароля AS pw FROM Пользователи WHERE Логин = ?", (login,))
    if user:
        # в открытом режиме старый хеш заменяется паролем, чтобы он был виден в базе
        legacy_hash = app.config["PLAIN_PASSWORDS"] and str(user["pw"] or "").startswith(HASH_PREFIXES)
        if user["role_id"] == role["role_id"] and not user["blocked"] and not legacy_hash:
            return False
        db.execute("UPDATE Пользователи SET КодРоли = ?, Заблокирован = ?, ХешПароля = ? WHERE КодПользователя = ?",
                   (role["role_id"], False, hash_password(password) if legacy_hash else user["pw"], user["user_id"]))
    else:
        db.insert(
            "INSERT INTO Пользователи (Логин, Email, ХешПароля, КодРоли, Баланс, ДатаРегистрации, Заблокирован) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (login, email, hash_password(password), role["role_id"], Decimal("0.00"), db.now(), False))
    db.commit()
    return True


# ------------------------------------------------------------------- ошибки
ERRORS = {400: "Некорректный запрос (возможно, устарела страница — обновите её).",
          403: "Недостаточно прав для этого действия.",
          404: "Страница не найдена.",
          413: "Файл слишком большой: максимум 5 МБ.",
          500: "Внутренняя ошибка сервера."}


def make_handler(code):
    def handler(_error):
        return render_template("error.html", code=code, message=ERRORS[code]), code
    return handler


for _code in ERRORS:
    app.register_error_handler(_code, make_handler(_code))

from steam_auth import init_steam_auth

init_steam_auth(app, get_db)

if __name__ == "__main__":
    
    _db = Db(app.config["DB_PATH"])
    try:
        if ensure_admin(_db, *DEFAULT_ADMIN):
            print(f"Администратор «{DEFAULT_ADMIN[0]}» записан в базу.")
        if ensure_catalog_defaults(_db):
            print("Игры CS2 и Dota 2 добавлены в каталог.")
    finally:
        _db.close()
    app.run(host="0.0.0.0", port=3000, debug=False)
