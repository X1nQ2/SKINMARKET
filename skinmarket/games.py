"""Игры, которые разрешены в каталоге SKINMARKET.

Предметы добавляются только из инвентаря Steam, поэтому список игр
ограничен: CS2, Dota 2 и Rust. Остальные игры (Apex Legends, Team Fortress 2,
PUBG: Battlegrounds) убраны из каталога.
"""

ALLOWED_GAMES = {
    "CS2": 730,
    "Dota 2": 570,
    "Rust": 252490,
}

APPID_TO_GAME = {str(appid): name for name, appid in ALLOWED_GAMES.items()}

# Игры, которые раньше были в демо-каталоге, но больше не используются.
RETIRED_GAMES = ("Apex Legends", "Team Fortress 2", "PUBG: Battlegrounds")


def game_name_for_appid(appid):
    """Название разрешённой игры по AppID или None."""
    return APPID_TO_GAME.get(str(appid or "").strip())


def is_allowed_appid(appid):
    return game_name_for_appid(appid) is not None
