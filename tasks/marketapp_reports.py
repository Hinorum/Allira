import asyncio
import base64
import logging
from datetime import datetime, timedelta, timezone, time as dt_time
from telegram.ext import ContextTypes

from utils.database import (
    save_marketapp_profit, get_previous_profit, get_profit_for_period,
    save_rent_events, save_blockchain_rent_events, get_sync_state, set_sync_state
)
from utils.http_client import get_client, with_retry

logger = logging.getLogger(__name__)

MARKETAPP_API_URL = "https://api.marketapp.org"
TONCENTER_API_URL = "https://toncenter.com/api/v2"
RENT_CATEGORIES = ["gifts", "usernames", "numbers"]

MSK = timezone(timedelta(hours=3))


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_ton(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    return f"{value:.2f}"


def _nano_to_ton(nano: str) -> float:
    return int(nano) / 1_000_000_000


def _ts_now() -> int:
    return int(datetime.now(MSK).timestamp())


def _ts_days_ago(days: int) -> int:
    return int((datetime.now(MSK) - timedelta(days=days)).timestamp())


def userfriendly_to_raw(addr: str) -> str:
    addr = (addr or "").strip()
    if addr.startswith("0:"):
        return addr.lower()
    if len(addr) == 48 and addr[:2] in ("EQ", "UQ"):
        try:
            urlsafe = addr[2:].replace("-", "+").replace("_", "/")
            padding = (4 - len(urlsafe) % 4) % 4
            urlsafe += "=" * padding
            decoded = base64.b64decode(urlsafe)
            return "0:" + decoded[2:34].hex()
        except Exception:
    return addr.lower()


@with_retry(max_retries=2, base_delay=5.0)
async def fetch_toncenter_txns(address: str, limit: int = 100, lt: str = None, hash_val: str = None) -> list | None:
    try:
        params = {"address": address, "limit": limit}
        if lt and hash_val:
            import urllib.parse
            params["lt"] = lt
            params["hash"] = hash_val
        client = await get_client()
        response = await client.get(
            f"{TONCENTER_API_URL}/getTransactions",
            params=params,
            timeout=20.0
        )
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "10"))
            logger.warning(f"TON Center 429, ожидание {retry_after}с...")
            await asyncio.sleep(retry_after)
            return None
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            logger.error(f"TON Center API: {data}")
            return None
        return data.get("result", [])
    except Exception as e:
        logger.error(f"TON Center API ошибка: {e}")
        return None


@with_retry(max_retries=2, base_delay=3.0)
async def sync_rent_from_blockchain(wallet: str, max_pages: int = 50) -> int:
    raw_wallet = userfriendly_to_raw(wallet)
    sync_state = await get_sync_state(wallet)

    cur_lt = None
    cur_hash = None
    if sync_state:
        cur_lt = sync_state.get("last_synced_lt")
        cur_hash = sync_state.get("last_synced_hash")

    new_events = []
    pages = 0
    last_utime = 0
    last_lt = ""
    last_hash = ""

    while pages < max_pages:
        if pages > 0:
            await asyncio.sleep(1.2)

        items = await fetch_toncenter_txns(wallet, limit=100, lt=cur_lt, hash_val=cur_hash)
        if not items:
            break

        for item in items:
            in_msg = item.get("in_msg", {})
            dest = in_msg.get("destination", "")
            dest_raw = userfriendly_to_raw(dest)
            if dest_raw != raw_wallet:
                continue

            message = in_msg.get("message", "")
            if "marketapp" not in message.lower():
                continue

            utime = item.get("utime", 0)
            value = int(in_msg.get("value", 0))
            if value <= 0:
                continue

            tx_id = item.get("transaction_id", {})
            tx_hash = tx_id.get("hash", "")
            tx_lt = tx_id.get("lt", "")
            source = in_msg.get("source", "")

            new_events.append({
                "tx_hash": tx_hash,
                "ts": utime,
                "src": source,
                "dst": dest,
                "value_nano": str(value),
            })

            if utime > last_utime:
                last_utime = utime
                last_lt = tx_lt
                last_hash = tx_hash

        if not items:
            break

        if cur_lt and cur_hash:
            break

        cur_lt = tx_lt
        cur_hash = tx_hash
        pages += 1

    if last_lt and last_hash:
        await set_sync_state(wallet, last_lt, last_hash, last_utime)

    if new_events:
        saved = await save_blockchain_rent_events(new_events)
        logger.info(f"Блокчейн: сохранено {saved} событий аренды")
        return saved
    return 0


