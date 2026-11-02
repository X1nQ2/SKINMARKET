"""Сквозные тесты: регистрация, продажа, покупка, отмена, отзывы, админка."""


def balance(run_sql, login):
    return run_sql("SELECT Баланс FROM Пользователи WHERE Логин = ?", (login,))[0][0]


def make_seller(make_client, login="seller"):
    c = make_client()
    c.register(login)
    c.post("/profile/become-seller")
    return c


def new_listing(c, price="500", qty="3", name="AK-47 | Redline"):
    return c.post("/sell/new", {"game": "1", "category": "1", "rarity": "3", "name": name,
                                "description": "тест", "image_url": "", "price": price, "qty": qty})


def test_register_login_logout(make_client):
    c = make_client()
    assert c.register("alice").status_code == 302
    assert c.get("/profile").status_code == 200
    c.post("/logout")
    assert c.get("/profile").status_code == 302
    assert c.post("/login", {"login": "alice", "password": "bad"}).status_code == 400
    assert c.post("/login", {"login": "alice", "password": "secret1"}).status_code == 302


def test_register_validation(make_client):
    c = make_client()
    assert c.post("/register", {"login": "a", "email": "x", "password": "1", "password2": "1"}).status_code == 400
    c.register("bob")
    other = make_client()
    assert other.register("bob").status_code == 400  # дубликат логина


def test_csrf_required(make_client):
    c = make_client()
    assert c.post("/register", {"login": "eve", "email": "e@e.ru", "password": "secret1",
                                "password2": "secret1"}, csrf=False).status_code == 400


def test_catalog_search_and_listing(make_client):
    seller = make_seller(make_client)
    assert new_listing(seller).status_code == 302
    c = make_client()
    html = c.get("/").get_data(as_text=True)
    assert "AK-47" in html
    assert "AK-47" in c.get("/?q=redline").get_data(as_text=True)
    assert "AK-47" not in c.get("/?q=нет-такого").get_data(as_text=True)
    assert c.get("/listing/1").status_code == 200
    assert c.get("/listing/999").status_code == 404


def test_full_purchase_flow(make_client, run_sql):
    seller = make_seller(make_client)
    new_listing(seller, price="500", qty="3")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "2000"})
    assert balance(run_sql, "buyer") == 2000

    assert buyer.post("/buy/1", {"qty": "2"}).status_code == 302
    assert balance(run_sql, "buyer") == 1000
    assert run_sql("SELECT Количество FROM Объявления")[0][0] == 1
    assert balance(run_sql, "seller") == 0  # деньги удерживаются до выполнения

    assert seller.post("/orders/1/complete").status_code == 302
    assert balance(run_sql, "seller") == 1000

    assert buyer.post("/orders/1/review", {"rating": "5", "text": "ок"}).status_code == 302
    assert run_sql("SELECT COUNT(*) FROM Отзывы")[0][0] == 1
    buyer.post("/orders/1/review", {"rating": "4"})  # повторный отзыв не создаётся
    assert run_sql("SELECT COUNT(*) FROM Отзывы")[0][0] == 1
    assert "ок" in make_client().get("/listing/1").get_data(as_text=True)


def test_buy_all_marks_sold_and_cancel_restores(make_client, run_sql):
    seller = make_seller(make_client)
    new_listing(seller, price="100", qty="2")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "1000"})
    buyer.post("/buy/1", {"qty": "2"})
    assert run_sql("SELECT s.НазваниеСтатуса FROM Объявления o JOIN СтатусыОбъявлений s "
                   "ON o.КодСтатуса = s.КодСтатуса")[0][0] == "Продано"
    buyer.post("/orders/1/cancel")
    assert balance(run_sql, "buyer") == 1000
    row = run_sql("SELECT Количество, s.НазваниеСтатуса FROM Объявления o JOIN СтатусыОбъявлений s "
                  "ON o.КодСтатуса = s.КодСтатуса")[0]
    assert (row[0], row[1]) == (2, "Активно")
    buyer.post("/orders/1/cancel")  # повторная отмена не возвращает деньги дважды
    assert balance(run_sql, "buyer") == 1000


def test_insufficient_funds_and_own_listing(make_client, run_sql):
    seller = make_seller(make_client)
    new_listing(seller, price="500", qty="1")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/buy/1", {"qty": "1"})
    assert run_sql("SELECT COUNT(*) FROM Заказы")[0][0] == 0
    seller.post("/profile/topup", {"amount": "1000"})
    seller.post("/buy/1", {"qty": "1"})
    assert run_sql("SELECT COUNT(*) FROM Заказы")[0][0] == 0


