"""Steam-инвентарь: список игр и добавление предметов в каталог SKINMARKET.

Предметы можно добавлять только из реального инвентаря Steam. Если инвентарь
закрыт настройками приватности, Steam отвечает 401/403 — показываем понятную
инструкцию вместо технической ошибки.
"""
from decimal import Decimal, InvalidOperation
from html import unescape
import os
import re
import time

import requests
from flask import flash, g, redirect, render_template, request, url_for

from games import ALLOWED_GAMES, game_name_for_appid, is_allowed_appid

API = "https://api.steampowered.com"
COMMUNITY = "https://steamcommunity.com/inventory/{steamid}/{appid}/2"
IMAGE_BASE = "https://community.cloudflare.steamstatic.com/economy/image/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}

PRIVATE_MESSAGE = (
    "Инвентарь Steam закрыт настройками приватности. Откройте Steam → Профиль → "
    "Изменить профиль → Настройки приватности → «Инвентарь» → «Открытый» "
    "(и «Общие сведения» → «Открытые»), затем обновите эту страницу."
)

# Кэш инвентаря, чтобы не дёргать Steam на каждой странице и не получать 429.
INVENTORY_CACHE = {}
INVENTORY_CACHE_TTL = 300


def clear_inventory_cache():
    INVENTORY_CACHE.clear()


class SteamInventoryError(Exception):
    """Понятная пользователю ошибка Steam."""


class SteamInventoryPrivate(SteamInventoryError):
    """Инвентарь недоступен из-за приватности."""


def _ensure_table(db):
    if db.sqlite:
        db.execute("""CREATE TABLE IF NOT EXISTS SteamInventoryListings (
            SteamID TEXT NOT NULL, AppID TEXT NOT NULL DEFAULT '730', AssetID TEXT NOT NULL,
            КодПользователя INTEGER NOT NULL, КодОбъявления INTEGER NOT NULL,
            PRIMARY KEY (SteamID, AppID, AssetID))""")
        columns = {r[1] for r in db.conn.execute("PRAGMA table_info(SteamInventoryListings)").fetchall()}
        if "AppID" not in columns:
            db.execute("ALTER TABLE SteamInventoryListings ADD COLUMN AppID TEXT NOT NULL DEFAULT '730'")
    else:
        try:
            db.execute("""CREATE TABLE SteamInventoryListings (
                SteamID TEXT(20) NOT NULL, AppID TEXT(12) NOT NULL, AssetID TEXT(40) NOT NULL,
                КодПользователя LONG NOT NULL, КодОбъявления LONG NOT NULL,
                CONSTRAINT PK_SteamInventoryListings PRIMARY KEY (SteamID, AppID, AssetID))""")
            db.commit()
        except Exception:
            # Старая схема: добавляем AppID, если его ещё нет.
            try:
                db.execute("ALTER TABLE SteamInventoryListings ADD COLUMN AppID TEXT(12)")
                db.commit()
            except Exception:
                pass
            db.one("SELECT TOP 1 AssetID FROM SteamInventoryListings")


def get_owned_games(steam_id):
    """Игры пользователя, оставляем только разрешённые для каталога."""
    key = (os.getenv("STEAM_API_KEY") or "").strip()
    if not key:
        raise SteamInventoryError(
            "Для загрузки списка игр добавьте STEAM_API_KEY в файл .env "
            "(ключ Steam Web API: https://steamcommunity.com/dev/apikey)."
        )
    try:
        response = requests.get(API + "/IPlayerService/GetOwnedGames/v1/", params={
            "key": key, "steamid": steam_id, "include_appinfo": 1,
            "include_played_free_games": 1,
        }, timeout=20)
        response.raise_for_status()
        data = response.json().get("response", {})
    except requests.RequestException:
        raise SteamInventoryError("Не удалось получить список игр Steam. Проверьте интернет и API-ключ.")
    except ValueError:
        raise SteamInventoryError("Steam вернул некорректный список игр. Попробуйте позже.")
    if "games" not in data:
        raise SteamInventoryError(
            "Steam не вернул список игр. Проверьте настройки приватности: профиль и «Детали игр» "
            "должны быть открыты, а API-ключ действителен."
        )
    games = [game for game in data["games"] if is_allowed_appid(game.get("appid"))]
    return sorted(games, key=lambda x: x.get("name", "").lower())


