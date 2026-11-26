"""Сквозные тесты: аккаунты, Steam-инвентарь, продажа, покупка, отмена, отзывы, админка."""


def balance(run_sql, login):
    return run_sql("SELECT Баланс FROM Пользователи WHERE Логин = ?", (login,))[0][0]


def make_seller(make_client, login="seller"):
    c = make_client()
    c.register(login)
    c.post("/profile/become-seller")
    return c


def new_steam_listing(seller, steam_store, price="500", name="AK-47 | Redline", assetid=None, appid="730",
                      category=None, rarity=None):
    """Добавляет предмет так же, как это делает страница «Инвентарь Steam»."""
    steam_id = seller.link_steam()
    if assetid is None:
        assetid = str(100000 + sum(len(v) for v in steam_store.values()) + 1)
    game_name = {"730": "CS2", "570": "Dota 2", "252490": "Rust"}[appid]
    steam_store.setdefault((steam_id, appid), []).append({
        "assetid": assetid,
        "name": name,
        "type": "Rifle",
        "image": "https://example.com/item.png",
        "tradable": True,
        "marketable": True,
        "rarity": "Restricted",
        "market_hash_name": name,
        "description": f"Описание Steam для {name}.",
    })
    payload = {"appid": appid, "game_name": game_name, "assetid": assetid, "price": price}
    if category:
        payload["category"] = category
    if rarity:
        payload["rarity"] = rarity
    return seller.post("/profile/inventory/list", payload)


def test_register_redirects_to_steam(make_client):
    c = make_client()
    response = c.get("/register")
    assert response.status_code == 302
    assert "/auth/steam" in response.headers["Location"]


def test_register_login_logout(make_client):
    c = make_client()
    assert c.register("alice").status_code == 302
    assert c.get("/profile").status_code == 200
    c.post("/logout")
    assert c.get("/profile").status_code == 302
    assert c.post("/login", {"login": "alice", "password": "bad"}).status_code == 400
    assert c.post("/login", {"login": "alice", "password": "secret1"}).status_code == 302


def test_csrf_required(make_client):
    c = make_client()
    assert c.post("/register", {"login": "eve", "email": "e@e.ru", "password": "secret1",
                                "password2": "secret1"}, csrf=False).status_code == 400


def test_catalog_search_and_listing(make_client, steam_store):
    seller = make_seller(make_client)
    assert new_steam_listing(seller, steam_store).status_code == 302
    c = make_client()
    html = c.get("/").get_data(as_text=True)
    assert "AK-47" in html
    assert "AK-47" in c.get("/?q=redline").get_data(as_text=True)
    assert "AK-47" not in c.get("/?q=нет-такого").get_data(as_text=True)
    assert c.get("/listing/1").status_code == 200
    listing_html = c.get("/listing/1").get_data(as_text=True)
    assert "Описание Steam для AK-47 | Redline." in listing_html
    assert "О товаре" in listing_html
    assert "Продавец" in listing_html
    assert c.get("/listing/999").status_code == 404


def test_full_purchase_flow(make_client, steam_store, run_sql):
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, price="500")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "2000"})
    assert balance(run_sql, "buyer") == 2000

    assert buyer.post("/buy/1", {"qty": "1"}).status_code == 302
    assert balance(run_sql, "buyer") == 1500
    assert run_sql("SELECT Количество FROM Объявления")[0][0] == 0
    assert balance(run_sql, "seller") == 0  # деньги удерживаются до выполнения

    assert seller.post("/orders/1/send").status_code == 302
    assert balance(run_sql, "seller") == 0  # после отправки деньги ещё удерживаются
    assert run_sql(
        "SELECT st.НазваниеСтатуса FROM Заказы z "
        "JOIN СтатусыЗаказов st ON z.КодСтатуса = st.КодСтатуса WHERE z.КодЗаказа = 1"
    )[0][0] == "Отправлен"

    assert buyer.post("/orders/1/confirm").status_code == 302
    assert balance(run_sql, "seller") == 500

    assert buyer.post("/orders/1/review", {"rating": "5", "text": "ок"}).status_code == 302
    assert run_sql("SELECT COUNT(*) FROM Отзывы")[0][0] == 1
    buyer.post("/orders/1/review", {"rating": "4"})  # повторный отзыв не создаётся
    assert run_sql("SELECT COUNT(*) FROM Отзывы")[0][0] == 1
    assert "ок" in make_client().get("/listing/1").get_data(as_text=True)


def test_buy_all_marks_sold_and_cancel_restores(make_client, steam_store, run_sql):
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, price="100")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "1000"})
    buyer.post("/buy/1", {"qty": "1"})
    assert run_sql("SELECT s.НазваниеСтатуса FROM Объявления o JOIN СтатусыОбъявлений s "
                   "ON o.КодСтатуса = s.КодСтатуса")[0][0] == "Продано"
    buyer.post("/orders/1/cancel")
    assert balance(run_sql, "buyer") == 1000
    row = run_sql("SELECT Количество, s.НазваниеСтатуса FROM Объявления o JOIN СтатусыОбъявлений s "
                  "ON o.КодСтатуса = s.КодСтатуса")[0]
    assert (row[0], row[1]) == (1, "Активно")
    buyer.post("/orders/1/cancel")  # повторная отмена не возвращает деньги дважды
    assert balance(run_sql, "buyer") == 1000


