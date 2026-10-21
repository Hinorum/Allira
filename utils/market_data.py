"""Живые рыночные данные для автопостинга.

Раньше автопостинг брал ровно один эндпоинт CoinGecko за раз (top/trending/
global) и подставлял его в промпт. Три следствия:

  * тренды вообще не работали — путь `/trending` отдаёт 404, живой путь
    `/search/trending`;
  * в посте не было ни одной цифры по NFT, хотя бот живёт в TON и в его
    промптах фигурируют коллекции;
  * при любой ошибке уходил один и тот же статичный текст без цифр.

Здесь собраны источники и форматтеры. Модуль ничего не знает про промпты и
про Telegram: на выходе — plain-текст с реальными числами, который автопостинг
складывает в факт-лист поста. Форматтеры чистые (без сети), поэтому их
покрывают тесты.
"""

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from cachetools import TTLCache

from utils.http_client import get_client, with_retry
from utils.config import DEFAULT_COLLECTIONS

logger = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3"
MARKETAPP_API_URL = "https://api.marketapp.org"
TONAPI_API_URL = "https://tonapi.io/v2"

# RSS-ленты крипто-новостей. Ключей не требуют — в отличие от новостного
# API CryptoCompare, который теперь отдаёт 401 без ключа.
NEWS_FEEDS = (
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
)

# Слова, по которым новость считается про NFT/TON, а не про монеты.
# Сопоставление по границам слова: без них «ton» ловил «Button», а
# «wallet» — новости про DOGE-кошельки, не про NFT.
NFT_PATTERN = re.compile(
    r"\b(nft|nfts|ton|getgems|collection|collections|pfp|mint|minting|"
    r"telegram|bluechip|rugpull|dao|soulbound|airdrop|metaverse)\b",
    re.IGNORECASE,
)

# Кэш живых данных: пост выходит раз в 12-14 часов, а внутри одного поста
# данные нужны в нескольких блоках (цифры + тренды + NFT). Кэш на 10 минут
# снимает лишние обращения к API и переживает пик 429 у CoinGecko.
_cache: TTLCache = TTLCache(maxsize=16, ttl=600)

# Ключ CoinGecko для TON. С 2025 года на бирже тикер сменился на GRAM,
# но id в API остался прежним — искать по символу/имени нельзя.
TON_COIN_ID = "the-open-network"

# Категории аренды NFT на Marketapp: подарки, юзернеймы, номера.
RENT_CATEGORIES = ("gifts", "usernames", "numbers")

_UA = {"User-Agent": "AlliraCryptoBot/1.0"}


def _num(value, digits: int = 2) -> str:
    """Число в строку с разделителем тысяч; не-числа — пустая строка.

    Для цен: у монет дешевле доллара двух знаков не хватало — Dogecoin
    с курсом $0.13 печатался как «$0.13», а копеечные монеты как «$0.00».
    Теперь точность подстраивается под величину.
    """
    if value is None or isinstance(value, bool):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if digits == 0:
        return f"{number:,.0f}"
    if 0 < abs(number) < 1:
        return f"{number:,.4f}"
    return f"{number:,.{digits}f}"