def _plain_steam_text(value):
    """Превращает HTML/BBCode-описание Steam в обычный текст."""
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\[/?[^\]]+\]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def fetch_game_inventory(steam_id, appid):
    """Возвращает список предметов инвентаря Steam для игры appid.

    Ответ Steam кэшируется на 5 минут: страница инвентаря и подгрузка описания
    товара не должны каждый раз заново запрашивать весь инвентарь и ловить 429.
    """
    appid = str(appid or "").strip()
    if not is_allowed_appid(appid):
        raise SteamInventoryError("Добавлять предметы можно только из CS2, Dota 2 и Rust.")

    key = (str(steam_id), appid)
    cached = INVENTORY_CACHE.get(key)
    if cached and time.time() - cached[0] < INVENTORY_CACHE_TTL:
        return list(cached[1])

    items, cursor = [], None
    for _ in range(5):
        params = {"l": "english", "count": 2000}
        if cursor:
            params["start_assetid"] = cursor
        try:
            response = requests.get(
                COMMUNITY.format(steamid=steam_id, appid=appid),
                params=params, headers=HEADERS, timeout=20,
            )
            response.raise_for_status()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            if status in (401, 403):
                raise SteamInventoryPrivate(PRIVATE_MESSAGE)
            if status == 429:
                raise SteamInventoryError("Steam временно ограничил запросы. Подождите минуту и повторите.")
            raise SteamInventoryError(f"Steam вернул ошибку {status or 'сети'}. Попробуйте позже.")
        except requests.RequestException:
            raise SteamInventoryError("Не удалось связаться со Steam. Проверьте интернет и повторите.")

        try:
            payload = response.json()
        except ValueError:
            raise SteamInventoryError("Steam вернул некорректный ответ. Попробуйте позже.")

        if payload.get("success") != 1:
            error_text = str(payload.get("Error") or "").lower()
            if "private" in error_text or "closed" in error_text:
                raise SteamInventoryPrivate(PRIVATE_MESSAGE)
            raise SteamInventoryError(
                "Инвентарь этой игры недоступен: он скрыт настройками приватности Steam "
                "или у аккаунта нет предметов."
            )

        descriptions = {
            (str(d.get("classid")), str(d.get("instanceid"))): d
            for d in payload.get("descriptions", [])
        }
        for asset in payload.get("assets", []):
            description = descriptions.get((str(asset.get("classid")), str(asset.get("instanceid"))))
            if not description:
                continue
            icon = description.get("icon_url_large") or description.get("icon_url")
            tags = description.get("tags") or []
            rarity = next(
                (t.get("localized_tag_name") for t in tags if t.get("category") == "Rarity"),
                "Обычное",
            )
            description_lines = [
                _plain_steam_text(entry.get("value"))
                for entry in (description.get("descriptions") or [])
                if isinstance(entry, dict)
            ]
            description_text = "\n\n".join(line for line in description_lines if line)[:2000]
            items.append({
                "assetid": str(asset.get("assetid")),
                "name": description.get("market_name") or description.get("name") or "Предмет",
                "type": description.get("type") or "",
                "image": IMAGE_BASE + icon if icon else None,
                "tradable": bool(description.get("tradable", 0)),
                "marketable": bool(description.get("marketable", 0)),
                "rarity": rarity,
                "rarity_ru": _rarity_name(rarity),
                "market_hash_name": description.get("market_hash_name") or description.get("name"),
                "description": description_text,
            })
        if not payload.get("more_items"):
            break
        cursor = payload.get("last_assetid")
        if not cursor:
            break
    INVENTORY_CACHE[key] = (time.time(), items)
    return items