def test_insufficient_funds_and_own_listing(make_client, steam_store, run_sql):
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, price="500")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/buy/1", {"qty": "1"})
    assert run_sql("SELECT COUNT(*) FROM Заказы")[0][0] == 0
    seller.post("/profile/topup", {"amount": "1000"})
    seller.post("/buy/1", {"qty": "1"})
    assert run_sql("SELECT COUNT(*) FROM Заказы")[0][0] == 0


def test_cannot_oversell(make_client, steam_store, run_sql):
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, price="10")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "1000"})
    buyer.post("/buy/1", {"qty": "5"})
    assert run_sql("SELECT COUNT(*) FROM Заказы")[0][0] == 0
    assert balance(run_sql, "buyer") == 1000


def test_cart_checkout(make_client, steam_store, run_sql):
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, price="100", name="AK-47 | Redline", assetid="101")
    new_steam_listing(seller, steam_store, price="200", name="M4A4 | Howl", assetid="102")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "1000"})
    buyer.post("/cart/add/1", {"qty": "1"})
    buyer.post("/cart/add/2", {"qty": "1"})
    assert "Итого" in buyer.get("/cart").get_data(as_text=True)
    buyer.post("/cart/checkout")
    assert run_sql("SELECT COUNT(*) FROM Заказы")[0][0] == 2
    assert balance(run_sql, "buyer") == 700


def test_cart_checkout_is_atomic(make_client, steam_store, run_sql):
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, price="100", name="AK-47 | Redline", assetid="201")
    new_steam_listing(seller, steam_store, price="900", name="Dragon Lore", assetid="202")
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


def test_default_catalog_cs2_dota_rust(app, run_sql):
    import server
    from db import Db
    db = Db(app.config["DB_PATH"])
    assert server.ensure_catalog_defaults(db) is True
    assert server.ensure_catalog_defaults(db) is False  # повторный запуск ничего не дублирует
    db.close()
    games = {r[0] for r in run_sql("SELECT НазваниеИгры FROM Игры")}
    assert games == set(server.DEFAULT_CATALOG) == {"CS2", "Dota 2", "Rust"}
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


def test_non_images_rejected(make_client, run_sql):
    admin = make_admin_client(make_client, run_sql)
    for fake in (io.BytesIO(b"hello"), io.BytesIO(b"<svg onload=alert(1)></svg>")):
        admin.post("/admin/catalog", {"kind": "game", "name": "Rust", "logo": (fake, "x.png")})
    assert run_sql("SELECT COUNT(*) FROM Игры WHERE НазваниеИгры = 'Rust'")[0][0] == 0


def test_admin_item_image_update(make_client, steam_store, run_sql):
    admin = make_admin_client(make_client, run_sql)
    new_steam_listing(admin, steam_store, name="AWP | Asiimov", assetid="301")
    item_id = run_sql("SELECT КодТовара FROM Товары WHERE Название = 'AWP | Asiimov'")[0][0]
    assert run_sql("SELECT URLИзображения FROM Товары WHERE КодТовара = ?",
                   (item_id,))[0][0] == "https://example.com/item.png"
    admin.post("/admin/catalog", {"kind": "item_image", "item_id": str(item_id),
                                  "image_url": "https://example.com/a.png"})
    assert run_sql("SELECT URLИзображения FROM Товары WHERE КодТовара = ?",
                   (item_id,))[0][0] == "https://example.com/a.png"
    admin.post("/admin/catalog", {"kind": "item_image", "item_id": str(item_id),
                                  "image_url": "javascript:alert(1)"})
    assert run_sql("SELECT URLИзображения FROM Товары WHERE КодТовара = ?",
                   (item_id,))[0][0] == "https://example.com/a.png"
    assert "AWP" in admin.get("/admin/catalog?game=1").get_data(as_text=True)


def test_steam_item_is_reused(make_client, steam_store, run_sql):
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, name="Karambit", assetid="401")
    new_steam_listing(seller, steam_store, name="Karambit", assetid="402")
    assert run_sql("SELECT COUNT(*) FROM Товары")[0][0] == 1
    assert run_sql("SELECT COUNT(*) FROM Объявления")[0][0] == 2
    assert run_sql("SELECT URLИзображения FROM Товары")[0][0] == "https://example.com/item.png"
    assert "https://example.com/item.png" in make_client().get("/").get_data(as_text=True)


def test_manual_item_creation_disabled(make_client, run_sql):
    admin = make_admin_client(make_client, run_sql)
    admin.post("/admin/catalog", {"kind": "item", "game": "1", "category": "1", "rarity": "3",
                                  "name": "Bad skin", "price": "100", "qty": "1"})
    assert run_sql("SELECT COUNT(*) FROM Товары")[0][0] == 0
    admin.post("/admin/catalog", {"kind": "item_listing", "item_id": "1", "price": "100", "qty": "1"})
    assert run_sql("SELECT COUNT(*) FROM Объявления")[0][0] == 0
    assert "только из инвентаря Steam" in admin.get("/admin/catalog").get_data(as_text=True)


