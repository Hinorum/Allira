import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from tasks.marketapp_reports import fetch_my_rented, fetch_rent_history, _ts_now, _format_ton, _nano_to_ton, RENT_CATEGORIES

logger = logging.getLogger(__name__)


async def marketapprent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Использование: /marketapprent <адрес_кошелька>\n"
            "Пример: /marketapprent UQDCaRr4ZXxAv46TNQhwpsdhKIz8IqoBqbOvpWhBgvxplaqA"
        )
        return

    wallet_address = context.args[0]
    api_token = context.bot_data.get("MARKETAPP_API_KEY")

    if not api_token:
        await update.message.reply_text("MARKETAPP_API_KEY не настроен.")
        return

    await update.message.reply_text(f"Получаю данные для кошелька:\n<code>{wallet_address}</code>", parse_mode="HTML")

    all_events = []
    for i, category in enumerate(RENT_CATEGORIES):
        if i > 0:
            await asyncio.sleep(2)
        items = await fetch_rent_history(api_token, category, limit=100)
        if items:
            for item in items:
                if item.get("dst") == wallet_address:
                    all_events.append(item)

    now_ts = _ts_now()
    day_ts = now_ts - 86400
    week_ts = now_ts - 604800

    day_income = sum(_nano_to_ton(e.get("price_nano", "0")) for e in all_events if e.get("ts", 0) >= day_ts)
    week_income = sum(_nano_to_ton(e.get("price_nano", "0")) for e in all_events if e.get("ts", 0) >= week_ts)
    total_income = sum(_nano_to_ton(e.get("price_nano", "0")) for e in all_events)

    lines = [
        "<b>Данные по аренде:</b>",
        f"<code>{wallet_address}</code>\n",
        f"<b>Доход:</b>",
        f"  Сутки: {_format_ton(day_income)} TON",
        f"  Неделя: {_format_ton(week_income)} TON",
        f"  Всего: {_format_ton(total_income)} TON",
    ]

    if all_events:
        lines.append(f"\nОпераций: {len(all_events)}")
        lines.append("\n<b>Последние поступления:</b>")
        for ev in all_events[:5]:
            name = ev.get("name", "?")
            price = _nano_to_ton(ev.get("price_nano", "0"))
            lines.append(f"  {name}: {_format_ton(price)} TON")
        if len(all_events) > 5:
            lines.append(f"  ... и ещё {len(all_events) - 5}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
