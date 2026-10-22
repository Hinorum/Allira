import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from utils.common import escape_html
from utils.database import set_user_banned

logger = logging.getLogger(__name__)


def _admin_ids(context: ContextTypes.DEFAULT_TYPE) -> set[int]:
    return set(context.bot_data.get("ADMIN_USER_IDS") or ())


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    return bool(
        message
        and message.from_user
        and message.from_user.id in _admin_ids(context)
    )


def _resolve_target(message, context: ContextTypes.DEFAULT_TYPE, command: str):
    """Возвращает (user_id, first_name, текст_ошибки).

    Цель — автор сообщения в реплае или числовой id в аргументах.
    """
    replied = message.reply_to_message
    if replied and replied.from_user:
        return replied.from_user.id, replied.from_user.first_name, None

    args = context.args or []
    if args:
        arg = args[0].strip()
        if arg.lstrip("-").isdigit():
            return int(arg), None, None
        return None, None, (
            "id должен быть числом. Либо ответь реплаем на сообщение юзера:\n"
            f"/{command}"
        )

    return None, None, (
        "Кого? Ответь реплаем на его сообщение или укажи id:\n"
        f"/{command} 123456789"
    )


def _error_text(error: TelegramError) -> str:
    return getattr(error, "message", None) or str(error)


async def _set_ban(update: Update, context: ContextTypes.DEFAULT_TYPE, banned: bool):
    """Бан/разбан по реплаю на сообщение юзера или по его числовому id.

    В users уже был флаг is_banned, но способа его выставить не было —
    команда фильтровала ботоводов вхолостую.
    """
    message = update.message
    if not message:
        return

    if not _is_admin(update, context):
        await message.reply_text("Команда доступна только администраторам.")
        return

    target_id, target_name, error = _resolve_target(
        message, context, "ban" if banned else "unban"
    )
    if error:
        await message.reply_text(error)
        return

    if banned and target_id in _admin_ids(context):
        await message.reply_text("Администратора забанить нельзя.")
        return

    await set_user_banned(target_id, banned)
    display_name = target_name or str(target_id)
    who = escape_html(display_name)

    if not banned and message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            await context.bot.unban_chat_member(
                chat_id=message.chat.id, user_id=target_id, only_if_banned=True
            )
        except TelegramError as e:
            logger.warning(
                f"{target_id} разбанен в БД, но снять бан в чате {message.chat.id} "
                f"не удалось: {e}"
            )
            await message.reply_text(
                f"Пользователь {who} ({target_id}) разбанен в боте, но снять "
                f"бан в чате не удалось.\nПричина: {_error_text(e)}",
                parse_mode="HTML",
            )
            return

    verb = "забанен 🔴" if banned else "разбанен 🟢"
    await message.reply_text(f"Пользователь {who} ({target_id}) {verb}.", parse_mode="HTML")
    logger.info(f"{'Бан' if banned else 'Разбан'} {target_id} by {message.from_user.id}")


async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перманентно забанить юзера в текущей группе и удалить все его сообщения.

    ban_chat_member с revoke_messages=True удаляет историю сообщений и
    запрещает возвращаться в чат. Дополнительно ставим флаг is_banned,
    чтобы бот молчал, даже если бан снимут вручную.
    """
    message = update.message
    if not message:
        return

    if not _is_admin(update, context):
        await message.reply_text("Команда доступна только администраторам.")
        return

    chat = message.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("Забанить можно только в группе или супергруппе.")
        return

    target_id, target_name, error = _resolve_target(message, context, "kick")
    if error:
        await message.reply_text(error)
        return

    if target_id in _admin_ids(context):
        await message.reply_text("Администратора забанить нельзя.")
        return
    if target_id == getattr(context.bot, "id", None):
        await message.reply_text("Себя забанить нельзя.")
        return

    display_name = target_name or str(target_id)
    who = escape_html(display_name)
    try:
        await context.bot.ban_chat_member(
            chat_id=chat.id, user_id=target_id, revoke_messages=True
        )
    except TelegramError as e:
        logger.warning(f"Не удалось забанить {target_id} в {chat.id}: {e}")
        await message.reply_text(
            f"Не удалось забанить {display_name} ({target_id}).\n"
            "Проверь, что бот — админ с правами «Блокировать участников» "
            f"и «Удалять сообщения».\nПричина: {_error_text(e)}"
        )
        return

    await set_user_banned(target_id, True)
    logger.info(
        f"Перманентный бан {target_id} в {chat.id} by {message.from_user.id}"
    )
    await message.reply_text(
        f"Пользователь {who} ({target_id}) забанен навсегда, "
        "его сообщения удалены.",
        parse_mode="HTML",
    )


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_ban(update, context, banned=True)


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_ban(update, context, banned=False)