def test_sell_update_changes_only_price(make_client, steam_store, run_sql):
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, price="100", assetid="501")
    seller.post("/sell/1/update", {"price": "250", "qty": "9999"})
    row = run_sql("SELECT Цена, Количество FROM Объявления WHERE КодОбъявления = 1")[0]
    assert (row[0], row[1]) == (250, 1)


def test_sell_remove_frees_steam_item(make_client, steam_store, run_sql):
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, name="Glock-18 | Fade", assetid="601")
    assert run_sql("SELECT COUNT(*) FROM SteamInventoryListings")[0][0] == 1
    seller.post("/sell/1/remove")
    assert run_sql("SELECT COUNT(*) FROM SteamInventoryListings")[0][0] == 0
    # Предмет снова можно добавить: он остался в инвентаре Steam.
    new_steam_listing(seller, steam_store, name="Glock-18 | Fade", assetid="601")
    assert run_sql("SELECT COUNT(*) FROM Объявления")[0][0] == 2


def test_inventory_without_steam(make_client):
    c = make_client()
    c.register("nosteam")
    response = c.get("/profile/inventory")
    assert response.status_code == 200
    assert "не привязан Steam" in response.get_data(as_text=True)


def test_inventory_private_message(make_client, steam_store, monkeypatch):
    import steam_inventory
    c = make_client()
    c.register("private")
    c.link_steam()

    def private(*_args, **_kwargs):
        raise steam_inventory.SteamInventoryPrivate(steam_inventory.PRIVATE_MESSAGE)

    monkeypatch.setattr(steam_inventory, "fetch_game_inventory", private)
    html = c.get("/profile/inventory").get_data(as_text=True)
    assert "закрыт настройками приватности" in html
    assert "401 Client Error" not in html


def test_inventory_steam_style_browser(make_client, steam_store):
    c = make_client()
    c.register("inventory")
    steam_id = c.link_steam()
    steam_store[(steam_id, "730")] = [
        {
            "assetid": "801", "name": "AK-47 | Redline", "type": "Rifle",
            "image": "https://example.com/ak.png", "tradable": True, "marketable": True,
            "rarity": "Classified", "market_hash_name": "AK-47 | Redline",
        },
        {
            "assetid": "802", "name": "AWP | Asiimov", "type": "Sniper Rifle",
            "image": "https://example.com/awp.png", "tradable": True, "marketable": True,
            "rarity": "Covert", "market_hash_name": "AWP | Asiimov",
        },
    ]
    html = c.get("/profile/inventory?appid=730").get_data(as_text=True)
    assert "inv-browser" in html
    assert "Поиск по предметам" in html
    assert "AK-47 | Redline" in html and "AWP | Asiimov" in html
    assert "inv-detail-form" in html
    # Поиск сужает сетку и выбирает первый подходящий предмет.
    filtered = c.get("/profile/inventory?appid=730&q=awp").get_data(as_text=True)
    assert "AWP | Asiimov" in filtered
    assert "AK-47 | Redline" not in filtered
    # Фильтр «только передаваемые» не ломает страницу.
    assert c.get("/profile/inventory?appid=730&tradable=1").status_code == 200


def test_inventory_shows_all_catalog_games(make_client, steam_store, monkeypatch):
    import steam_inventory
    c = make_client()
    c.register("allgames")
    c.link_steam()
    monkeypatch.setattr(
        steam_inventory, "get_owned_games",
        lambda _steam_id: [{"appid": 730, "name": "CS2"}],
    )
    html = c.get("/profile/inventory?appid=570").get_data(as_text=True)
    assert "Dota 2" in html
    assert "Rust" in html
    assert "нет в библиотеке" in html


def test_steam_listing_uses_selected_category_and_rarity(make_client, steam_store, run_sql):
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, name="AWP | Neo-Noir", assetid="905",
                      category="Снайперские винтовки", rarity="Легендарное")
    row = run_sql(
        "SELECT k.НазваниеКатегории, r.НазваниеРедкости FROM Объявления o "
        "JOIN Товары t ON o.КодТовара = t.КодТовара "
        "JOIN Категории k ON t.КодКатегории = k.КодКатегории "
        "JOIN Редкости r ON t.КодРедкости = r.КодРедкости WHERE o.КодОбъявления = 1"
    )[0]
    assert (row[0], row[1]) == ("Снайперские винтовки", "Легендарное")
    page = seller.get("/profile/inventory?appid=730").get_data(as_text=True)
    assert 'name="category"' in page and 'name="rarity"' in page
    assert "Снайперские винтовки" in page


def test_existing_product_gets_new_seller_category(make_client, steam_store, run_sql):
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, name="No Mercy Wood Gloves", assetid="910",
                      appid="252490", category="Оружие")
    new_steam_listing(seller, steam_store, name="No Mercy Wood Gloves", assetid="911",
                      appid="252490", category="Одежда", rarity="Эпическое")
    row = run_sql(
        "SELECT k.НазваниеКатегории, r.НазваниеРедкости FROM Товары t "
        "JOIN Категории k ON t.КодКатегории = k.КодКатегории "
        "JOIN Редкости r ON t.КодРедкости = r.КодРедкости WHERE t.Название = ?",
        ("No Mercy Wood Gloves",),
    )[0]
    assert (row[0], row[1]) == ("Одежда", "Эпическое")


