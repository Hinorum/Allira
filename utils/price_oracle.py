"""Курсоид: цены крипты в RUB на дату операции.

Источники:
1. CoinGecko — прайс крипты в USD/EUR. Бесплатного ключа не требует, но
   исторические данные глубже года без paid-плана доступны по дням.
2. ЦБ РФ (cbr-xml-daily.ru) — официальные курсы валют (USD, EUR, CNY…) к RUB.
   Бесплатно, без ключа.

Зачем RUB: для ФНС доход считается в рублях по курсу ЦБ (для USD/EUR) или
по рыночной котировке (для крипты — на дату операции). Берём исторический
CoinGecko в USD и переводим в RUB через курс ЦБ за тот же день — это
типовой приём, которым пользуются большинство декларантов.

Кэш на диске (SQLite, таблица fiat_rates) держит и прайсы, и пары
USD→RUB/EUR→RUB — без него /tax за год с десятками аирдропов на каждый
запрос к API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from utils.database import get_db
from utils.http_client import get_client, with_retry

logger = logging.getLogger(__name__)

COINGECKO_API = "https://api.coingecko.com/api/v3"

# Дефолтный каталог активов: symbol → coin gecko id. Расширяется через
# /wallet_add или ручной ввод в crypto_assets. Этого списка хватает для
# типового TON-портфеля.
DEFAULT_COINGECKO_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "TON": "the-open-network",
    "USDT": "tether",
    "USDC": "usd-coin",
    "BNB": "binancecoin",
    "SOL": "solana",
    "TRX": "tron",
    "DOGE": "dogecoin",
    "NOT": "notcoin",
    "HMSTR": "hamster-kombat",
    "JVT": "jvt",
}

# Символы фиата, для которых ЦБ РФ даёт прямой курс к RUB.
CBR_DIRECT_QUOTES: dict[str, str] = {
    "USD": "R01235",
    "EUR": "R01239",
    "GBP": "R01035",
    "CNY": "R01375",
    "TRY": "R01700",
    "AED": "R01230",
    "KZT": "R01335",
}


# ------------------------------------------------------------------ CoinGecko


@with_retry(max_retries=2, base_delay=2.0)
async def _cg_get(path: str, params: dict) -> dict | None:
    client = await get_client()
    response = await client.get(
        f"{COINGECKO_API}{path}",
        params=params,
        headers={"User-Agent": "AlliraCryptoBot/1.0", "Accept": "application/json"},
        timeout=20.0,
    )
    if response.status_code == 429:
        # Платный план CoinGecko — единственное решение, но /tax всё равно
        # отдаст отчёт «без рыночной котировки», а не упадёт.
        logger.warning("CoinGecko 429 — лимит запросов исчерпан")
        return None
    if response.status_code != 200:
        logger.warning(f"CoinGecko {path}: {response.status_code}")
        return None
    try:
        return response.json()
    except Exception:
        return None


async def get_usd_rub_rate(date_iso: str) -> Optional[float]:
    """Курс ЦБ РФ USD→RUB на дату (или ближайший предшествующий торговый день).

    date_iso в формате 'YYYY-MM-DD'. Возвращает None при ошибке.
    """
    cached = _read_rate_cache(date_iso, "USD", "RUB")
    if cached is not None:
        return cached
    rate = await _cbr_get_rate("USD", date_iso)
    if rate is not None:
        _write_rate_cache(date_iso, "USD", "RUB", rate, "cbr")
    return rate


async def get_coingecko_price(
    coingecko_id: str, date_iso: str, vs: str = "usd"
) -> Optional[float]:
    """Историческая цена актива в vs-валюте на указанную дату.

    CoinGecko принимает dd-mm-yyyy. Возвращает None при ошибке.
    """
    symbol = (coingecko_id or "").strip().lower()
    if not symbol:
        return None
    cache_key = f"{symbol.upper()}@{vs.upper()}"
    cached = _read_rate_cache(date_iso, cache_key, vs.upper())
    if cached is not None:
        return cached
    try:
        day = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return None
    data = await _cg_get(
        f"/coins/{symbol}/history",
        {"date": day, "localization": "false"},
    )
    if not data:
        return None
    market = (data.get("market_data") or {}).get("current_price") or {}
    rate = market.get(vs.lower())
    if rate is None:
        return None
    _write_rate_cache(date_iso, cache_key, vs.upper(), float(rate), "coingecko")
    return float(rate)


async def get_asset_price_rub(symbol: str, ts: int) -> Optional[float]:
    """Цена 1 единицы актива в RUB на дату ts (секунды).

    Алгоритм:
      1. Ищем coingecko_id для symbol (в БД → DEFAULT_COINGECKO_IDS).
      2. Берём историческую цену в USD на дату ts.
      3. Умножаем на курс ЦБ USD→RUB за тот же день.
      Если USD-пары нет (Tether, RU4 / кегль) — возвращаем напрямую RUB.
    """
    date_iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    cg_id, _decimals = await _resolve_coingecko(symbol)
    if not cg_id:
        # Символ не знаем — считаем как 0 (для ФНС это «без рыночной котировки»).
        logger.info(f"get_asset_price_rub: неизвестный актив {symbol!r}")
        return None

    # Стаблкоины: исторического колебания к RUB нет, берём 1 USD → курс ЦБ.
    stable_like = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "EURT", "PYUSD"}
    if symbol.upper() in stable_like:
        return await get_usd_rub_rate(date_iso)

    usd_price = await get_coingecko_price(cg_id, date_iso, "usd")
    if usd_price is None:
        return None
    usd_rub = await get_usd_rub_rate(date_iso)
    if usd_rub is None:
        return None
    return usd_price * usd_rub


async def _resolve_coingecko(symbol: str) -> tuple[Optional[str], int]:
    """Достаём coingecko_id из БД или из дефолтного справочника."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT coingecko_id, decimals FROM crypto_assets WHERE symbol = ?",
            (symbol.upper(),),
        ).fetchone()
    if row:
        return (row["coingecko_id"] or "").strip() or None, int(row["decimals"] or 9)
    cg_id = DEFAULT_COINGECKO_IDS.get(symbol.upper())
    return cg_id, 9


