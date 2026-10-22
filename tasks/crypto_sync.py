"""Фоновые задачи крипто-бухгалтерии.

crypto_sync_jobs(application) — фоновая дода, которая:
  • раз в 15 минут сканирует кошельки владельца (TON через TonAPI, EVM через
    Etherscan V2) и записывает найденные переводы в бухгалтерию —
    см. utils/chain_scan.py и utils/auto_import.py;
  • раз в сутки прогревает кэш котировок ЦБ и CoinGecko.

Классификация переводов (доход / реализация / обмен / перемещение) живёт в
utils/auto_import.py — там же и сопоставление ончейн-переводов с выписками
бирж. Раньше здесь был отдельный Jetton-сканер: он считал любой входящий
перевод с неизвестного адреса аирдропом, из-за чего вывод с биржи попадал в
налоговую базу как доход. Теперь переводы с бирж и между своими кошельками
распознаются как перемещения и базу не трогают.

Сделано отдельным модулем, чтобы не трогать marketapp_reports.py:
там сложилась плотная логика NFT-аренды, и подмешивать сюда общий
сканер было бы источником регрессий.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from telegram.ext import Application

from utils.config import BotConfig
from utils.crypto_db import (Chain, TxType, add_transaction, ensure_asset,
                             list_wallets)
from utils.database import get_db
from utils.http_client import get_client
from utils.price_oracle import get_asset_price_rub

logger = logging.getLogger(__name__)

TONAPI_API_URL = "https://tonapi.io/v2"
# Уникальный префикс для external_id, чтобы дедупликация в crypto_db
# работала и при повторных прогонах сканера.
EXTERNAL_PREFIX = "jetton-sync"


# --------------------------------------------------------- регистрация


def setup_crypto_sync_jobs(application: Application) -> None:
    """Подключает периодический скан кошельков (TON + EVM)."""
    config: BotConfig = application.bot_data.get("BOT_CONFIG")  # type: ignore[assignment]
    if not config:
        logger.warning("setup_crypto_sync_jobs: BOT_CONFIG не найден")
        return
    owner_id = int(config.crypto_owner_id or 0)
    if not owner_id:
        logger.info("setup_crypto_sync_jobs: CRYPTO_OWNER_ID не задан — пропуск")
        return
    if not config.tonapi_api_key:
        logger.info(
            "setup_crypto_sync_jobs: TONAPI_API_KEY не задан — без ключа "
            "будут жёсткие лимиты, но сканер работать будет"
        )

    application.job_queue.run_repeating(
        _sync_wallets_job,
        interval=900,    # раз в 15 минут
        name="crypto_chain_sync",
        data={"owner_id": owner_id, "config": config},
    )
    application.job_queue.run_repeating(
        _warm_rate_cache_job,
        interval=86400,  # раз в сутки
        name="crypto_rate_warm",
        data={"owner_id": owner_id},
    )
    logger.info(f"Крипто-скан: owner_id={owner_id}, скан кошельков каждые 15 мин")


# --------------------------------------------------------- Jetton-скан


async def _sync_wallets_job(context) -> None:
    """Фоновый скан кошельков: переводы → бухгалтерия."""
    from utils.auto_import import run

    owner_id = int(context.job.data.get("owner_id") or 0)
    if not owner_id:
        return
    cfg: BotConfig = context.job.data.get("config")  # type: ignore[assignment]
    try:
        result = await run(owner_id, cfg)
    except Exception as e:
        logger.error(f"crypto_sync: скан не удался: {e}", exc_info=True)
        return
    if result.saved or result.review:
        logger.info(
            f"crypto_sync: +{result.saved} операций, "
            f"{result.duplicates} дублей, на проверку {len(result.review)}"
        )


async def sync_jetton_transfers(wallet, api_key: str) -> int:
    """Скан переводов одного TON-кошелька.

    Оставлен как обёртка над общим конвейером (chain_scan + auto_import):
    админ-панель вызывает его по кнопке «Скан TON». Классификация — общая,
    поэтому вывод с биржи и переводы между своими кошельками больше не
    попадают в базу как доход.
    """
    from utils.auto_import import classify_moves, persist_moves
    from utils.chain_scan import scan_ton

    moves = await scan_ton(wallet.address, api_key)
    if not moves:
        return 0
    classified = await classify_moves(wallet.owner_id, moves)
    saved, _dups, _review = await persist_moves(
        wallet.owner_id, classified, wallet_id=wallet.id)
    return saved


def _get_watermark(wallet_id: int) -> int:
    """Последний обработанный timestamp для кошелька (защита от ре-сканов).

    Храним в crypto_wallets.note? нет — там label. Заведём маленький
    ключ в таблице crypto_wallets через обновление: добавим поле
    last_jetton_ts при миграции (см. init_db ниже). Чтобы не делать
    отдельную миграцию, используем существующую таблицу bot_stats как
    key-value для служебных меток.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM bot_stats WHERE key = ?",
            (f"jetton_sync_ts:{wallet_id}",),
        ).fetchone()
    return int(row["value"]) if row else 0