def test_seller_can_change_category_and_rarity_on_sell_page(make_client, steam_store, run_sql):
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, name="No Mercy Wood Gloves", assetid="930",
                      appid="252490", category="Оружие")
    seller.post("/sell/1/update", {"category": "Одежда", "rarity": "Эпическое"})
    row = run_sql(
        "SELECT k.НазваниеКатегории, r.НазваниеРедкости FROM Объявления o "
        "JOIN Товары t ON o.КодТовара = t.КодТовара "
        "JOIN Категории k ON t.КодКатегории = k.КодКатегории "
        "JOIN Редкости r ON t.КодРедкости = r.КодРедкости WHERE o.КодОбъявления = 1"
    )[0]
    assert (row[0], row[1]) == ("Одежда", "Эпическое")
    page = seller.get("/sell").get_data(as_text=True)
    assert 'name="category"' in page and 'name="rarity"' in page


def test_admin_analytics_dashboard(make_client, steam_store, run_sql):
    admin = make_admin_client(make_client, run_sql)
    seller = make_seller(make_client, "seller")
    new_steam_listing(seller, steam_store, price="1000", name="AWP | Asiimov", assetid="9001")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "3000"})
    buyer.post("/buy/1", {"qty": "1"})
    seller.post("/orders/1/send")
    buyer.post("/orders/1/confirm")

    html = admin.get("/admin").get_data(as_text=True)
    for marker in ("Продажи за 14 дней", "По играм", "Топ продавцов",
                   "Заказы по статусам", "Средний чек", "analytics-grid",
                   "chart-bar", "bar-track", "seller-top", "chips"):
        assert marker in html
    assert "1 000" in html.replace("\u00a0", " ")  # оборот и средний чек
    assert admin.get("/").status_code == 200


def test_admin_csv_exports_are_admin_only(make_client, steam_store, run_sql):
    admin = make_admin_client(make_client, run_sql)
    seller = make_seller(make_client, "seller")
    new_steam_listing(seller, steam_store, price="700", name="M4A1-S | Printstream", assetid="9002")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "2000"})
    buyer.post("/buy/1", {"qty": "1"})

    orders = admin.get("/admin/export/orders.csv")
    assert orders.status_code == 200
    assert "text/csv" in orders.headers["Content-Type"]
    assert "attachment" in orders.headers["Content-Disposition"]
    text = orders.get_data(as_text=True)
    assert text.startswith("\ufeff")
    assert "Покупатель" in text and "M4A1-S | Printstream" in text
    assert "buyer" in text and "seller" in text

    users = admin.get("/admin/export/users.csv")
    assert users.status_code == 200
    users_text = users.get_data(as_text=True)
    assert "Логин" in users_text and "buyer@example.com" in users_text

    assert buyer.get("/admin/export/orders.csv").status_code == 403
    assert make_client().get("/admin/export/users.csv").status_code in (302, 403)


def test_orders_and_cart_show_steam_names(app, make_client, steam_store, monkeypatch):
    import server
    app.config["TESTING"] = False
    monkeypatch.setattr(server, "steam_profile", lambda steam_id: {
        "name": "b4h4-",
        "avatar": "https://example.com/avatar.jpg",
        "profile_url": f"https://steamcommunity.com/profiles/{steam_id}/",
    })
    seller = make_seller(make_client, "seller")
    new_steam_listing(seller, steam_store, price="300", name="Karambit | Fade", assetid="9003")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "1000"})
    buyer.post("/cart/add/1", {"qty": "1"})

    cart_html = buyer.get("/cart").get_data(as_text=True)
    assert "b4h4-" in cart_html
    assert "user-avatar" in cart_html
    assert "seller@example.com" not in cart_html

    buyer.post("/cart/checkout")
    orders_html = buyer.get("/orders").get_data(as_text=True)
    assert "b4h4-" in orders_html
    assert "user-avatar" in orders_html

    sales_html = seller.get("/sales").get_data(as_text=True)
    assert "buyer" in sales_html  # у покупателя нет привязки Steam — показываем логин


def test_orders_and_cart_fall_back_to_login(make_client, steam_store):
    seller = make_seller(make_client, "seller")
    new_steam_listing(seller, steam_store, price="300", assetid="9004")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "1000"})
    buyer.post("/cart/add/1", {"qty": "1"})
    assert "seller" in buyer.get("/cart").get_data(as_text=True)
    buyer.post("/cart/checkout")
    assert "seller" in buyer.get("/orders").get_data(as_text=True)