# ---------------------------------------------------------------- ЦБ РФ


async def _cbr_get_rate(currency: str, date_iso: str) -> Optional[float]:
    """Курс ЦБ РФ: currency (USD/EUR/…) → RUB на date_iso.

    cbr-xml-daily.ru/archive/YYYY/MM/DD/daily_json.js — бесплатный эндпоинт,
    отдаёт все курсы за день единым JSON. Выходной/праздник — поле не
    появляется: тогда пробуем ближайший предыдущий день (до 7 попыток).
    """
    base = "https://www.cbr-xml-daily.ru"
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d")
    except ValueError:
        return None
    for delta in range(0, 8):
        cur = d - timedelta(days=delta)
        url_date = cur.strftime("%Y/%m/%d")
        url = f"{base}/archive/{url_date}/daily_json.js"
        client = await get_client()
        try:
            response = await client.get(url, timeout=15.0)
        except Exception as e:
            logger.warning(f"CBR {url}: {e}")
            await asyncio.sleep(1.0)
            continue
        if response.status_code != 200:
            continue
        try:
            payload = response.json()
        except Exception:
            continue
        valutes = (payload or {}).get("Valute") or {}
        rec = valutes.get(currency.upper())
        if rec and rec.get("Value") is not None:
            nominal = float(rec.get("Nominal") or 1)
            return float(rec["Value"]) / nominal
    return None


# --------------------------------------------------------- кэш на диске


def _read_rate_cache(date_iso: str, base: str, quote: str) -> Optional[float]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT rate FROM fiat_rates WHERE date=? AND base_symbol=? AND quote_symbol=?",
            (date_iso, base, quote),
        ).fetchone()
    if not row:
        return None
    try:
        return float(row["rate"])
    except (TypeError, ValueError):
        return None


def _write_rate_cache(date_iso: str, base: str, quote: str, rate: float, source: str):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO fiat_rates (date, base_symbol, quote_symbol, rate, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date, base_symbol, quote_symbol) DO UPDATE SET
                rate = excluded.rate,
                source = excluded.source,
                fetched_at = CURRENT_TIMESTAMP
            """,
            (date_iso, base, quote, str(rate), source),
        )


# --------------------------------------------------------- вспомогательное


def format_rub(value: float | None, decimals: int = 2) -> str:
    """Человекочитаемый RUB: 1234567.89 → 1 234 567,89 ₽."""
    if value is None:
        return "—"
    if abs(value) >= 1:
        formatted = f"{value:,.{decimals}f}"
        # Точки группировки → пробелы, запятая дробной части.
        formatted = formatted.replace(",", " ").replace(".", ",")
        return f"{formatted} ₽"
    return f"{value:.{decimals}f} ₽".replace(".", ",")


def normalize_iso_date(date_str: str) -> Optional[str]:
    """Принимает YYYY-MM-DD, DD.MM.YYYY, DD/MM/YYYY → YYYY-MM-DD."""
    if not date_str:
        return None
    s = date_str.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


async def backfill_rates(symbol: str, start_ts: int, end_ts: int) -> int:
    """Прогрев кэша: ежедневные USD→RUB и прайс symbol→RUB в окне дат.

    Возвращает количество успешно загруженных дней. Прогон фоном — на
    /tax за большой год кэш будет готов.
    """
    saved = 0
    day = datetime.fromtimestamp(start_ts, tz=timezone.utc).date()
    end_day = datetime.fromtimestamp(end_ts, tz=timezone.utc).date()
    while day <= end_day:
        iso = day.isoformat()
        if await get_usd_rub_rate(iso) is not None:
            saved += 1
        if await get_asset_price_rub(symbol, int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp())) is not None:
            pass
        day += timedelta(days=1)
        await asyncio.sleep(0.25)  # не давим CoinGecko
    return saved