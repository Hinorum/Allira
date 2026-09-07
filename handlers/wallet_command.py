import logging
from telegram import Update
from telegram.ext import ContextTypes

from tasks.marketapp_reports import fetch_total_rent_income, fetch_my_rented

logger = logging.getLogger(__name__)


async def marketapprent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_token = context.bot_data.get("MARKETAPP_API_KEY")

    if not api_token:
        await update.message.reply_text("MARKETAPP_API_KEY не настроен.")
        return

    await update.message.reply_text("Получаю данные по аренде...")

    rented = await fetch_my_rented(api_token)
    income = await fetch_total_rent_income(api_token)

    lines = ["<b>Данные по аренде Marketapp:</b>\n"]

    if rented:
        lines.append(f"<b>Активные аренды:</b> {len(rented)}")
        for item in rented[:5]:
            name = item.get("nft_name", "?")
            lines.append(f"  - {name}")
        if len(rented) > 5:
            lines.append(f"  ... и ещё {len(rented) - 5}")
    else:
        lines.append("Активные аренды: нет данных")

    lines.append("")

    if income:
        lines.append(f"<b>Общий доход:</b> {_format_ton(income['total_ton'])} TON")
        if income["events"]:
            lines.append("\n<b>Последние поступления:</b>")
            for ev in income["events"][:5]:
                lines.append(f"  {ev['name']} ({ev['category']}): {_format_ton(ev['price_ton'])} TON")
    else:
        lines.append("Доход: нет данных")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def _format_ton(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    return f"{value:.2f}"