def test_inventory_shows_russian_rarity(make_client, steam_store):
    c = make_client()
    c.register("rarity")
    steam_id = c.link_steam()
    steam_store[(steam_id, "730")] = [{
        "assetid": "9005", "name": "AK-47 | Redline", "type": "Rifle",
        "image": "https://example.com/ak.png", "tradable": True, "marketable": True,
        "rarity": "Classified", "rarity_ru": "Эпическое",
        "market_hash_name": "AK-47 | Redline",
    }]
    html = c.get("/profile/inventory?appid=730").get_data(as_text=True)
    assert "Эпическое" in html
    assert "Classified" not in html
    assert "rarity-epic" in html
    # Фильтр по русскому названию редкости тоже работает.
    assert "AK-47 | Redline" in c.get(
        "/profile/inventory?appid=730&rarity=Эпическое").get_data(as_text=True)


def test_inventory_cache_reuses_steam_response(monkeypatch):
    import steam_inventory
    steam_inventory.clear_inventory_cache()
    calls = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"assets": [], "descriptions": [], "success": 1}

    def fake_get(url, **kwargs):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(steam_inventory.requests, "get", fake_get)
    steam_inventory.fetch_game_inventory("76561198000000009", "730")
    steam_inventory.fetch_game_inventory("76561198000000009", "730")
    assert len(calls) == 1
    steam_inventory.clear_inventory_cache()
    steam_inventory.fetch_game_inventory("76561198000000009", "730")
    assert len(calls) == 2


def test_faq_and_404_search(make_client):
    html = make_client().get("/faq").get_data(as_text=True)
    assert "Как купить и продать" in html
    assert "error-search" not in html
    page = make_client().get("/no-such-page").get_data(as_text=True)
    assert "404" in page and "error-search" in page
    assert "Политика конфиденциальности" in make_client().get("/").get_data(as_text=True)
    assert 'href="/faq"' in make_client().get("/").get_data(as_text=True)


def test_steam_persona_name_cleanup():
    from steam_auth import _clean_persona_name
    assert _clean_persona_name("Steam Community :: b4h4-") == "b4h4-"
    assert _clean_persona_name("b4h4- :: Steam Community") == "b4h4-"
    assert _clean_persona_name("b4h4-") == "b4h4-"


def test_steam_description_cleanup():
    from steam_inventory import _plain_steam_text
    text = _plain_steam_text("<b>This is a skin</b> for the <i>Assault Rifle</i>.<br>[b]Steam[/b] item.")
    assert "This is a skin for the Assault Rifle." in text
    assert "<b>" not in text and "[b]" not in text
    assert "\n" in text


def test_catalog_shows_steam_seller_name(app, make_client, steam_store, monkeypatch):
    import server
    app.config["TESTING"] = False
    monkeypatch.setattr(server, "steam_profile", lambda steam_id: {
        "name": "b4h4-",
        "avatar": "https://example.com/avatar.jpg",
        "profile_url": f"https://steamcommunity.com/profiles/{steam_id}/",
    })
    seller = make_seller(make_client)
    new_steam_listing(seller, steam_store, name="AWP | Dragon Lore", assetid="901")
    html = make_client().get("/").get_data(as_text=True)
    assert "b4h4-" in html
    assert "steam_76561198000000001" not in html
    assert "https://example.com/avatar.jpg" in html


def test_only_allowed_games_can_be_imported(make_client, run_sql):
    seller = make_seller(make_client)
    seller.link_steam()
    response = seller.post("/profile/inventory/list", {
        "appid": "1172470", "assetid": "701", "price": "100",
    })
    assert response.status_code == 302
    assert run_sql("SELECT COUNT(*) FROM Товары")[0][0] == 0


def test_catalog_game_chips_with_default_logo(app, make_client):
    from pathlib import Path
    logos = Path(app.config["UPLOAD_ROOT"]) / "game_images"
    logos.mkdir(parents=True)
    (logos / "cs2.png").write_bytes(PNG)
    html = make_client().get("/").get_data(as_text=True)
    assert "chip" in html and "CS2" in html and "/static/game_images/cs2.png" in html
    assert make_client().get("/?game=1").status_code == 200


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


# ------------------------------------------------------ публичная страница продавца
def test_seller_page_shows_profile_listings_and_reviews(make_client, steam_store, run_sql):
    seller = make_seller(make_client, "seller")
    new_steam_listing(seller, steam_store, price="500", name="AK-47 | Redline", assetid="9201")
    new_steam_listing(seller, steam_store, price="900", name="AWP | Asiimov", assetid="9202")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "2000"})
    buyer.post("/buy/1", {"qty": "1"})
    seller.post("/orders/1/send")
    buyer.post("/orders/1/confirm")
    buyer.post("/orders/1/review", {"rating": "5", "text": "Быстро и вежливо"})

    html = make_client().get(f"/seller/{seller.user_id}").get_data(as_text=True)
    assert "seller" in html
    assert "На сайте с 01.01.2026" in html
    assert "Выполненных сделок" in html
    assert "AK-47 | Redline" not in html  # проданный товар в активных не показываем
    assert "AWP | Asiimov" in html
    assert "★ 5.0" in html
    assert "Быстро и вежливо" in html and "buyer" in html
    assert "Написать продавцу" in html
    assert "Объявления продавца" in html


def test_seller_page_without_reviews_and_listings(make_client):
    c = make_client()
    c.register("empty_seller")
    html = make_client().get(f"/seller/{c.user_id}").get_data(as_text=True)
    assert "Отзывов пока нет" in html
    assert "нет активных объявлений" in html
    assert "Написать продавцу" in html


