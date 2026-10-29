"""Синхронизация CEX (Bybit + OKX): единая функция для джобы и команд.

Раз в час фоновая задача тянет сделки, депозиты и выводы (включая
off-chain переводы) через read-only API и пишет их в БД с защитой от
дублей по external_id. Ошибки (429, ключ отозван) логируются, но не
блокируют бота — следующий прогон повторит попытку.

`sync_cex()` — единственный вход и для фоновой джобы, и для `/cex_sync`,
и для кнопки «Обновить всё». Возвращает отчёт, а не печатает в чат: так
его можно переиспользовать и тестировать без Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from telegram.ext import Application

from utils.cex_api import (
    TRADE_OVERLAP_MS, TRANSFER_OVERLAP_MS,
    get_cex_coverage, get_cex_transfer_watermark, get_cex_watermark,
    set_cex_coverage, set_cex_transfer_watermark, set_cex_watermark,
    sync_bybit_trades, sync_bybit_transfers,
    sync_okx_trades, sync_okx_transfers,
)
from utils.crypto_db import (
    add_transaction, find_duplicate, CryptoError, TxType,
)
from utils.price_oracle import get_asset_price_rub

logger = logging.getLogger(__name__)

# Сколько дней истории брать при первом синке. Дальше окно сужается
# водяным знаком. Bybit хранит сделки два года, OKX — только 90 дней.
# Двухлетнее окно покрывает и текущий, и прошлый налоговый год.
LOOKBACK_DAYS = {"bybit": 730, "okx": 90}


@dataclass
class ExchangeSync:
    """Итог по одной бирже."""

    name: str
    saved: int = 0
    duplicates: int = 0
    transfers_saved: int = 0
    transfers_duplicates: int = 0
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
    rematched: int = 0
    rematch_error: str = ""

    @property
    def saved(self) -> int:
        return sum(e.saved for e in self.exchanges)

    @property
    def duplicates(self) -> int:
        return sum(e.duplicates for e in self.exchanges)

    @property
    def transfers_saved(self) -> int:
        return sum(e.transfers_saved for e in self.exchanges)

    @property
    def errors(self) -> list[str]:
        errs = [err for e in self.exchanges for err in e.errors]
        if self.rematch_error:
            errs.append(self.rematch_error)
        return errs

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
            logger.info(f"cex_sync {ex.name}: +{ex.saved} записей")
        if ex.transfers_saved:
            logger.info(f"cex_sync {ex.name}: +{ex.transfers_saved} переводов")
        for err in ex.errors[:3]:
            logger.warning(f"cex_sync {ex.name}: {err}")
    if report.rematched:
        logger.info(f"cex_sync: сопоставлено переводов — {report.rematched}")


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

    # После депозитов/выводов обязательно сопоставляем их с ончейн-строками:
    # иначе вывод с биржи останется «доходом» до следующего /autoimport.
    # Это же покрывает ручной /cex_sync и часовую джобу.
    try:
        from utils.auto_import import rematch_transfers
        report.rematched = rematch_transfers(owner_id)
    except Exception as e:
        logger.error(f"sync_cex: сопоставление: {e}", exc_info=True)
        report.rematch_error = f"сопоставление: {e}"
    return report


async def _sync_exchange(owner_id: int, cfg, name: str) -> ExchangeSync:
    """Одна биржа: сделки + переводы → запись → сдвиг водяных знаков."""
    ex = ExchangeSync(name=name)
    try:
        watermark = get_cex_watermark(name)
        coverage = get_cex_coverage(name)
        if watermark and coverage:
            # Перекрытие ловит сделку, которая появилась в истории позже
            # фактического времени исполнения.
            since_ms = max(0, watermark - TRADE_OVERLAP_MS)
        else:
            # Нет отметки покрытия (первый синк или обновление со старой
            # версии) — читаем всё доступное окно заново. Дедупликация по
            # external_id сделает повтор безопасным, зато мы будем точно
            # знать, с какой даты история действительно загружена.
            since_ms = int(
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

        saved, dups = await persist_cex_rows(owner_id, result.rows, name)
        ex.saved += saved
        ex.duplicates += dups
        timestamps = [int(r.ts) for r in result.rows
                      if getattr(r, "ts", 0)]
        if not result.errors:
            # Полный проход без ошибок: следующий синк начнётся от конца
            # окна с суточным перекрытием, даже если сделок не было.
            set_cex_watermark(name, until_ms)
            # Запоминаем, с какой даты история реально прочитана. MIN
            # внутри set_cex_coverage не даст сдвинуть её вперёд.
            set_cex_coverage(name, since_ms)
        elif timestamps:
            # Часть окон уже прочитана. Ставим знак на последнюю
            # сохранённую сделку, чтобы не потерять более новые окна.
            set_cex_watermark(name, max(timestamps) * 1000)
        ex.errors.extend(result.errors)

        # Переводы качаем своим водяным знаком с перекрытием: подтверждение
        # вывода может появиться позже, чем закончится прошлое окно.
        transfer_since = get_cex_transfer_watermark(name)
        if transfer_since:
            transfer_since = max(0, transfer_since - TRANSFER_OVERLAP_MS)
        else:
            transfer_since = int(
                (time.time() - LOOKBACK_DAYS[name] * 86400) * 1000
            )

        if name == "bybit":
            transfers = await sync_bybit_transfers(
                cfg.bybit_api_key, cfg.bybit_api_secret,
                since_ts=transfer_since, until_ts=until_ms,
            )
        else:
            transfers = await sync_okx_transfers(
                cfg.okx_api_key, cfg.okx_api_secret, cfg.okx_passphrase,
                since_ts=transfer_since, until_ts=until_ms,
            )

        await _fill_transfer_in_cost(transfers.rows)
        saved, dups = await persist_cex_rows(owner_id, transfers.rows, name)
        ex.transfers_saved = saved
        ex.transfers_duplicates = dups
        ex.saved += saved
        ex.duplicates += dups
        ex.errors.extend(transfers.errors)
        if not transfers.errors:
            # Сдвигаем только после полного успеха: иначе непрочитанное
            # окно (429/сеть) выпадет из следующего прогона.
            set_cex_transfer_watermark(name, until_ms)
    except Exception as e:
        logger.error(f"cex_sync {name}: {e}", exc_info=True)
        ex.errors.append(str(e))
    return ex


async def _fill_transfer_in_cost(rows: list) -> None:
    """Оценить внешний ввод в рублях по рынку на дату зачисления.

    Свой перевод после rematch станет is_internal, и лот не откроется.
    Для ввода с карты или с неотслеживаемого кошелька это лучше нулевой
    себестоимости: иначе вся будущая прибыль попадёт в налоговую базу.
    """
    for row in rows:
        if row.tx_type != TxType.TRANSFER_IN:
            continue
        if row.cost_basis_rub or row.fiat_value_rub:
            continue
        try:
            price = await get_asset_price_rub(row.asset, row.ts)
        except Exception:
            price = None
        if price is None:
            continue
        try:
            rub = Decimal(str(row.amount)) * Decimal(str(price))
        except (InvalidOperation, ValueError):
            continue
        row.cost_basis_rub = f"{rub:.2f}"
        row.note += " (себестоимость — по рынку на дату ввода)"


async def _fee_rub(row, quote_price: float | None) -> tuple[Decimal, str]:
    """Перевести комиссию сделки в рубли и собрать примечание.

    Комиссия может быть в quote-валюте пары, в base-активе или в третьем
    активе (например, BNB). Для quote берём уже полученный курс, для
    остальных — цену на дату сделки. Если котировки нет, комиссия не
    выдумывается: в примечании остаётся её сырое значение.
    """
    fee_text = str(getattr(row, "fee", "") or "").strip()
    if not fee_text:
        return Decimal(0), ""
    try:
        fee = Decimal(fee_text)
    except (InvalidOperation, ValueError):
        return Decimal(0), ""
    if fee == 0:
        return Decimal(0), ""

    fee_asset = str(getattr(row, "fee_asset", "") or "").strip().upper()
    quote_asset = str(getattr(row, "quote_asset", "") or "").strip().upper()
    price = None
    if fee_asset and fee_asset == quote_asset and quote_price is not None:
        price = quote_price
    elif fee_asset:
        try:
            price = await get_asset_price_rub(fee_asset, row.ts)
        except Exception:
            price = None

    label = "комиссия" if fee > 0 else "ребейт"
    raw = f"{abs(fee)} {fee_asset or '?'}"
    if price is None:
        return Decimal(0), f", {label} {raw} (без котировки)"

    rub = (fee * Decimal(str(price))).quantize(Decimal("0.01"))
    return rub, f", {label} {raw}"


async def persist_cex_rows(owner_id: int, rows: list,
                           exchange: str) -> tuple[int, int]:
    """Записывает строки сделок и переводов.

    Возвращает (saved, duplicates). Каждая сделка — ДВЕ проводки:
      покупка  BTC/USDT → Дт BTC (BUY, себестоимость) / Кт USDT (TRADE_OUT)
      продажа  BTC/USDT → Дт USDT (TRADE_IN, выручка) / Кт BTC (SELL)

    Перевод (TRANSFER_IN/TRANSFER_OUT) пишется одной проводкой. После
    сопоставления он помечается is_internal и лоты не трогает; несопостав-
    ленный внешний ввод остаётся лотом с себестоимостью по рынку.

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
            quote_asset = getattr(row, "quote_asset", "")
            quote_amount = getattr(row, "quote_amount", "")
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
            q_price = None
            if quote_asset:
                try:
                    q_price = await get_asset_price_rub(quote_asset, row.ts)
                except Exception:
                    q_price = None
            if (not fiat and not cost and quote_asset and quote_amount
                    and q_price is not None):
                try:
                    rub_total = float(quote_amount) * q_price
                except (TypeError, ValueError):
                    rub_total = 0.0
                if rub_total:
                    if row.tx_type == TxType.BUY:
                        cost = f"{rub_total:.2f}"
                    else:
                        fiat = f"{rub_total:.2f}"

            # Комиссия уменьшает выручку продажи или увеличивает
            # себестоимость покупки. Для остальных типов (переводы,
            # доходы) она остаётся только в примечании.
            fee_rub, fee_note = await _fee_rub(row, q_price)
            if fee_note:
                row.note = f"{row.note}{fee_note}"
            if fee_rub:
                if row.tx_type == TxType.BUY and cost:
                    cost = f"{Decimal(str(cost)) + fee_rub:.2f}"
                elif row.tx_type == TxType.SELL and fiat:
                    fiat = f"{max(Decimal(0), Decimal(str(fiat)) - fee_rub):.2f}"

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
