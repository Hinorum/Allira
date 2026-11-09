"""Единые проверки доступа к владельческим командам.

Раньше в шести хендлерах лежали почти одинаковые копии `_owner_id` и
`_is_owner_chat`, и они успели разойтись: админ-меню закрывалось при пустых
CREATOR_*, а крипто-команды в том же случае пускали пользователя без
username. Здесь одна политика: владелец определяется по числовому ID, затем
по @username, а если не задано ни то ни другое — доступ закрыт.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

DENY_MESSAGE = "Команды доступны только владельцу бота."
NO_OWNER_MESSAGE = (
    "Владелец не настроен. Добавь CRYPTO_OWNER_ID или CREATOR_USER_IDS в .env."
)


def owner_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Telegram ID владельца учёта (0 — не настроен)."""
    return int(context.bot_data.get("CRYPTO_OWNER_ID") or 0)


def is_creator_user(user, bot_data) -> bool:
    """Создатель бота: числовой ID приоритетнее @username.

    Если не задано ни ID, ни username, вернуть True нельзя: иначе `"" ==
    ""` пропускал бы любого пользователя без username.
    """
    if user is None:
        return False
    creator_ids = bot_data.get("CREATOR_USER_IDS") or ()
    creator_username = (
        bot_data.get("CREATOR_USERNAME") or ""
    ).strip().lstrip("@").lower()
    username = (getattr(user, "username", None) or "").strip().lower()

    if creator_ids:
        return user.id in creator_ids
    if creator_username:
        return bool(username) and username == creator_username
    return False


async def _reply(update: Update | None, text: str) -> None:
    message = getattr(update, "message", None) if update is not None else None
    if message is not None:
        await message.reply_text(text)


async def require_owner(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deny_message: str = DENY_MESSAGE,
    no_owner_message: str = NO_OWNER_MESSAGE,
) -> bool:
    """Проверить создателя и наличие владельца учёта.

    Возвращает True, если команду можно выполнять. Иначе уже отвечает
    пользователю и возвращает False.
    """
    user = getattr(update, "effective_user", None)
    if not is_creator_user(user, context.bot_data):
        await _reply(update, deny_message)
        return False
    if not owner_id(context):
        await _reply(update, no_owner_message)
        return False
    return True