def test_seller_page_unknown_is_404(make_client):
    assert make_client().get("/seller/999").status_code == 404


def test_seller_links_on_catalog_and_listing(make_client, steam_store):
    seller = make_seller(make_client, "seller")
    new_steam_listing(seller, steam_store, price="500", name="M4A4 | Howl", assetid="9301")
    catalog = make_client().get("/").get_data(as_text=True)
    assert f"/seller/{seller.user_id}" in catalog
    listing = make_client().get("/listing/1").get_data(as_text=True)
    assert f"/seller/{seller.user_id}" in listing
    assert "Все объявления продавца" in listing


def test_seller_page_shows_steam_profile(app, make_client, steam_store, monkeypatch):
    import server
    app.config["TESTING"] = False
    monkeypatch.setattr(server, "steam_profile", lambda steam_id: {
        "name": "b4h4-",
        "avatar": "https://example.com/avatar.jpg",
        "profile_url": f"https://steamcommunity.com/profiles/{steam_id}/",
    })
    seller = make_seller(make_client, "seller")
    new_steam_listing(seller, steam_store, name="Karambit | Fade", assetid="9401")
    html = make_client().get(f"/seller/{seller.user_id}").get_data(as_text=True)
    assert "b4h4-" in html
    assert "https://example.com/avatar.jpg" in html
    assert "steam_76561198000000001" not in html
    assert "https://steamcommunity.com/profiles/76561198000000001/" in html


# ------------------------------------------------------- Steam Trade URL и чат
def test_trade_url_validation():
    from order_chat import normalize_trade_url
    url, error = normalize_trade_url(
        "https://steamcommunity.com/tradeoffer/new/?partner=123456789&token=AbCdEf12")
    assert error == ""
    assert url == "https://steamcommunity.com/tradeoffer/new/?partner=123456789&token=AbCdEf12"
    # Без схемы и с лишними параметрами — приводим к каноническому виду.
    url, error = normalize_trade_url(
        "steamcommunity.com/tradeoffer/new/?token=AbCdEf12&partner=123456789&locale=ru")
    assert error == ""
    assert url == "https://steamcommunity.com/tradeoffer/new/?partner=123456789&token=AbCdEf12"
    assert normalize_trade_url("")[0] == ""

    for bad in (
        "https://example.com/tradeoffer/new/?partner=1&token=AbCdEf12",
        "https://steamcommunity.com/tradeoffer/new/?token=AbCdEf12",
        "https://steamcommunity.com/tradeoffer/new/?partner=1",
        "https://steamcommunity.com/tradeoffer/1/?partner=1&token=AbCdEf12",
        "https://steamcommunity.com/tradeoffer/new/?partner=abc&token=AbCdEf12",
    ):
        url, error = normalize_trade_url(bad)
        assert url is None and error


def test_chat_schema_migrates_old_database(tmp_path):
    """Старая база без чата и SteamTradeURL обновляется на первом запуске."""
    import sqlite3
    import order_chat
    from db import Db

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE Пользователи (КодПользователя INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    order_chat._SCHEMA_READY.clear()
    db = Db(str(path))
    try:
        order_chat.ensure_chat_schema(db)
        assert "SteamTradeURL" in order_chat._columns(db, "Пользователи")
        assert order_chat._table_exists(db, "СообщенияЗаказов")
        # Повторный вызов ничего не ломает.
        order_chat.ensure_chat_schema(db)
        assert "SteamTradeURL" in order_chat._columns(db, "Пользователи")
    finally:
        db.close()
        order_chat._SCHEMA_READY.clear()


def test_profile_trade_url_save_and_remove(make_client, run_sql):
    c = make_client()
    c.register("trader")
    assert c.get("/profile").status_code == 200
    assert run_sql("SELECT SteamTradeURL FROM Пользователи WHERE Логин = 'trader'")[0][0] is None

    valid = "https://steamcommunity.com/tradeoffer/new/?partner=987654321&token=Token1234"
    assert c.post("/profile/trade-url", {"trade_url": valid}).status_code == 302
    assert run_sql("SELECT SteamTradeURL FROM Пользователи WHERE Логин = 'trader'")[0][0] == valid

    c.post("/profile/trade-url", {"trade_url": "https://example.com/tradeoffer/new/?partner=1&token=1234"})
    assert run_sql("SELECT SteamTradeURL FROM Пользователи WHERE Логин = 'trader'")[0][0] == valid

    c.post("/profile/trade-url", {"trade_url": ""})
    assert run_sql("SELECT SteamTradeURL FROM Пользователи WHERE Логин = 'trader'")[0][0] is None


def test_order_detail_access_and_trade_link(make_client, steam_store):
    seller = make_seller(make_client, "seller")
    new_steam_listing(seller, steam_store, price="500", name="AK-47 | Redline", assetid="9501")
    trade = "https://steamcommunity.com/tradeoffer/new/?partner=123456789&token=AbCdEf12"
    seller.post("/profile/trade-url", {"trade_url": trade})
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "1000"})
    buyer.post("/buy/1", {"qty": "1"})

    page = buyer.get("/orders/1")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Заказ №1" in html
    assert "Открыть обмен в Steam" in html
    # В HTML амперсанд экранируется, поэтому сравниваем с &amp;.
    assert trade.replace("&", "&amp;") in html  # ссылка обмена продавца
    assert "Чат с seller" in html

    seller_html = seller.get("/orders/1").get_data(as_text=True)
    assert "Проверить свою ссылку обмена" in seller_html
    assert "Чат с buyer" in seller_html

    stranger = make_client()
    stranger.register("stranger")
    assert stranger.get("/orders/1").status_code == 403
    assert make_client().get("/orders/1").status_code == 302
    assert stranger.post("/orders/1/messages", {"text": "чужой"}).status_code == 403


