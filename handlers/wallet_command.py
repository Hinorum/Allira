import asyncio
import base64
import logging
from telegram import Update
from telegram.ext import ContextTypes

from tasks.marketapp_reports import MARKETAPP_API_URL, RENT_CATEGORIES, _format_ton, _nano_to_ton, MSK
from utils.http_client import get_client

logger = logging.getLogger(__name__)


def _userfriendly_to_raw(addr: str) -> str:
    addr = addr.strip()
    if addr.startswith("0:"):
        return addr.lower()
    if len(addr) == 48 and addr[:2] in ("EQ", "UQ"):
        urlsafe = addr[2:].replace("-", "+").replace("_", "/")
        padding = 4 - len(urlsafe) % 4
        if padding != 4:
            urlsafe += "=" * padding
        decoded = base64.b64decode(urlsafe)
        return "0:" + decoded[2:34].hex()
    return addr.lower()


async def _fetch_all_history(api_token: str, category: str) -> list:
    events = []
    cursor = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            client = await get_client()
            response = await client.get(
                f"{MARKETAPP_API_URL}/v1/rent/{category}/history/",
                params=params,
                headers={"Authorization": api_token, "User-Agent": "AlliraBot/1.0"},
                timeout=15.0
            )
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "10"))
                await asyncio.sleep(retry_after)
                break
            response.raise_for_status()
            data = response.json()
            items = data.get("items", [])
            events.extend(items)
            cursor = data.get("cursor")
            if not cursor or not items:
                break
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Marketapp API ошибка ({category}/history): {e}")
            break
    return events


async def marketapprent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_token = context.bot_data.get("MARKETAPP_API_KEY")

    if not api_token:
        await update.message.reply_text("MARKETAPP_API_KEY не настроен.")
        return

    await update.message.reply_text("Считаю прибыль с аренды...")

    wallet = context.bot_data.get("MARKETAPP_WALLET", "")
    raw_wallet = _userfriendly_to_raw(wallet) if wallet else ""

    from datetime import datetime
    now_ts = int(datetime.now(MSK).timestamp())
    day_ts = now_ts - 86400
    week_ts = now_ts - 604800
    month_ts = now_ts - 2592000

    day_income = 0.0
    week_income = 0.0
    month_income = 0.0
    total_income = 0.0
    total_ops = 0

    for i, category in enumerate(RENT_CATEGORIES):
        if i > 0:
            await asyncio.sleep(2)
        events = await _fetch_all_history(api_token, category)
        for ev in events:
            if raw_wallet:
                src_raw = _userfriendly_to_raw(ev.get("src", ""))
                dst_raw = _userfriendly_to_raw(ev.get("dst", ""))
                if src_raw != raw_wallet and dst_raw != raw_wallet:
                    continue
            ts = ev.get("ts", 0)
            price = _nano_to_ton(ev.get("price_nano", "0"))
            total_income += price
            total_ops += 1
            if ts >= day_ts:
                day_income += price
            if ts >= week_ts:
                week_income += price
            if ts >= month_ts:
                month_income += price

    lines = [
        "<b>Прибыль с аренды NFT:</b>\n",
        f"Сутки: <b>{_format_ton(day_income)} TON</b>",
        f"Неделя: <b>{_format_ton(week_income)} TON</b>",
        f"Месяц: <b>{_format_ton(month_income)} TON</b>",
        f"Всего: <b>{_format_ton(total_income)} TON</b>",
        f"\nОпераций: {total_ops}",
    ]

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

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
