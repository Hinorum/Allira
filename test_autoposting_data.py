"""Тест пересобранного автопостинга.

Проверяет:
  1. utils/market_data — форматтеры: топ-монет, лидеры роста/падения,
     глобальная картина, тренды, TON; NFT: коллекции, аренда, продажа,
     карточка коллекции.
  2. utils/market_data — живой эндпоинт трендов: именно `/search/trending`,
     а не `/trending` (старый путь отдаёт 404).
  3. tasks/autoposting — ротация типов постов и углов (не повторяет
     последние два), сборка промпта, фолбэки, caption.
  4. utils/config — разбор NFT_COLLECTIONS.

Сеть в тестах не используется: ответы API подставляются заглушками.
"""

import asyncio
import os
import sys
import tempfile

TEST_DB = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = TEST_DB
sys.path.insert(0, ".")

# Кириллица, «Σ» и эмодзи в выводе против кодовой страницы cp866/cp1251
# консоли Windows роняют скрипт UnicodeEncodeError на финальном print.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

FAILURES: list[str] = []


def check(cond: bool, name: str, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ------------------------------------------------------------------ фикстуры

MARKETS = [
    {
        "id": "bitcoin", "name": "Bitcoin", "symbol": "btc",
        "current_price": 82394.0, "market_cap": 1.65e12,
        "total_volume": 2.71e10,
        "price_change_percentage_24h": 0.64,
        "price_change_percentage_7d_in_currency": -0.65,
    },
    {
        "id": "ethereum", "name": "Ethereum", "symbol": "eth",
        "current_price": 2428.22, "market_cap": 2.9e11,
        "total_volume": 1.08e10,
        "price_change_percentage_24h": 1.34,
        "price_change_percentage_7d_in_currency": -2.51,
    },
    {
        "id": "zcash", "name": "Zcash", "symbol": "zec",
        "current_price": 1194.81,
        "price_change_percentage_24h": 6.29,
    },
    {
        "id": "near", "name": "NEAR Protocol", "symbol": "near",
        "current_price": 4.83,
        "price_change_percentage_24h": -2.0,
    },
]

GLOBAL = {
    "data": {
        "total_market_cap": {"usd": 2.77e12},
        "total_volume": {"usd": 7.53e10},
        "market_cap_percentage": {"btc": 59.5, "eth": 10.7},
        "active_cryptocurrencies": 22407,
    }
}

TRENDING = {
    "coins": [
        {"item": {"name": "Pepe", "symbol": "pepe", "market_cap_rank": 53}},
        {"item": {"name": "Pearl", "symbol": "pearl", "market_cap_rank": 91}},
    ]
}

TON_MARKETS = [
    {
        "id": "the-open-network", "name": "Gram (prev. Toncoin)",
        "symbol": "gram", "current_price": 1.33, "market_cap": 3.77e9,
        "price_change_percentage_24h": -1.50,
        "price_change_percentage_7d_in_currency": -3.04,
    }
]

COLLECTIONS = [
    {
        "name": "Lost Dogs",
        "address": "EQAl_hUCAeEv-fKtGxYtITAS6PPxuMRaQwHj0QAHeWe6ZSD0",
        "extra_data": {
            "items": 60802, "floor": "98000000000", "rent_floor": "1500000000",
            "volume7d": "420000000000", "volume30d": "1800000000000",
            "owners": 21000, "on_sale_all": 1200,
        },
    },
    {
        "name": "Quiet Collection",
        "address": "EQSomeOtherCollectionAddress",
        "extra_data": {
            "items": 100, "floor": "500000000", "volume30d": "10000000000",
            "owners": 40, "on_sale_all": 3,
        },
    },
    {
        "name": "Dead Collection",
        "address": "EQDead",
        "extra_data": {},
    },
]

RENT_ITEMS = {
    "cursor": None,
    "items": [
        {
            "nft_address": "0:abc",
            "nft_name": "Bear",
            "price_per_day": "2500000000",
            "attributes": [{"trait_type": "Backdrop", "value": "Red"}],
            "min_duration": 3600,
            "max_duration": 2592000,
            "listed_at": 1730000000,
        }
    ],
}

SALE_ITEMS = {
    "cursor": None,
    "items": [
        {
            "address": "0:1", "name": "Golden Key", "collection_address": "EQCol",
            "min_bid": "1500000000", "currency": "GRAM",
            "attributes": [{"trait_type": "Rarity", "value": "Legendary"}],
            "item_num": 7,
        },
        {
            "address": "0:2", "name": "Common Box", "collection_address": "EQCol",
            "min_bid": "100000000", "currency": "GRAM",
            "attributes": [], "item_num": 8,
        },
    ],
}

TONAPI_COLLECTION = {
    "address": "0:25fe",
    "next_item_index": 60802,
    "metadata": {
        "name": "Lost Dogs",
        "description": "Lost Dogs is the first mergeable NFT collection on TON, "
                       "featuring an expansive narrative game and a world full "
                       "of exploration!! To customize your dogs, you can use the "
                       "Mutagen Collection to manipulate your Dog's DNA during "
                       "breeding and unlock their offspring's mutant potential! "
                       "Hurry up! Adopt a Lost Dog, find them their perfect match "
                       "(or a bunch of them!), and breed new pups! Visit our "
                       "official channels for instructions and more information.",
        "marketplace": "getgems.io",
    },
}

TONAPI_ITEMS = {
    "nft_items": [
        {"address": "0:1", "index": 0, "verified": True,
         "metadata": {"name": "Adam"},
         "sale": {"price": {"value": "98000000000", "decimals": 9}}},
        {"address": "0:2", "index": 1, "verified": True,
         "metadata": {"name": "Eve"},
         "sale": {"price": {"value": "3003000000000", "decimals": 9}}},
        {"address": "0:3", "index": 2, "verified": True,
         "metadata": {"name": "Cain"},
         "sale": None},
    ]
}


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def patch_market_data(monkey: dict) -> None:
    """Подменяет сетевые вызовы market_data на фикстуры."""
    import utils.market_data as md

    async def fake_cg_json(path, params=None):
        if path == "/search/trending":
            return TRENDING
        if path == "/global":
            return GLOBAL
        if path == "/coins/markets":
            ids = (params or {}).get("ids")
            if ids:
                return TON_MARKETS
            return MARKETS
        return None

    async def fake_marketapp_json(path, params=None, api_key=""):
        if not api_key:
            return None
        if path == "/v1/collections/":
            return COLLECTIONS
        if path.startswith("/v1/rent/"):
            return RENT_ITEMS
        if path == "/v1/gifts/onsale/":
            return SALE_ITEMS
        return None

    async def fake_tonapi_json(path, params=None, api_key=""):
        if path.endswith("/items"):
            return TONAPI_ITEMS
        return TONAPI_COLLECTION

    monkey["_cg_json"] = md._cg_json
    monkey["_marketapp_json"] = md._marketapp_json
    monkey["_tonapi_json"] = md._tonapi_json
    md._cg_json = fake_cg_json
    md._marketapp_json = fake_marketapp_json
    md._tonapi_json = fake_tonapi_json
    md._cache.clear()


def restore_market_data(monkey: dict) -> None:
    import utils.market_data as md

    md._cg_json = monkey["_cg_json"]
    md._marketapp_json = monkey["_marketapp_json"]
    md._tonapi_json = monkey["_tonapi_json"]
    md._cache.clear()


async def main() -> int:
    # ------------------------------------------------- форматтеры: крипта
    print("1. Форматтеры крипты")
    from utils.market_data import (
        format_global, format_movers, format_top_coins, format_trending, format_ton,
    )

    top = format_top_coins(MARKETS)
    check("Bitcoin" in top and "$82,394.00" in top, "топ: имя и цена монеты", top)
    check("+0.64%" in top, "топ: изменение за 24ч", top)
    check("-0.65%" in top, "топ: изменение за 7д", top)
    check("27.10B" in top, "топ: объём торгов в компактном виде", top)

    movers = format_movers(MARKETS)
    check("NEAR Protocol" in movers and "-2.00%" in movers, "лидер падения", movers)
    check("Zcash" in movers and "+6.29%" in movers, "лидер роста", movers)

    glob = format_global(GLOBAL)
    check("2.77T" in glob, "глобальная: капитализация", glob)
    check("75.30B" in glob, "глобальная: объём", glob)
    check("59.5%" in glob, "глобальная: доминация BTC", glob)

    trends = format_trending(TRENDING)
    check("Pepe" in trends and "#53" in trends, "тренды: монета и ранг", trends)

    ton = format_ton(TON_MARKETS[0])
    check("GRAM" in ton and "$1.33" in ton, "TON: тикер и цена", ton)
    check("-1.50%" in ton, "TON: изменение за 24ч", ton)

    check(format_top_coins([]) == "", "пустой топ даёт пустую строку")
    check(format_movers([]) == "", "пустые лидеры дают пустую строку")
    check(format_trending({"coins": []}) == "", "пустые тренды дают пустую строку")
    check(format_ton({}) == "", "пустой TON даёт пустую строку")

    # ------------------------------------------------------ форматтеры: NFT
    print("2. Форматтеры NFT")
    from utils.market_data import (
        format_collection_meta, format_collections, format_rent_listings,
        format_sale_listings,
    )

    colls = format_collections(COLLECTIONS)
    check("Lost Dogs" in colls, "коллекции: имя", colls)
    check("98.00 TON" in colls, "коллекции: флор из нано-строки", colls)
    check("1.50 TON/сут" in colls, "коллекции: аренда", colls)
    check("1800.00 TON" in colls, "коллекции: объём 30д", colls)
    check("60802" in colls, "коллекции: тираж", colls)
    check("21000" in colls, "коллекции: держатели", colls)
    check(
        colls.index("Lost Dogs") < colls.index("Quiet Collection"),
        "коллекции: сортировка по объёму (Lost Dogs выше)",
        colls,
    )
    check("Dead Collection" not in colls, "коллекции: без статистики не попадают в вывод")

    rent = format_rent_listings(RENT_ITEMS["items"], "gifts")
    check("Bear" in rent and "2.50 TON/сут" in rent, "аренда: имя и цена в сутки", rent)
    check("Backdrop" in rent, "аренда: признак предмета", rent)
    check("подарки" in rent, "аренда: категория в заголовке", rent)
    check(
        "юзернеймы" in format_rent_listings(RENT_ITEMS["items"], "usernames"),
        "аренда: категория юзернеймов в заголовке",
    )
    check(
        "номера" in format_rent_listings(RENT_ITEMS["items"], "numbers"),
        "аренда: категория номеров в заголовке",
    )

    sale = format_sale_listings(SALE_ITEMS["items"])
    check("Common Box" in sale and "0.10 TON" in sale, "продажа: самый дешёвый лот", sale)
    check("1.50 TON" in sale, "продажа: цена лота", sale)
    check(
        sale.index("Common Box") < sale.index("Golden Key"),
        "продажа: сортировка по цене",
        sale,
    )

    meta = format_collection_meta(TONAPI_COLLECTION, TONAPI_ITEMS["nft_items"])
    check("Lost Dogs" in meta, "карточка: имя коллекции", meta)
    check("60802" in meta, "карточка: тираж", meta)
    check("98.00" in meta and "3003.00" in meta, "карточка: цены лотов", meta)
    check("Adam" in meta and "Eve" in meta, "карточка: имена персонажей", meta)
    check("mergeable NFT collection" in meta, "карточка: описание коллекции", meta)

    check(format_collections([]) == "", "пустые коллекции дают пустую строку")
    check(format_rent_listings([]) == "", "пустая аренда даёт пустую строку")
    check(format_sale_listings([]) == "", "пустая продажа даёт пустую строку")
    check(format_collection_meta({}, []) == "", "пустая карточка даёт пустую строку")

    # ------------------------------------------- единицы сумм Marketapp
    print("2a. Единицы сумм Marketapp")
    from utils.market_data import _amount

    check(_amount("98000000000") == 98.0, "нано-TON → TON", str(_amount("98000000000")))
    check(_amount("98.0") == 98.0, "готовые TON не делятся ещё раз", str(_amount("98.0")))
    check(_amount("98") == 98.0, "целое в единицах не делится ещё раз", str(_amount("98")))
    check(
        _amount("2500000", "USDT") == 2.5,
        "нано-USDT → USDT",
        str(_amount("2500000", "USDT")),
    )
    check(
        _amount("2.5", "USDT") == 2.5,
        "готовые USDT не делятся ещё раз",
        str(_amount("2.5", "USDT")),
    )
    check(_amount("500000", "USDT") == 0.5, "нано-USDT ниже порога", str(_amount("500000", "USDT")))
    check(_amount(None) == 0.0, "пустая сумма → 0")
    check(_amount("мусор") == 0.0, "не-число → 0")

    from utils.market_data import _num

    check(_num(82394.0) == "82,394.00", "цена: обычный вид", _num(82394.0))
    check(_num(0.083) == "0.0830", "цена: копеечная монета не обнуляется", _num(0.083))
    check(_num(0) == "0.00", "цена: ноль", _num(0))

    # ------------------------------ общий кэш рынка для чата и для канала
    print("2b. Общий кэш CoinGecko")
    from utils.market import format_snapshot

    snapshot = format_snapshot(MARKETS)
    check("BTC $82,394.00" in snapshot, "снапшот чата: цена BTC", snapshot)
    check("ETH $2,428.22" in snapshot, "снапшот чата: цена ETH", snapshot)
    check("Zcash" not in snapshot, "снапшот чата: только своя пятёрка", snapshot)
    check(format_snapshot([]) == "", "пустой снапшот → пустая строка")

    # Монета дешевле доллара: раньше печаталась «$0» — модель писала, что
    # монета ничего не стоит.
    cheap = format_snapshot([{
        "id": "dogecoin", "symbol": "doge", "current_price": 0.083,
        "price_change_percentage_24h": 1.5,
    }])
    check("$0.0" in cheap and "$0 " not in cheap, "снапшот чата: мелкая цена не обнуляется", cheap)

    # ---------------------------------------------- сборка живого факт-листа
    print("3. Сборка факт-листа")
    import utils.market_data as md

    monkey: dict = {}
    patch_market_data(monkey)
    try:
        facts, sources = await md.fetch_market_facts(
            api_key="test-key", tonapi_key="", collections=md.DEFAULT_COLLECTIONS
        )
        check("Bitcoin" in facts, "факт-лист: крипта", facts[:200])
        check("Lost Dogs" in facts, "факт-лист: NFT-коллекция", facts[:200])
        check("CoinGecko" in sources, "источники: CoinGecko", str(sources))
        check("Marketapp" in sources, "источники: Marketapp", str(sources))
        check("TonAPI" in sources, "источники: TonAPI", str(sources))
        check("UTC" in facts, "факт-лист: отметка времени", facts[:80])

        crypto_only, crypto_sources = await md.fetch_market_facts(
            api_key="test-key", tonapi_key="", want_crypto=True, want_nft=False
        )
        check("Bitcoin" in crypto_only, "только крипта: есть крипта", crypto_only[:120])
        check("Lost Dogs" not in crypto_only, "только крипта: нет NFT", crypto_only[:120])
        check("Marketapp" not in crypto_sources, "только крипта: источник NFT не выбран")

        nft_only, nft_sources = await md.fetch_market_facts(
            api_key="test-key", tonapi_key="", want_crypto=False, want_nft=True
        )
        check("Lost Dogs" in nft_only, "только NFT: есть NFT", nft_only[:120])
        check("Bitcoin" not in nft_only, "только NFT: нет крипты", nft_only[:120])

        # Без ключа Marketapp и без сети блок NFT должен быть пустым, но
        # сборка не должна падать.
        no_key, no_key_sources = await md.fetch_market_facts(
            api_key="", tonapi_key="", want_crypto=False, want_nft=True
        )
        check("CoinGecko" not in no_key_sources, "без ключа: Marketapp не в источниках")

        # Один упавший источник не должен уносить остальные: раньше
        # исключение от любого блока роняло весь сбор, и в канал уходил
        # текст-заглушка вместо поста с данными.
        async def broken_marketapp(path, params=None, api_key=""):
            raise RuntimeError("Marketapp упал")

        md._marketapp_json = broken_marketapp
        md._cache.clear()
        try:
            partial, partial_sources = await md.fetch_market_facts(
                api_key="test-key", tonapi_key="", want_crypto=True, want_nft=True
            )
            check("Bitcoin" in partial, "упавший Marketapp: крипта уцелела", partial[:120])
            check("Lost Dogs" in partial, "упавший Marketapp: TonAPI уцелел", partial[:120])
            check("Marketapp" not in partial_sources,
                  "упавший Marketapp: в источниках не значится", str(partial_sources))
        finally:
            md._marketapp_json = monkey["_marketapp_json"]
            md._cache.clear()

        async def broken_cg(path, params=None):
            raise RuntimeError("CoinGecko упал")

        md._cg_json = broken_cg
        md._cache.clear()
        try:
            partial2, partial2_sources = await md.fetch_market_facts(
                api_key="test-key", tonapi_key="", want_crypto=True, want_nft=True
            )
            check("Lost Dogs" in partial2, "упавший CoinGecko: NFT уцелел", partial2[:120])
            check("CoinGecko" not in partial2_sources,
                  "упавший CoinGecko: в источниках не значится", str(partial2_sources))
        finally:
            md._cg_json = monkey["_cg_json"]
            md._cache.clear()
    finally:
        restore_market_data(monkey)

    # ---------------------------------------------- путь трендов у CoinGecko
    print("4. Путь эндпоинта трендов")
    import utils.market_data as md

    async def fake_cg_get(path, params=None):
        return FakeResponse(200, TRENDING)

    async def fake_cg_get_404(path, params=None):
        return FakeResponse(404, {"error": "Incorrect path"})

    original_get = md._cg_get
    try:
        md._cg_get = fake_cg_get
        md._cache.clear()
        trending_ok = await md._cg_json("/search/trending")
        check(trending_ok is not None, "/search/trending отвечает", str(trending_ok)[:80])

        md._cg_get = fake_cg_get_404
        md._cache.clear()
        trending_bad = await md._cg_json("/trending")
        check(trending_bad is None, "/trending даёт 404 и не ломает сбор", str(trending_bad))
    finally:
        md._cg_get = original_get
        md._cache.clear()

    # -------------------------------------------------- ротация в автопостинге
    print("5. Ротация типов и углов поста")
    from tasks import autoposting

    autoposting._recent_types.clear()
    autoposting._recent_angles.clear()

    picked = [autoposting.pick_post_type() for _ in range(12)]
    check(len(set(picked)) == 3, "ротация типов: используются все три типа", str(picked))
    check(
        all(picked[i] != picked[i + 2] for i in range(len(picked) - 2)),
        "ротация типов: третий пост не повторяет первый",
        str(picked),
    )

    angles = [autoposting.pick_angle() for _ in range(12)]
    check(
        len(set(angles)) >= 4,
        "ротация углов: разнообразие сохраняется",
        str(angles),
    )
    check(
        all(angles[i] != angles[i + 2] for i in range(len(angles) - 2)),
        "ротация углов: без повторов через один",
        str(angles),
    )
    check(
        all(a in autoposting.POST_ANGLES for a in angles),
        "ротация углов: все значения известны",
        str(angles),
    )

    # ------------------------------------------------------ сборка промпта
    print("6. Промпт и caption")
    topic = autoposting.build_topic(
        "nft", "Lost Dogs: тираж 60802, флор 98.00 TON", ["Marketapp", "TonAPI"]
    )
    check("Источники данных: Marketapp, TonAPI" in topic, "промпт: перечислены источники", topic)
    check("NFT-рынок TON" in topic, "промпт: тема NFT", topic)
    check("Lost Dogs" in topic, "промпт: факт-лист внутри", topic)
    check("ТОЛЬКО цифры и факты" in topic, "промпт: запрет выдумывать цифры", topic)

    topic_crypto = autoposting.build_topic("crypto", "Bitcoin $82394", ["CoinGecko"])
    check("криптовалютный рынок" in topic_crypto, "промпт: тема крипты", topic_crypto)

    topic_empty = autoposting.build_topic("mixed", "", [])
    check("ДАННЫХ НЕТ" in topic_empty, "промпт: без данных — честная просьба не выдумывать", topic_empty)

    caption = autoposting.build_caption("Тест <b>пост</b> & проверка", "nft")
    check("&lt;b&gt;" in caption, "caption: HTML экранирован", caption)
    check("#NFT" in caption and "#TON" in caption, "caption: теги по теме NFT", caption)
    check("#CryptoNews" not in caption, "caption: теги крипты не в NFT-посте", caption)

    long_caption = autoposting.build_caption("а" * 2000, "crypto")
    check(len(long_caption) <= 1024, "caption: уложился в лимит Telegram", str(len(long_caption)))

    mixed_caption = autoposting.build_caption("Текст", "mixed")
    check("#CryptoNews" in mixed_caption and "#NFT" in mixed_caption,
          "caption: в смешанном посте оба тега", mixed_caption)

    # ------------------------------------------------------------ фолбэки
    print("7. Фолбэки без данных")
    check(
        bool(autoposting.get_fallback_nft_data()),
        "фолбэк для NFT есть",
    )
    crypto_fallback = autoposting.get_fallback_crypto_data()
    nft_fallback = autoposting.get_fallback_nft_data()
    check(crypto_fallback != nft_fallback, "фолбэки различаются по темам")
    check(
        "$" not in crypto_fallback,
        "фолбэк не рисует выдуманных цен",
        crypto_fallback,
    )

    # ------------------------------------------------------------- новости
    print("7a. Новости из RSS")
    from utils.market_data import format_news, parse_news_feed

    rss = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title><![CDATA[TON NFT collection launches new breeding update]]></title>
    <link>https://example.com/ton-nft</link>
    <pubDate>__NOW__</pubDate>
    <description><![CDATA[<p>Some <b>html</b> in the description</p>]]></description>
  </item>
  <item>
    <title>Bitcoin ETF inflows hit record high</title>
    <link>https://example.com/btc</link>
    <pubDate>__NOW__</pubDate>
  </item>
  <item>
    <title>Old news from last year</title>
    <link>https://example.com/old</link>
    <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
  </item>
</channel></rss>"""

    from email.utils import format_datetime
    from datetime import datetime, timezone

    now_rfc = format_datetime(datetime.now(timezone.utc), usegmt=True)
    rss_fresh = rss.replace("__NOW__", now_rfc)

    items = parse_news_feed(rss_fresh, "Test")
    check(len(items) == 3, "RSS: все три item разобраны", str(len(items)))
    check(
        items[0]["title"] == "TON NFT collection launches new breeding update",
        "RSS: CDATA-заголовок разобран",
        items[0]["title"],
    )
    check(items[0]["link"] == "https://example.com/ton-nft", "RSS: ссылка разобрана")
    check(items[0]["source"] == "Test", "RSS: источник проставлен")

    tracking = parse_news_feed(
        rss_fresh.replace(
            "<link>https://example.com/ton-nft</link>",
            "<link>https://example.com/ton-nft?utm_source=rss_feed&amp;utm_medium=rss</link>",
        ),
        "Test",
    )
    check(
        tracking[0]["link"] == "https://example.com/ton-nft",
        "RSS: трекинговые параметры убраны из ссылки",
        tracking[0]["link"],
    )

    check(parse_news_feed("", "Test") == [], "пустой RSS → пустой список")
    check(parse_news_feed("<rss><channel>", "Test") == [], "битый XML → пустой список")
    check(parse_news_feed("<html><body>no items</body></html>", "Test") == [],
          "HTML вместо RSS → пустой список")

    news_all = format_news(items)
    check("TON NFT collection" in news_all, "новости: заголовок попал в блок", news_all)
    check("Bitcoin ETF" in news_all, "новости: обычная крипто-новость", news_all)
    check("Old news from last year" not in news_all, "новости: старые отсеяны", news_all)
    check("example.com" in news_all, "новости: ссылка в блоке", news_all)

    news_nft = format_news(items, nft_only=True)
    check("TON NFT collection" in news_nft, "новости NFT: подходящая попала", news_nft)
    check("Bitcoin ETF" not in news_nft, "новости NFT: монежная отсеяна", news_nft)

    # «wallet» в заголовке про DOGE — не NFT-новость
    doge = [{
        "title": "Dogecoin wallet, app teams unite in push for DOGE",
        "link": "", "published": now_rfc, "source": "Test",
    }]
    check(
        format_news(doge, nft_only=True) == "",
        "новости NFT: «wallet» про DOGE не считается NFT-новостью",
        format_news(doge, nft_only=True),
    )
    check(
        "Dogecoin" in format_news(doge),
        "новости: общая крипто-новость проходит",
    )

    # --------------------------------------------- разбор NFT_COLLECTIONS
    print("8. Разбор NFT_COLLECTIONS")
    from utils.config import _parse_collections

    default = _parse_collections("")
    check("Lost Dogs" in default, "пустая настройка → дефолтный watchlist", str(default))

    parsed = _parse_collections("Mutagen:EQMutagenAddress,Another:EQOther")
    check(parsed.get("Mutagen") == "EQMutagenAddress", "разбор имени и адреса", str(parsed))
    check(parsed.get("Another") == "EQOther", "разбор второй пары", str(parsed))

    bare = _parse_collections("EQAl_hUCAeEv-fKtGxYtITAS6PPxuMRaQwHj0QAHeWe6ZSD0")
    check("Lost Dogs" in bare, "голый известный адрес → имя из дефолта", str(bare))

    raw = _parse_collections("0:abcdef")
    check("0:abcdef" in raw.values(), "raw-адрес 0: не режется на имя/адрес", str(raw))

    print()
    if FAILURES:
        print(f"❌ Провалено проверок: {len(FAILURES)}")
        for name in FAILURES:
            print(f"   — {name}")
        return 1
    print("✅ Все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
