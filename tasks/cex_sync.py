"""Фоновая задача: периодический синк CEX (Bybit + OKX).

Раз в час запрашивает сделки через API и добавляет их в БД с защитой от
дублей по external_id. Ошибки (429, ключ отозван) логируются, но не
блокируют бота — следующий прогон повторит попытку.
"""

from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application

from utils.cex_api import (get_cex_watermark, set_cex_watermark,
                           sync_bybit_trades, sync_okx_trades)
from utils.crypto_db import add_transaction, CryptoError, TxType
from utils.price_oracle import get_asset_price_rub

logger = logging.getLogger(__name__)


def setup_cex_sync_jobs(application: Application) -> None:
    """Подключает фоновый CEX-синк, если есть ключи хотя бы одной биржи."""
    cfg = application.bot_data.get("BOT_CONFIG")
    if not cfg:
        return
    owner_id = int(cfg.crypto_owner_id or 0)
    bybit = bool(cfg.bybit_api_key and cfg.bybit_api_secret)
    okx = bool(cfg.okx_api_key and cfg.okx_api_secret and cfg.okx_passphrase)
    if not owner_id or (not bybit and not okx):
        logger.info("setup_cex_sync_jobs: ничего не настроено — пропуск")
        return

    application.job_queue.run_repeating(
        _sync_job,
        interval=3600,
        name="cex_sync_hourly",
        data={"owner_id": owner_id, "bybit": bybit, "okx": okx,
              "bybit_key": cfg.bybit_api_key, "bybit_secret": cfg.bybit_api_secret,
              "okx_key": cfg.okx_api_key, "okx_secret": cfg.okx_api_secret,
              "okx_passphrase": cfg.okx_passphrase},
    )
    logger.info(f"CEX-скан: bybit={bybit} okx={okx}, owner={owner_id}, каждый час")


async def _sync_job(context) -> None:
    owner_id = int(context.job.data.get("owner_id") or 0)
    if not owner_id:
        return

    if context.job.data.get("bybit"):
        try:
            await _run_bybit(context.job.data, owner_id)
        except Exception as e:
            logger.error(f"cex_sync bybit: {e}", exc_info=True)
        await asyncio.sleep(2.0)

    if context.job.data.get("okx"):
        try:
            await _run_okx(context.job.data, owner_id)
        except Exception as e:
            logger.error(f"cex_sync okx: {e}", exc_info=True)


async def _run_bybit(data: dict, owner_id: int) -> None:
    since_ms = get_cex_watermark("bybit") or (
        int((asyncio.get_event_loop().time() - 365 * 86400) * 1000)
    )
    until_ms = int(asyncio.get_event_loop().time() * 1000)
    result = await sync_bybit_trades(
        data["bybit_key"], data["bybit_secret"],
        since_ts=since_ms, until_ts=until_ms,
    )
    saved, _dups = await persist_cex_rows(owner_id, result.rows, "bybit")
    if result.rows:
        max_ts = max(r.ts for r in result.rows)
        set_cex_watermark("bybit", max_ts * 1000)
    if saved:
        logger.info(f"cex_sync bybit: +{saved} сделок")
    if result.errors:
        logger.warning(f"cex_sync bybit: {result.errors[:3]}")


async def _run_okx(data: dict, owner_id: int) -> None:
    since_ms = get_cex_watermark("okx") or (
        int((asyncio.get_event_loop().time() - 90 * 86400) * 1000)
    )
    until_ms = int(asyncio.get_event_loop().time() * 1000)
    result = await sync_okx_trades(
        data["okx_key"], data["okx_secret"], data["okx_passphrase"],
        since_ts=since_ms, until_ts=until_ms,
    )
    saved, _dups = await persist_cex_rows(owner_id, result.rows, "okx")
    if result.rows:
        max_ts = max(r.ts for r in result.rows)
        set_cex_watermark("okx", max_ts * 1000)
    if saved:
        logger.info(f"cex_sync okx: +{saved} сделок")
    if result.errors:
        logger.warning(f"cex_sync okx: {result.errors[:3]}")


async def persist_cex_rows(owner_id: int, rows: list, exchange: str) -> tuple[int, int]:
    """Записывает сделки с двойной записью «дебет-кредит».

    Возвращает (saved, duplicates). Каждая сделка — ДВЕ проводки:
      покупка  BTC/USDT → Дт BTC (BUY, себестоимость) / Кт USDT (TRADE_OUT)
      продажа  BTC/USDT → Дт USDT (TRADE_IN, выручка) / Кт BTC (SELL)

    RUB-стоимость quote-ветки берётся из price_oracle (для стейблкоинов —
    курс ЦБ на дату сделки). Если котировки нет, обе ноги пишутся без
    RUB — расчёт зафиксирует это как «нет котировки» в отчёте /tax.

    Используется и фоновой джобой (cex_sync_hourly), и ручным /cex_sync.
    """
    saved = 0
    dups = 0
    for row in rows:
        try:
            fiat = row.fiat_value_rub or ""
            cost = row.cost_basis_rub or ""

            # RUB-оценка quote-ветки сделки — если она ещё не задана.
            if not fiat and not cost and getattr(row, "quote_asset", ""):
                try:
                    q_price = await get_asset_price_rub(row.quote_asset, row.ts)
                except Exception:
                    q_price = None
                if q_price is not None:
                    rub_total = float(row.quote_amount) * q_price
                    if row.tx_type == TxType.BUY:
                        cost = f"{rub_total:.2f}"
                    else:
                        fiat = f"{rub_total:.2f}"

            add_transaction(
                owner_id=owner_id,
                ts=row.ts,
                asset_symbol=row.asset,
                tx_type=row.tx_type,
                amount=row.amount,
                fiat_value_rub=fiat or None,
                cost_basis_rub=cost or None,
                note=row.note,
                source=exchange,
                external_id=row.external_id,
            )
            saved += 1

            # Парная проводка quote-валюты (дебет-кредит).
            quote_asset = getattr(row, "quote_asset", "")
            quote_amount = getattr(row, "quote_amount", "")
            if quote_asset and quote_amount and float(quote_amount) > 0:
                leg_type = (TxType.TRADE_OUT if row.tx_type == TxType.BUY
                            else TxType.TRADE_IN)
                add_transaction(
                    owner_id=owner_id,
                    ts=row.ts,
                    asset_symbol=quote_asset,
                    tx_type=leg_type,
                    amount=quote_amount,
                    note=f"{row.note} [парная проводка quote: {quote_asset}]",
                    source=exchange,
                    external_id=f"{row.external_id}:quote",
                )
                saved += 1
        except CryptoError as e:
            if "дубл" in str(e).lower():
                dups += 1
            else:
                logger.info(f"cex persist skip: {e}")
                dups += 1
        except Exception as e:
            logger.error(f"cex persist fail: {e}", exc_info=True)
    return saved, dups