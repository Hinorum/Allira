import logging
import random
import asyncio
import httpx
import time
from telegram.ext import ContextTypes
from telegram.constants import ChatAction
from utils.ai_responses import generate_image_description, generate_post_content

logger = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3"
PEXELS_URL = "https://api.pexels.com/v1/search"

used_images = set()
last_request_time = 0


async def get_crypto_data():
    global last_request_time

    current_time = time.time()
    if current_time - last_request_time < 3.0:
        await asyncio.sleep(3.0)
    last_request_time = time.time()

    endpoints = [
        {
            "url": f"{COINGECKO_URL}/coins/markets",
            "params": {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 10,
                "page": 1,
                "sparkline": False,
                "price_change_percentage": "24h,7d"
            }
        },
        {
            "url": f"{COINGECKO_URL}/trending",
            "params": {}
        },
        {
            "url": f"{COINGECKO_URL}/global",
            "params": {}
        }
    ]

    endpoint = random.choice(endpoints)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                endpoint["url"],
                params=endpoint["params"],
                headers={"User-Agent": "AlliraCryptoBot/1.0"}
            )

            if response.status_code == 429:
                logger.warning("CoinGecko rate limit")
                return get_fallback_crypto_data()

            response.raise_for_status()
            data = response.json()

            if endpoint["url"].endswith("markets"):
                return format_market_data(data)
            elif endpoint["url"].endswith("trending"):
                return format_trending_data(data)
            else:
                return format_global_data(data)

    except Exception as e:
        logger.error(f"CoinGecko error: {e}")
        return get_fallback_crypto_data()


def get_fallback_crypto_data():
    messages = [
        "Крипторынок сегодня показывает интересную динамику! Bitcoin держится уверенно, альткоины готовятся к рывку. Следим за трендами!",
        "Анализ рынка: волатильность растет, что открывает возможности для трейдеров. Важно следить за уровнями поддержки и сопротивления.",
        "HODL или трейдить? Вечный вопрос криптоэнтузиастов. Диверсификация - ключ к успеху в долгосрочной перспективе.",
        "DeFi сектор продолжает развиваться! Новые протоколы предлагают инновационные решения для кредитования и стейкинга.",
        "Web3 и метавселенные набирают обороты. Следим за проектами, которые меняют правила игры в цифровом пространстве."
    ]
    return random.choice(messages)