async def sync_blockchain_rent_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        wallet = context.bot_data.get("MARKETAPP_WALLET", "")
        if not wallet:
            return
        await sync_rent_from_blockchain(wallet, max_pages=3)
    except Exception as e:
        logger.error(f"Ошибка синхронизации блокчейна: {e}", exc_info=True)
    return addr.lower()


@with_retry(max_retries=2, base_delay=5.0)
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
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "10"))
            logger.warning(f"Marketapp API 429, ожидание {retry_after}с...")
            await asyncio.sleep(retry_after)
            return None
        response.raise_for_status()
        data = response.json()
        return data.get("items", [])
    except Exception as e:
        logger.error(f"Marketapp API ошибка ({category}/history): {e}")
        return None


async def fetch_income_for_period(api_token: str, since_ts: int) -> float:
    total_ton = 0.0

    for i, category in enumerate(RENT_CATEGORIES):
        if i > 0:
            await asyncio.sleep(2)
        items = await fetch_rent_history(api_token, category, limit=100)
        if items:
            for item in items:
                ts = item.get("ts", 0)
                if ts >= since_ts:
                    total_ton += _nano_to_ton(item.get("price_nano", "0"))

    return total_ton


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
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "10"))
            logger.warning(f"Marketapp API 429 (my-rented), ожидание {retry_after}с...")
            await asyncio.sleep(retry_after)
            return None
        response.raise_for_status()
        data = response.json()
        return data.get("items", [])
    except Exception as e:
        logger.error(f"Marketapp API ошибка (my-rented): {e}")
        return None


async def fetch_rent_income_events(api_token: str, category: str) -> list | None:
    return await fetch_rent_history(api_token, category, limit=100)


async def collect_rent_events(api_token: str, wallet: str) -> int:
    raw_wallet = userfriendly_to_raw(wallet)
    collected = []

    for i, category in enumerate(RENT_CATEGORIES):
        if i > 0:
            await asyncio.sleep(2)
        items = await fetch_rent_history(api_token, category, limit=100)
        if not items:
            continue
        for item in items:
            src_raw = userfriendly_to_raw(item.get("src", ""))
            dst_raw = userfriendly_to_raw(item.get("dst", ""))
            if src_raw != raw_wallet and dst_raw != raw_wallet:
                continue
            record = {**item, "category": category}
            collected.append(record)

    if not collected:
        return 0

    saved = await save_rent_events(collected)
    if saved:
        logger.info(f"Сохранено новых событий аренды: {saved}")
    return saved


async def sync_rent_events_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        bot_data = context.bot_data
        api_token = bot_data.get("MARKETAPP_API_KEY")
        wallet = bot_data.get("MARKETAPP_WALLET", "")

        if not api_token or not wallet:
            return

        await collect_rent_events(api_token, wallet)
    except Exception as e:
        logger.error(f"Ошибка сбора событий аренды: {e}", exc_info=True)


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
        api_token = bot_data.get("MARKETAPP_API_KEY")

        if not api_token:
            logger.warning("MARKETAPP_API_KEY не задан")
            return

        logger.info("Получаю данные за сутки...")
        since_ts = _ts_days_ago(1)
        profit = await fetch_income_for_period(api_token, since_ts)

        if profit == 0:
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
        api_token = bot_data.get("MARKETAPP_API_KEY")

        if not api_token:
            return

        logger.info("Получаю данные за неделю...")
        since_ts = _ts_days_ago(7)
        profit = await fetch_income_for_period(api_token, since_ts)

        if profit > 0:
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
        api_token = bot_data.get("MARKETAPP_API_KEY")

        if not api_token:
            return

        logger.info("Получаю данные за месяц...")
        since_ts = _ts_days_ago(30)
        profit = await fetch_income_for_period(api_token, since_ts)

        if profit > 0:
            await save_marketapp_profit("month", profit)

        profits = await get_profit_for_period("day", 30)
        report = format_monthly_report(profits)
        await _send_report(context, "month", report)
        logger.info("Ежемесячный отчет отправлен")

    except Exception as e:
        logger.error(f"Ошибка ежемесячного отчета: {e}", exc_info=True)


def setup_marketapp_jobs(application):
    for job_name in ["marketapp_daily", "marketapp_weekly", "marketapp_monthly", "marketapp_rent_sync", "blockchain_rent_sync"]:
        jobs = application.job_queue.get_jobs_by_name(job_name)
        for job in jobs:
            job.schedule_removal()

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

    application.job_queue.run_repeating(
        sync_blockchain_rent_job,
        interval=timedelta(minutes=10),
        first=30,
        name="blockchain_rent_sync"
    )

    logger.info("Marketapp отчеты настроены (09:00 MSK, блокчейн-синхронизация каждые 10 мин)")
