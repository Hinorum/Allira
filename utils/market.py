import logging

from utils.market_data import get_markets

logger = logging.getLogger(__name__)

# id монет, которые видит Аллира: основной рынок + пара мемкоинов для разнообразия
SNAPSHOT_IDS = ("bitcoin", "ethereum", "solana", "binancecoin", "dogecoin")


def _price(value: float) -> str:
    """Цена с разумной точностью.

    Раньше все цены печатались без дробной части: Dogecoin с курсом
    $0.13 превращался в «DOGE $0» — и модель писала в чат, что он
    ничего не стоит.
    """
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:,.4f}"


def format_snapshot(data: list) -> str:
    """Список монет CoinGecko → короткая строка для промпта."""
    wanted = set(SNAPSHOT_IDS)
    parts: list[str] = []
    for coin in data:
        if coin.get("id") not in wanted:
            continue
        symbol = (coin.get("symbol") or "").upper()
        price = coin.get("current_price")
        change = coin.get("price_change_percentage_24h")
        if not symbol or not isinstance(price, (int, float)):
            continue
        change_str = f" {change:+.1f}%" if isinstance(change, (int, float)) else ""
        parts.append(f"{symbol} {_price(price)}{change_str}")
    if not parts:
        return ""
    # Разделитель "; " — запятая внутри пары "BTC $118,432" ломала бы чтение
    return "Снапшот рынка (CoinGecko, 24ч): " + "; ".join(parts)


async def get_market_snapshot() -> str:
    """Короткий рыночный снапшот для промпта. '' при ошибке/пустоте.

    Аллира — криптоперсонаж: без живых цифр модель выдумывает курсы.

    Данные берутся из общего кэша utils.market_data: раньше чат ходил в
    CoinGecko отдельным запросом по пяти id, а канал — сотней по
    капитализации. Два запроса вместо одного, и оба упирались в 429
    бесплатного тарифа. Кэш и на провал тоже общий: 10 минут без цифр
    лучше, чем блокировка.
    """
    try:
        markets = await get_markets()
    except Exception as e:
        logger.warning(f"Снапшот рынка недоступен: {e}")
        return ""

    return format_snapshot(markets)
