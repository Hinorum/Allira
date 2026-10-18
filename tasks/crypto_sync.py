"""Фоновые задачи крипто-бухгалтерии.

crypto_sync_jobs(application) — фоновая джоба, которая для каждого
отслеживаемого TON-кошелька через TonAPI подтягивает:
  • Jetton Transfer (входящие и исходящие) — записываются как
    transfer_in / transfer_out с автоценой через price_oracle.
  • TON Transfer без комментария rent — могут быть аирдропы (без явного
    маркера); пока оставляем только транзакции с известными Jetton-
    переводами, TON без метки не пишем.
  • Регулярно прогревает кэш котировок ЦБ за последние 7 дней.

Сделано отдельным модулем, чтобы не трогать marketapp_reports.py:
там сложилась плотная логика NFT-аренды, и подмешивать сюда общий
Jetton-скан было бы источником регрессий.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
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
    """Подключает периодический Jetton-скан для каждого TON-кошелька."""
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
        name="crypto_jetton_sync",
        data={"owner_id": owner_id, "api_key": config.tonapi_api_key},
    )
    application.job_queue.run_repeating(
        _warm_rate_cache_job,
        interval=86400,  # раз в сутки
        name="crypto_rate_warm",
        data={"owner_id": owner_id},
    )
    logger.info(f"Крипто-скан: owner_id={owner_id}, jetton_sync каждые 15 мин")


# --------------------------------------------------------- Jetton-скан


async def _sync_wallets_job(context) -> None:
    owner_id = int(context.job.data.get("owner_id") or 0)
    api_key = context.job.data.get("api_key") or ""
    if not owner_id:
        return
    wallets = list_wallets(owner_id, chain=Chain.TON)
    if not wallets:
        logger.debug("crypto_sync: нет TON-кошельков")
        return
    for wallet in wallets:
        try:
            saved = await sync_jetton_transfers(wallet, api_key)
            logger.info(
                f"crypto_sync: {wallet.address[:8]}… → {saved} переводов"
            )
        except Exception as e:
            logger.error(
                f"crypto_sync: ошибка для {wallet.address}: {e}",
                exc_info=True,
            )
        await asyncio.sleep(2.0)


async def sync_jetton_transfers(wallet, api_key: str) -> int:
    """Сканер Jetton-переводов одного TON-кошелька.

    Использует /v2/accounts/{addr}/jettons/history — пагинированный список
    переводов всех Jetton'ов на кошельке. Каждый перевод — отдельная
    запись в crypto_transactions с source='jetton-sync' и уникальным
    external_id (дедупликация встроена в add_transaction).
    """
    client = await get_client()
    headers = {"User-Agent": "AlliraBot/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_ts = _get_watermark(wallet.id)
    saved = 0

    before_lt: Optional[int] = None
    while True:
        params = {"limit": 100}
        if before_lt:
            params["before_lt"] = before_lt
        try:
            resp = await client.get(
                f"{TONAPI_API_URL}/accounts/{wallet.address}/jettons/history",
                params=params,
                headers=headers,
                timeout=20.0,
            )
        except Exception as e:
            logger.warning(f"jetton history сеть: {e}")
            await asyncio.sleep(2.0)
            return saved

        if resp.status_code == 429:
            await asyncio.sleep(5.0)
            return saved
        if resp.status_code != 200:
            logger.warning(f"jetton history {resp.status_code}: {resp.text[:120]}")
            return saved
        try:
            data = resp.json()
        except Exception:
            return saved

        events = (data or {}).get("events") or []
        if not events:
            break

        stop = False
        for ev in events:
            ts = int(ev.get("timestamp", 0) or 0)
            if ts and last_ts and ts <= last_ts:
                stop = True
                break
            actions = ev.get("actions") or []
            for action in actions:
                if action.get("type") != "JettonTransfer":
                    continue
                jt = action.get("JettonTransfer", {}) or {}
                jetton = jt.get("jetton") or {}
                symbol = (jetton.get("symbol") or "").upper()
                if not symbol:
                    continue
                decimals = int(jetton.get("decimals") or 9)
                raw_amount = jt.get("amount")
                if not raw_amount:
                    continue
                try:
                    amount_int = int(raw_amount)
                except (TypeError, ValueError):
                    continue
                if amount_int == 0:
                    continue

                sender = (jt.get("sender") or {}).get("address", "")
                recipient = (jt.get("recipient") or {}).get("address", "")
                wallet_norm = wallet.address.lower()
                direction: str
                tx_type: str
                if recipient and recipient.lower() == wallet_norm:
                    direction = "in"
                    tx_type = TxType.AIRDROP  # дефолт — аирдроп; пользователь
                                              # может скорректировать вручную
                elif sender and sender.lower() == wallet_norm:
                    direction = "out"
                    tx_type = TxType.SWAP_OUT  # пользователь уточнит
                else:
                    continue

                # Получить котировку для авто-цены (если возможно).
                fiat = ""
                try:
                    market = await get_asset_price_rub(symbol, ts)
                    if market:
                        human_amount = amount_int / (10 ** decimals)
                        fiat = str(round(human_amount * market, 2))
                except Exception:
                    pass

                ext_id = f"{EXTERNAL_PREFIX}:{ev.get('event_id', '')}:{symbol}"
                try:
                    ensure_asset(
                        symbol=symbol,
                        name=jetton.get("name") or symbol,
                        chain=Chain.TON,
                        decimals=decimals,
                        coingecko_id="",
                        contract=(jetton.get("address") or ""),
                        kind="jetton",
                    )
                    add_transaction(
                        owner_id=wallet.owner_id,
                        ts=ts,
                        wallet_id=wallet.id,
                        asset_symbol=symbol,
                        tx_type=tx_type,
                        amount=str(amount_int),
                        fiat_value_rub=fiat or None,
                        note=f"auto jetton-sync; op_hash={ev.get('event_id', '')[:20]}",
                        source="jetton-sync",
                        external_id=ext_id,
                    )
                    saved += 1
                except Exception as e:
                    logger.info(f"jetton-sync: пропуск {symbol} {tx_type}: {e}")

            if ts:
                _set_watermark(wallet.id, max(last_ts, ts))
        if stop:
            break
        # Следующая страница — по последнему lt.
        before_lt = events[-1].get("lt")
        if not before_lt:
            break
        await asyncio.sleep(0.6)
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
    """Раз в сутки прогреваем кэш USD→RUB за вчера и сегодня.

    CoinGecko историю за прошлые дни не трогаем — там ленивая загрузка
    при первом обращении через get_coingecko_price.
    """
    from utils.price_oracle import get_usd_rub_rate
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        await get_usd_rub_rate(today)
        await get_usd_rub_rate(yesterday)
    except Exception as e:
        logger.warning(f"rate warm: {e}")