def _rarity_name(text):
    text = (text or "").lower()
    if any(x in text for x in ("covert", "contraband", "extraordinary", "exceedingly rare",
                               "rare special", "легендар", "контрабанд")):
        return "Легендарное"
    if any(x in text for x in ("classified", "remarkable", "exotic", "эпичес", "засекреч")):
        return "Эпическое"
    if any(x in text for x in ("restricted", "mil-spec", "high grade", "запрещ", "редк")):
        return "Редкое"
    return "Обычное"


def init_steam_inventory(app, get_db):
    def render_inventory(**overrides):
        """Единый набор переменных для страницы инвентаря."""
        context = {
            "items": [], "games": [], "steam_id": None, "selected_appid": "",
            "selected_game": None, "inventory_error": None, "imported": set(),
            "rarities": [], "q": "", "rarity_filter": "", "tradable_only": False,
            "page": 1, "pages": 1, "total_items": 0, "all_total": 0,
            "selected_item": None, "game_error": None,
            "category_options": [], "rarity_options": [],
        }
        context.update(overrides)
        return render_template("inventory.html", **context)

    @app.get("/profile/inventory")
    def steam_inventory_page():
        if not g.user:
            return redirect(url_for("login"))
        db = get_db()
        _ensure_table(db)
        linked = db.one("SELECT SteamID FROM SteamAccounts WHERE КодПользователя = ?", (g.user["user_id"],))
        if not linked:
            return render_inventory(inventory_error="К аккаунту SKINMARKET не привязан Steam.")
        try:
            owned = get_owned_games(linked["SteamID"])
            owned_ids = {str(game["appid"]) for game in owned}
            # Показываем все три игры каталога, даже если игры нет в библиотеке:
            # так видно, что Dota 2 поддерживается, а не «пропала» из списка.
            games = [
                {"appid": appid, "name": name, "owned": str(appid) in owned_ids}
                for name, appid in ALLOWED_GAMES.items()
            ]
            default_appid = next((game["appid"] for game in games if game["owned"]), games[0]["appid"])
            selected = str(request.args.get("appid") or default_appid)
            if not is_allowed_appid(selected):
                selected = str(default_appid)
            game = next(x for x in games if str(x["appid"]) == selected)

            game_error = None
            try:
                items = fetch_game_inventory(linked["SteamID"], selected)
            except SteamInventoryError as exc:
                app.logger.warning("Steam inventory error: %s", exc)
                items, game_error = [], str(exc)

            # Поиск и фильтры не меняют инвентарь Steam, только то, что видно на странице.
            query = " ".join((request.args.get("q") or "").split())[:80]
            rarity_filter = (request.args.get("rarity") or "").strip()
            tradable_only = request.args.get("tradable") == "1"
            rarities = sorted({
                item.get("rarity_ru") or item.get("rarity")
                for item in items if item.get("rarity_ru") or item.get("rarity")
            })
            filtered = items
            if query:
                words = query.casefold().split()
                filtered = [
                    item for item in filtered
                    if all(word in f"{item['name']} {item['type']}".casefold() for word in words)
                ]
            if rarity_filter in rarities:
                filtered = [
                    item for item in filtered
                    if (item.get("rarity_ru") or item.get("rarity")) == rarity_filter
                ]
            else:
                rarity_filter = ""
            if tradable_only:
                filtered = [item for item in filtered if item["tradable"]]

            per_page = 25
            pages = max(1, (len(filtered) + per_page - 1) // per_page)
            try:
                page = int(request.args.get("page", "1"))
            except ValueError:
                page = 1
            page = min(max(page, 1), pages)
            start = (page - 1) * per_page
            page_items = filtered[start:start + per_page]

            wanted_asset = (request.args.get("item") or "").strip()
            selected_item = next(
                (item for item in page_items if item["assetid"] == wanted_asset), None
            ) or (page_items[0] if page_items else None)

            rows = db.query(
                "SELECT AssetID FROM SteamInventoryListings "
                "WHERE SteamID = ? AND AppID = ? AND КодПользователя = ?",
                (linked["SteamID"], selected, g.user["user_id"]),
            ) if selected else []

            game_row = db.one(
                "SELECT КодИгры AS id FROM Игры WHERE НазваниеИгры = ?", (game["name"],)
            )
            category_rows = db.query(
                "SELECT НазваниеКатегории AS cat_name FROM Категории WHERE КодИгры = ? ORDER BY НазваниеКатегории",
                (game_row["id"],),
            ) if game_row else []
            rarity_rows = db.query("SELECT НазваниеРедкости AS rarity_name FROM Редкости ORDER BY НазваниеРедкости")

            return render_inventory(
                items=page_items, games=games, steam_id=linked["SteamID"],
                selected_appid=selected, selected_game=game, imported={x["AssetID"] for x in rows},
                rarities=rarities, q=query, rarity_filter=rarity_filter,
                tradable_only=tradable_only, page=page, pages=pages,
                total_items=len(filtered), all_total=len(items),
                selected_item=selected_item, game_error=game_error,
                category_options=[row["cat_name"] for row in category_rows],
                rarity_options=[row["rarity_name"] for row in rarity_rows]
                or ["Обычное", "Редкое", "Эпическое", "Легендарное"],
            )
        except (SteamInventoryError, ValueError, KeyError) as exc:
            app.logger.warning("Steam inventory error: %s", exc)
            return render_inventory(steam_id=linked["SteamID"], inventory_error=str(exc))

    @app.post("/profile/inventory/list")
    def list_steam_item():
        if not g.user:
            return redirect(url_for("login"))
        db = get_db()
        _ensure_table(db)
        linked = db.one("SELECT SteamID FROM SteamAccounts WHERE КодПользователя = ?", (g.user["user_id"],))
        if not linked:
            return redirect(url_for("profile"))

        appid = re.sub(r"\D", "", request.form.get("appid", ""))[:10]
        game_name = game_name_for_appid(appid)
        if not game_name:
            flash("Добавлять предметы можно только из CS2, Dota 2 и Rust.", "error")
            return redirect(url_for("steam_inventory_page"))

        asset_id = re.sub(r"\D", "", request.form.get("assetid", ""))[:40]
        try:
            price = Decimal(request.form.get("price", "").replace(",", ".")).quantize(Decimal("0.01"))
            if not asset_id or price <= 0 or price > Decimal("10000000"):
                raise ValueError
        except (InvalidOperation, ValueError):
            flash("Не удалось проверить предмет или цену. Проверьте инвентарь и повторите.", "error")
            return redirect(url_for("steam_inventory_page", appid=appid))

        try:
            items = fetch_game_inventory(linked["SteamID"], appid)
        except SteamInventoryError as error:
            flash(str(error), "error")
            return redirect(url_for("steam_inventory_page", appid=appid))

        item = next((x for x in items if x["assetid"] == asset_id), None)
        if not item or not item["tradable"]:
            flash("Предмет отсутствует в инвентаре или его нельзя передавать.", "error")
            return redirect(url_for("steam_inventory_page", appid=appid))
        if db.one(
            "SELECT AssetID FROM SteamInventoryListings WHERE SteamID = ? AND AppID = ? AND AssetID = ?",
            (linked["SteamID"], appid, asset_id),
        ):
            flash("Этот предмет уже добавлен в SKINMARKET.", "error")
            return redirect(url_for("steam_inventory_page", appid=appid))

        game = db.one("SELECT КодИгры AS id FROM Игры WHERE НазваниеИгры = ?", (game_name,))
        game_id = game["id"] if game else db.insert("INSERT INTO Игры (НазваниеИгры) VALUES (?)", (game_name,))

        # Категорию и редкость продавец выбирает на странице инвентаря.
        category_choice = " ".join((request.form.get("category") or "").split())[:40]
        category_name = category_choice or "Предметы"
        category = db.one(
            "SELECT КодКатегории AS id FROM Категории WHERE КодИгры = ? AND НазваниеКатегории = ?",
            (game_id, category_name),
        )
        category_id = category["id"] if category else db.insert(
            "INSERT INTO Категории (КодИгры, НазваниеКатегории) VALUES (?, ?)",
            (game_id, category_name))

        rarity_options = {
            row["rarity_name"] for row in db.query("SELECT НазваниеРедкости AS rarity_name FROM Редкости")
        }
        rarity_choice = (request.form.get("rarity") or "").strip()
        rarity_name = rarity_choice if rarity_choice in rarity_options else _rarity_name(item.get("rarity"))
        rarity = db.one("SELECT КодРедкости AS id FROM Редкости WHERE НазваниеРедкости = ?", (rarity_name,))
        rarity_id = rarity["id"] if rarity else db.insert(
            "INSERT INTO Редкости (НазваниеРедкости) VALUES (?)", (rarity_name,))

        # Один и тот же предмет в одной игре — одна запись каталога.
        product = db.one(
            "SELECT КодТовара AS item_id, URLИзображения AS image_url, Описание AS description "
            "FROM Товары WHERE КодИгры = ? AND Название = ?", (game_id, item["name"]))
        steam_description = item.get("description") or (
            f"{item['name']} — предмет из инвентаря Steam для игры {game_name}."
        )
        if product:
            product_id = product["item_id"]
            updates, params = [], []
            if item["image"] and not product["image_url"]:
                updates.append("URLИзображения = ?")
                params.append(item["image"])
            old_description = (product["description"] or "").strip()
            if steam_description and (not old_description
                                      or old_description.startswith("Предмет из инвентаря Steam")):
                updates.append("Описание = ?")
                params.append(steam_description)
            # Если предмет уже был в каталоге, выбор продавца всё равно применяем:
            # иначе категория навсегда остаётся той, что подставилась при первом добавлении.
            if category_choice:
                updates.append("КодКатегории = ?")
                params.append(category_id)
            if rarity_choice in rarity_options:
                updates.append("КодРедкости = ?")
                params.append(rarity_id)
            if updates:
                db.execute(
                    f"UPDATE Товары SET {', '.join(updates)} WHERE КодТовара = ?",
                    (*params, product_id),
                )
        else:
            product_id = db.insert(
                "INSERT INTO Товары (КодИгры, КодКатегории, КодРедкости, Название, Описание, URLИзображения) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (game_id, category_id, rarity_id, item["name"], steam_description, item["image"]))

        seller = db.one("SELECT КодРоли AS id FROM Роли WHERE НазваниеРоли = ?", ("Продавец",))
        if seller and g.user["role_name"] == "Покупатель":
            db.execute("UPDATE Пользователи SET КодРоли = ? WHERE КодПользователя = ?",
                       (seller["id"], g.user["user_id"]))
        active = db.one("SELECT КодСтатуса AS id FROM СтатусыОбъявлений WHERE НазваниеСтатуса = ?", ("Активно",))
        listing_id = db.insert(
            "INSERT INTO Объявления (КодПродавца, КодТовара, Цена, Количество, КодСтатуса, ДатаСоздания) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (g.user["user_id"], product_id, price, 1, active["id"], db.now()))
        db.execute(
            "INSERT INTO SteamInventoryListings (SteamID, AppID, AssetID, КодПользователя, КодОбъявления) "
            "VALUES (?, ?, ?, ?, ?)",
            (linked["SteamID"], appid, asset_id, g.user["user_id"], listing_id))
        flash("Объявление создано из инвентаря Steam. Передача предмета покупателю пока выполняется вручную "
              "через Steam Trade Offer.", "success")
        return redirect(url_for("sell"))
