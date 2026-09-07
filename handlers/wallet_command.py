import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from tasks.marketapp_reports import fetch_my_rented, fetch_rent_income_events, _ts_now, _format_ton, _nano_to_ton, RENT_CATEGORIES

logger = logging.getLogger(__name__)


async def marketapprent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_token = context.bot_data.get("MARKETAPP_API_KEY")

    if not api_token:
        await update.message.reply_text("MARKETAPP_API_KEY не настроен.")
        return

    await update.message.reply_text("Получаю данные по аренде...")

    rented = await fetch_my_rented(api_token)

    all_events = []
    for i, category in enumerate(RENT_CATEGORIES):
        if i > 0:
            await asyncio.sleep(2)
        items = await fetch_rent_income_events(api_token, category)
        if items:
            all_events.extend(items)

    now_ts = _ts_now()
    day_ts = now_ts - 86400
    week_ts = now_ts - 604800

    day_income = sum(_nano_to_ton(e.get("price_nano", "0")) for e in all_events if e.get("ts", 0) >= day_ts)
    week_income = sum(_nano_to_ton(e.get("price_nano", "0")) for e in all_events if e.get("ts", 0) >= week_ts)
    month_income = sum(_nano_to_ton(e.get("price_nano", "0")) for e in all_events)

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
    lines.append(f"<b>Доход:</b>")
    lines.append(f"  Сутки: {_format_ton(day_income)} TON")
    lines.append(f"  Неделя: {_format_ton(week_income)} TON")
    lines.append(f"  Месяц: {_format_ton(month_income)} TON")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
