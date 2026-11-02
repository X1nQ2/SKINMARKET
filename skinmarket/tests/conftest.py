import sqlite3
import sys
from pathlib import Path

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
                             UPLOAD_ROOT=str(tmp_path / "static"))
    server._lookup_cache.clear()
    return server.app


class Client:
    """Обёртка над тестовым клиентом: сама подставляет CSRF-токен в POST."""

    def __init__(self, app):
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
        return self.post("/register", {"login": login, "email": f"{login}@example.com",
                                       "password": password, "password2": password})


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
