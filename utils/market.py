import logging
import time
from cachetools import TTLCache

from utils.http_client import get_client, with_retry

logger = logging.getLogger(__name__)

COINS_URL = "https://api.coingecko.com/api/v3/coins/markets"
# id монет, которые видит Аллира: основной рынок + пара мемкоинов для разнообразия
SNAPSHOT_IDS = "bitcoin,ethereum,solana,binancecoin,dogecoin"

# Кэш и на успех, и на провал: при 429 от CoinGecko не хотим спамить
# запросом на каждое сообщение — 10 минут без цифр лучше, чем блокировка.
_snapshot_cache: TTLCache = TTLCache(maxsize=2, ttl=600)


@with_retry(max_retries=1, base_delay=1.0)
async def _fetch_markets(params: dict):
    client = await get_client()
    return await client.get(
        COINS_URL,
        params=params,
        headers={"User-Agent": "AlliraCryptoBot/1.0"},
        timeout=10.0,
    )


def format_snapshot(data: list) -> str:
    """Список ответа CoinGecko → короткая строка для промпта."""
    parts: list[str] = []
    for coin in data:
        symbol = (coin.get("symbol") or "").upper()
        price = coin.get("current_price")
        change = coin.get("price_change_percentage_24h")
        if not symbol or price is None:
            continue
        change_str = f" {change:+.1f}%" if isinstance(change, (int, float)) else ""
        parts.append(f"{symbol} ${price:,.0f}{change_str}")
    if not parts:
        return ""
    # Разделитель "; " — запятая внутри пары "BTC $118,432" ломала бы чтение
    return "Снапшот рынка (CoinGecko, 24ч): " + "; ".join(parts)


async def get_market_snapshot() -> str:
    """Короткий рыночный снапшот для промпта. '' при ошибке/пустоте.

    Аллира — криптоперсонаж: без живых цифр модель выдумывает курсы.
    Результат (включая провал) кэшируется на 10 минут.
    """
    cached = _snapshot_cache.get("snapshot")
    if cached is not None:
        return cached

    result = ""
    try:
        response = await _fetch_markets({
            "vs_currency": "usd",
            "ids": SNAPSHOT_IDS,
            "order": "market_cap_desc",
            "price_change_percentage": "24h",
        })
        if response.status_code == 200:
            result = format_snapshot(response.json())
        else:
            logger.warning(f"CoinGecko markets: {response.status_code}")
    except Exception as e:
        logger.warning(f"Снапшот рынка недоступен: {e}")

    if not result:
        # Фиксируем и провал, чтобы не долбить API с каждого сообщения
        result = ""
    _snapshot_cache["snapshot"] = result
    return result