def test_cannot_oversell(make_client, run_sql):
    seller = make_seller(make_client)
    new_listing(seller, price="10", qty="1")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "1000"})
    buyer.post("/buy/1", {"qty": "5"})
    assert run_sql("SELECT COUNT(*) FROM Заказы")[0][0] == 0
    assert balance(run_sql, "buyer") == 1000


def test_cart_checkout(make_client, run_sql):
    seller = make_seller(make_client)
    new_listing(seller, price="100", qty="5")
    new_listing(seller, price="200", qty="5", name="M4A4 | Howl")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "1000"})
    buyer.post("/cart/add/1", {"qty": "2"})
    buyer.post("/cart/add/2", {"qty": "1"})
    assert "Итого" in buyer.get("/cart").get_data(as_text=True)
    buyer.post("/cart/checkout")
    assert run_sql("SELECT COUNT(*) FROM Заказы")[0][0] == 2
    assert balance(run_sql, "buyer") == 600


def test_cart_checkout_is_atomic(make_client, run_sql):
    seller = make_seller(make_client)
    new_listing(seller, price="100", qty="5")
    new_listing(seller, price="900", qty="5", name="Dragon Lore")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "500"})
    buyer.post("/cart/add/1", {"qty": "1"})
    buyer.post("/cart/add/2", {"qty": "1"})
    buyer.post("/cart/checkout")
    assert run_sql("SELECT COUNT(*) FROM Заказы")[0][0] == 0
    assert balance(run_sql, "buyer") == 500


def test_buyer_cannot_open_seller_pages(make_client):
    c = make_client()
    c.register("buyer")
    assert c.get("/sell").status_code == 403
    assert c.get("/sales").status_code == 403
    assert c.get("/admin").status_code == 403


def test_admin_can_block_user(make_client, run_sql):
    admin = make_client()
    admin.register("boss")
    run_sql("UPDATE Пользователи SET КодРоли = 3 WHERE Логин = 'boss'")
    victim = make_client()
    victim.register("victim")
    assert admin.get("/admin").status_code == 200
    admin.post("/admin/users/2/block")
    victim.post("/logout")
    assert victim.post("/login", {"login": "victim", "password": "secret1"}).status_code == 403
    assert admin.post("/admin/catalog", {"kind": "game", "name": "Dota 2"}).status_code == 302
    assert run_sql("SELECT COUNT(*) FROM Игры")[0][0] == 2


def test_withdraw_limits(make_client, run_sql):
    c = make_client()
    c.register("rich")
    c.post("/profile/topup", {"amount": "100"})
    c.post("/profile/withdraw", {"amount": "500"})
    assert balance(run_sql, "rich") == 100
    c.post("/profile/withdraw", {"amount": "40,50"})
    assert balance(run_sql, "rich") == 59.5


def test_info_pages_and_footer(make_client):
    c = make_client()
    for url in ("/delivery", "/contacts", "/terms", "/privacy"):
        assert c.get(url).status_code == 200
    html = c.get("/").get_data(as_text=True)
    assert "Политика конфиденциальности" in html and "support@skinmarket.example" in html


def test_default_admin_bootstrap(app, make_client, run_sql):
    import server
    from db import Db
    db = Db(app.config["DB_PATH"])
    assert server.ensure_admin(db, *server.DEFAULT_ADMIN) is True
    assert server.ensure_admin(db, *server.DEFAULT_ADMIN) is False  # повторный запуск ничего не меняет
    db.close()
    assert run_sql("SELECT COUNT(*) FROM Пользователи WHERE Логин = 'rrrr'")[0][0] == 1
    c = make_client()
    assert c.post("/login", {"login": "rrrr", "password": "123456"}).status_code == 302
    assert c.get("/admin").status_code == 200


def test_plain_passwords_visible_in_db(app, make_client, run_sql):
    app.config["PLAIN_PASSWORDS"] = True
    c = make_client()
    c.register("plainuser", "abc12345")
    assert run_sql("SELECT ХешПароля FROM Пользователи WHERE Логин = 'plainuser'")[0][0] == "abc12345"
    c.post("/logout")
    assert c.post("/login", {"login": "plainuser", "password": "abc12345"}).status_code == 302
    c.post("/logout")
    assert c.post("/login", {"login": "plainuser", "password": "wrong"}).status_code == 400


def test_hashed_mode_and_legacy_hash_login(app, make_client, run_sql):
    app.config["PLAIN_PASSWORDS"] = False
    c = make_client()
    c.register("hashed", "abc12345")
    stored = run_sql("SELECT ХешПароля FROM Пользователи WHERE Логин = 'hashed'")[0][0]
    assert stored != "abc12345" and stored.startswith(("scrypt:", "pbkdf2:"))
    app.config["PLAIN_PASSWORDS"] = True  # старый хеш продолжает работать
    c.post("/logout")
    assert c.post("/login", {"login": "hashed", "password": "abc12345"}).status_code == 302