def _compact(value) -> str:
    """Большие числа в компактном виде: 1.65T, 27.1B, 980M."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= limit:
            return f"{number / limit:.2f}{suffix}"
    return f"{number:.2f}"


def _signed(value, digits: int = 2) -> str:
    """Процент с знаком: +1.25 / -0.40."""
    if value is None or isinstance(value, bool):
        return ""
    try:
        return f"{float(value):+.{digits}f}%"
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------- CoinGecko

# Пауза между запросами к CoinGecko. Бесплатный тариф отдаёт 429 на
# пачках: раньше сбор блока делал 4 запроса подряд, и к четвёртому
# (отдельный запрос курса TON) лимит уже срабатывал — блок выпадал.
_CG_MIN_GAP = 1.2
_last_cg_request = 0.0


@with_retry(max_retries=2, base_delay=2.0)
async def _cg_get(path: str, params: dict | None = None):
    client = await get_client()
    return await client.get(
        f"{COINGECKO_URL}{path}",
        params=params or {},
        headers=_UA,
        timeout=20.0,
    )


async def _cg_json(path: str, params: dict | None = None):
    """Ответ CoinGecko или None. 429/404 не поднимаются — просто пропуск."""
    global _last_cg_request
    gap = time.time() - _last_cg_request
    if gap < _CG_MIN_GAP:
        await asyncio.sleep(_CG_MIN_GAP - gap)
    try:
        response = await _cg_get(path, params)
    except Exception as e:
        logger.warning(f"CoinGecko {path}: {e}")
        return None
    finally:
        _last_cg_request = time.time()
    if response.status_code == 429:
        logger.warning(f"CoinGecko {path}: rate limit")
        return None
    if response.status_code != 200:
        logger.warning(f"CoinGecko {path}: HTTP {response.status_code}")
        return None
    try:
        return response.json()
    except Exception as e:
        logger.warning(f"CoinGecko {path}: битый JSON ({e})")
        return None


def format_top_coins(coins: list) -> str:
    """Топ-монеты по капитализации: цена, 24ч, 7д, объём."""
    lines = []
    for coin in coins[:8]:
        name = coin.get("name") or coin.get("symbol") or "?"
        symbol = (coin.get("symbol") or "").upper()
        price = _num(coin.get("current_price"))
        change_24h = _signed(coin.get("price_change_percentage_24h"))
        change_7d = _signed(coin.get("price_change_percentage_7d_in_currency"))
        volume = _compact(coin.get("total_volume"))
        if not price:
            continue
        tail = f"24ч {change_24h}" if change_24h else ""
        if change_7d:
            tail += f", 7д {change_7d}"
        if volume:
            tail += f", объём {volume}$"
        label = f"{name} ({symbol})" if symbol else str(name)
        lines.append(f"{label}: ${price} — {tail}")
    if not lines:
        return ""
    return "Топ криптовалют по капитализации:\n" + "\n".join(lines)


def format_movers(coins: list) -> str:
    """Лучший рост и падение за 24ч среди крупных монет."""
    scored = []
    for coin in coins:
        change = coin.get("price_change_percentage_24h")
        price = coin.get("current_price")
        if change is None or price is None:
            continue
        scored.append((float(change), coin))
    if not scored:
        return ""
    scored.sort(key=lambda pair: pair[0])
    lines = []
    for change, coin in (scored[0], scored[-1]):
        name = coin.get("name") or coin.get("symbol") or "?"
        price = _num(coin.get("current_price"))
        lines.append(f"{name}: ${price}, 24ч {_signed(change)}")
    if len(lines) < 2:
        return ""
    return "Лидеры падения и роста за 24ч:\n" + "\n".join(lines)


def format_global(data: dict) -> str:
    """Глобальные цифры рынка: капитализация, объём, доминация BTC."""
    gdata = (data or {}).get("data") or {}
    total_mcap = gdata.get("total_market_cap", {}).get("usd")
    total_volume = gdata.get("total_volume", {}).get("usd")
    dominance = gdata.get("market_cap_percentage", {}) or {}
    btc = dominance.get("btc")
    eth = dominance.get("eth")
    active = gdata.get("active_cryptocurrencies")

    lines = []
    if total_mcap:
        lines.append(f"Общая капитализация рынка: ${_compact(total_mcap)}")
    if total_volume:
        lines.append(f"Объём торгов за 24ч: ${_compact(total_volume)}")
    if btc:
        dom = f"доминация BTC {float(btc):.1f}%"
        if eth:
            dom += f", ETH {float(eth):.1f}%"
        lines.append(dom)
    if active:
        lines.append(f"Активных монет: {active}")
    if not lines:
        return ""
    return "Глобальная картина рынка:\n" + "\n".join(lines)


def format_trending(data: dict) -> str:
    """Живой путь трендов CoinGecko — `/search/trending`, не `/trending`."""
    coins = ((data or {}).get("coins") or [])[:6]
    names = []
    for entry in coins:
        item = entry.get("item") or {}
        name = item.get("name")
        symbol = (item.get("symbol") or "").upper()
        rank = item.get("market_cap_rank")
        if not name:
            continue
        label = f"{name} ({symbol})" if symbol else str(name)
        if rank:
            label += f", ранг #{rank}"
        names.append(label)
    if not names:
        return ""
    return "В поисковых трендах CoinGecko прямо сейчас:\n" + "\n".join(names)


def format_ton(coin: dict) -> str:
    """Курс нативного токена сети (TON/GRAM) с 24ч и 7д."""
    if not coin:
        return ""
    price = _num(coin.get("current_price"))
    if not price:
        return ""
    symbol = (coin.get("symbol") or "ton").upper()
    change_24h = _signed(coin.get("price_change_percentage_24h"))
    change_7d = _signed(coin.get("price_change_percentage_7d_in_currency"))
    cap = _compact(coin.get("market_cap"))
    line = f"{symbol}: ${price}"
    if change_24h:
        line += f", 24ч {change_24h}"
    if change_7d:
        line += f", 7д {change_7d}"
    if cap:
        line += f", капитализация {cap}$"
    return "Нативный токен сети:\n" + line


async def get_markets() -> list:
    """Топ-100 монет CoinGecko. Общий кэш для канала и для чата.

    Раньше чат и автопостинг ходили в CoinGecko разными запросами: чат
    брал пять монет по ids, канал — сотню по капитализации. Два запроса
    вместо одного, и оба упирались в 429 бесплатного тарифа.
    """
    cached = _cache.get("markets")
    if cached is not None:
        return cached

    data = await _cg_json(
        "/coins/markets",
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 100,
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "24h,7d",
        },
    )
    markets = data if isinstance(data, list) else []
    # Кэшируем и пустой ответ: при 429 не хотим долбить API на каждое
    # сообщение в чат.
    _cache["markets"] = markets
    return markets


async def fetch_crypto_facts() -> str:
    """Живой блок по крипте. Пустая строка — данных нет."""
    cached = _cache.get("crypto")
    if cached is not None:
        return cached

    blocks: list[str] = []
    ton_coin: dict | None = None

    # per_page=100: в одном ответе умещается и топ для показа, и лидеры
    # роста/падения, и нативный токен сети. Отдельный запрос за TON раньше
    # был четвёртым подряд — и упирался в 429 бесплатного тарифа.
    markets = await get_markets()
    if markets:
        top = format_top_coins(markets)
        if top:
            blocks.append(top)
        movers = format_movers(markets)
        if movers:
            blocks.append(movers)
        for coin in markets:
            if coin.get("id") == TON_COIN_ID:
                ton_coin = coin
                break

    trending = await _cg_json("/search/trending")
    if trending:
        block = format_trending(trending)
        if block:
            blocks.append(block)

    global_data = await _cg_json("/global")
    if global_data:
        block = format_global(global_data)
        if block:
            blocks.append(block)

    if ton_coin is None:
        # Токен выпал из сотни — спросим его отдельно, но только тогда.
        ton_list = await _cg_json(
            "/coins/markets",
            {
                "vs_currency": "usd",
                "ids": TON_COIN_ID,
                "order": "market_cap_desc",
                "sparkline": "false",
                "price_change_percentage": "24h,7d",
            },
        )
        if isinstance(ton_list, list) and ton_list:
            ton_coin = ton_list[0]

    block = format_ton(ton_coin or {})
    if block:
        blocks.append(block)

    result = "\n\n".join(blocks)
    _cache["crypto"] = result
    return result


# ---------------------------------------------------------------- Marketapp


@with_retry(max_retries=2, base_delay=3.0)
async def _marketapp_get(path: str, params: dict | None, api_key: str):
    client = await get_client()
    return await client.get(
        f"{MARKETAPP_API_URL}{path}",
        params=params or {},
        headers={"Authorization": api_key, **_UA},
        timeout=20.0,
    )


async def _marketapp_json(path: str, params: dict | None, api_key: str):
    if not api_key:
        return None
    try:
        response = await _marketapp_get(path, params, api_key)
    except Exception as e:
        logger.warning(f"Marketapp {path}: {e}")
        return None
    if response.status_code in (401, 403):
        logger.warning(f"Marketapp {path}: ключ не принят ({response.status_code})")
        return None
    if response.status_code == 429:
        logger.warning(f"Marketapp {path}: rate limit")
        return None
    if response.status_code != 200:
        logger.warning(f"Marketapp {path}: HTTP {response.status_code}")
        return None
    try:
        return response.json()
    except Exception as e:
        logger.warning(f"Marketapp {path}: битый JSON ({e})")
        return None


def _amount(value, currency: str = "") -> float:
    """Сумма Marketapp: нано-единицы или уже готовые единицы.

    API мечет формат: `price_per_day` документирован как «nanograms»,
    `price` в ленте платежей — уже в TON, а у `floor`/`min_bid` единицы
    не описаны вовсе. Раньше всё делилось на 1e9, и цена в USDT
    показывалась в тысячу раз дороже реальной.

    Разбор по разряду: значение ниже порога — это уже единицы, выше —
    нано. Порог разный для TON (1e6 nano = 0.001 TON) и USDT (1e4).
    """
    if value is None:
        return 0.0
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return 0.0
    if raw == 0:
        return 0.0
    if (currency or "").upper() == "USDT":
        divisor, units_limit = 1e6, 1e4
    else:
        divisor, units_limit = 1e9, 1e6
    if abs(raw) < units_limit:
        return raw
    return raw / divisor


def _currency_label(currency: str) -> str:
    """Человеческое имя валюты. TON и GRAM — одна и та же сеть."""
    normalized = (currency or "").upper()
    if normalized in ("", "GRAM", "TON"):
        return "TON"
    return normalized


def format_collections(collections: list) -> str:
    """Статистика NFT-коллекций: флор, аренда, объём, держатели.

    Сортировка — по 30-дневному объёму: это единственный признак «коллекция
    живая», а не просто существует.
    """
    rows = []
    for coll in collections or []:
        stats = coll.get("extra_data") or {}
        name = coll.get("name") or "?"
        floor = _amount(stats.get("floor"))
        rent_floor = _amount(stats.get("rent_floor"))
        volume_7d = _amount(stats.get("volume7d"))
        volume_30d = _amount(stats.get("volume30d"))
        items = stats.get("items")
        owners = stats.get("owners")
        on_sale = stats.get("on_sale_all")

        parts = []
        if floor:
            parts.append(f"флор {floor:.2f} TON")
        if rent_floor:
            parts.append(f"аренда от {rent_floor:.2f} TON/сут")
        if volume_7d:
            parts.append(f"объём 7д {volume_7d:.2f} TON")
        if volume_30d:
            parts.append(f"объём 30д {volume_30d:.2f} TON")
        if items:
            parts.append(f"предметов {items}")
        if owners:
            parts.append(f"держателей {owners}")
        if on_sale:
            parts.append(f"в продаже {on_sale}")
        if not parts:
            continue
        rows.append((volume_30d or volume_7d, name, ", ".join(parts), coll.get("address")))

    if not rows:
        return ""
    rows.sort(key=lambda row: row[0], reverse=True)
    lines = []
    for _, name, parts, address in rows[:6]:
        line = f"{name}: {parts}"
        if address:
            line += f" (getgems.io/collection/{address})"
        lines.append(line)
    return "NFT-коллекции TON (Marketapp, по объёму за 30 дней):\n" + "\n".join(lines)


def format_rent_listings(items: list, category: str = "") -> str:
    """Живые лоты аренды: сколько стоит аренда подарка/юзернейма/номера."""
    lines = []
    for item in items or []:
        name = item.get("nft_name") or "?"
        per_day = _amount(item.get("price_per_day"))
        if not per_day:
            continue
        attributes = item.get("attributes") or []
        trait = ""
        for attr in attributes:
            if isinstance(attr, dict) and attr.get("trait_type"):
                trait = f" [{attr.get('trait_type')}: {attr.get('value')}]"
                break
        lines.append(f"{name}{trait}: аренда {per_day:.2f} TON/сут")
    if not lines:
        return ""
    title = {
        "gifts": "Сейчас в аренде (подарки, Marketapp)",
        "usernames": "Сейчас в аренде (юзернеймы, Marketapp)",
        "numbers": "Сейчас в аренде (номера, Marketapp)",
    }.get(category, "Сейчас в аренде (Marketapp)")
    return f"{title}:\n" + "\n".join(lines)


def format_sale_listings(items: list) -> str:
    """Живые лоты продажи: минимальная цена и топ-лоты."""
    rows = []
    for item in items or []:
        name = item.get("name") or "?"
        currency = _currency_label(item.get("currency"))
        min_bid = _amount(item.get("min_bid"), item.get("currency"))
        if not min_bid:
            continue
        rows.append((min_bid, name, currency, item.get("attributes")))
    if not rows:
        return ""
    rows.sort(key=lambda row: row[0])
    lines = []
    for min_bid, name, currency, attributes in rows[:6]:
        trait = ""
        for attr in attributes or []:
            if isinstance(attr, dict) and attr.get("trait_type"):
                trait = f" ({attr.get('trait_type')}: {attr.get('value')})"
                break
        lines.append(f"{name}{trait}: от {min_bid:.2f} {currency}")
    cheapest = rows[0][0]
    return (
        "Лоты в продаже (Marketapp), самые дешёвые:\n"
        + "\n".join(lines)
        + f"\nМинимальная цена лота сейчас: {cheapest:.2f} {rows[0][2]}"
    )



async def fetch_marketapp_facts(api_key: str) -> str:
    """Блок по NFT-рынку TON: коллекции + аренда + продажа."""
    if not api_key:
        return ""
    cached = _cache.get(f"nft_marketapp_{bool(api_key)}")
    if cached is not None:
        return cached

    blocks: list[str] = []

    collections = await _marketapp_json("/v1/collections/", None, api_key)
    if isinstance(collections, list) and collections:
        block = format_collections(collections)
        if block:
            blocks.append(block)

    # Аренда есть у трёх категорий: подарки, юзернеймы и номера. Раньше
    # читались только подарки — половина аренды TON-рынка в пост не
    # попадала вовсе.
    for category in RENT_CATEGORIES:
        rent = await _marketapp_json(f"/v1/rent/{category}/", {"limit": 4}, api_key)
        if isinstance(rent, dict):
            block = format_rent_listings(rent.get("items"), category)
            if block:
                blocks.append(block)

    sale = await _marketapp_json("/v1/gifts/onsale/", {"limit": 6}, api_key)
    if isinstance(sale, dict):
        block = format_sale_listings(sale.get("items"))
        if block:
            blocks.append(block)

    result = "\n\n".join(blocks)
    _cache[f"nft_marketapp_{bool(api_key)}"] = result
    return result


# ------------------------------------------------------------------ TonAPI


@with_retry(max_retries=2, base_delay=3.0)
async def _tonapi_get(path: str, params: dict | None = None, api_key: str = ""):
    client = await get_client()
    headers = dict(_UA)
    if api_key:
        headers["Authorization"] = api_key
    return await client.get(
        f"{TONAPI_API_URL}{path}",
        params=params or {},
        headers=headers,
        timeout=20.0,
    )


async def _tonapi_json(path: str, params: dict | None = None, api_key: str = ""):
    try:
        response = await _tonapi_get(path, params, api_key)
    except Exception as e:
        logger.warning(f"TonAPI {path}: {e}")
        return None
    if response.status_code != 200:
        logger.warning(f"TonAPI {path}: HTTP {response.status_code}")
        return None
    try:
        return response.json()
    except Exception as e:
        logger.warning(f"TonAPI {path}: битый JSON ({e})")
        return None


def format_collection_meta(collection: dict, items: list | None = None,
                           fallback_name: str = "") -> str:
    """Карточка конкретной коллекции: описание, тираж, лоты, персонажи."""
    if not collection:
        return ""
    metadata = collection.get("metadata") or {}
    name = metadata.get("name") or fallback_name or "?"
    supply = collection.get("next_item_index")
    description = (metadata.get("description") or "").strip()
    if len(description) > 220:
        description = description[:217] + "..."

    lines = [f"Коллекция {name}:"]
    if supply and supply > 0:
        lines.append(f"тираж {supply} предметов")
    if description:
        lines.append(f"описание: {description}")

    prices = []
    item_names = []
    for item in items or []:
        sale = item.get("sale") or {}
        price = sale.get("price") or {}
        value = price.get("value")
        decimals = price.get("decimals")
        if value is not None and decimals is not None:
            try:
                prices.append(float(value) / (10 ** int(decimals)))
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        item_name = ((item.get("metadata") or {}).get("name") or "").strip()
        if item_name and item_name not in item_names:
            item_names.append(item_name)

    if prices:
        lines.append(
            f"в продаже {len(prices)} лотов, цена от {min(prices):.2f} до {max(prices):.2f} TON"
        )
    if item_names:
        lines.append("персонажи: " + ", ".join(item_names[:6]))
    if not (supply or description or prices):
        return ""
    return "\n".join(lines)


async def fetch_collection_facts(collections: dict[str, str], api_key: str = "") -> str:
    """Карточки отслеживаемых коллекций через TonAPI."""
    if not collections:
        return ""
    cache_key = "nft_collections_" + ",".join(sorted(collections.values()))
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    blocks: list[str] = []
    for label, address in collections.items():
        collection = await _tonapi_json(f"/nfts/collections/{address}", None, api_key)
        if not collection:
            continue
        items = await _tonapi_json(
            f"/nfts/collections/{address}/items", {"limit": 50}, api_key
        )
        item_list = (items or {}).get("nft_items") if isinstance(items, dict) else None
        block = format_collection_meta(collection, item_list, label)
        if block:
            blocks.append(block)

    result = "\n\n".join(blocks)
    _cache[cache_key] = result
    return result


# ------------------------------------------------------------------ Новости


def _local_name(tag: str) -> str:
    """Имя тега без XML-пространства имён: `{ns}item` → `item`."""
    return (tag or "").rsplit("}", 1)[-1]


def _child_text(node, name: str) -> str:
    """Текст первого дочернего элемента с таким именем."""
    for child in node:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _clean_url(url: str) -> str:
    """Ссылка без трекинговых параметров.

    RSS-ленты лепят в адрес `?utm_source=rss_feed&utm_medium=rss&utm_campaign=…`
    — это треть промпта на каждый заголовок, и в канал такая ссылка
    выглядит мусором.
    """
    if not url:
        return ""
    return url.split("?", 1)[0].rstrip("?&")


def _strip_html(text: str) -> str:
    """Убирает теги и схлопывает пробелы — описание в RSS это HTML."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-z]+;|&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_news_feed(xml_text: str, source: str = "") -> list[dict]:
    """RSS → список заголовков: {title, link, published}.

    Заголовок — единственное, что уходит в пост: полные тексты новостей
    съедают промпт и модель начинает их пересказывать вместо своего текста.
    """
    if not xml_text or "<item" not in xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning(f"RSS {source}: битый XML ({e})")
        return []

    items = []
    for node in root.iter():
        if _local_name(node.tag) != "item":
            continue
        title = _strip_html(_child_text(node, "title"))
        if not title:
            continue
        link = _child_text(node, "link")
        if not link:
            for child in node:
                if _local_name(child.tag) == "link" and child.get("href"):
                    link = child.get("href", "")
                    break
        published = ""
        for field in ("pubDate", "published", "date"):
            published = _child_text(node, field)
            if published:
                break
        items.append({
            "title": title,
            "link": _clean_url(link),
            "published": published,
            "source": source,
        })
    return items


