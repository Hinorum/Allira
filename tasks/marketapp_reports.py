import logging
from datetime import datetime, timedelta, timezone, time as dt_time
from telegram.ext import ContextTypes

from utils.database import save_marketapp_profit, get_previous_profit, get_profit_for_period
from utils.http_client import get_client, with_retry

logger = logging.getLogger(__name__)

MARKETAPP_API_URL = "https://api.marketapp.org"
RENT_CATEGORIES = ["gifts", "usernames", "numbers"]


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_ton(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    return f"{value:.2f}"


def _nano_to_ton(nano: str) -> float:
    return int(nano) / 1_000_000_000


@with_retry(max_retries=2, base_delay=1.0)
async def fetch_rent_history(api_token: str, category: str, limit: int = 100) -> list | None:
    try:
        client = await get_client()
        response = await client.get(
            f"{MARKETAPP_API_URL}/v1/rent/{category}/history/",
            params={"limit": limit},
            headers={
                "Authorization": api_token,
                "User-Agent": "AlliraBot/1.0"
            },
            timeout=15.0
        )
        response.raise_for_status()
        data = response.json()
        return data.get("items", [])
    except Exception as e:
        logger.error(f"Marketapp API ошибка ({category}/history): {e}")
        return None


async def fetch_total_rent_income(api_token: str) -> dict | None:
    total_ton = 0.0
    events = []

    for category in RENT_CATEGORIES:
        items = await fetch_rent_history(api_token, category, limit=100)
        if items:
            for item in items:
                price_ton = _nano_to_ton(item.get("price_nano", "0"))
                total_ton += price_ton
                events.append({
                    "name": item.get("name", "?"),
                    "category": category,
                    "price_ton": price_ton,
                    "ts": item.get("ts", 0),
                })

    if not events:
        return None

    events.sort(key=lambda x: x["ts"], reverse=True)
    return {"total_ton": total_ton, "events": events[:10]}


async def fetch_my_rented(api_token: str) -> list | None:
    try:
        client = await get_client()
        response = await client.get(
            f"{MARKETAPP_API_URL}/v1/rent/my-rented/",
            headers={
                "Authorization": api_token,
                "User-Agent": "AlliraBot/1.0"
            },
            timeout=15.0
        )
        response.raise_for_status()
        data = response.json()
        return data.get("items", [])
    except Exception as e:
        logger.error(f"Marketapp API ошибка (my-rented): {e}")
        return None


@with_retry(max_retries=2, base_delay=1.0)
async def fetch_profit(api_key: str, wallet_address: str, period: str) -> float | None:
    return None


def format_daily_report(current_profit: float, previous_profit: float | None) -> str:
    lines = [
        "<b>📊 ОТЧЁТ ЗА СУТКИ</b>\n",
        f"Прибыль с аренды: <b>{_format_ton(current_profit)} TON</b>",
    ]

    if previous_profit is not None and previous_profit > 0:
        change = ((current_profit - previous_profit) / previous_profit) * 100
        emoji = "\U0001f7e2" if change > 0 else "\U0001f534" if change < 0 else "\u26aa"
        lines.append(f"Изменение: {emoji} {change:+.1f}% к предыдущему отчёту")
    elif previous_profit is not None and previous_profit == 0:
        lines.append("Изменение: \U0001f7e2 новая прибыль!")

    lines.append(f"\n<i>{datetime.now().strftime('%d.%m.%Y %H:%M')}</i>")
    return "\n".join(lines)


def format_weekly_report(profits: list) -> str:
    if not profits:
        return "<b>📊 ОТЧЁТ ЗА НЕДЕЛЮ</b>\n\nНет данных за неделю."

    total = sum(p["profit_ton"] for p in profits)
    avg = total / len(profits) if profits else 0
    min_p = min(p["profit_ton"] for p in profits)
    max_p = max(p["profit_ton"] for p in profits)

    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).strftime("%d.%m")
    week_end = now.strftime("%d.%m")

    lines = [
        f"<b>📊 ОТЧЁТ ЗА НЕДЕЛЮ ({week_start} - {week_end})</b>\n",
        f"Прибыль: <b>{_format_ton(total)} TON</b>",
        f"Средняя в день: {_format_ton(avg)} TON",
        f"Мин/Макс: {_format_ton(min_p)} / {_format_ton(max_p)} TON",
        f"Отчётов: {len(profits)}",
    ]

    if len(profits) >= 2:
        first = profits[-1]["profit_ton"]
        last = profits[0]["profit_ton"]
        if first > 0:
            change = ((last - first) / first) * 100
            emoji = "\U0001f7e2" if change > 0 else "\U0001f534" if change < 0 else "\u26aa"
            lines.append(f"\nДинамика недели: {emoji} {change:+.1f}%")

    lines.append(f"\n<i>{now.strftime('%d.%m.%Y %H:%M')}</i>")
    return "\n".join(lines)


