import logging
from collections import OrderedDict
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes

from tasks.marketapp_reports import (
    sync_rent_from_blockchain,
    _format_ton,
    _nano_to_ton,
    _escape_html,
    MSK,
    MARKETAPP_API_URL,
)
from utils.database import get_all_rent_events
from utils.http_client import get_client

logger = logging.getLogger(__name__)

MAX_SYNC_PAGES = 300
MAX_LIST_MESSAGES = 20


async def marketapprent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_token = context.bot_data.get("MARKETAPP_API_KEY")
    wallet = context.bot_data.get("MARKETAPP_WALLET", "")

    if not wallet:
        await update.message.reply_text("MARKETAPP_WALLET не настроен — фильтрация прибыли невозможна.")
        return

    await update.message.reply_text("Синхронизирую полную историю из блокчейна...")

    saved = await sync_rent_from_blockchain(wallet, max_pages=MAX_SYNC_PAGES, from_scratch=True)

    events = await get_all_rent_events()
    sorted_events = sorted(events, key=lambda e: e["ts"], reverse=True)

    now_ts = int(datetime.now(MSK).timestamp())
    day_ts = now_ts - 86400
    week_ts = now_ts - 604800
    month_ts = now_ts - 2592000

    def total_in(since_ts: int) -> float:
        return sum(_nano_to_ton(ev["price_nano"]) for ev in events if ev["ts"] >= since_ts)

    total_ops = len(sorted_events)
    day_income = total_in(day_ts)
    week_income = total_in(week_ts)
    month_income = total_in(month_ts)
    total_income = total_in(0)

    if sorted_events:
        first_ts = sorted_events[-1]["ts"]
        last_ts = sorted_events[0]["ts"]
        period_text = (
            f"{datetime.fromtimestamp(first_ts, MSK).strftime('%d.%m.%Y')} — "
            f"{datetime.fromtimestamp(last_ts, MSK).strftime('%d.%m.%Y')}"
        )
    else:
        period_text = "нет данных"

    lines = [
        "<b>Прибыль с аренды NFT:</b>\n",
        f"Сутки (24ч): <b>{_format_ton(day_income)} TON</b>",
        f"Неделя: <b>{_format_ton(week_income)} TON</b>",
        f"Месяц (30д): <b>{_format_ton(month_income)} TON</b>",
        f"За всё время: <b>{_format_ton(total_income)} TON</b>",
        f"\nПериод сбора: {period_text}",
        f"Операций в базе: <b>{total_ops}</b>",
    ]

    if saved:
        lines.append(f"Новых событий: <b>{saved}</b>")

    if sorted_events:
        lines.append("\n<b>Разбивка по месяцам:</b>")
        monthly = OrderedDict()
        for ev in events:
            key = datetime.fromtimestamp(ev["ts"], MSK).strftime("%m.%Y")
            monthly[key] = monthly.get(key, 0.0) + _nano_to_ton(ev["price_nano"])

        for key in sorted(monthly.keys()):
            lines.append(f"{key}: <b>{_format_ton(monthly[key])} TON</b>")

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
                    name = _escape_html(item.get("nft_name", "?"))
                    price = _nano_to_ton(item.get("price_per_day", "0"))
                    lines.append(f"  - {name}: {_format_ton(price)} TON/день")
    except Exception:
        pass

    lines.append(f"\n<i>{datetime.now(MSK).strftime('%d.%m.%Y %H:%M')}</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    if sorted_events:
        await send_rent_history(update, sorted_events)


async def send_rent_history(update: Update, events: list):
    header = "<b>Все сдачи в аренду от начала пользования кошелька:</b>"
    lines = [header]
    chunks = []
    current_len = 0

    for ev in events:
        amount = _format_ton(_nano_to_ton(ev["price_nano"]))
        line = (
            f"{datetime.fromtimestamp(ev['ts'], MSK).strftime('%d.%m.%Y %H:%M')} — "
            f"+{amount} TON"
        )
        if current_len and current_len + len(line) + 1 > 3800:
            chunks.append("\n".join(lines))
            lines = [header]
            current_len = 0
        lines.append(line)
        current_len += len(line) + 1

    if len(lines) > 1 or not chunks:
        chunks.append("\n".join(lines))

    for chunk in chunks[:MAX_LIST_MESSAGES]:
        await update.message.reply_text(chunk, parse_mode="HTML")

    if len(chunks) > MAX_LIST_MESSAGES:
        await update.message.reply_text(
            f"Показаны первые {len(chunks[:MAX_LIST_MESSAGES])} сообщений. "
            f"Всего событий в базе: <b>{len(events)}</b>.",
            parse_mode="HTML"
        )