def _is_fresh(item: dict, max_age_hours: int = 36) -> bool:
    """Новость не старше max_age_hours. Без даты — пропускаем."""
    raw = item.get("published")
    if not raw:
        return False
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return False
    if moment is None:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - moment).total_seconds() / 3600
    return -2 <= age <= max_age_hours


def format_news(items: list[dict], nft_only: bool = False) -> str:
    """Свежие заголовки — короткий блок для факт-листа."""
    chosen = []
    for item in items:
        title = item.get("title") or ""
        if nft_only and not NFT_PATTERN.search(title):
            continue
        if not _is_fresh(item):
            continue
        chosen.append(item)
    if not chosen:
        return ""

    lines = []
    for item in chosen[:5]:
        line = f"- {item['title']}"
        source = item.get("source")
        if source:
            line += f" ({source})"
        if item.get("link"):
            line += f" — {item['link']}"
        lines.append(line)
    if not lines:
        return ""
    title = "Свежие новости по NFT и TON:" if nft_only else "Свежие крипто-новости:"
    return title + "\n" + "\n".join(lines)


@with_retry(max_retries=1, base_delay=2.0)
async def _fetch_feed(url: str):
    client = await get_client()
    # Таймаут скромный: лента — вспомогательный источник, и ждать её
    # полминуты ради поста нельзя.
    return await client.get(url, headers=_UA, timeout=12.0, follow_redirects=True)


