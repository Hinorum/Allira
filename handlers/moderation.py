import logging

from telegram import Update
from telegram.ext import ContextTypes

from utils.common import escape_html
from utils.database import set_user_banned

logger = logging.getLogger(__name__)


def _admin_ids(context: ContextTypes.DEFAULT_TYPE) -> set[int]:
    return set(context.bot_data.get("ADMIN_USER_IDS") or ())


async def _set_ban(update: Update, context: ContextTypes.DEFAULT_TYPE, banned: bool):
    """Бан/разбан по реплаю на сообщение юзера или по его числовому id.

    В users уже был флаг is_banned, но способа его выставить не было —
    команда фильтровала ботоводов вхолостую.
    """
    message = update.message
    if not message:
        return

    if message.from_user.id not in _admin_ids(context):
        await message.reply_text("Команда доступна только администраторам.")
        return

    target_id = None
    target_name = None

    replied = message.reply_to_message
    if replied and replied.from_user:
        target_id = replied.from_user.id
        target_name = replied.from_user.first_name
    elif context.args:
        arg = context.args[0].strip()
        if arg.lstrip("-").isdigit():
            target_id = int(arg)
        else:
            await message.reply_text(
                "id должен быть числом. Либо ответь реплаем на сообщение юзера:\n"
                f"/{'ban' if banned else 'unban'}"
            )
            return

    if not target_id:
        await message.reply_text(
            "Кого? Ответь реплаем на его сообщение или укажи id:\n"
            f"/{'ban' if banned else 'unban'} 123456789"
        )
        return

    if banned and target_id in _admin_ids(context):
        await message.reply_text("Администратора забанить нельзя.")
        return

    await set_user_banned(target_id, banned)
    who = escape_html(target_name or str(target_id))
    verb = "забанен 🔴" if banned else "разбанен 🟢"
    await message.reply_text(f"Пользователь {who} ({target_id}) {verb}.", parse_mode="HTML")
    logger.info(f"{'Бан' if banned else 'Разбан'} {target_id} by {message.from_user.id}")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_ban(update, context, banned=True)


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_ban(update, context, banned=False)
