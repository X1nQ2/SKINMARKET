"""Скачивает картинки игр из Steam в static/game_images/.

Запуск (нужен интернет):  python fetch_game_images.py

Логотипы, которые уже лежат в папке (например, cs2.png и dota2.png), не перезаписываются.
Картинки принадлежат правообладателям игр; используйте их только в учебном проекте.
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

DEST = Path(__file__).resolve().parent / "static" / "game_images"
# название игры в каталоге -> (код приложения в Steam, часть названия для проверки)
GAMES = {
    "CS2": (730, "counter-strike"),
    "Dota 2": (570, "dota"),
    "Rust": (252490, "rust"),
    "Team Fortress 2": (440, "team fortress"),
    "PUBG: Battlegrounds": (578080, "pubg"),
    "Apex Legends": (1172470, "apex"),
}
API = "https://store.steampowered.com/api/appdetails?appids={id}&filters=basic"
CDN = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{id}/header.jpg"
MAX_BYTES = 5 * 1024 * 1024
SIGNATURES = (("png", b"\x89PNG"), ("jpg", b"\xff\xd8\xff"), ("gif", b"GIF8"))


def slugify(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SKINMARKET-student-project"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read(MAX_BYTES + 1)


def image_ext(data):
    for ext, signature in SIGNATURES:
        if data.startswith(signature):
            return ext
    return "webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None


def download_logo(game, app_id, name_part, fetcher=fetch, dest=DEST):
    """Возвращает (статус, подробность). Статусы: ok, skip, error."""
    slug = slugify(game)
    dest.mkdir(parents=True, exist_ok=True)
    existing = sorted(dest.glob(f"{slug}.*"))
    if existing:
        return "skip", f"уже есть {existing[0].name}"
    url = CDN.format(id=app_id)
    try:
        info = json.loads(fetcher(API.format(id=app_id)))[str(app_id)]
        if not info.get("success"):
            return "error", "Steam не знает такое приложение"
        title = info["data"]["name"]
        if name_part not in title.lower():
            return "error", f"под кодом {app_id} найдено «{title}», это не {game}: пропускаю"
        url = info["data"].get("header_image", url).split("?")[0]
    except Exception as error:  # API не ответил: пробуем прямую ссылку
        print(f"  (appdetails недоступен: {error}; пробую прямую ссылку)")
    try:
        data = fetcher(url)
    except Exception as error:
        return "error", f"не удалось скачать {url}: {error}"
    ext = image_ext(data)
    if not ext or len(data) > MAX_BYTES:
        return "error", "получен не файл картинки или он слишком большой"
    (dest / f"{slug}.{ext}").write_bytes(data)
    return "ok", f"{slug}.{ext} ({len(data) // 1024} КБ)"


def main():
    failed = 0
    for game, (app_id, name_part) in GAMES.items():
        status, detail = download_logo(game, app_id, name_part)
        print(f"{game}: {status} — {detail}")
        failed += status == "error"
    print("Готово." if not failed else f"Готово, ошибок: {failed}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
