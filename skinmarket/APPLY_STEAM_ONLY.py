from pathlib import Path
import re
import shutil
import sys

project = Path(__file__).resolve().parent
server = project / "skinmarket" / "server.py"
bundle = project / "bundle"
if not server.is_file():
    print("ОШИБКА: не найден skinmarket/server.py. Распакуйте архив в корень проекта.")
    sys.exit(1)

source = server.read_text(encoding="utf-8")
pattern = re.compile(
    r'@app\.route\("/register",\s*methods=\["GET",\s*"POST"\]\)\s*'
    r'def register\(\):.*?(?=\n@app\.route\("/login",)',
    re.S,
)
replacement = """@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("catalog"))
    # Новые аккаунты создаются только после подтверждения Steam OpenID.
    return redirect(url_for("steam_login"))
"""
updated, count = pattern.subn(lambda _m: replacement, source, count=1)
if count != 1:
    print("ОШИБКА: не удалось однозначно найти функцию register() в server.py.")
    print("Ничего не заменено.")
    sys.exit(2)

backup = server.with_suffix(".py.before_steam_backup")
shutil.copy2(server, backup)
shutil.copy2(bundle / "steam_auth.py", project / "skinmarket" / "steam_auth.py")
shutil.copy2(bundle / "register.html", project / "skinmarket" / "templates" / "register.html")
server.write_text(updated, encoding="utf-8")

requirements = project / "skinmarket" / "requirements.txt"
if requirements.exists():
    req = requirements.read_text(encoding="utf-8")
    if "requests" not in req.lower():
        requirements.write_text(req.rstrip() + "\nrequests>=2.31,<3.0\n", encoding="utf-8")

print("Готово: регистрация новых пользователей переведена только на Steam.")
print("Резервная копия server.py: " + str(backup))
print("Теперь перезапустите приложение.")
