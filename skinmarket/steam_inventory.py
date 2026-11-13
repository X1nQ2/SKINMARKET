"""Steam inventories for owned games and import to SKINMARKET."""
from decimal import Decimal, InvalidOperation
import os
import re
import requests
from flask import flash, g, redirect, render_template, request, url_for

API = 'https://api.steampowered.com'
COMMUNITY = 'https://steamcommunity.com/inventory/{steamid}/{appid}/2'
IMAGE_BASE = 'https://community.cloudflare.steamstatic.com/economy/image/'


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
            # Existing older schema: add AppID if missing, preserving prior listings.
            try:
                db.execute("ALTER TABLE SteamInventoryListings ADD COLUMN AppID TEXT(12)")
                db.commit()
            except Exception:
                pass
            db.one("SELECT TOP 1 AssetID FROM SteamInventoryListings")


def get_owned_games(steam_id):
    key = (os.getenv('STEAM_API_KEY') or '').strip()
    if not key:
        raise ValueError('Для загрузки списка игр добавьте STEAM_API_KEY в файл .env (ключ Steam Web API).')
    r = requests.get(API + '/IPlayerService/GetOwnedGames/v1/', params={
        'key': key, 'steamid': steam_id, 'include_appinfo': 1, 'include_played_free_games': 1
    }, timeout=20)
    r.raise_for_status()
    data = r.json().get('response', {})
    if 'games' not in data:
        raise ValueError('Steam не вернул список игр. Проверьте публичность профиля и API-ключ.')
    return sorted(data['games'], key=lambda x: x.get('name', '').lower())


def fetch_game_inventory(steam_id, appid):
    items, cursor = [], None
    for _ in range(5):
        params = {'l': 'russian', 'count': 2000}
        if cursor: params['start_assetid'] = cursor
        r = requests.get(COMMUNITY.format(steamid=steam_id, appid=appid), params=params,
                         headers={'User-Agent': 'SKINMARKET/1.0'}, timeout=20)
        r.raise_for_status()
        payload = r.json()
        if payload.get('success') != 1:
            raise ValueError('Инвентарь этой игры недоступен или скрыт настройками приватности Steam.')
        descs = {(str(d.get('classid')), str(d.get('instanceid'))): d for d in payload.get('descriptions', [])}
        for a in payload.get('assets', []):
            d = descs.get((str(a.get('classid')), str(a.get('instanceid'))))
            if not d: continue
            icon = d.get('icon_url_large') or d.get('icon_url')
            tags = d.get('tags') or []
            rarity = next((t.get('localized_tag_name') for t in tags if t.get('category') == 'Rarity'), 'Обычное')
            items.append({'assetid': str(a.get('assetid')), 'name': d.get('market_name') or d.get('name') or 'Предмет',
                          'type': d.get('type') or '', 'image': IMAGE_BASE + icon if icon else None,
                          'tradable': bool(d.get('tradable', 0)), 'marketable': bool(d.get('marketable', 0)),
                          'rarity': rarity, 'market_hash_name': d.get('market_hash_name') or d.get('name')})
        if not payload.get('more_items'): break
        cursor = payload.get('last_assetid')
        if not cursor: break
    return items