def format_monthly_report(profits: list) -> str:
    if not profits:
        return "<b>📊 ОТЧЁТ ЗА МЕСЯЦ</b>\n\nНет данных за месяц."

    total = sum(p["profit_ton"] for p in profits)
    avg = total / len(profits) if profits else 0
    min_p = min(p["profit_ton"] for p in profits)
    max_p = max(p["profit_ton"] for p in profits)

    now = datetime.now()
    month_name = now.strftime("%B %Y")

    lines = [
        f"<b>📊 ОТЧЁТ ЗА {month_name.upper()}</b>\n",
        f"Прибыль: <b>{_format_ton(total)} TON</b>",
        f"Средняя в день: {_format_ton(avg)} TON",
        f"Мин/Макс: {_format_ton(min_p)} / {_format_ton(max_p)} TON",
        f"Отчётов: {len(profits)}",
    ]

    lines.append(f"\n<i>{now.strftime('%d.%m.%Y %H:%M')}</i>")
    return "\n".join(lines)


async def _send_report(context: ContextTypes.DEFAULT_TYPE, period: str, report_text: str, save_db: bool = False, db_period: str = None, profit: float = None):
    bot_data = context.bot_data
    channel_id = bot_data.get("NEWS_CHANNEL_ID")

    if not channel_id:
        return

    if save_db and profit is not None and db_period:
        await save_marketapp_profit(db_period, profit)

    await context.bot.send_message(
        chat_id=channel_id,
        text=report_text,
        parse_mode="HTML"
    )


async def daily_profit_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        bot_data = context.bot_data
        api_key = bot_data.get("MARKETAPP_API_KEY")
        wallet_address = bot_data.get("MARKETAPP_WALLET")

        if not api_key or not wallet_address:
            logger.warning("MARKETAPP_API_KEY или MARKETAPP_WALLET не заданы")
            return

        logger.info("Получаю данные за сутки...")
        profit = await fetch_profit(api_key, wallet_address, "day")

        if profit is None:
            logger.warning("Не удалось получить данные за сутки")
            return

        previous = await get_previous_profit("day")
        previous_profit = previous["profit_ton"] if previous else None

        report = format_daily_report(profit, previous_profit)
        await _send_report(context, "day", report, save_db=True, db_period="day", profit=profit)
        logger.info(f"Ежедневный отчет отправлен: {profit} TON")

    except Exception as e:
        logger.error(f"Ошибка ежедневного отчета: {e}", exc_info=True)


async def weekly_profit_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        bot_data = context.bot_data
        api_key = bot_data.get("MARKETAPP_API_KEY")
        wallet_address = bot_data.get("MARKETAPP_WALLET")

        if not api_key or not wallet_address:
            return

        logger.info("Получаю данные за неделю...")
        profit = await fetch_profit(api_key, wallet_address, "week")

        if profit is not None:
            await save_marketapp_profit("week", profit)

        profits = await get_profit_for_period("day", 7)
        report = format_weekly_report(profits)
        await _send_report(context, "week", report)
        logger.info("Еженедельный отчет отправлен")

    except Exception as e:
        logger.error(f"Ошибка еженедельного отчета: {e}", exc_info=True)


async def monthly_profit_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        if datetime.now().day != 1:
            return

        bot_data = context.bot_data
        api_key = bot_data.get("MARKETAPP_API_KEY")
        wallet_address = bot_data.get("MARKETAPP_WALLET")

        if not api_key or not wallet_address:
            return

        logger.info("Получаю данные за месяц...")
        profit = await fetch_profit(api_key, wallet_address, "month")

        if profit is not None:
            await save_marketapp_profit("month", profit)

        profits = await get_profit_for_period("day", 30)
        report = format_monthly_report(profits)
        await _send_report(context, "month", report)
        logger.info("Ежемесячный отчет отправлен")

    except Exception as e:
        logger.error(f"Ошибка ежемесячного отчета: {e}", exc_info=True)


def setup_marketapp_jobs(application):
    for job_name in ["marketapp_daily", "marketapp_weekly", "marketapp_monthly"]:
        jobs = application.job_queue.get_jobs_by_name(job_name)
        for job in jobs:
            job.schedule_removal()

    MSK = timezone(timedelta(hours=3))
    report_time = dt_time(hour=9, minute=0, tzinfo=MSK)

    application.job_queue.run_daily(
        daily_profit_report,
        time=report_time,
        name="marketapp_daily"
    )

    application.job_queue.run_daily(
        weekly_profit_report,
        time=report_time,
        days=(0,),
        name="marketapp_weekly"
    )

    application.job_queue.run_daily(
        monthly_profit_report,
        time=report_time,
        name="marketapp_monthly"
    )

    logger.info("Marketapp отчеты настроены (09:00 MSK: день/понедельник/1-е число)")
