import logging
from collections import OrderedDict
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes

from tasks.marketapp_reports import (
    collect_rent_events,
    sync_rent_from_blockchain,
    _format_ton,
    _nano_to_ton,
    _escape_html,
    userfriendly_to_raw,
    MSK,
    MARKETAPP_API_URL,
)
from utils.database import get_all_rent_events
from utils.http_client import get_client

logger = logging.getLogger(__name__)

MAX_SYNC_PAGES = 1000
MAX_LIST_MESSAGES = 20


def _dedupe_events(events: list) -> list:
    seen = set()
    unique = []
    for ev in events:
        key = (
            int(ev.get("ts", 0) or 0),
            (ev.get("src") or "").strip().lower(),
            (ev.get("dst") or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(ev)
    return unique


def _total_nano(events: list, since_ts: int = 0) -> int:
    return sum(int(ev["price_nano"] or 0) for ev in events if ev["ts"] >= since_ts)


async def marketapprent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_token = context.bot_data.get("MARKETAPP_API_KEY")

    wallet = ""
    if context.args:
        candidate = context.args[0].strip()
        if userfriendly_to_raw(candidate).startswith("0:"):
            wallet = candidate
        else:
            await update.message.reply_text(
                "Неверный адрес кошелька.\n"
                "Использование: /marketapprent <адрес кошелька>"
            )
            return
    if not wallet:
        wallet = context.bot_data.get("MARKETAPP_WALLET", "")

    if not wallet:
        await update.message.reply_text(
            "Кошелёк не передан и MARKETAPP_WALLET не настроен.\n"
            "Использование: /marketapprent <адрес кошелька>"
        )
        return

    wallet_raw = userfriendly_to_raw(wallet)
    owner_wallet = context.bot_data.get("MARKETAPP_WALLET", "")
    is_owner = bool(owner_wallet and userfriendly_to_raw(owner_wallet) == wallet_raw)

    await update.message.reply_text(
        f"Синхронизирую историю для кошелька:\n<code>{_escape_html(wallet)}</code>",
        parse_mode="HTML"
    )

    api_saved = 0
    blockchain_saved = 0

    await update.message.reply_text("Сканирую полную историю из блокчейна...")
    blockchain_saved = await sync_rent_from_blockchain(wallet, max_pages=MAX_SYNC_PAGES, from_scratch=True)

    if api_token and is_owner:
        await update.message.reply_text("Дополняю метаданными из Marketapp...")
        try:
            api_saved = await collect_rent_events(api_token, wallet)
        except Exception as e:
            logger.error(f"Marketapp API дополнение: {e}", exc_info=True)

    events = _dedupe_events(await get_all_rent_events(wallet))
    sorted_events = sorted(events, key=lambda e: e["ts"], reverse=True)

    now_ts = int(datetime.now(MSK).timestamp())
    day_ts = now_ts - 86400
    week_ts = now_ts - 604800
    month_ts = now_ts - 2592000

    total_ops = len(sorted_events)
    day_income = _total_nano(sorted_events, day_ts) / 1_000_000_000
    week_income = _total_nano(sorted_events, week_ts) / 1_000_000_000
    month_income = _total_nano(sorted_events, month_ts) / 1_000_000_000
    total_income = _total_nano(sorted_events, 0) / 1_000_000_000

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

    if api_saved or blockchain_saved:
        parts = []
        if blockchain_saved:
            parts.append(f"блокчейн: +{blockchain_saved} платежей")
        if api_saved:
            parts.append(f"метаданные Marketapp: {api_saved}")
        lines.append("Сохранено: " + ", ".join(parts))

    if sorted_events:
        lines.append("\n<b>Разбивка по месяцам:</b>")
        monthly = OrderedDict()
        for ev in sorted_events:
            key = datetime.fromtimestamp(ev["ts"], MSK).strftime("%m.%Y")
            monthly[key] = monthly.get(key, 0) + int(ev["price_nano"] or 0)

        for key in sorted(monthly.keys()):
            lines.append(f"{key}: <b>{_format_ton(monthly[key] / 1_000_000_000)} TON</b>")

    try:
        if api_token and is_owner:
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