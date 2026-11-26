"""SKINMARKET: маркетплейс игровых скинов (Flask + Access)."""
import csv
import io
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from db import Db, join_sql
from games import ALLOWED_GAMES, RETIRED_GAMES
import order_flow
from order_chat import (MAX_MESSAGE_LEN, MAX_TRADE_URL_LEN, conversations,
                        ensure_chat_schema, mark_order_read, normalize_trade_url,
                        order_messages, send_message, unread_count)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
_db_path_setting = (os.getenv("DB_PATH") or "").strip()
if not _db_path_setting:
    _db_path_setting = str(BASE_DIR / "database.accdb")
_db_path = Path(_db_path_setting).expanduser()
if not _db_path.is_absolute():
    _db_path = BASE_DIR / _db_path
app.config.update(
    DB_PATH=str(_db_path.resolve()),
    CARTS_PATH=os.getenv("CARTS_PATH", str(BASE_DIR / "carts.json")),
    MAX_TOPUP=Decimal(os.getenv("MAX_TOPUP", "100000")),
    PLAIN_PASSWORDS=os.getenv("PLAIN_PASSWORDS", "1") == "1",
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    UPLOAD_ROOT=str(BASE_DIR / "static"),
    PER_PAGE=12,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1",
)


def load_secret_key():
    configured = os.getenv("SECRET_KEY")
    if configured:
        return configured
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


