import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from tasks.marketapp_reports import fetch_my_rented, _format_ton, _nano_to_ton

logger = logging.getLogger(__name__)


async def marketapprent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_token = context.bot_data.get("MARKETAPP_API_KEY")

    if not api_token:
        await update.message.reply_text("MARKETAPP_API_KEY не настроен.")
        return

    await update.message.reply_text("Получаю данные по аренде...")

    rented = await fetch_my_rented(api_token)

    if not rented:
        await update.message.reply_text("Нет активных аренд.")
        return

    total_income = 0.0
    lines = ["<b>Ваши аренды:</b>\n"]

    for item in rented:
        name = item.get("nft_name", "?")
        price_nano = item.get("price_per_day", "0")
        price_ton = _nano_to_ton(price_nano)
        total_income += price_ton
        lines.append(f"  - <b>{name}</b>: {_format_ton(price_ton)} TON/день")

    lines.append(f"\n<b>Общий доход в день:</b> {_format_ton(total_income)} TON")
    lines.append(f"<b>В месяц (30 дн):</b> {_format_ton(total_income * 30)} TON")
    lines.append(f"\nАктивных аренд: {len(rented)}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
