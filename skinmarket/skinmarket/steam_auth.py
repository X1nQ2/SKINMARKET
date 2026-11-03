"""Steam OpenID 2.0 authentication and account linking for SKINMARKET."""
import re
import secrets
from urllib.parse import urlencode

import requests
from flask import abort, flash, g, redirect, request, session, url_for
from werkzeug.security import generate_password_hash

STEAM_ENDPOINT = "https://steamcommunity.com/openid/"
STEAM_ID_RE = re.compile(r"^https?://steamcommunity\.com/openid/id/([0-9]{17})/?$")


def init_steam_auth(app, get_db):
    @app.before_request
    def ensure_steam_schema():
        if app.config.get("_STEAM_SCHEMA_READY"):
            return
        db = get_db()
        if db.sqlite:
            db.execute("""CREATE TABLE IF NOT EXISTS SteamAccounts (
                SteamID TEXT(20) PRIMARY KEY,
                КодПользователя INTEGER NOT NULL UNIQUE,
                ДатаПривязки TEXT
            )""")
        else:
            try:
                db.execute("""CREATE TABLE SteamAccounts (
                    SteamID TEXT(20) NOT NULL,
                    КодПользователя LONG NOT NULL,
                    ДатаПривязки DATETIME,
                    CONSTRAINT PK_SteamAccounts PRIMARY KEY (SteamID)
                )""")
                db.commit()
            except Exception:
                db.one("SELECT TOP 1 SteamID FROM SteamAccounts")
        app.config["_STEAM_SCHEMA_READY"] = True

    def callback_url(state):
        return url_for("steam_callback", _external=True, state=state)

    def begin(purpose):
        if purpose == "link" and not g.user:
            flash("Сначала войдите в SKINMARKET.", "error")
            return redirect(url_for("login"))
        if purpose == "login" and g.user:
            return redirect(url_for("catalog"))
        state = secrets.token_urlsafe(32)
        session["steam_state"] = state
        session["steam_purpose"] = purpose
        session["steam_next"] = request.args.get("next", "")
        params = {
            "openid.ns": "http://specs.openid.net/auth/2.0",
            "openid.mode": "checkid_setup",
            "openid.return_to": callback_url(state),
            "openid.realm": request.url_root.rstrip("/"),
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        }
        return redirect(STEAM_ENDPOINT + "?" + urlencode(params))

    @app.get("/auth/steam")
    def steam_login():
        return begin("login")

    @app.get("/auth/steam/link")
    def steam_link():
        return begin("link")

    @app.get("/auth/steam/callback")
    def steam_callback():
        state = session.pop("steam_state", None)
        purpose = session.pop("steam_purpose", None)
        next_url = session.pop("steam_next", "")
        if not state or request.args.get("state") != state:
            abort(400)
        if purpose not in ("login", "link") or request.args.get("openid.mode") != "id_res":
            flash("Авторизация Steam отменена или не выполнена.", "error")
            return redirect(url_for("profile") if g.user else url_for("login"))
        if request.args.get("openid.return_to") != callback_url(state):
            abort(400)
        if request.args.get("openid.op_endpoint", "").rstrip("/") != STEAM_ENDPOINT.rstrip("/"):
            abort(400)

        claimed = request.args.get("openid.claimed_id", "")
        identity = request.args.get("openid.identity", "")
        match = STEAM_ID_RE.fullmatch(claimed)
        if not match or identity != claimed:
            abort(400)
        steam_id = match.group(1)

        verify = {k: v for k, v in request.args.items() if k.startswith("openid.")}
        verify["openid.mode"] = "check_authentication"
        try:
            response = requests.post(STEAM_ENDPOINT, data=verify, timeout=10)
            response.raise_for_status()
        except requests.RequestException:
            app.logger.exception("Steam OpenID verification failed")
            flash("Не удалось проверить ответ Steam. Попробуйте позже.", "error")
            return redirect(url_for("profile") if g.user else url_for("login"))
        if "is_valid:true" not in {line.strip().lower() for line in response.text.splitlines()}:
            abort(400)

        db = get_db()
        linked = db.one("SELECT КодПользователя AS user_id FROM SteamAccounts WHERE SteamID = ?", (steam_id,))

        if purpose == "link":
            if not g.user:
                flash("Сессия истекла. Войдите и повторите привязку.", "error")
                return redirect(url_for("login"))
            if linked and linked["user_id"] != g.user["user_id"]:
                flash("Этот Steam уже привязан к другому аккаунту.", "error")
                return redirect(url_for("profile"))
            if not linked:
                existing = db.one("SELECT SteamID FROM SteamAccounts WHERE КодПользователя = ?",
                                  (g.user["user_id"],))
                if existing:
                    flash("К профилю уже привязан Steam. Сначала отвяжите его.", "error")
                    return redirect(url_for("profile"))
                db.execute("INSERT INTO SteamAccounts (SteamID, КодПользователя, ДатаПривязки) VALUES (?, ?, ?)",
                           (steam_id, g.user["user_id"], db.now()))
            flash("Steam успешно привязан.", "success")
            return redirect(url_for("profile"))

        if linked:
            user = db.one("SELECT Заблокирован AS blocked FROM Пользователи WHERE КодПользователя = ?",
                          (linked["user_id"],))
            if not user or user["blocked"]:
                flash("Аккаунт SKINMARKET заблокирован или недоступен.", "error")
                return redirect(url_for("login"))
            session.clear()
            session["uid"] = linked["user_id"]
            flash("Вы вошли через Steam.", "success")
            return redirect(next_url if next_url.startswith("/") and not next_url.startswith("//")
                            else url_for("catalog"))

        role = db.one("SELECT КодРоли AS role_id FROM Роли WHERE НазваниеРоли = ?", ("Покупатель",))
        if not role:
            raise RuntimeError("Не найдена роль «Покупатель».")
        login_name = "steam_" + steam_id
        email = "steam_" + steam_id + "@steam.local"
        if db.one("SELECT КодПользователя FROM Пользователи WHERE Логин = ? OR Email = ?",
                  (login_name, email)):
            flash("Не удалось создать профиль автоматически. Обратитесь в поддержку.", "error")
            return redirect(url_for("login"))
        user_id = db.insert(
            "INSERT INTO Пользователи (Логин, Email, ХешПароля, КодРоли, Баланс, ДатаРегистрации, Заблокирован) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (login_name, email, generate_password_hash(secrets.token_urlsafe(48)),
             role["role_id"], 0, db.now(), False))
        db.execute("INSERT INTO SteamAccounts (SteamID, КодПользователя, ДатаПривязки) VALUES (?, ?, ?)",
                   (steam_id, user_id, db.now()))
        session.clear()
        session["uid"] = user_id
        flash("Аккаунт SKINMARKET создан и привязан к Steam.", "success")
        return redirect(url_for("profile"))

    @app.context_processor
    def inject_steam_link_status():
        user = g.get("user")
        if not user:
            return {"steam_linked": None}
        try:
            row = get_db().one("SELECT SteamID FROM SteamAccounts WHERE КодПользователя = ?",
                               (user["user_id"],))
            return {"steam_linked": row["SteamID"] if row else None}
        except Exception:
            return {"steam_linked": None}

    @app.post("/auth/steam/unlink")
    def steam_unlink():
        if not g.user:
            return redirect(url_for("login"))
        db = get_db()
        row = db.one("SELECT ХешПароля AS pw FROM Пользователи WHERE КодПользователя = ?",
                     (g.user["user_id"],))
        if row and row["pw"] and str(row["pw"]).startswith(("scrypt:", "pbkdf2:")):
            db.execute("DELETE FROM SteamAccounts WHERE КодПользователя = ?", (g.user["user_id"],))
            flash("Steam отвязан. Теперь можно входить по логину и паролю.", "success")
        else:
            flash("Сначала задайте отдельный пароль SKINMARKET, затем отвяжите Steam.", "error")
        return redirect(url_for("profile"))