def _escape_html(text: str) -> str:
    """Экранирует спецсимволы для Telegram HTML parse_mode"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_market_data(coins):
    top_coins = coins[:5]
    lines = ["<b>Топ криптовалют сегодня:</b>\n"]

    for i, coin in enumerate(top_coins, 1):
        name = _escape_html(coin['name'])
        price = coin['current_price']
        change = coin.get('price_change_percentage_24h', 0) or 0
        emoji = "\U0001f7e2" if change > 0 else "\U0001f534" if change < 0 else "\u26aa"

        lines.append(f"{i}. {name}: ${price:,.2f} {emoji} {change:+.2f}%")

    lines.append(f"\n<i>Данные CoinGecko на {time.strftime('%H:%M UTC')}</i>")
    return "\n".join(lines)


def format_trending_data(data):
    coins = data.get('coins', [])[:5]
    lines = ["<b>Сейчас в тренде:</b>\n"]

    for i, coin_data in enumerate(coins, 1):
        coin = coin_data['item']
        name = _escape_html(coin['name'])
        symbol = coin['symbol']
        market_cap_rank = coin.get('market_cap_rank', 'N/A')
        lines.append(f"{i}. {name} ({symbol.upper()}) - Ранг #{market_cap_rank}")

    return "\n".join(lines)


def format_global_data(data):
    gdata = data.get('data', {})
    total_mcap = gdata.get('total_market_cap', {}).get('usd', 0)
    total_volume = gdata.get('total_volume', {}).get('usd', 0)
    btc_dominance = gdata.get('market_cap_percentage', {}).get('btc', 0)

    lines = [
        "<b>Глобальный рынок криптовалют:</b>\n",
        f"Общая капитализация: ${total_mcap:,.0f}",
        f"Объем торгов (24ч): ${total_volume:,.0f}",
        f"Доминация Bitcoin: {btc_dominance:.1f}%",
    ]

    return "\n".join(lines)


async def get_pexels_image(query: str, api_key: str):
    global used_images

    if len(used_images) > 100:
        used_images = set(list(used_images)[-50:])

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                PEXELS_URL,
                params={
                    "query": query,
                    "per_page": 10,
                    "orientation": "landscape",
                    "size": "medium"
                },
                headers={"Authorization": api_key}
            )

            response.raise_for_status()
            data = response.json()

            if not data.get("photos"):
                return None

            for photo in data["photos"]:
                image_url = photo["src"]["large"]
                if image_url not in used_images:
                    used_images.add(image_url)
                    return {
                        "url": image_url,
                        "photographer": photo.get("photographer", "Unknown"),
                        "photographer_url": photo.get("photographer_url", "")
                    }

            used_images.clear()
            image_url = data["photos"][0]["src"]["large"]
            used_images.add(image_url)
            return {
                "url": image_url,
                "photographer": data["photos"][0].get("photographer", "Unknown"),
                "photographer_url": data["photos"][0].get("photographer_url", "")
            }

    except Exception as e:
        logger.error(f"Pexels error: {e}")
        return None


async def do_autoposting(context: ContextTypes.DEFAULT_TYPE):
    try:
        bot_data = context.bot_data
        channel_id = bot_data.get("NEWS_CHANNEL_ID")

        if not channel_id:
            return

        logger.info("Начинаю автопостинг...")

        crypto_text = await get_crypto_data()

        speaker = random.choice(["allira", "lane"])
        post_content = await generate_post_content(
            crypto_text,
            speaker,
            bot_data["DEFAULT_MODEL"],
            bot_data["OPENROUTER_API_KEY"]
        )

        final_caption = f"{post_content}\n\n{crypto_text}\n\n#CryptoNews #AlliraBot"

        image_query = await generate_image_description(
            crypto_text,
            bot_data["DEFAULT_MODEL"],
            bot_data["OPENROUTER_API_KEY"]
        )

        image_data = await get_pexels_image(image_query, bot_data["PEXELS_API_KEY"])

        if image_data:
            await context.bot.send_photo(
                chat_id=channel_id,
                photo=image_data["url"],
                caption=final_caption[:1024],
                parse_mode="HTML"
            )
            logger.info(f"Пост с картинкой отправлен. Фото: {image_data['photographer']}")
        else:
            await context.bot.send_message(
                chat_id=channel_id,
                text=final_caption,
                parse_mode="HTML"
            )
            logger.info("Текстовый пост отправлен")

        global used_images
        if len(used_images) > 50:
            used_images = set(list(used_images)[-25:])

    except Exception as e:
        logger.error(f"Ошибка автопостинга: {e}", exc_info=True)
        try:
            await context.bot.send_message(
                context.bot_data.get("NEWS_CHANNEL_ID"),
                text="Крипторынок продолжает удивлять! Следите за обновлениями. #CryptoNews"
            )
        except Exception:
            pass


async def wakeup_task(context: ContextTypes.DEFAULT_TYPE):
    try:
        import os
        port = int(os.getenv("PORT", "10000"))
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://localhost:{port}/health", timeout=5)
            if response.status_code == 200:
                logger.debug("Health-check OK")
    except Exception as e:
        logger.error(f"Health-check failed: {e}")


def setup_autoposting(application):
    for job_name in ["autoposting", "wakeup"]:
        jobs = application.job_queue.get_jobs_by_name(job_name)
        for job in jobs:
            job.schedule_removal()

    application.job_queue.run_repeating(
        do_autoposting,
        interval=28800,
        first=random.randint(60, 1800),
        name="autoposting"
    )

    application.job_queue.run_repeating(
        wakeup_task,
        interval=120,
        first=10,
        name="wakeup"
    )

    logger.info("Автопостинг настроен")