def test_admin_hash_replaced_by_plain(app, run_sql):
    import server
    from db import Db
    app.config["PLAIN_PASSWORDS"] = False
    db = Db(app.config["DB_PATH"]); server.ensure_admin(db, *server.DEFAULT_ADMIN); db.close()
    app.config["PLAIN_PASSWORDS"] = True
    db = Db(app.config["DB_PATH"]); assert server.ensure_admin(db, *server.DEFAULT_ADMIN) is True; db.close()
    assert run_sql("SELECT ХешПароля FROM Пользователи WHERE Логин = 'rrrr'")[0][0] == "123456"


# ------------------------------------------------------------- каталог и картинки
import io  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def png(name="pic.png"):
    return io.BytesIO(PNG), name


def make_admin_client(make_client, run_sql, login="boss"):
    c = make_client()
    c.register(login)
    run_sql("UPDATE Пользователи SET КодРоли = 3 WHERE Логин = ?", (login,))
    return c


def test_default_catalog_cs2_and_dota(app, run_sql):
    import server
    from db import Db
    db = Db(app.config["DB_PATH"])
    assert server.ensure_catalog_defaults(db) is True
    assert server.ensure_catalog_defaults(db) is False  # повторный запуск ничего не дублирует
    db.close()
    games = {r[0] for r in run_sql("SELECT НазваниеИгры FROM Игры")}
    assert games == set(server.DEFAULT_CATALOG)
    assert {"CS2", "Dota 2", "Rust", "Team Fortress 2", "PUBG: Battlegrounds", "Apex Legends"} <= games
    assert run_sql("SELECT COUNT(*) FROM Категории k JOIN Игры i ON k.КодИгры = i.КодИгры "
                   "WHERE i.НазваниеИгры = 'Dota 2'")[0][0] == len(server.DEFAULT_CATALOG["Dota 2"])


def test_admin_adds_game_with_logo(app, make_client, run_sql):
    from pathlib import Path
    admin = make_admin_client(make_client, run_sql)
    assert admin.get("/admin/catalog").status_code == 200
    r = admin.post("/admin/catalog", {"kind": "game", "name": "Rust", "logo": png("logo.png")})
    assert r.status_code == 302
    game_id = run_sql("SELECT КодИгры FROM Игры WHERE НазваниеИгры = 'Rust'")[0][0]
    assert (Path(app.config["UPLOAD_ROOT"]) / "game_images" / f"game-{game_id}.png").exists()
    assert f"game-{game_id}.png" in admin.get("/").get_data(as_text=True)
    admin.post("/admin/catalog", {"kind": "game_logo", "game": str(game_id), "logo": png("new.png")})
    assert len(list((Path(app.config["UPLOAD_ROOT"]) / "game_images").glob(f"game-{game_id}.*"))) == 1


def test_non_images_rejected(app, make_client, run_sql):
    admin = make_admin_client(make_client, run_sql)
    for fake in (io.BytesIO(b"hello"), io.BytesIO(b"<svg onload=alert(1)></svg>")):
        admin.post("/admin/catalog", {"kind": "game", "name": "Fake", "logo": (fake, "x.png")})
    assert run_sql("SELECT COUNT(*) FROM Игры WHERE НазваниеИгры = 'Fake'")[0][0] == 0
    admin.post("/admin/catalog", {"kind": "item", "game": "1", "category": "1", "rarity": "2", "name": "Bad skin",
                                  "image_file": (io.BytesIO(b"MZ\x90\x00"), "evil.png")})
    assert run_sql("SELECT COUNT(*) FROM Товары")[0][0] == 0


def test_admin_item_and_image_update(app, make_client, run_sql):
    admin = make_admin_client(make_client, run_sql)
    admin.post("/admin/catalog", {"kind": "item", "game": "1", "category": "1", "rarity": "3",
                                  "name": "AWP | Asiimov", "description": "d", "image_file": png()})
    row = run_sql("SELECT URLИзображения FROM Товары WHERE Название = 'AWP | Asiimov'")[0][0]
    assert row.startswith("/static/skin_images/") and row.endswith(".png")
    admin.post("/admin/catalog", {"kind": "item_image", "item_id": "1", "image_url": "https://example.com/a.png"})
    assert run_sql("SELECT URLИзображения FROM Товары WHERE КодТовара = 1")[0][0] == "https://example.com/a.png"
    admin.post("/admin/catalog", {"kind": "item_image", "item_id": "1", "image_url": "javascript:alert(1)"})
    assert run_sql("SELECT URLИзображения FROM Товары WHERE КодТовара = 1")[0][0] == "https://example.com/a.png"
    assert "AWP" in admin.get("/admin/catalog?game=1").get_data(as_text=True)


