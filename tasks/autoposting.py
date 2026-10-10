import logging
import random
import re
import urllib.parse
import time
from collections import deque
from io import BytesIO
from datetime import datetime
from cachetools import TTLCache
from telegram.ext import ContextTypes
from utils.ai_responses import generate_post_content
from utils.common import escape_html
from utils.database import increment_stat
from utils.http_client import get_client, with_retry

logger = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3"
POLLINATIONS_URL = "https://image.pollinations.ai/prompt"

_coingecko_cache = TTLCache(maxsize=5, ttl=600)
last_request_time = 0

# Углы (форматы) поста: раньше каждый цикл был «вставь цифры в 3-6
# предложений» — один и тот же формат из цикла в цикл. Память на 4 поста
# (~2 суток при интервале 12-14ч) не даёт повторять последние два угла.
POST_ANGLES = {
    "hot_take": "Формат: горячее мнение (hot take) — резкая оценка ситуации одним тезисом в начале.",
    "question": "Формат: закончи пост интригующим вопросом к аудитории.",
    "analysis": "Формат: мини-разбор — 2-3 причины, почему рынок движется именно так.",
    "scenario": "Формат: «а что если» — короткий сценарий на ближайшие дни, без обещаний и финансовых советов.",
    "stat_of_day": "Формат: цифра дня — начни с одной яркой цифры из данных и коротко прокомментируй её.",
    "myth": "Формат: развенчание мифа — типичное заблуждение трейдеров и его опровержение.",
}
_recent_angles: deque[str] = deque(maxlen=4)

# Данные CoinGecko форматируются с <b>/<i> для HTML-подачи, но в промпт LLM
# они уходят обычным текстом: иначе модель лепит теги в ответ, а после
# escape_html в канале подписчики видят осмысленные <b> как текст.
_MARKUP_RE = re.compile(r"</?(?:b|i|em|strong|u|code|s)>")


def strip_markup(text: str) -> str:
    """Снимает известные HTML-теги, оставляя текст и цифры."""
    return _MARKUP_RE.sub("", text)


def pick_angle() -> str:
    """Ключ угла поста, не повторяющий последние два — для разнообразия."""
    recent = list(_recent_angles)[-2:]
    candidates = [k for k in POST_ANGLES if k not in recent]
    angle = random.choice(candidates or list(POST_ANGLES))
    _recent_angles.append(angle)
    return angle


def get_smart_interval() -> int:
    return random.choice([43200, 50400])


def get_fallback_crypto_data():
    messages = [
        "Крипторынок сегодня показывает интересную динамику! Bitcoin держится уверенно, альткоины готовятся к рывку.",
        "Анализ рынка: волатильность растет, что открывает возможности для трейдеров.",
        "HODL или трейдить? Вечный вопрос криптоэнтузиастов. Диверсификация - ключ к успеху.",
        "DeFi сектор продолжает развиваться! Новые протоколы предлагают инновационные решения.",
        "Web3 и метавселенные набирают обороты. Следим за проектами, которые меняют правила игры."
    ]
    return random.choice(messages)


def format_market_data(coins):
    top_coins = coins[:5]
    lines = ["<b>Топ криптовалют сегодня:</b>\n"]

    for i, coin in enumerate(top_coins, 1):
        name = escape_html(coin['name'])
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
        name = escape_html(coin['name'])
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


@with_retry(max_retries=2, base_delay=2.0)
async def _fetch_coingecko(url: str, params: dict):
    client = await get_client()
    return await client.get(
        url,
        params=params,
        headers={"User-Agent": "AlliraCryptoBot/1.0"},
        timeout=15.0
    )


_endpoint_order = 0


def _rotated_endpoints() -> list[dict]:
    """Эндпоинты в круговой очерёдности: каждый пост начинается со следующего.

    Раньше здесь был random.choice — один и тот же «топ-5» мог выпадать
    несколько циклов подряд, а тренды и глобальные данные простаивали.
    """
    global _endpoint_order
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
    start = _endpoint_order % len(endpoints)
    _endpoint_order += 1
    return endpoints[start:] + endpoints[:start]


async def get_crypto_data():
    global last_request_time

    errors: list[str] = []
    # Пауза считается один раз на вызов, а не на каждый эндпоинт: внутри
    # одного «дай мне данные» попытки идут подряд — это одна операция,
    # а не всплеск запросов. Иначе первая же 429 блокировала бы
    # оставшиеся эндпоинты и мы снова валились в заглушку.
    rate_limited = time.time() - last_request_time < 3.0

    # Проходим по всем эндпоинтам, пока один не даст данные: раньше
    # ошибка или 429 первого же выбора random.choice уводили в статичную
    # заглушку без единой цифры.
    for endpoint in _rotated_endpoints():
        url = endpoint["url"]

        # Кэш-ключ — сам эндпоинт. Раньше все три ответа лежали под одним
        # ключом: пост с «топом монет» мог получить закэшированные «тренды».
        if url in _coingecko_cache:
            return _coingecko_cache[url]

        if rate_limited:
            logger.info("CoinGecko: вызов слишком частый, беру следующий эндпоинт")
            continue

        last_request_time = time.time()
        try:
            response = await _fetch_coingecko(url, endpoint["params"])

            if response.status_code == 429:
                logger.warning("CoinGecko rate limit")
                errors.append(f"{url} (429)")
                continue

            response.raise_for_status()
            data = response.json()

            if url.endswith("markets"):
                result = format_market_data(data)
            elif url.endswith("trending"):
                result = format_trending_data(data)
            else:
                result = format_global_data(data)

            _coingecko_cache[url] = result
            return result

        except Exception as e:
            logger.error(f"CoinGecko error: {e}")
            errors.append(f"{url} ({e})")

    if errors:
        logger.warning(f"CoinGecko недоступен, все эндпоинты исчерпаны: {errors}")
    elif rate_limited:
        logger.info("CoinGecko: данные не запрошены (пауза), отдаю запасной текст")
    return get_fallback_crypto_data()