def _set_watermark(wallet_id: int, ts: int) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO bot_stats (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = MAX(CAST(value AS INTEGER), ?),
                updated_at = CURRENT_TIMESTAMP
            """,
            (f"jetton_sync_ts:{wallet_id}", str(ts), ts),
        )


# --------------------------------------------------------- прогрев кэша


async def _warm_rate_cache_job(context) -> None:
    """Раз в сутки прогреваем кэш котировок.

    Что делаем:
      * USD→RUB за вчера и сегодня (через ЦБ РФ).
      * Исторические цены активов из БД за каждый день активности —
        чтобы /tax не лагал на десятках тысяч операций.

    CoinGecko сам по себе ленивый (первый запрос за день = 1 API-вызов),
    но если операций 10к, это 10к запросов подряд. Прогреваем пачками
    по ассетам: для каждого актива из crypto_assets берём диапазон
    дат от первой до последней операции в БД и заполняем дыры.
    """
    from utils.crypto_db import list_assets, list_transactions
    from utils.database import get_db
    from utils.price_oracle import get_asset_price_rub, get_usd_rub_rate

    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        await get_usd_rub_rate(today)
        await get_usd_rub_rate(yesterday)
    except Exception as e:
        logger.warning(f"rate warm usd: {e}")

    # Прогрев по ассетам: для каждого актива из crypto_assets с хотя бы
    # одной операцией — диапазон дат от min(ts) до max(ts).
    try:
        with get_db() as conn:
            asset_ranges = conn.execute("""
                SELECT a.id, a.symbol, a.coingecko_id,
                       MIN(t.ts) AS min_ts, MAX(t.ts) AS max_ts
                FROM crypto_assets a
                JOIN crypto_transactions t ON t.asset_id = a.id
                GROUP BY a.id
            """).fetchall()
    except Exception as e:
        logger.warning(f"rate warm: ranges: {e}")
        return

    today_dt = datetime.now(tz=timezone.utc).date()
    for r in asset_ranges:
        if not r["coingecko_id"]:
            continue
        start_day = datetime.fromtimestamp(r["min_ts"], tz=timezone.utc).date()
        end_day = min(today_dt,
                      datetime.fromtimestamp(r["max_ts"], tz=timezone.utc).date())
        # Не больше 366 дней назад — CoinGecko free не отдаёт глубже.
        earliest = today_dt - timedelta(days=366)
        if start_day < earliest:
            start_day = earliest

        # Прогреваем дыры — пропускаем дни, по которым уже есть кэш.
        with get_db() as conn:
            cached_rows = conn.execute(
                """SELECT date FROM fiat_rates
                   WHERE base_symbol=? AND quote_symbol='RUB'
                     AND date BETWEEN ? AND ?""",
                (r["coingecko_id"].upper(), start_day.isoformat(), end_day.isoformat()),
            ).fetchall()
        cached_set = {row["date"] for row in cached_rows}
        missing: list[str] = []
        cur = start_day
        while cur <= end_day:
            iso = cur.isoformat()
            if iso not in cached_set:
                missing.append(iso)
            cur += timedelta(days=1)

        if not missing:
            continue

        logger.info(f"rate warm {r['symbol']}: {len(missing)} дней")
        for iso in missing:
            try:
                ts = int(datetime.strptime(iso, "%Y-%m-%d")
                         .replace(tzinfo=timezone.utc).timestamp())
                await get_asset_price_rub(r["symbol"], ts)
            except Exception as e:
                logger.warning(f"rate warm {r['symbol']} {iso}: {e}")
            await asyncio.sleep(0.3)  # не давим CoinGecko