async def fetch_news_facts(nft_only: bool = False) -> str:
    """Свежие заголовки из RSS-лент. Пустая строка — ленты недоступны."""
    cached = _cache.get(f"news_{nft_only}")
    if cached is not None:
        return cached

    items: list[dict] = []
    for source, url in NEWS_FEEDS:
        try:
            response = await _fetch_feed(url)
        except Exception as e:
            logger.warning(f"RSS {source}: {type(e).__name__} {e}")
            continue
        if response.status_code != 200:
            logger.warning(f"RSS {source}: HTTP {response.status_code}")
            continue
        items.extend(parse_news_feed(response.text, source))

    result = format_news(items, nft_only)
    _cache[f"news_{nft_only}"] = result
    return result


# ------------------------------------------------------------------- Сводка


def _stamp() -> str:
    return time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime())


async def fetch_market_facts(
    api_key: str = "",
    tonapi_key: str = "",
    collections: dict[str, str] | None = None,
    want_crypto: bool = True,
    want_nft: bool = True,
) -> tuple[str, list[str]]:
    """Собирает факт-лист для поста.

    Возвращает (текст, список использованных источников). Источник попадает
    в список, только если по нему пришли реальные данные: в промпте модели
    важно знать, откуда цифры, чтобы она не приписала источнику то, чего
    в нём нет.

    Источники независимы: падение одного не уносит остальные. Раньше
    исключение от любого блока роняло весь сбор — и в канал уходил
    текст-заглушка вместо поста с данными.
    """
    watchlist = collections or DEFAULT_COLLECTIONS
    blocks: list[str] = []
    sources: list[str] = []

    if want_crypto:
        try:
            crypto = await fetch_crypto_facts()
        except Exception as e:
            logger.warning(f"Блок крипты не собрался: {e}")
            crypto = ""
        if crypto:
            blocks.append(crypto)
            sources.append("CoinGecko")

    # Новости идут отдельным блоком: в посте о крипте это заголовки дня,
    # в посте о NFT — только те, что про NFT и TON.
    try:
        news = await fetch_news_facts(nft_only=not want_crypto)
    except Exception as e:
        logger.warning(f"Блок новостей не собрался: {e}")
        news = ""
    if news:
        blocks.append(news)
        sources.append("RSS-новости")

    if want_nft:
        nft_parts = []

        try:
            marketapp = await fetch_marketapp_facts(api_key)
        except Exception as e:
            logger.warning(f"Блок Marketapp не собрался: {e}")
            marketapp = ""
        if marketapp:
            nft_parts.append(marketapp)
            sources.append("Marketapp")

        try:
            tonapi = await fetch_collection_facts(watchlist, tonapi_key)
        except Exception as e:
            logger.warning(f"Блок TonAPI не собрался: {e}")
            tonapi = ""
        if tonapi:
            nft_parts.append(tonapi)
            sources.append("TonAPI")

        if nft_parts:
            blocks.append("\n\n".join(nft_parts))

    if not blocks:
        return "", sources

    return f"Данные собраны {_stamp()}\n\n" + "\n\n".join(blocks), sources
