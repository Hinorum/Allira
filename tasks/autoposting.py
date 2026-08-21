import logging
import random
import asyncio
import httpx
import time
import os
import re
import urllib.parse
from datetime import datetime
from telegram.ext import ContextTypes
from utils.ai_responses import generate_post_content
from utils.database import increment_stat

logger = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3"
POLLINATIONS_URL = "https://image.pollinations.ai/prompt"

last_request_time = 0



def get_smart_interval() -> int:
    return random.choice([43200, 50400])


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
        "Крипторынок сегодня показывает интересную динамику! Bitcoin держится уверенно, альткоины готовятся к рывку.",
        "Анализ рынка: волатильность растет, что открывает возможности для трейдеров.",
        "HODL или трейдить? Вечный вопрос криптоэнтузиастов. Диверсификация - ключ к успеху.",
        "DeFi сектор продолжает развиваться! Новые протоколы предлагают инновационные решения.",
        "Web3 и метавселенные набирают обороты. Следим за проектами, которые меняют правила игры."
    ]
    return random.choice(messages)


def _escape_html(text: str) -> str:
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


async def generate_image(post_text: str) -> bytes | None:
    """Генерирует картинку через Pollinations.ai по настроению поста."""
    from utils.ai_responses import get_llm_response

    fallback_styles = [
        "cinematic drone shot, epic storm clouds over open road, dramatic lighting",
        "aerial view of rushing river through canyon, golden hour, motion blur",
        "macro shot of cracked earth with single green sprout, resilience, hope",
        "figure standing at crossroads in fog, two paths diverging, mysterious atmosphere",
        "waves crashing against rocky shore, spray, powerful ocean energy",
        "vast desert with single road stretching to horizon, freedom, journey",
        "thunderstorm over city skyline, lightning, electric atmosphere",
        "northern lights dance over snowy mountains, cosmic energy, awe",
        "wind blowing through tall grass field, golden light, natural movement",
        "lighthouse beam cutting through dense fog, guiding light, determination",
    ]

    try:
        prompt_response = await get_llm_response(
            user_prompt=f"Based on this text's mood and energy, write ONE short image prompt (5-10 words). "
                        f"NO crypto, NO coins, NO Bitcoin, NO charts, NO graphs. "
                        f"Translate the emotion into a visual scene.\n\nText: {post_text[:500]}",
            system_prompt="You are an image prompt generator. Reply with ONLY the English prompt, no other text. "
                          "Focus on mood, energy, movement. Never mention cryptocurrency, coins, logos, or text.",
            model="nvidia/nemotron-3.5-lightning:free",
            api_key=os.getenv("OPENROUTER_API_KEY", "")
        )
        prompt_response = re.sub(r'[^a-zA-Z\s,-]', '', prompt_response).strip()
        if len(prompt_response.split()) < 3:
            prompt_response = random.choice(fallback_styles)
    except Exception:
        prompt_response = random.choice(fallback_styles)

    prompt = f"{prompt_response}, cinematic photography, high quality, no text no letters no logos no watermark"
    encoded = urllib.parse.quote(prompt)
    url = f"{POLLINATIONS_URL}/{encoded}?width=1280&height=720&nologo=true&seed={random.randint(1, 99999)}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url, follow_redirects=True)
            if response.status_code == 200 and len(response.content) > 1000:
                logger.info(f"Картинка сгенерирована ({len(response.content)} bytes)")
                return response.content
            else:
                logger.warning(f"Pollinations: плохой ответ ({response.status_code})")
                return None
    except Exception as e:
        logger.error(f"Pollinations error: {e}")
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

        image_bytes = await generate_image(post_content)

        if image_bytes:
            from io import BytesIO
            photo_file = BytesIO(image_bytes)
            photo_file.name = "crypto_post.jpg"

            await context.bot.send_photo(
                chat_id=channel_id,
                photo=photo_file,
                caption=final_caption[:1024],
                parse_mode="HTML"
            )
            logger.info("Пост с AI- картинкой отправлен")
        else:
            await context.bot.send_message(
                chat_id=channel_id,
                text=final_caption,
                parse_mode="HTML"
            )
            logger.info("Текстовый пост отправлен (картинка не сгенерировалась)")

        increment_stat("total_posts")

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

    interval = get_smart_interval()
    application.job_queue.run_repeating(
        do_autoposting,
        interval=interval,
        first=random.randint(60, 300),
        name="autoposting"
    )

    application.job_queue.run_repeating(
        wakeup_task,
        interval=120,
        first=10,
        name="wakeup"
    )

    logger.info(f"Автопостинг настроен (интервал: {interval//3600}ч)")