def init_steam_inventory(app, get_db):
    @app.get('/profile/inventory')
    def steam_inventory_page():
        if not g.user: return redirect(url_for('login'))
        db = get_db(); _ensure_table(db)
        linked = db.one('SELECT SteamID FROM SteamAccounts WHERE КодПользователя = ?', (g.user['user_id'],))
        if not linked:
            return render_template('inventory.html', items=[], games=[], steam_id=None, selected_appid='',
                                   inventory_error='К аккаунту SKINMARKET не привязан Steam.', imported=set())
        try:
            games = get_owned_games(linked['SteamID'])
            selected = str(request.args.get('appid') or (games[0]['appid'] if games else ''))
            game = next((x for x in games if str(x['appid']) == selected), None)
            items = fetch_game_inventory(linked['SteamID'], selected) if game else []
            rows = db.query('SELECT AssetID FROM SteamInventoryListings WHERE SteamID = ? AND AppID = ? AND КодПользователя = ?',
                            (linked['SteamID'], selected, g.user['user_id'])) if selected else []
            return render_template('inventory.html', items=items, games=games, steam_id=linked['SteamID'],
                                   selected_appid=selected, selected_game=game, inventory_error=None,
                                   imported={x['AssetID'] for x in rows})
        except (requests.RequestException, ValueError, KeyError) as exc:
            app.logger.warning('Steam inventory error: %s', exc)
            return render_template('inventory.html', items=[], games=[], steam_id=linked['SteamID'],
                                   selected_appid='', inventory_error=str(exc), imported=set())

    @app.post('/profile/inventory/list')
    def list_steam_item():
        if not g.user: return redirect(url_for('login'))
        db = get_db(); _ensure_table(db)
        linked = db.one('SELECT SteamID FROM SteamAccounts WHERE КодПользователя = ?', (g.user['user_id'],))
        if not linked: return redirect(url_for('profile'))
        appid = re.sub(r'\D', '', request.form.get('appid', ''))[:10]
        asset_id = request.form.get('assetid', '')[:40]
        game_name = request.form.get('game_name', 'Steam game')[:120]
        try:
            price = Decimal(request.form.get('price', '').replace(',', '.')).quantize(Decimal('0.01'))
            if not appid or not asset_id or price <= 0 or price > Decimal('10000000'): raise ValueError
            items = fetch_game_inventory(linked['SteamID'], appid)
        except (InvalidOperation, ValueError, requests.RequestException):
            flash('Не удалось проверить предмет или цену. Проверьте инвентарь и повторите.', 'error')
            return redirect(url_for('steam_inventory_page', appid=appid))
        item = next((x for x in items if x['assetid'] == asset_id), None)
        if not item or not item['tradable']:
            flash('Предмет отсутствует в инвентаре или его нельзя передавать.', 'error')
            return redirect(url_for('steam_inventory_page', appid=appid))
        if db.one('SELECT AssetID FROM SteamInventoryListings WHERE SteamID = ? AND AppID = ? AND AssetID = ?',
                  (linked['SteamID'], appid, asset_id)):
            flash('Этот предмет уже добавлен в SKINMARKET.', 'error')
            return redirect(url_for('steam_inventory_page', appid=appid))
        game = db.one('SELECT КодИгры AS id FROM Игры WHERE НазваниеИгры = ?', (game_name,))
        game_id = game['id'] if game else db.insert('INSERT INTO Игры (НазваниеИгры) VALUES (?)', (game_name,))
        category = db.one('SELECT КодКатегории AS id FROM Категории WHERE КодИгры = ?', (game_id,))
        category_id = category['id'] if category else db.insert('INSERT INTO Категории (КодИгры, НазваниеКатегории) VALUES (?, ?)', (game_id, 'Предметы'))
        rarity_text = (item.get('rarity') or '').lower()
        rarity_name = 'Легендарное' if any(x in rarity_text for x in ('covert','extraordinary','контрабанд','легендар')) else 'Эпическое' if any(x in rarity_text for x in ('classified','remarkable','засекреч','эпичес')) else 'Редкое' if any(x in rarity_text for x in ('restricted','mil-spec','запрещ','редк')) else 'Обычное'
        rarity = db.one('SELECT КодРедкости AS id FROM Редкости WHERE НазваниеРедкости = ?', (rarity_name,))
        rarity_id = rarity['id'] if rarity else db.insert('INSERT INTO Редкости (НазваниеРедкости) VALUES (?)', (rarity_name,))
        product_id = db.insert('INSERT INTO Товары (КодИгры, КодКатегории, КодРедкости, Название, Описание, URLИзображения) VALUES (?, ?, ?, ?, ?, ?)',
            (game_id, category_id, rarity_id, item['name'], f"Предмет из инвентаря Steam · AppID {appid} · AssetID {asset_id}", item['image']))
        seller = db.one('SELECT КодРоли AS id FROM Роли WHERE НазваниеРоли = ?', ('Продавец',))
        if seller and g.user['role_name'] == 'Покупатель':
            db.execute('UPDATE Пользователи SET КодРоли = ? WHERE КодПользователя = ?', (seller['id'], g.user['user_id']))
        active = db.one('SELECT КодСтатуса AS id FROM СтатусыОбъявлений WHERE НазваниеСтатуса = ?', ('Активно',))
        listing_id = db.insert('INSERT INTO Объявления (КодПродавца, КодТовара, Цена, Количество, КодСтатуса, ДатаСоздания) VALUES (?, ?, ?, ?, ?, ?)',
            (g.user['user_id'], product_id, float(price), 1, active['id'], db.now()))
        db.execute('INSERT INTO SteamInventoryListings (SteamID, AppID, AssetID, КодПользователя, КодОбъявления) VALUES (?, ?, ?, ?, ?)',
                   (linked['SteamID'], appid, asset_id, g.user['user_id'], listing_id))
        flash('Объявление создано из инвентаря Steam. Передача предмета покупателю пока выполняется вручную.', 'success')
        return redirect(url_for('catalog'))
