"""Синхронизация CEX (Bybit + OKX): единая функция для джобы и команд.

Раз в час фоновая задача тянет сделки через read-only API и пишет их в БД
с защитой от дублей по external_id. Ошибки (429, ключ отозван) логируются,
но не блокируют бота — следующий прогон повторит попытку.

`sync_cex()` — единственный вход и для фоновой джобы, и для `/cex_sync`,
и для кнопки «Обновить всё». Возвращает отчёт, а не печатает в чат: так
его можно переиспользовать и тестировать без Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from telegram.ext import Application

from utils.cex_api import (get_cex_watermark, set_cex_watermark,
                           sync_bybit_trades, sync_okx_trades)
from utils.crypto_db import (
    add_transaction, find_duplicate, CryptoError, TxType,
)
from utils.price_oracle import get_asset_price_rub

logger = logging.getLogger(__name__)

# Сколько дней истории брать при первом синке. Дальше окно сужается
# водяным знаком. OKX отдаёт спот-сделки только за 90 дней.
LOOKBACK_DAYS = {"bybit": 365, "okx": 90}


@dataclass
class ExchangeSync:
    """Итог по одной бирже."""

    name: str
    saved: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return "Bybit" if self.name == "bybit" else "OKX"

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class CexSyncReport:
    """Итог синка по всем настроенным биржам."""

    exchanges: list[ExchangeSync] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return sum(e.saved for e in self.exchanges)

    @property
    def duplicates(self) -> int:
        return sum(e.duplicates for e in self.exchanges)

    @property
    def errors(self) -> list[str]:
        return [err for e in self.exchanges for err in e.errors]

    @property
    def configured(self) -> bool:
        return bool(self.exchanges)


def _bybit_configured(cfg) -> bool:
    return bool(getattr(cfg, "bybit_api_key", "")
                and getattr(cfg, "bybit_api_secret", ""))


def _okx_configured(cfg) -> bool:
    return bool(getattr(cfg, "okx_api_key", "")
                and getattr(cfg, "okx_api_secret", "")
                and getattr(cfg, "okx_passphrase", ""))


def setup_cex_sync_jobs(application: Application) -> None:
    """Подключает фоновый CEX-синк, если есть ключи хотя бы одной биржи."""
    cfg = application.bot_data.get("BOT_CONFIG")
    if not cfg:
        return
    owner_id = int(getattr(cfg, "crypto_owner_id", 0) or 0)
    bybit = _bybit_configured(cfg)
    okx = _okx_configured(cfg)
    if not owner_id or (not bybit and not okx):
        logger.info("setup_cex_sync_jobs: ничего не настроено — пропуск")
        return

    application.job_queue.run_repeating(
        _sync_job,
        interval=3600,
        name="cex_sync_hourly",
        data={"owner_id": owner_id, "cfg": cfg},
    )
    logger.info(f"CEX-скан: bybit={bybit} okx={okx}, owner={owner_id}, каждый час")


async def _sync_job(context) -> None:
    data = context.job.data or {}
    owner_id = int(data.get("owner_id") or 0)
    cfg = data.get("cfg") or context.bot_data.get("BOT_CONFIG")
    if not owner_id or not cfg:
        return

    report = await sync_cex(owner_id, cfg)
    for ex in report.exchanges:
        if ex.saved:
            logger.info(f"cex_sync {ex.name}: +{ex.saved} сделок")
        for err in ex.errors[:3]:
            logger.warning(f"cex_sync {ex.name}: {err}")


async def sync_cex(owner_id: int, cfg) -> CexSyncReport:
    """Синкает все настроенные биржи. Исключения не выходят наружу."""
    report = CexSyncReport()

    exchanges = [
        ("bybit", _bybit_configured(cfg)),
        ("okx", _okx_configured(cfg)),
    ]
    first = True
    for name, configured in exchanges:
        if not configured:
            continue
        # Пауза между биржами: два запроса подряд чаще ловят 429.
        if not first:
            await asyncio.sleep(2.0)
        first = False
        report.exchanges.append(await _sync_exchange(owner_id, cfg, name))

    if not report.exchanges:
        logger.info("sync_cex: ни одна биржа не настроена — пропуск")
    return report


async def _sync_exchange(owner_id: int, cfg, name: str) -> ExchangeSync:
    """Одна биржа: запрос → запись → сдвиг водяного знака."""
    ex = ExchangeSync(name=name)
    try:
        since_ms = get_cex_watermark(name) or int(
            (time.time() - LOOKBACK_DAYS[name] * 86400) * 1000
        )
        until_ms = int(time.time() * 1000)

        if name == "bybit":
            result = await sync_bybit_trades(
                cfg.bybit_api_key, cfg.bybit_api_secret,
                since_ts=since_ms, until_ts=until_ms,
            )
        else:
            result = await sync_okx_trades(
                cfg.okx_api_key, cfg.okx_api_secret, cfg.okx_passphrase,
                since_ts=since_ms, until_ts=until_ms,
            )

        ex.saved, ex.duplicates = await persist_cex_rows(
            owner_id, result.rows, name
        )
        timestamps = [int(r.ts) for r in result.rows
                      if getattr(r, "ts", 0)]
        if timestamps:
            set_cex_watermark(name, max(timestamps) * 1000)
        ex.errors.extend(result.errors)
    except Exception as e:
        logger.error(f"cex_sync {name}: {e}", exc_info=True)
        ex.errors.append(str(e))
    return ex


async def persist_cex_rows(owner_id: int, rows: list,
                           exchange: str) -> tuple[int, int]:
    """Записывает сделки с двойной записью «дебет-кредит».

    Возвращает (saved, duplicates). Каждая сделка — ДВЕ проводки:
      покупка  BTC/USDT → Дт BTC (BUY, себестоимость) / Кт USDT (TRADE_OUT)
      продажа  BTC/USDT → Дт USDT (TRADE_IN, выручка) / Кт BTC (SELL)

    RUB-стоимость quote-ветки берётся из price_oracle (для стейблкоинов —
    курс ЦБ на дату сделки). Если котировки нет, обе ноги пишутся без
    RUB — расчёт зафиксирует это как «нет котировки» в отчёте /tax.
    """
    saved = 0
    dups = 0
    for row in rows:
        try:
            fiat = row.fiat_value_rub or ""
            cost = row.cost_basis_rub or ""
            # Источник строки: CSV-импорт несёт свой (bybit/okx/csv), API —
            # общий на биржу. Без этого импорт CSV помечался бы как
            # источник биржи и дедупликация склеивала бы разные данные.
            source = getattr(row, "source", "") or exchange

            # Дубль проверяем ДО вставки: add_transaction на дубле
            # возвращает существующую запись, не бросая исключения, и
            # «сохранено» росло на каждый повторно импортированный ряд.
            if find_duplicate(owner_id, source, row.external_id) is not None:
                dups += 1
                continue

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
                source=source,
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
                    source=source,
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
