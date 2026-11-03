"""Steam OpenID 2.0 login, new-account creation and account linking."""
import re
import secrets
from urllib.parse import urlencode

import requests
from flask import abort, flash, g, redirect, request, session, url_for
from werkzeug.security import generate_password_hash

STEAM_ENDPOINT = "https://steamcommunity.com/openid/login"
STEAM_ID_RE = re.compile(
    r"^https?://steamcommunity\.com/openid/id/([0-9]{17})/?$"
)


def init_steam_auth(app, get_db):
    """Register Steam routes and lazily create the SteamAccounts table."""

    @app.before_request
    def ensure_steam_schema():
        if app.config.get("_STEAM_SCHEMA_READY"):
            return

        db = get_db()
        if db.sqlite:
            db.execute(
                """CREATE TABLE IF NOT EXISTS SteamAccounts (
                    SteamID TEXT(20) PRIMARY KEY,
                    КодПользователя INTEGER NOT NULL UNIQUE,
                    ДатаПривязки TEXT
                )"""
            )
        else:
            try:
                db.execute(
                    """CREATE TABLE SteamAccounts (
                        SteamID TEXT(20) NOT NULL,
                        КодПользователя LONG NOT NULL,
                        ДатаПривязки DATETIME,
                        CONSTRAINT PK_SteamAccounts PRIMARY KEY (SteamID)
                    )"""
                )
                db.commit()
            except Exception:
                # A pre-existing table is fine; a missing/broken table must still fail.
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
        # Consume the state exactly once; compare it before trusting any Steam data.
        expected_state = session.pop("steam_state", None)
        purpose = session.pop("steam_purpose", None)
        next_url = session.pop("steam_next", "")
        if not expected_state or request.args.get("state") != expected_state:
            abort(400)

        if purpose not in ("login", "link") or request.args.get("openid.mode") != "id_res":
            flash("Авторизация Steam отменена или не выполнена.", "error")
            return redirect(url_for("profile") if g.user else url_for("login"))

        if request.args.get("openid.return_to") != callback_url(expected_state):
            abort(400)
        if request.args.get("openid.op_endpoint", "").rstrip("/") != STEAM_ENDPOINT.rstrip("/"):
            abort(400)

        claimed_id = request.args.get("openid.claimed_id", "")
        identity = request.args.get("openid.identity", "")
        match = STEAM_ID_RE.fullmatch(claimed_id)
        if not match or identity != claimed_id:
            abort(400)
        steam_id = match.group(1)

        # Verify the signed assertion directly with Steam (not merely its query parameters).
        verification_data = {
            key: value for key, value in request.args.items()
            if key.startswith("openid.")
        }
        verification_data["openid.mode"] = "check_authentication"
        try:
            response = requests.post(
                STEAM_ENDPOINT, data=verification_data, timeout=12
            )
            response.raise_for_status()
        except requests.RequestException:
            app.logger.exception("Steam OpenID verification failed")
            flash("Не удалось проверить ответ Steam. Попробуйте позже.", "error")
            return redirect(url_for("profile") if g.user else url_for("login"))

        verified_lines = {
            line.strip().lower() for line in response.text.splitlines()
        }
        if "is_valid:true" not in verified_lines:
            abort(400)

        db = get_db()
        linked = db.one(
            "SELECT КодПользователя AS user_id FROM SteamAccounts WHERE SteamID = ?",
            (steam_id,),
        )

        if purpose == "link":
            # Linking is permitted only inside an already authenticated local account.
            if not g.user:
                flash("Сессия истекла. Войдите и повторите привязку.", "error")
                return redirect(url_for("login"))
            if linked and linked["user_id"] != g.user["user_id"]:
                flash("Этот Steam уже привязан к другому аккаунту.", "error")
                return redirect(url_for("profile"))
            if not linked:
                current_link = db.one(
                    "SELECT SteamID FROM SteamAccounts WHERE КодПользователя = ?",
                    (g.user["user_id"],),
                )
                if current_link:
                    flash("К профилю уже привязан Steam.", "error")
                    return redirect(url_for("profile"))
                db.execute(
                    "INSERT INTO SteamAccounts "
                    "(SteamID, КодПользователя, ДатаПривязки) VALUES (?, ?, ?)",
                    (steam_id, g.user["user_id"], db.now()),
                )
            flash("Steam успешно привязан.", "success")
            return redirect(url_for("profile"))

        # Login flow: an existing Steam mapping signs into its linked SKINMARKET user.
        if linked:
            user = db.one(
                "SELECT Заблокирован AS blocked FROM Пользователи "
                "WHERE КодПользователя = ?",
                (linked["user_id"],),
            )
            if not user or user["blocked"]:
                flash("Аккаунт SKINMARKET заблокирован или недоступен.", "error")
                return redirect(url_for("login"))
            session.clear()
            session["uid"] = linked["user_id"]
            flash("Вы вошли через Steam.", "success")
            return redirect(
                next_url if next_url.startswith("/") and not next_url.startswith("//")
                else url_for("catalog")
            )

        # First Steam login creates a local buyer account. Steam OpenID has no email.
        role = db.one(
            "SELECT КодРоли AS role_id FROM Роли WHERE НазваниеРоли = ?",
            ("Покупатель",),
        )
        if not role:
            raise RuntimeError("Не найдена роль «Покупатель».")

        login_name = "steam_" + steam_id
        email = "steam_" + steam_id + "@steam.local"
        duplicate = db.one(
            "SELECT КодПользователя FROM Пользователи WHERE Логин = ? OR Email = ?",
            (login_name, email),
        )
        if duplicate:
            flash(
                "Не удалось автоматически создать профиль. Обратитесь в поддержку.",
                "error",
            )
            return redirect(url_for("login"))

        # Always store a real password hash for the unguessable internal credential,
        # even when legacy local accounts are configured to use plaintext passwords.
        user_id = db.insert(
            "INSERT INTO Пользователи "
            "(Логин, Email, ХешПароля, КодРоли, Баланс, ДатаРегистрации, Заблокирован) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                login_name,
                email,
                generate_password_hash(secrets.token_urlsafe(48)),
                role["role_id"],
                0,
                db.now(),
                False,
            ),
        )
        db.execute(
            "INSERT INTO SteamAccounts "
            "(SteamID, КодПользователя, ДатаПривязки) VALUES (?, ?, ?)",
            (steam_id, user_id, db.now()),
        )
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
            row = get_db().one(
                "SELECT SteamID FROM SteamAccounts WHERE КодПользователя = ?",
                (user["user_id"],),
            )
            return {"steam_linked": row["SteamID"] if row else None}
        except Exception:
            return {"steam_linked": None}

    @app.post("/auth/steam/unlink")
    def steam_unlink():
        if not g.user:
            return redirect(url_for("login"))
        db = get_db()
        row = db.one(
            "SELECT ХешПароля AS pw FROM Пользователи WHERE КодПользователя = ?",
            (g.user["user_id"],),
        )
        # Keep users from locking themselves out of accounts created via Steam only.
        if row and row["pw"] and str(row["pw"]).startswith(("scrypt:", "pbkdf2:")):
            db.execute(
                "DELETE FROM SteamAccounts WHERE КодПользователя = ?",
                (g.user["user_id"],),
            )
            flash("Steam отвязан. Теперь можно входить по логину и паролю.", "success")
        else:
            flash(
                "Сначала задайте отдельный пароль SKINMARKET, затем отвяжите Steam.",
                "error",
            )
        return redirect(url_for("profile"))
