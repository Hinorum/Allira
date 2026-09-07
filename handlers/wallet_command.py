import logging
from telegram import Update
from telegram.ext import ContextTypes

from tasks.marketapp_reports import fetch_profit

logger = logging.getLogger(__name__)


async def marketapprent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Использование: /marketapprent <адрес_кошелька>\n"
            "Пример: /marketapprent UQDCaRr4ZXxAv46TNQhwpsdhKIz8IqoBqbOvpWhBgvxplaqA"
        )
        return

    wallet_address = context.args[0]
    api_key = context.bot_data.get("MARKETAPP_API_KEY")

    if not api_key:
        await update.message.reply_text("MARKETAPP_API_KEY не настроен.")
        return

    await update.message.reply_text("Получаю данные по кошельку...")

    periods = [
        ("day", "Сутки"),
        ("week", "Неделя"),
        ("month", "Месяц"),
    ]

    lines = [f"<b>Данные по кошельку:</b>\n<code>{wallet_address}</code>\n"]

    for period, label in periods:
        profit = await fetch_profit(api_key, wallet_address, period)
        if profit is not None:
            lines.append(f"{label}: <b>{profit:.2f} TON</b>")
        else:
            lines.append(f"{label}: нет данных")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