def test_order_chat_and_unread_counter(make_client, steam_store, run_sql):
    seller = make_seller(make_client, "seller")
    new_steam_listing(seller, steam_store, price="500", name="AWP | Asiimov", assetid="9601")
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "1000"})
    buyer.post("/buy/1", {"qty": "1"})

    assert buyer.post("/orders/1/messages", {"text": "Привет! Когда сможете отправить обмен?"}).status_code == 302
    assert run_sql("SELECT COUNT(*) FROM СообщенияЗаказов")[0][0] == 1
    assert buyer.post("/orders/1/messages", {"text": ""}).status_code == 302
    assert run_sql("SELECT COUNT(*) FROM СообщенияЗаказов")[0][0] == 1
    assert buyer.post("/orders/1/messages", {"text": "x" * 1001}).status_code == 302
    assert run_sql("SELECT COUNT(*) FROM СообщенияЗаказов")[0][0] == 1

    # У продавца в шапке появляется счётчик, у автора сообщения — нет.
    seller_home = seller.get("/").get_data(as_text=True)
    assert "msg-badge" in seller_home
    assert "Сообщения" in seller_home
    assert "msg-badge" not in buyer.get("/").get_data(as_text=True)

    # Открытие заказа помечает сообщение прочитанным.
    seller_page = seller.get("/orders/1").get_data(as_text=True)
    assert "Привет! Когда сможете отправить обмен?" in seller_page
    assert "msg-badge" not in seller.get("/").get_data(as_text=True)
    assert run_sql("SELECT Прочитано FROM СообщенияЗаказов")[0][0] == 1

    seller.post("/orders/1/messages", {"text": "Готов, отправляйте обмен"})
    assert "msg-badge" in buyer.get("/").get_data(as_text=True)
    conversations = buyer.get("/messages").get_data(as_text=True)
    assert "Готов, отправляйте обмен" in conversations
    assert "заказ №1" in conversations
    assert "AWP | Asiimov" in conversations
    buyer.get("/orders/1")
    assert "msg-badge" not in buyer.get("/").get_data(as_text=True)


def test_messages_page_empty(make_client):
    c = make_client()
    c.register("silent")
    html = c.get("/messages").get_data(as_text=True)
    assert "Сообщений пока нет" in html


# --------------------------------------- подтверждение получения и споры
def order_status(run_sql, order_id=1):
    return run_sql(
        "SELECT st.НазваниеСтатуса FROM Заказы z "
        "JOIN СтатусыЗаказов st ON z.КодСтатуса = st.КодСтатуса WHERE z.КодЗаказа = ?",
        (order_id,))[0][0]


def paid_order(make_client, steam_store, price="500", assetid="9701"):
    seller = make_seller(make_client, "seller")
    new_steam_listing(seller, steam_store, price=price, name="AK-47 | Redline", assetid=assetid)
    buyer = make_client()
    buyer.register("buyer")
    buyer.post("/profile/topup", {"amount": "1000"})
    buyer.post("/buy/1", {"qty": "1"})
    return seller, buyer


def test_order_flow_states_and_permissions(make_client, steam_store, run_sql):
    seller, buyer = paid_order(make_client, steam_store)
    assert order_status(run_sql) == "Оплачен"

    # Покупатель не может отправлять, продавец — подтверждать и открывать спор.
    assert buyer.post("/orders/1/send").status_code == 403
    assert seller.post("/orders/1/confirm").status_code == 403
    assert seller.post("/orders/1/dispute", {"reason": "Не хочу отдавать"}).status_code == 403

    # Подтверждение до отправки ничего не выплачивает.
    buyer.post("/orders/1/confirm")
    assert order_status(run_sql) == "Оплачен"
    assert balance(run_sql, "seller") == 0

    seller.post("/orders/1/send")
    assert order_status(run_sql) == "Отправлен"
    assert balance(run_sql, "seller") == 0
    assert run_sql("SELECT ДатаОтправки FROM Заказы WHERE КодЗаказа = 1")[0][0]

    # Продавец видит кнопку отправки, покупатель — подтверждение и спор.
    seller_html = seller.get("/orders/1").get_data(as_text=True)
    assert "Товар отправлен" not in seller_html
    buyer_html = buyer.get("/orders/1").get_data(as_text=True)
    assert "Подтвердить получение" in buyer_html
    assert "Открыть спор" in buyer_html
    assert "сама закроется автоматически" not in buyer_html
    assert "автоматически" in buyer_html
    orders_html = buyer.get("/orders").get_data(as_text=True)
    assert "Подтвердить получение" in orders_html

    buyer.post("/orders/1/confirm")
    assert order_status(run_sql) == "Выполнен"
    assert balance(run_sql, "seller") == 500
    assert "Выполнен" in buyer.get("/orders").get_data(as_text=True)


