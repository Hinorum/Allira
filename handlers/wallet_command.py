import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from tasks.marketapp_reports import fetch_rent_history, fetch_my_rented, _ts_now, _format_ton, _nano_to_ton, RENT_CATEGORIES

logger = logging.getLogger(__name__)


async def marketapprent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_token = context.bot_data.get("MARKETAPP_API_KEY")

    if not api_token:
        await update.message.reply_text("MARKETAPP_API_KEY не настроен.")
        return

    await update.message.reply_text("Считаю прибыль с аренды...")

    all_events = []
    for i, category in enumerate(RENT_CATEGORIES):
        if i > 0:
            await asyncio.sleep(2)
        items = await fetch_rent_history(api_token, category, limit=100)
        if items:
            all_events.extend(items)

    now_ts = _ts_now()
    day_ts = now_ts - 86400
    week_ts = now_ts - 604800
    month_ts = now_ts - 25920000

    day_income = sum(_nano_to_ton(e.get("price_nano", "0")) for e in all_events if e.get("ts", 0) >= day_ts)
    week_income = sum(_nano_to_ton(e.get("price_nano", "0")) for e in all_events if e.get("ts", 0) >= week_ts)
    month_income = sum(_nano_to_ton(e.get("price_nano", "0")) for e in all_events if e.get("ts", 0) >= month_ts)
    total_income = sum(_nano_to_ton(e.get("price_nano", "0")) for e in all_events)

    lines = [
        "<b>Прибыль с аренды NFT:</b>\n",
        f"Сутки: <b>{_format_ton(day_income)} TON</b>",
        f"Неделя: <b>{_format_ton(week_income)} TON</b>",
        f"Месяц: <b>{_format_ton(month_income)} TON</b>",
        f"Всего: <b>{_format_ton(total_income)} TON</b>",
        f"\nОпераций: {len(all_events)}",
    ]

    rented = await fetch_my_rented(api_token)
    if rented:
        lines.append(f"\nАктивных аренд: {len(rented)}")
        for item in rented[:5]:
            name = item.get("nft_name", "?")
            price = _nano_to_ton(item.get("price_per_day", "0"))
            lines.append(f"  - {name}: {_format_ton(price)} TON/день")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
