import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes

from tasks.marketapp_reports import MARKETAPP_API_URL, _format_ton, _nano_to_ton, MSK, collect_rent_events
from utils.database import get_rent_events
from utils.http_client import get_client

logger = logging.getLogger(__name__)


async def marketapprent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_token = context.bot_data.get("MARKETAPP_API_KEY")
    wallet = context.bot_data.get("MARKETAPP_WALLET", "")

    if not api_token:
        await update.message.reply_text("MARKETAPP_API_KEY не настроен.")
        return

    if not wallet:
        await update.message.reply_text("MARKETAPP_WALLET не настроен — фильтрация прибыли невозможна.")
        return

    await update.message.reply_text("Собираю данные по аренде...")

    saved = await collect_rent_events(api_token, wallet)

    now_ts = int(datetime.now(MSK).timestamp())
    day_ts = now_ts - 86400
    week_ts = now_ts - 604800
    month_ts = now_ts - 2592000

    events = await get_rent_events(since_ts=0)

    def total_in(since_ts: int) -> float:
        return sum(_nano_to_ton(ev["price_nano"]) for ev in events if ev["ts"] >= since_ts)

    total_ops = len(events)
    day_income = total_in(day_ts)
    week_income = total_in(week_ts)
    month_income = total_in(month_ts)
    total_income = total_in(0)

    lines = [
        "<b>Прибыль с аренды NFT:</b>\n",
        f"Сутки: <b>{_format_ton(day_income)} TON</b>",
        f"Неделя: <b>{_format_ton(week_income)} TON</b>",
        f"Месяц: <b>{_format_ton(month_income)} TON</b>",
        f"Всего: <b>{_format_ton(total_income)} TON</b>",
        f"\nОпераций в базе: {total_ops}",
    ]

    if saved:
        lines.append(f"Новых событий: <b>{saved}</b>")

    try:
        client = await get_client()
        response = await client.get(
            f"{MARKETAPP_API_URL}/v1/rent/my-rented/",
            headers={"Authorization": api_token, "User-Agent": "AlliraBot/1.0"},
            timeout=15.0
        )
        if response.status_code == 200:
            rented = response.json().get("items", [])
            if rented:
                lines.append(f"\nАктивных аренд: {len(rented)}")
                for item in rented[:5]:
                    name = item.get("nft_name", "?")
                    price = _nano_to_ton(item.get("price_per_day", "0"))
                    lines.append(f"  - {name}: {_format_ton(price)} TON/день")
    except Exception:
        pass

    lines.append(f"\n<i>{datetime.now(MSK).strftime('%d.%m.%Y %H:%M')}</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")