def test_order_auto_confirm_after_24_hours(make_client, steam_store, run_sql):
    seller, buyer = paid_order(make_client, steam_store, assetid="9702")
    seller.post("/orders/1/send")
    run_sql("UPDATE Заказы SET ДатаОтправки = '2020-01-01 08:00:00' WHERE КодЗаказа = 1")
    assert balance(run_sql, "seller") == 0

    # Любой запрос запускает ленивую проверку 24 часов.
    buyer.get("/orders")
    assert order_status(run_sql) == "Выполнен"
    assert balance(run_sql, "seller") == 500
    assert "Продажа" in [row[0] for row in run_sql("SELECT Тип FROM Транзакции")]
    # После выполнения можно оставить отзыв.
    buyer.post("/orders/1/review", {"rating": "4", "text": "Закрылось само"})
    assert run_sql("SELECT COUNT(*) FROM Отзывы")[0][0] == 1


def test_dispute_freezes_auto_confirm_and_admin_refunds(make_client, steam_store, run_sql):
    seller, buyer = paid_order(make_client, steam_store, assetid="9703")
    seller.post("/orders/1/send")

    # Причина обязательна.
    buyer.post("/orders/1/dispute", {"reason": "ой"})
    assert order_status(run_sql) == "Отправлен"
    buyer.post("/orders/1/dispute", {"reason": "Скин не пришёл, обмен отклонён"})
    assert order_status(run_sql) == "Спор"

    # Даже со старой датой отправки спор не закрывается автоматически.
    run_sql("UPDATE Заказы SET ДатаОтправки = '2020-01-01 08:00:00' WHERE КодЗаказа = 1")
    buyer.get("/orders")
    assert order_status(run_sql) == "Спор"
    assert balance(run_sql, "seller") == 0

    admin = make_admin_client(make_client, run_sql)
    html = admin.get("/admin").get_data(as_text=True)
    assert "Споры" in html
    assert "Скин не пришёл, обмен отклонён" in html
    assert "Вернуть покупателю" in html and "Выплатить продавцу" in html

    assert admin.post("/admin/orders/1/refund").status_code == 302
    assert order_status(run_sql) == "Отменён"
    assert balance(run_sql, "buyer") == 1000
    assert balance(run_sql, "seller") == 0
    assert admin.get("/admin").get_data(as_text=True).count("Открытых споров нет") == 1


def test_admin_can_pay_seller_in_dispute(make_client, steam_store, run_sql):
    seller, buyer = paid_order(make_client, steam_store, assetid="9704")
    seller.post("/orders/1/send")
    buyer.post("/orders/1/dispute", {"reason": "Передумал, но предмет получил"})
    admin = make_admin_client(make_client, run_sql)

    admin.post("/admin/orders/1/payout")
    assert order_status(run_sql) == "Выполнен"
    assert balance(run_sql, "seller") == 500
    assert balance(run_sql, "buyer") == 500

    # Повторное решение не начисляет деньги второй раз.
    admin.post("/admin/orders/1/payout")
    assert balance(run_sql, "seller") == 500


def test_dispute_only_after_send(make_client, steam_store, run_sql):
    seller, buyer = paid_order(make_client, steam_store, assetid="9705")
    buyer.post("/orders/1/dispute", {"reason": "Продавец молчит уже час"})
    assert order_status(run_sql) == "Оплачен"
    html = buyer.get("/orders/1").get_data(as_text=True)
    assert "Открыть спор" not in html  # до отправки достаточно отмены


def test_order_schema_migrates_old_database(tmp_path):
    """В старой базе появляются поля сделки и новые статусы."""
    import sqlite3
    import order_flow
    from db import Db

    path = tmp_path / "old_orders.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE Заказы (КодЗаказа INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE СтатусыЗаказов (КодСтатуса INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "НазваниеСтатуса TEXT NOT NULL UNIQUE)")
    conn.executemany("INSERT INTO СтатусыЗаказов (НазваниеСтатуса) VALUES (?)",
                     [("Оплачен",), ("Выполнен",), ("Отменён",)])
    conn.commit()
    conn.close()

    order_flow._SCHEMA_READY.clear()
    db = Db(str(path))
    try:
        order_flow.ensure_order_schema(db)
        columns = db.columns("Заказы")
        assert "ДатаОтправки" in columns and "ПричинаСпора" in columns
        names = {row["НазваниеСтатуса"] for row in
                 db.query("SELECT НазваниеСтатуса FROM СтатусыЗаказов")}
        assert {"Оплачен", "Отправлен", "Спор", "Выполнен", "Отменён"} <= names
        order_flow.ensure_order_schema(db)  # повторный вызов безопасен
        assert len(db.query("SELECT НазваниеСтатуса FROM СтатусыЗаказов")) == 6
    finally:
        db.close()
        order_flow._SCHEMA_READY.clear()