def test_seller_uploads_image_and_item_is_reused(make_client, run_sql):
    seller = make_seller(make_client)
    base = {"game": "1", "category": "1", "rarity": "3", "name": "Karambit", "description": "", "price": "100", "qty": "1"}
    seller.post("/sell/new", dict(base, image_file=png()))
    seller.post("/sell/new", dict(base, price="120"))  # тот же скин без картинки
    assert run_sql("SELECT COUNT(*) FROM Товары")[0][0] == 1
    assert run_sql("SELECT COUNT(*) FROM Объявления")[0][0] == 2
    assert run_sql("SELECT URLИзображения FROM Товары")[0][0].startswith("/static/skin_images/")
    assert "/static/skin_images/" in make_client().get("/").get_data(as_text=True)


def test_catalog_game_chips_with_default_logo(app, make_client):
    from pathlib import Path
    logos = Path(app.config["UPLOAD_ROOT"]) / "game_images"
    logos.mkdir(parents=True)
    (logos / "cs2.png").write_bytes(PNG)
    html = make_client().get("/").get_data(as_text=True)
    assert "chip" in html and "CS2" in html and "/static/game_images/cs2.png" in html
    assert make_client().get("/?game=1").status_code == 200


def test_admin_item_with_price_appears_in_catalog(make_client, run_sql):
    admin = make_admin_client(make_client, run_sql)
    admin.post("/admin/catalog", {"kind": "item", "game": "1", "category": "1", "rarity": "4",
                                  "name": "Dark Moon Baby Roshan", "price": "2 500,50", "qty": "3"})
    row = run_sql("SELECT Цена, Количество FROM Объявления")[0]
    assert (row[0], row[1]) == (2500.5, 3)
    html = make_client().get("/").get_data(as_text=True)
    assert "Dark Moon Baby Roshan" in html


def test_admin_item_without_price_is_not_on_showcase_until_listed(make_client, run_sql):
    admin = make_admin_client(make_client, run_sql)
    admin.post("/admin/catalog", {"kind": "item", "game": "1", "category": "1", "rarity": "4", "name": "Roshan"})
    assert run_sql("SELECT COUNT(*) FROM Товары")[0][0] == 1
    assert run_sql("SELECT COUNT(*) FROM Объявления")[0][0] == 0
    assert "Roshan" not in make_client().get("/").get_data(as_text=True)
    page = admin.get("/admin/catalog").get_data(as_text=True)
    assert "нет на витрине" in page
    admin.post("/admin/catalog", {"kind": "item_listing", "item_id": "1", "price": "700", "qty": "2"})
    assert run_sql("SELECT COUNT(*) FROM Объявления")[0][0] == 1
    assert "Roshan" in make_client().get("/").get_data(as_text=True)
    assert "в продаже" in admin.get("/admin/catalog").get_data(as_text=True)


def test_admin_item_bad_price_creates_nothing(make_client, run_sql):
    admin = make_admin_client(make_client, run_sql)
    admin.post("/admin/catalog", {"kind": "item", "game": "1", "category": "1", "rarity": "4",
                                  "name": "Bad", "price": "abc", "qty": "1"})
    assert run_sql("SELECT COUNT(*) FROM Товары")[0][0] == 0
    admin.post("/admin/catalog", {"kind": "item", "game": "1", "category": "1", "rarity": "4",
                                  "name": "Bad2", "price": "10", "qty": "0"})
    assert run_sql("SELECT COUNT(*) FROM Товары")[0][0] == 0


def test_fetch_game_images_script(tmp_path):
    import json
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    import fetch_game_images as f

    def fake(url):
        if "appdetails" in url:
            return json.dumps({"252490": {"success": True, "data": {"name": "Rust", "header_image": "https://x/h.jpg?t=1"}}}).encode()
        assert url == "https://x/h.jpg"
        return b"\xff\xd8\xff" + b"0" * 10

    assert f.download_logo("Rust", 252490, "rust", fetcher=fake, dest=tmp_path)[0] == "ok"
    assert (tmp_path / "rust.jpg").exists()
    assert f.download_logo("Rust", 252490, "rust", fetcher=fake, dest=tmp_path)[0] == "skip"  # не перезаписывает
    wrong = lambda url: json.dumps({"1": {"success": True, "data": {"name": "Other game"}}}).encode()
    assert f.download_logo("Apex Legends", 1, "apex", fetcher=wrong, dest=tmp_path)[0] == "error"