@app.template_filter("date")
def date_filter(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%d.%m.%Y") if value else ""


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
    try:
        ensure_chat_schema(get_db())
        order_flow.ensure_order_schema(get_db())
    except Exception:
        app.logger.exception("Не удалось подготовить таблицы чата, сделки и Steam Trade URL")
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
    # Ленивое автоподтверждение: отдельного планировщика в учебном проекте нет.
    auto_confirm_orders(get_db())


@app.context_processor
def inject_globals():
    cart_count = 0
    unread_messages = 0
    if g.get("user"):
        cart_count = sum(get_cart(g.user["user_id"]).values())
        try:
            unread_messages = unread_count(get_db(), g.user["user_id"])
        except Exception:
            app.logger.debug("Не удалось посчитать непрочитанные сообщения")
    return {"csrf_token": csrf_token, "user": g.get("user"), "cart_count": cart_count,
            "unread_messages": unread_messages,
            "max_message_len": MAX_MESSAGE_LEN, "max_trade_url_len": MAX_TRADE_URL_LEN,
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
    "u.Логин AS seller_login, o.КодОбъявления AS listing_id, t.Название AS item_name, "
    "z.ДатаОтправки AS sent_at, z.ПричинаСпора AS dispute_reason"
)


def fetch_listing(db, listing_id):
    return db.one(f"SELECT {LISTING_COLS} FROM {LISTING_FROM} WHERE o.КодОбъявления = ?", (listing_id,))


INTERNAL_DESCRIPTION_PREFIX = "Предмет из инвентаря Steam"
_DESCRIPTION_RETRY = {}


def attach_steam_profiles(db, rows, id_key, login_key, name_key, avatar_key, url_key):
    """Добавляет к строкам ник, аватар и ссылку Steam по коду пользователя."""
    user_ids = {row[id_key] for row in rows if row.get(id_key)}
    profiles = {}
    if user_ids and not app.config.get("TESTING"):
        for user_id in user_ids:
            try:
                link = db.one("SELECT SteamID FROM SteamAccounts WHERE КодПользователя = ?", (user_id,))
                if link:
                    profiles[user_id] = steam_profile(link["SteamID"])
            except Exception:
                app.logger.debug("Не удалось получить профиль Steam пользователя %s", user_id)
    for row in rows:
        profile = profiles.get(row[id_key], {})
        row[name_key] = profile.get("name") or row.get(login_key) or "Пользователь"
        row[avatar_key] = profile.get("avatar")
        row[url_key] = profile.get("profile_url")
    return rows


def attach_seller_names(db, rows):
    """Ник и аватар продавца для карточек товара и заказов."""
    return attach_steam_profiles(db, rows, "seller_id", "seller_login",
                                 "seller_name", "seller_avatar", "seller_profile_url")


def attach_buyer_names(db, rows):
    """Ник и аватар покупателя для страницы «Заказы покупателей»."""
    return attach_steam_profiles(db, rows, "buyer_id", "buyer_login",
                                 "buyer_name", "buyer_avatar", "buyer_profile_url")


def steam_description_for_listing(db, listing):
    """Возвращает описание предмета из Steam, обновляя старые служебные описания."""
    description = (listing.get("item_desc") or "").strip()
    if description and not description.startswith(INTERNAL_DESCRIPTION_PREFIX):
        return description
    if app.config.get("TESTING"):
        return None
    mapping = db.one(
        "SELECT SteamID, AppID, AssetID FROM SteamInventoryListings WHERE КодОбъявления = ?",
        (listing["listing_id"],),
    )
    if not mapping:
        return None
    key = (str(mapping["SteamID"]), str(mapping["AppID"]), str(mapping["AssetID"]))
    if _DESCRIPTION_RETRY.get(key, 0) > time.time():
        return None
    try:
        items = fetch_game_inventory(mapping["SteamID"], mapping["AppID"])
    except SteamInventoryError:
        _DESCRIPTION_RETRY[key] = time.time() + 300
        return None
    except Exception:
        _DESCRIPTION_RETRY[key] = time.time() + 300
        app.logger.exception("Не удалось загрузить описание предмета из Steam")
        return None
    item = next((x for x in items if x["assetid"] == str(mapping["AssetID"])), None)
    text = (item.get("description") or "").strip() if item else ""
    if text:
        db.execute("UPDATE Товары SET Описание = ? WHERE КодТовара = ?", (text, listing["item_id"]))
        listing["item_desc"] = text
        return text
    _DESCRIPTION_RETRY[key] = time.time() + 300
    return None


def reference_data(db):
    placeholders = ", ".join("?" for _ in ALLOWED_GAMES)
    games = db.query(
        f"SELECT КодИгры AS game_id, НазваниеИгры AS game_name FROM Игры "
        f"WHERE НазваниеИгры IN ({placeholders}) ORDER BY НазваниеИгры",
        tuple(ALLOWED_GAMES))
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


def order_status_css(name):
    """CSS-класс для цветного статуса заказа."""
    return {
        order_flow.PAID: "paid",
        order_flow.SENT: "sent",
        order_flow.DISPUTE: "dispute",
        order_flow.DONE: "done",
        order_flow.CANCELLED: "cancelled",
    }.get(name, "muted")


def mark_order_sent(db, order):
    """Продавец отправил предмет: деньги пока остаются у платформы."""
    paid_id = lookup("order", order_flow.PAID)
    if order["status_id"] != paid_id:
        raise ShopError("Отправить можно только оплаченный заказ.")
    changed = db.execute(
        "UPDATE Заказы SET КодСтатуса = ?, ДатаОтправки = ? "
        "WHERE КодЗаказа = ? AND КодСтатуса = ?",
        (lookup("order", order_flow.SENT), db.now(), order["order_id"], paid_id))
    if changed != 1:
        raise ShopError("Заказ уже обработан.")


def payout_order(db, order, from_status):
    """Закрывает заказ в пользу продавца: деньги уходят на его баланс."""
    total = D(order["order_sum"])
    changed = db.execute(
        "UPDATE Заказы SET КодСтатуса = ? WHERE КодЗаказа = ? AND КодСтатуса = ?",
        (lookup("order", order_flow.DONE), order["order_id"], lookup("order", from_status)))
    if changed != 1:
        raise ShopError("Заказ уже обработан.")
    db.execute("UPDATE Пользователи SET Баланс = Баланс + ? WHERE КодПользователя = ?",
               (total, order["seller_id"]))
    add_transaction(db, order["seller_id"], order["order_id"], "Продажа", total)


def refund_order(db, order, from_status):
    """Закрывает заказ в пользу покупателя.

    Количество не возвращаем: предмет уже мог быть отправлен, повторно
    выставлять его в каталог нельзя.
    """
    total = D(order["order_sum"])
    changed = db.execute(
        "UPDATE Заказы SET КодСтатуса = ? WHERE КодЗаказа = ? AND КодСтатуса = ?",
        (lookup("order", order_flow.CANCELLED), order["order_id"], lookup("order", from_status)))
    if changed != 1:
        raise ShopError("Заказ уже обработан.")
    db.execute("UPDATE Пользователи SET Баланс = Баланс + ? WHERE КодПользователя = ?",
               (total, order["buyer_id"]))
    add_transaction(db, order["buyer_id"], order["order_id"], "Возврат", total)


def open_dispute(db, order, reason):
    """Покупатель открывает спор: выплата замораживается до решения админа."""
    sent_id = lookup("order", order_flow.SENT)
    if order["status_id"] != sent_id:
        raise ShopError("Спор можно открыть только по отправленному заказу.")
    changed = db.execute(
        "UPDATE Заказы SET КодСтатуса = ?, ПричинаСпора = ? "
        "WHERE КодЗаказа = ? AND КодСтатуса = ?",
        (lookup("order", order_flow.DISPUTE), reason or None, order["order_id"], sent_id))
    if changed != 1:
        raise ShopError("Заказ уже обработан.")


def auto_confirm_orders(db):
    """Ленивое автоподтверждение: закрывает заказы, отправленные 24 часа назад."""
    try:
        rows = db.query(
            f"SELECT {ORDER_COLS} FROM {ORDER_FROM} WHERE z.КодСтатуса = ?",
            (lookup("order", order_flow.SENT),))
        now = datetime.now()
        changed = False
        for order in rows:
            if order_flow.is_due(order, now):
                payout_order(db, order, order_flow.SENT)
                changed = True
        if changed:
            db.commit()
    except Exception:
        db.rollback()
        app.logger.exception("Не удалось автоматически подтвердить заказы")


def cancel_order(db, order):
    paid_id = lookup("order", order_flow.PAID)
    if order["status_id"] != paid_id:
        raise ShopError("Отменить можно только оплаченный заказ: после отправки откройте спор.")
    total = D(order["order_sum"])
    db.execute("UPDATE Заказы SET КодСтатуса = ? WHERE КодЗаказа = ?",
               (lookup("order", order_flow.CANCELLED), order["order_id"]))
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

    where = ["i.НазваниеИгры IN (" + ", ".join("?" for _ in ALLOWED_GAMES) + ")",
             "o.КодСтатуса = ?", "o.Количество > 0"]
    params = [*ALLOWED_GAMES, lookup("listing", "Активно")]
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
    attach_seller_names(db, rows)

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
    attach_seller_names(db, [listing])
    description = steam_description_for_listing(db, listing)
    return render_template("listing.html", l=listing, reviews=reviews[:10], rating=rating,
                           reviews_count=len(reviews), available=available,
                           product_description=description)


def seller_reviews(db, seller_id, limit=None):
    """Отзывы о продавце, начиная с последних, вместе с данными покупателя."""
    rows = db.query(
        "SELECT v.Оценка AS rating, v.Комментарий AS review_text, v.ДатаОтзыва AS created_at, "
        "z.КодПокупателя AS buyer_id, b.Логин AS buyer_login "
        "FROM " + join_sql("Отзывы AS v", ("Заказы AS z", "v.КодЗаказа = z.КодЗаказа"),
                           ("Объявления AS o", "z.КодОбъявления = o.КодОбъявления"),
                           ("Пользователи AS b", "z.КодПокупателя = b.КодПользователя"))
        + " WHERE o.КодПродавца = ? ORDER BY v.КодОтзыва DESC", (seller_id,))
    return rows[:limit] if limit else rows


@app.get("/seller/<int:seller_id>")
def seller_page(seller_id):
    """Публичная страница продавца: рейтинг, объявления, отзывы."""
    db = get_db()
    seller = db.one(
        "SELECT u.КодПользователя AS user_id, u.Логин AS login, u.ДатаРегистрации AS created_at, "
        "r.НазваниеРоли AS role_name "
        "FROM " + join_sql("Пользователи AS u", ("Роли AS r", "u.КодРоли = r.КодРоли"))
        + " WHERE u.КодПользователя = ?", (seller_id,))
    if not seller:
        abort(404)
    seller["seller_id"] = seller["user_id"]
    seller["seller_login"] = seller["login"]
    attach_seller_names(db, [seller])
    # Технический логин Steam не показываем, если профиль Steam не удалось получить.
    if seller["seller_name"] == seller["login"] and str(seller["login"]).startswith("steam_"):
        seller["seller_name"] = "Продавец"

    listings = db.query(
        f"SELECT {LISTING_COLS} FROM {LISTING_FROM} "
        "WHERE o.КодПродавца = ? AND o.КодСтатуса = ? AND o.Количество > 0 "
        "ORDER BY o.КодОбъявления DESC",
        (seller_id, lookup("listing", "Активно")))
    attach_seller_names(db, listings)

    reviews = seller_reviews(db, seller_id)
    reviews_count = len(reviews)
    rating = round(sum(r["rating"] for r in reviews) / reviews_count, 1) if reviews_count else None
    attach_buyer_names(db, reviews)
    deals = db.one(
        "SELECT COUNT(*) AS cnt FROM " + join_sql(
            "Заказы AS z", ("Объявления AS o", "z.КодОбъявления = o.КодОбъявления"))
        + " WHERE o.КодПродавца = ? AND z.КодСтатуса = ?",
        (seller_id, lookup("order", "Выполнен")))["cnt"]

    contact_url = seller["seller_profile_url"] or url_for("contacts")
    return render_template(
        "seller.html", seller=seller, listings=listings, reviews=reviews[:20],
        rating=rating, reviews_count=reviews_count, deals=deals,
        contact_url=contact_url, contact_external=bool(seller["seller_profile_url"]),
    )


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
    db = get_db()
    transactions = db.query(
        "SELECT Тип AS trans_type, Сумма AS amount, Дата AS created_at, КодЗаказа AS order_id "
        "FROM Транзакции WHERE КодПользователя = ? ORDER BY КодТранзакции DESC", (g.user["user_id"],))
    trade = db.one("SELECT SteamTradeURL AS trade_url FROM Пользователи WHERE КодПользователя = ?",
                   (g.user["user_id"],))
    return render_template("profile.html", transactions=transactions[:50],
                           trade_url=(trade or {}).get("trade_url") or "")


@app.post("/profile/trade-url")
@login_required
def profile_trade_url():
    url, error = normalize_trade_url(request.form.get("trade_url", ""))
    if error:
        flash(error, "error")
        return redirect(url_for("profile"))
    get_db().execute("UPDATE Пользователи SET SteamTradeURL = ? WHERE КодПользователя = ?",
                     (url or None, g.user["user_id"]))
    flash("Steam Trade URL сохранён." if url else "Steam Trade URL удалён.", "success")
    return redirect(url_for("profile"))


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
    attach_seller_names(db, items)
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
    paid, sent = lookup("order", order_flow.PAID), lookup("order", order_flow.SENT)
    done = lookup("order", order_flow.DONE)
    for row in rows:
        row.update(can_cancel=row["status_id"] == paid,
                   can_confirm=row["status_id"] == sent,
                   can_dispute=row["status_id"] == sent,
                   can_review=row["status_id"] == done and row["order_id"] not in reviewed,
                   reviewed=row["order_id"] in reviewed,
                   status_css=order_status_css(row["status_name"]),
                   auto_confirm_at=order_flow.confirm_deadline(row))
    attach_seller_names(db, rows)
    return render_template("orders.html", orders=rows)


@app.get("/sales")
@roles_required(ROLE_SELLER, ROLE_ADMIN)
def sales():
    db = get_db()
    rows = db.query(f"SELECT {ORDER_COLS} FROM {ORDER_FROM} WHERE o.КодПродавца = ? ORDER BY z.КодЗаказа DESC",
                    (g.user["user_id"],))
    attach_buyer_names(db, rows)
    paid = lookup("order", order_flow.PAID)
    sent = lookup("order", order_flow.SENT)
    for row in rows:
        row["can_send"] = row["status_id"] == paid
        row["open"] = row["can_send"]
        row["awaiting"] = row["status_id"] == sent
        row["status_css"] = order_status_css(row["status_name"])
        row["auto_confirm_at"] = order_flow.confirm_deadline(row)
    return render_template("sales.html", orders=rows)


@app.get("/orders/<int:order_id>")
@login_required
def order_detail(order_id):
    db, order = order_action(order_id, "any")
    mark_order_read(db, order_id, g.user["user_id"])
    messages = order_messages(db, order_id)
    attach_seller_names(db, [order])
    attach_buyer_names(db, [order])
    is_buyer = g.user["user_id"] == order["buyer_id"]
    trade = db.one("SELECT SteamTradeURL AS trade_url FROM Пользователи WHERE КодПользователя = ?",
                   (order["seller_id"],))
    trade_url = (trade or {}).get("trade_url") or ""
    can_send = not is_buyer and order["status_id"] == lookup("order", order_flow.PAID)
    can_confirm = is_buyer and order["status_id"] == lookup("order", order_flow.SENT)
    if is_buyer:
        counterpart_name = order["seller_name"]
        counterpart_avatar = order["seller_avatar"]
        counterpart_login = order["seller_login"]
        seller_page_url = url_for("seller_page", seller_id=order["seller_id"])
    else:
        counterpart_name = order["buyer_name"]
        counterpart_avatar = order["buyer_avatar"]
        counterpart_login = order["buyer_login"]
        seller_page_url = None
    return render_template(
        "order_detail.html", o=order, messages=messages, is_buyer=is_buyer,
        trade_url=trade_url, counterpart_name=counterpart_name,
        counterpart_avatar=counterpart_avatar, counterpart_login=counterpart_login,
        seller_page_url=seller_page_url, can_send=can_send, can_confirm=can_confirm,
        can_dispute=can_confirm, status_css=order_status_css(order["status_name"]),
        auto_confirm_at=order_flow.confirm_deadline(order)
        if order["status_id"] == lookup("order", order_flow.SENT) else None,
    )


@app.post("/orders/<int:order_id>/messages")
@login_required
def order_send_message(order_id):
    db, order = order_action(order_id, "any")
    text = request.form.get("text", "").strip()
    if not text:
        flash("Введите сообщение.", "error")
    elif len(text) > MAX_MESSAGE_LEN:
        flash(f"Сообщение слишком длинное: максимум {MAX_MESSAGE_LEN} символов.", "error")
    else:
        send_message(db, order_id, g.user["user_id"], text)
    return redirect(url_for("order_detail", order_id=order_id, _anchor="chat"))


@app.get("/messages")
@login_required
def messages():
    db = get_db()
    rows = conversations(db, g.user["user_id"])
    attach_steam_profiles(db, rows, "counterpart_id", "counterpart_login",
                          "counterpart_name", "counterpart_avatar",
                          "counterpart_profile_url")
    return render_template("messages.html", conversations=rows)


def order_action(order_id, who):
    db = get_db()
    order = fetch_order(db, order_id)
    if not order:
        abort(404)
    allowed = {
        "seller": [order["seller_id"]],
        "buyer": [order["buyer_id"]],
        "any": [order["seller_id"], order["buyer_id"]],
    }[who]
    if g.user["user_id"] not in allowed:
        abort(403)
    return db, order


@app.post("/orders/<int:order_id>/send")
@roles_required(ROLE_SELLER, ROLE_ADMIN)
def order_send(order_id):
    """Продавец отметил, что предмет отправлен. Деньги ещё удерживаются."""
    db, order = order_action(order_id, "seller")
    try:
        mark_order_sent(db, order)
        flash("Предмет отправлен. Покупатель должен подтвердить получение в течение 24 часов.", "success")
    except ShopError as error:
        db.rollback()
        flash(str(error), "error")
    return redirect(url_for("sales"))


@app.post("/orders/<int:order_id>/confirm")
@login_required
def order_confirm(order_id):
    """Покупатель подтвердил получение — деньги переходят продавцу."""
    db, order = order_action(order_id, "buyer")
    try:
        payout_order(db, order, order_flow.SENT)
        flash("Получение подтверждено, деньги переведены продавцу.", "success")
    except ShopError as error:
        db.rollback()
        flash(str(error), "error")
    return redirect(url_for("orders"))


@app.post("/orders/<int:order_id>/dispute")
@login_required
def order_dispute(order_id):
    """Покупатель открывает спор: заказ уходит на рассмотрение администратору."""
    db, order = order_action(order_id, "buyer")
    reason = " ".join((request.form.get("reason") or "").split())[:500]
    try:
        if len(reason) < 5:
            raise ShopError("Опишите проблему: от 5 до 500 символов.")
        open_dispute(db, order, reason)
        flash("Спор открыт. Администратор проверит заказ и решит, кому вернуть деньги.", "success")
    except ShopError as error:
        db.rollback()
        flash(str(error), "error")
    return redirect(url_for("order_detail", order_id=order_id, _anchor="dispute"))


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
    db = get_db()
    rows = db.query(
        f"SELECT {LISTING_COLS} FROM {LISTING_FROM} WHERE o.КодПродавца = ? ORDER BY o.КодОбъявления DESC",
        (g.user["user_id"],))
    categories = db.query(
        "SELECT КодКатегории AS category_id, КодИгры AS game_id, НазваниеКатегории AS category_name "
        "FROM Категории ORDER BY НазваниеКатегории")
    by_game = {}
    for category in categories:
        by_game.setdefault(category["game_id"], []).append(category)
    for row in rows:
        row["categories"] = by_game.get(row["game_id"], [])
    rarities = db.query("SELECT НазваниеРедкости AS rarity_name FROM Редкости ORDER BY НазваниеРедкости")
    return render_template("sell.html", listings=rows, active=lookup("listing", "Активно"),
                           rarity_options=[r["rarity_name"] for r in rarities])


def own_listing(db, listing_id):
    row = db.one(f"SELECT {LISTING_COLS} FROM {LISTING_FROM} WHERE o.КодОбъявления = ?",
                 (listing_id,))
    if not row:
        abort(404)
    if row["seller_id"] != g.user["user_id"] and g.user["role_name"] != ROLE_ADMIN:
        abort(403)
    return row


@app.get("/sell/new")
@roles_required(ROLE_SELLER, ROLE_ADMIN)
def sell_new():
    """Новые объявления создаются только из реального инвентаря Steam."""
    linked = get_db().one("SELECT SteamID FROM SteamAccounts WHERE КодПользователя = ?", (g.user["user_id"],))
    if linked:
        return redirect(url_for("steam_inventory_page"))
    flash("Сначала привяжите аккаунт Steam, чтобы выбрать предмет из инвентаря.", "error")
    return redirect(url_for("profile"))


@app.post("/sell/<int:listing_id>/update")
@roles_required(ROLE_SELLER, ROLE_ADMIN)
def sell_update(listing_id):
    db = get_db()
    listing = own_listing(db, listing_id)
    changes = []

    raw_price = (request.form.get("price") or "").strip()
    if raw_price:
        try:
            price = parse_money(raw_price)
        except ShopError as error:
            flash(str(error), "error")
            return redirect(url_for("sell"))
        # Количество не меняем: один предмет из инвентаря Steam — одно объявление.
        db.execute("UPDATE Объявления SET Цена = ? WHERE КодОбъявления = ?", (price, listing_id))
        changes.append("цена")

    category_name = " ".join((request.form.get("category") or "").split())[:40]
    if category_name and category_name != listing["category_name"]:
        category = db.one(
            "SELECT КодКатегории AS id FROM Категории WHERE КодИгры = ? AND НазваниеКатегории = ?",
            (listing["game_id"], category_name),
        )
        category_id = category["id"] if category else db.insert(
            "INSERT INTO Категории (КодИгры, НазваниеКатегории) VALUES (?, ?)",
            (listing["game_id"], category_name))
        db.execute("UPDATE Товары SET КодКатегории = ? WHERE КодТовара = ?",
                   (category_id, listing["item_id"]))
        changes.append("категория")

    rarity_name = (request.form.get("rarity") or "").strip()
    if rarity_name and rarity_name != listing["rarity_name"]:
        rarity = db.one("SELECT КодРедкости AS id FROM Редкости WHERE НазваниеРедкости = ?", (rarity_name,))
        if rarity:
            db.execute("UPDATE Товары SET КодРедкости = ? WHERE КодТовара = ?",
                       (rarity["id"], listing["item_id"]))
            changes.append("редкость")

    if changes:
        flash("Обновлено: " + ", ".join(changes) + ".", "success")
    else:
        flash("Изменений нет.", "error")
    return redirect(url_for("sell"))


@app.post("/sell/<int:listing_id>/remove")
@roles_required(ROLE_SELLER, ROLE_ADMIN)
def sell_remove(listing_id):
    db = get_db()
    own_listing(db, listing_id)
    db.execute("UPDATE Объявления SET КодСтатуса = ? WHERE КодОбъявления = ?",
               (lookup("listing", "Снято"), listing_id))
    # Освобождаем предмет: после снятия его можно снова добавить из инвентаря.
    try:
        db.execute("DELETE FROM SteamInventoryListings WHERE КодОбъявления = ?", (listing_id,))
    except Exception:
        pass  # таблицы может не быть, если объявление создано старым способом
    flash("Объявление снято с продажи. Предмет снова доступен в инвентаре Steam.", "success")
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
    done = lookup("order", order_flow.DONE)
    dispute_id = lookup("order", order_flow.DISPUTE)
    completed = [order for order in orders_all if order["status_id"] == done]
    disputes = [order for order in orders_all if order["status_id"] == dispute_id]
    for order in orders_all:
        order["status_css"] = order_status_css(order["status_name"])
    attach_buyer_names(db, disputes)
    attach_seller_names(db, disputes)
    turnover = sum((D(order["order_sum"]) for order in completed), Decimal("0"))
    stats = {
        "users": len(users),
        "active_users": sum(1 for user in users if not user["blocked"]),
        "listings": sum(1 for x in listings if x["status_name"] == "Активно"),
        "orders": len(orders_all),
        "completed": len(completed),
        "turnover": turnover,
        "avg_check": turnover / len(completed) if completed else Decimal("0"),
    }

    # Продажи за последние 14 дней — считаем в Python, чтобы одинаково работать в Access и SQLite.
    today = datetime.now().date()
    buckets = {
        today - timedelta(days=offset): {"count": 0, "sum": Decimal("0")}
        for offset in range(13, -1, -1)
    }
    for order in completed:
        created = order["created_at"]
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except ValueError:
                continue
        if not created:
            continue
        day = created.date()
        if day in buckets:
            buckets[day]["count"] += 1
            buckets[day]["sum"] += D(order["order_sum"])
    max_day_sum = max((bucket["sum"] for bucket in buckets.values()), default=Decimal("0"))
    sales_by_day = [
        {
            "label": day.strftime("%d.%m"),
            "count": bucket["count"],
            "sum": bucket["sum"],
            "height": int(round(float(bucket["sum"]) * 100 / float(max_day_sum))) if max_day_sum else 0,
        }
        for day, bucket in buckets.items()
    ]

    game_stats = db.query(
        "SELECT i.НазваниеИгры AS game_name, COUNT(*) AS sales_count, SUM(z.Сумма) AS revenue "
        "FROM " + join_sql(
            "Заказы AS z",
            ("Объявления AS o", "z.КодОбъявления = o.КодОбъявления"),
            ("Товары AS t", "o.КодТовара = t.КодТовара"),
            ("Игры AS i", "t.КодИгры = i.КодИгры"),
        ) + " WHERE z.КодСтатуса = ? GROUP BY i.НазваниеИгры",
        (done,),
    )
    game_stats.sort(key=lambda row: float(row["revenue"] or 0), reverse=True)
    for row in game_stats:
        row["revenue"] = D(row["revenue"] or 0)
        row["share"] = int(round(float(row["revenue"]) * 100 / float(turnover))) if turnover else 0

    seller_stats = db.query(
        "SELECT o.КодПродавца AS seller_id, u.Логин AS seller_login, "
        "COUNT(*) AS sales_count, SUM(z.Сумма) AS revenue "
        "FROM " + join_sql(
            "Заказы AS z",
            ("Объявления AS o", "z.КодОбъявления = o.КодОбъявления"),
            ("Пользователи AS u", "o.КодПродавца = u.КодПользователя"),
        ) + " WHERE z.КодСтатуса = ? GROUP BY o.КодПродавца, u.Логин",
        (done,),
    )
    seller_stats.sort(key=lambda row: float(row["revenue"] or 0), reverse=True)
    seller_stats = seller_stats[:5]
    for row in seller_stats:
        row["revenue"] = D(row["revenue"] or 0)
    attach_seller_names(db, seller_stats)

    status_stats = db.query(
        "SELECT st.НазваниеСтатуса AS status_name, COUNT(*) AS cnt "
        "FROM " + join_sql("Заказы AS z", ("СтатусыЗаказов AS st", "z.КодСтатуса = st.КодСтатуса"))
        + " GROUP BY st.НазваниеСтатуса")
    status_stats.sort(key=lambda row: row["cnt"], reverse=True)

    roles = db.query("SELECT КодРоли AS role_id, НазваниеРоли AS role_name FROM Роли ORDER BY КодРоли")
    return render_template(
        "admin.html", users=users, listings=listings[:50], orders=orders_all[:50],
        stats=stats, roles=roles, sales_by_day=sales_by_day, game_stats=game_stats,
        seller_stats=seller_stats, status_stats=status_stats, disputes=disputes,
    )


def csv_response(filename, header, rows):
    """CSV с BOM и «;» — Excel открывает без настройки разделителя."""
    buffer = io.StringIO()
    buffer.write("\ufeff")
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        buffer.getvalue(),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/admin/export/orders.csv")
@roles_required(ROLE_ADMIN)
def admin_export_orders():
    rows = get_db().query(f"SELECT {ORDER_COLS} FROM {ORDER_FROM} ORDER BY z.КодЗаказа DESC")
    data = [
        (row["order_id"], row["item_name"], row["buyer_login"], row["seller_login"],
         row["qty"], row["order_sum"], row["created_at"], row["status_name"])
        for row in rows
    ]
    header = ["№", "Товар", "Покупатель", "Продавец", "Количество", "Сумма", "Дата", "Статус"]
    return csv_response("orders.csv", header, data)


@app.get("/admin/export/users.csv")
@roles_required(ROLE_ADMIN)
def admin_export_users():
    rows = get_db().query(
        "SELECT u.КодПользователя AS user_id, u.Логин AS login, u.Email AS email, u.Баланс AS balance, "
        "u.Заблокирован AS blocked, u.ДатаРегистрации AS created_at, r.НазваниеРоли AS role_name "
        "FROM " + join_sql("Пользователи AS u", ("Роли AS r", "u.КодРоли = r.КодРоли"))
        + " ORDER BY u.КодПользователя")
    data = [
        (row["user_id"], row["login"], row["email"], row["balance"], row["role_name"],
         "да" if row["blocked"] else "нет", row["created_at"])
        for row in rows
    ]
    header = ["№", "Логин", "Email", "Баланс", "Роль", "Заблокирован", "Дата регистрации"]
    return csv_response("users.csv", header, data)


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


@app.post("/admin/orders/<int:order_id>/refund")
@roles_required(ROLE_ADMIN)
def admin_order_refund(order_id):
    """Решение спора в пользу покупателя: деньги возвращаются на его баланс."""
    db = get_db()
    order = fetch_order(db, order_id)
    if not order:
        abort(404)
    try:
        refund_order(db, order, order_flow.DISPUTE)
        flash(f"Спор по заказу №{order_id} решён: деньги возвращены покупателю.", "success")
    except ShopError as error:
        db.rollback()
        flash(str(error), "error")
    return redirect(url_for("admin", _anchor="disputes"))


@app.post("/admin/orders/<int:order_id>/payout")
@roles_required(ROLE_ADMIN)
def admin_order_payout(order_id):
    """Решение спора в пользу продавца: деньги уходят на его баланс."""
    db = get_db()
    order = fetch_order(db, order_id)
    if not order:
        abort(404)
    try:
        payout_order(db, order, order_flow.DISPUTE)
        flash(f"Спор по заказу №{order_id} решён: деньги переведены продавцу.", "success")
    except ShopError as error:
        db.rollback()
        flash(str(error), "error")
    return redirect(url_for("admin", _anchor="disputes"))


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
                if name not in ALLOWED_GAMES:
                    raise ShopError("В каталог можно добавлять только CS2, Dota 2 и Rust.")
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
            elif kind in ("item", "item_listing"):
                raise ShopError("Товары добавляются только из инвентаря Steam: "
                                "профиль → «Инвентарь Steam» → выберите предмет и укажите цену.")
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
    allowed_placeholders = ", ".join("?" for _ in ALLOWED_GAMES)
    where, params = f" WHERE i.НазваниеИгры IN ({allowed_placeholders})", [*ALLOWED_GAMES]
    if f_game:
        where += " AND t.КодИгры = ?"
        params.append(f_game)
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


@app.get("/faq")
def faq():
    return render_template("faq.html")


# ---------------------------------------------------------- администратор по умолчанию
DEFAULT_ADMIN = ("rrrr", "rrrr@skinmarket.local", "123456")

DEFAULT_CATALOG = {
    "CS2": ["Винтовки", "Снайперские винтовки", "Пистолеты", "Пистолеты-пулемёты", "Дробовики", "Ножи", "Перчатки",
            "Наклейки", "Кейсы"],
    "Dota 2": ["Герои", "Курьеры", "Наборы", "Эффекты", "Сундуки"],
    "Rust": ["Оружие", "Одежда", "Постройки", "Инструменты"],
}


def retire_games(db):
    """Убирает игры, которые больше не входят в каталог.

    Если в игре уже есть товары/заказы, историю не удаляем — просто снимаем
    её объявления с продажи. Игры без товаров удаляются вместе с категориями.
    """
    changed = False
    for name in RETIRED_GAMES:
        game = db.one("SELECT КодИгры AS game_id FROM Игры WHERE НазваниеИгры = ?", (name,))
        if not game:
            continue
        game_id = game["game_id"]
        has_items = db.one("SELECT КодТовара AS item_id FROM Товары WHERE КодИгры = ?", (game_id,))
        if has_items:
            db.execute(
                "UPDATE Объявления SET КодСтатуса = ? WHERE КодТовара IN "
                "(SELECT КодТовара FROM Товары WHERE КодИгры = ?)",
                (lookup("listing", "Снято"), game_id))
        else:
            db.execute("DELETE FROM Категории WHERE КодИгры = ?", (game_id,))
            db.execute("DELETE FROM Игры WHERE КодИгры = ?", (game_id,))
        changed = True
    if changed:
        db.commit()
    return changed


def ensure_catalog_defaults(db):
    """Добавляет CS2, Dota 2 и Rust с категориями, если их ещё нет."""
    changed = retire_games(db)
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

from steam_auth import init_steam_auth, steam_profile
from steam_inventory import init_steam_inventory, fetch_game_inventory, SteamInventoryError

init_steam_auth(app, get_db)
init_steam_inventory(app, get_db)

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
