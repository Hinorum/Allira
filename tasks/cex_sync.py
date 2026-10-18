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
from utils.crypto_db import add_transaction, CryptoError

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
    saved = await _persist(result.rows, owner_id)
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
    saved = await _persist(result.rows, owner_id)
    if result.rows:
        max_ts = max(r.ts for r in result.rows)
        set_cex_watermark("okx", max_ts * 1000)
    if saved:
        logger.info(f"cex_sync okx: +{saved} сделок")
    if result.errors:
        logger.warning(f"cex_sync okx: {result.errors[:3]}")


async def _persist(rows: list, owner_id: int) -> int:
    saved = 0
    for row in rows:
        try:
            add_transaction(
                owner_id=owner_id,
                ts=row.ts,
                asset_symbol=row.asset,
                tx_type=row.tx_type,
                amount=row.amount,
                note=row.note,
                source=getattr(row, "_exchange", "cex"),
                external_id=row.external_id,
            )
            saved += 1
        except CryptoError:
            pass  # дубль — нормальное состояние
        except Exception as e:
            logger.error(f"cex persist fail: {e}", exc_info=True)
    return saved