def sanitize_image_prompt(text: str) -> str:
    """Оставляет в промпте картинки латиницу, кириллицу, цифры и разделители.

    Раньше regex вырезал всё не-латинское: цифры ("bitcoin to 100k" →
    "bitcoin to k"), а ответ модели по-русски превращался в пустоту и
    уходил в случайный фолбэк-стиль.
    """
    return re.sub(r"[^a-zA-ZА-Яа-яЁё0-9\s,-]", "", text).strip()


async def generate_image(post_text: str, model: str, api_key: str) -> bytes | None:
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
        # Крипто-образы: раньше весь пул был «пейзаж без смысла», картинка
        # не отсылала к теме поста.
        "busy trading floor at night, glowing candlestick charts on monitors, moody neon",
        "gold coins cascading through dark space, dramatic rim light, macro detail",
        "cyberpunk street market at dusk, holographic price tickers above the crowd",
        "lone trader silhouette before a wall of monitors, blue glow, contemplative",
        "stock chart carved into mountain landscape, sunrise over peaks, epic scale",
        "hands exchanging a glowing token in rain, neon reflections, street photo",
        "desert highway sign with arrow pointing up, endless blue sky, optimism",
    ]

    try:
        prompt_response = await get_llm_response(
            user_prompt=f"Based on this text's mood and energy, write ONE short image prompt (5-10 words). "
                        f"Crypto elements (coins, charts, blockchain) are welcome but should be subtle, "
                        f"part of the scene, not the main focus. Translate the emotion into a visual scene.\n\nText: {post_text[:500]}",
            system_prompt="You are an image prompt generator. Reply with ONLY the English prompt, no other text. "
                          "Focus on mood, energy, movement. Crypto can be present but subtle, not dominant.",
            model=model,
            api_key=api_key
        )
        # Латиница, кириллица и цифры — см. sanitize_image_prompt.
        prompt_response = sanitize_image_prompt(prompt_response)
        if len(prompt_response.split()) < 3:
            prompt_response = random.choice(fallback_styles)
    except Exception:
        prompt_response = random.choice(fallback_styles)

    prompt = f"{prompt_response}, cinematic photography, high quality, no text no letters no logos no watermark"
    encoded = urllib.parse.quote(prompt)
    url = f"{POLLINATIONS_URL}/{encoded}?width=1280&height=720&nologo=true&seed={random.randint(1, 99999)}"

    try:
        client = await get_client()
        response = await client.get(url, follow_redirects=True, timeout=60.0)
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

        # Угол поста и чистка HTML: теги CoinGecko нужны для полевой подачи
        # в канал, но в промпт модели уходят обычным текстом.
        crypto_text = strip_markup(await get_crypto_data())
        angle = POST_ANGLES[pick_angle()]
        topic = f"{angle}\n\nДанные CoinGecko:\n{crypto_text}"

        speaker = random.choice(["allira", "lane"])
        post_content = await generate_post_content(
            topic,
            speaker,
            bot_data["DEFAULT_MODEL"],
            bot_data["OPENROUTER_API_KEY"]
        )

        # caption уходит с parse_mode="HTML": текст модели нужно экранировать,
        # иначе один уголковый скобочный "<" в ответе роняет отправку поста.
        # Усечь надо ДО экранирования — иначе можно разрезать сущность &amp;.
        text = (post_content or "").strip()
        if len(text) > 900:
            text = text[:897] + "..."
        final_caption = f"{escape_html(text)}\n\n#CryptoNews #AlliraBot"
        if len(final_caption) > 1024:
            # Экранирование раздуло текст — ужимаем текстовую часть с запасом
            # на худший случай (один символ превращается в сущность до 5 символов).
            safe_text = text[:190] + "..."
            final_caption = f"{escape_html(safe_text)}\n\n#CryptoNews #AlliraBot"

        image_bytes = await generate_image(
            post_content,
            bot_data.get("IMAGE_PROMPT_MODEL", bot_data["DEFAULT_MODEL"]),
            bot_data["OPENROUTER_API_KEY"],
        )

        if image_bytes:
            photo_file = BytesIO(image_bytes)
            photo_file.name = "crypto_post.jpg"

            await context.bot.send_photo(
                chat_id=channel_id,
                photo=photo_file,
                caption=final_caption,
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

        await increment_stat("total_posts")

    except Exception as e:
        logger.error(f"Ошибка автопостинга: {e}", exc_info=True)
        try:
            await context.bot.send_message(
                context.bot_data.get("NEWS_CHANNEL_ID"),
                text="Крипторынок продолжает удивлять! Следите за обновлениями. #CryptoNews"
            )
        except Exception:
            pass





def setup_autoposting(application):
    for job_name in ["autoposting"]:
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

    # Раньше здесь была задача wakeup_task с запросом к localhost/health каждые
    # 120с. На Render она бесполезна: засыпание определяется по ВНЕШНЕМ входящему
    # трафику, а loopback-запрос внутри контейнера его не сбрасывает.
    logger.info(f"Автопостинг настроен (интервал: {interval//3600}ч)")
