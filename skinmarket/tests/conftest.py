import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402

TOKEN = "test-token"


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript((Path(__file__).parent / "schema_sqlite.sql").read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    carts = tmp_path / "carts.json"
    carts.write_text("{}", encoding="utf-8")
    server.app.config.update(TESTING=True, DB_PATH=str(db_path), CARTS_PATH=str(carts),
                             UPLOAD_ROOT=str(tmp_path / "static"),
                             _STEAM_SCHEMA_READY=False)
    server._lookup_cache.clear()
    return server.app


class Client:
    """Обёртка над тестовым клиентом: сама подставляет CSRF-токен в POST.

    Регистрация в приложении идёт только через Steam OpenID, поэтому для тестов
    пользователь создаётся напрямую в SQLite, а в сессию кладётся его uid.
    """

    def __init__(self, app):
        self.app = app
        self.user_id = None
        self.steam_id = None
        self.http = app.test_client()
        with self.http.session_transaction() as s:
            s["csrf_token"] = TOKEN

    def get(self, url, **kw):
        return self.http.get(url, **kw)

    def post(self, url, data=None, csrf=True, **kw):
        payload = dict(data or {})
        if csrf:
            with self.http.session_transaction() as s:
                s.setdefault("csrf_token", TOKEN)
                payload["csrf_token"] = s["csrf_token"]
        return self.http.post(url, data=payload, **kw)

    def register(self, login, password="secret1"):
        """Создаёт пользователя в тестовой БД и сразу авторизует его."""
        conn = sqlite3.connect(self.app.config["DB_PATH"])
        try:
            if conn.execute("SELECT 1 FROM Пользователи WHERE Логин = ?", (login,)).fetchone():
                return SimpleNamespace(status_code=400)
            conn.execute(
                "INSERT INTO Пользователи "
                "(Логин, Email, ХешПароля, КодРоли, Баланс, ДатаРегистрации, Заблокирован) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (login, f"{login}@example.com", server.hash_password(password), 1, 0,
                 "2026-01-01 00:00:00", 0),
            )
            user_id = conn.execute("SELECT КодПользователя FROM Пользователи WHERE Логин = ?",
                                   (login,)).fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        self.user_id = user_id
        with self.http.session_transaction() as s:
            s["uid"] = user_id
        return SimpleNamespace(status_code=302)

    def link_steam(self, steam_id="76561198000000001"):
        """Привязывает тестового пользователя к Steam, чтобы работал импорт предметов."""
        if self.user_id is None:
            raise RuntimeError("Сначала вызовите register().")
        conn = sqlite3.connect(self.app.config["DB_PATH"])
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS SteamAccounts ("
                "SteamID TEXT(20) PRIMARY KEY, "
                "КодПользователя INTEGER NOT NULL UNIQUE, "
                "ДатаПривязки TEXT)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO SteamAccounts (SteamID, КодПользователя, ДатаПривязки) "
                "VALUES (?, ?, ?)",
                (steam_id, self.user_id, "2026-01-01 00:00:00"),
            )
            conn.commit()
        finally:
            conn.close()
        self.steam_id = steam_id
        return steam_id


@pytest.fixture()
def make_client(app):
    return lambda: Client(app)


def sql(app, query, params=()):
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute(query, params).fetchall()
    conn.commit()
    conn.close()
    return rows


@pytest.fixture()
def run_sql(app):
    return lambda query, params=(): sql(app, query, params)


@pytest.fixture()
def steam_store(app, monkeypatch):
    """Подменяет обращения к Steam и хранит предметы для импорта в тестах."""
    import steam_inventory

    store = {}

    def fake_fetch(steam_id, appid):
        return list(store.get((str(steam_id), str(appid)), []))

    def fake_games(_steam_id):
        return [{"appid": 730, "name": "CS2"}, {"appid": 570, "name": "Dota 2"},
                {"appid": 252490, "name": "Rust"}]

    monkeypatch.setattr(steam_inventory, "fetch_game_inventory", fake_fetch)
    monkeypatch.setattr(steam_inventory, "get_owned_games", fake_games)
    return store
