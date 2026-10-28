"""Клиенты CEX (Bybit, OKX) для авто-импорта сделок и переводов.

Только read-only: ключи дают доступ к истории сделок, депозитов и выводов,
но не к выводу средств.
Перед созданием ключа в аккаунте биржи выбери:
  • Bybit: Account → API Management → permissions только Read.
  • OKX: Trade → API → permissions только Read.

Возвращают список ParsedRow, как и utils.csv_import — единый формат для
последующей записи в crypto_transactions через add_transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlencode

from utils.crypto_db import TxType
from utils.csv_import import ParsedRow
from utils.http_client import get_client

logger = logging.getLogger(__name__)

BYBIT_API = "https://api.bybit.com"
OKX_API = "https://www.okx.com"

# Bybit хранит историю сделок два года, OKX отдаёт fills-history только за
# три месяца. Для более старых периодов у OKX остаётся импорт выписки.
BYBIT_LOOKBACK_S = 730 * 86400
OKX_LOOKBACK_S = 90 * 86400

# Bybit execution/list не принимает диапазон шире 7 дней: если передать
# год одним запросом, API вернёт ошибку и история молча останется пустой.
BYBIT_TRADE_WINDOW_MS = 7 * 86400 * 1000

# Перекрытие инкрементального окна: сделка может появиться в истории с
# задержкой, поэтому начинаем чуть раньше водяного знака.
TRADE_OVERLAP_MS = 86400 * 1000

# Пауза между страницами сделок, чтобы не ловить 429.
TRADE_PAUSE_S = 0.4

# Bybit отдаёт историю депозитов и выводов окнами строго меньше 30 дней.
# Берём 29 суток с запасом на inclusive-границы.
BYBIT_TRANSFER_WINDOW_MS = 29 * 86400 * 1000

# Повторный скан переводов перекрывает прошлый водяной знак: подтверждение
# вывода может появиться через несколько дней после запроса.
TRANSFER_OVERLAP_MS = 7 * 86400 * 1000

# Пауза между страницами и окнами asset-эндпоинтов, чтобы не ловить 429.
TRANSFER_PAUSE_S = 0.5


@dataclass
class CexSyncResult:
    exchange: str
    saved: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    rows: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------- Bybit


async def _bybit_trade_pages(
    client, api_key: str, api_secret: str,
    since_ms: int, until_ms: int, result: CexSyncResult,
):
    """Страницы Bybit execution/list окнами не шире 7 дней.

    API не принимает startTime и endTime с разницей больше 7 суток.
    Внутри окна история листается cursor; окна идут от старых к новым,
    чтобы водяной знак не перескочил через непрочитанный участок.
    """
    start = int(since_ms)
    end_limit = int(until_ms)
    while start < end_limit:
        end = min(start + BYBIT_TRADE_WINDOW_MS, end_limit)
        cursor: str | None = None
        while True:
            params = {
                "category": "spot",
                "limit": "100",
                "startTime": str(start),
                "endTime": str(end),
            }
            if cursor:
                params["cursor"] = cursor

            headers = _bybit_sign(api_key, api_secret, params)
            try:
                resp = await client.get(
                    f"{BYBIT_API}/v5/execution/list",
                    params=params,
                    headers=headers,
                    timeout=20.0,
                )
            except Exception as e:
                result.errors.append(f"bybit сеть: {e}")
                return

            if resp.status_code == 429:
                await asyncio.sleep(5.0)
                result.errors.append("bybit 429 (лимит)")
                return
            if resp.status_code != 200:
                result.errors.append(
                    f"bybit {resp.status_code}: {resp.text[:120]}"
                )
                return

            try:
                payload = resp.json()
            except Exception:
                result.errors.append("bybit: невалидный JSON")
                return

            if payload.get("retCode") != 0:
                result.errors.append(
                    f"bybit retCode {payload.get('retCode')}: "
                    f"{payload.get('retMsg', '')[:120]}"
                )
                return

            data = payload.get("result") or {}
            for row in data.get("list") or []:
                yield row

            cursor = data.get("nextPageCursor")
            if not cursor:
                break
            await asyncio.sleep(TRADE_PAUSE_S)

        start = end + 1
        await asyncio.sleep(TRADE_PAUSE_S)


async def sync_bybit_trades(
    api_key: str, api_secret: str,
    since_ts: int | None = None, until_ts: int | None = None,
) -> CexSyncResult:
    """Подтянуть сделки Bybit (spot) и вернуть список ParsedRow.

    Параметры since/until_ts нужны для инкрементального скана:
    фоновая джоба хранит last_synced_ts в bot_stats.
    """
    result = CexSyncResult(exchange="bybit")
    if since_ts is None:
        since_ts = int((time.time() - BYBIT_LOOKBACK_S) * 1000)
    if until_ts is None:
        until_ts = int(time.time() * 1000)
    if since_ts >= until_ts:
        return result

    client = await get_client()
    async for raw in _bybit_trade_pages(
        client, api_key, api_secret, since_ts, until_ts, result,
    ):
        row = _bybit_row_to_parsed(raw)
        if row:
            result.saved += 1
            # Передаём наверх — caller сам запишет через add_transaction
            # с дедупликацией по external_id.
            setattr(row, "_exchange", "bybit")
            yield_row(row, result)

    return result


def _bybit_sign(api_key: str, api_secret: str,
                params: dict) -> dict[str, str]:
    """Bybit v5: HMAC-SHA256 от timestamp + api_key + recv_window + query_string."""
    ts = str(int(time.time() * 1000))
    recv = "5000"
    sorted_q = urlencode(sorted(params.items()))
    payload = f"{ts}{api_key}{recv}{sorted_q}"
    sig = hmac.new(api_secret.encode(), payload.encode(),
                   hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-SIGN": sig,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "Content-Type": "application/json",
    }


def _bybit_row_to_parsed(r: dict) -> ParsedRow | None:
    """Bybit exec: symbol=BTCUSDT, side=Buy/Sell, execQty, execPrice, execTime.

    Двойная запись: покупка BTC за USDT даёт две проводки —
      Дебет: BTC +qty (BUY, себестоимость = qty × price × курс quote→RUB)
      Кредит: USDT −qty×price (TRADE_OUT, конвертация).
    Продажа:
      Дебет: USDT +qty×price (TRADE_IN, выручка)
      Кредит: BTC −qty (SELL, выручка в RUB — через price_oracle).
    """
    try:
        ts_ms = int(r.get("execTime") or 0)
        if not ts_ms:
            return None
        ts = ts_ms // 1000
        symbol = (r.get("symbol") or "").strip().upper()
        if not symbol:
            return None
        side = (r.get("side") or "").strip().lower()
        qty = r.get("execQty") or "0"
        price = r.get("execPrice") or "0"
        if not qty or float(qty) == 0:
            return None

        base, quote = _split_symbol(symbol)
        if not base:
            return None
        quote_amount = _fmt_amount(float(qty) * float(price))
        exec_id = str(r.get("execId") or "").strip()
        fee = _positive_amount(r.get("execFee"))
        fee_asset = str(r.get("feeCurrency") or "").strip().upper()
        if fee and not fee_asset:
            # Bybit списывает комиссию в получаемом активе: при покупке
            # это base, при продаже — quote.
            fee_asset = base if side == "buy" else quote

        if side == "buy":
            return ParsedRow(
                ts=ts, asset=base, tx_type=TxType.BUY,
                amount=qty, fiat_value_rub="",
                cost_basis_rub="",  # заполнит cex_sync по курсу quote→RUB
                note=f"bybit buy {symbol}",
                external_id=f"bybit:{exec_id}",
                quote_asset=quote, quote_amount=quote_amount,
                fee=fee, fee_asset=fee_asset,
            )
        elif side == "sell":
            return ParsedRow(
                ts=ts, asset=base, tx_type=TxType.SELL,
                amount=qty, fiat_value_rub="",  # заполнит cex_sync
                cost_basis_rub="",
                note=f"bybit sell {symbol}",
                external_id=f"bybit:{exec_id}",
                quote_asset=quote, quote_amount=quote_amount,
                fee=fee, fee_asset=fee_asset,
            )
        return None
    except Exception as e:
        logger.info(f"bybit row skip: {e}")
        return None


def _fmt_amount(value: float) -> str:
    """Убрать хвостовые нули float-умножения: 1.2300000000000002 → '1.23'."""
    s = f"{value:.12f}".rstrip("0").rstrip(".")
    return s or "0"


def _split_symbol(symbol: str) -> tuple[str, str]:
    """BTCUSDT → (BTC, USDT); ETHUSDT → (ETH, USDT)."""
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH", "TON", "EUR"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[:-len(quote)], quote
    return "", ""


# ------------------------------------------------ Bybit: переводы


async def _bybit_transfer_pages(
    client, path: str, api_key: str, api_secret: str,
    since_ms: int, until_ms: int, result: CexSyncResult,
    extra: dict | None = None,
):
    """Страницы Bybit asset-API: окна < 30 дней и cursor внутри окна.

    Bybit не отдаёт депозиты/выводы одним запросом за год: окно запроса
    должно быть меньше 30 дней. Внутри окна история листается курсором.
    """
    start = int(since_ms)
    end_limit = int(until_ms)
    while start < end_limit:
        end = min(start + BYBIT_TRANSFER_WINDOW_MS, end_limit)
        cursor: str | None = None
        while True:
            params = {
                "startTime": str(start),
                "endTime": str(end),
                "limit": "50",
            }
            if extra:
                params.update(extra)
            if cursor:
                params["cursor"] = cursor

            headers = _bybit_sign(api_key, api_secret, params)
            try:
                resp = await client.get(
                    f"{BYBIT_API}{path}",
                    params=params,
                    headers=headers,
                    timeout=20.0,
                )
            except Exception as e:
                result.errors.append(f"bybit сеть: {e}")
                return

            if resp.status_code == 429:
                await asyncio.sleep(5.0)
                result.errors.append("bybit 429 (лимит)")
                return
            if resp.status_code != 200:
                result.errors.append(f"bybit {resp.status_code}: {resp.text[:120]}")
                return

            try:
                payload = resp.json()
            except Exception:
                result.errors.append("bybit: невалидный JSON")
                return

            if payload.get("retCode") != 0:
                result.errors.append(
                    f"bybit retCode {payload.get('retCode')}: "
                    f"{payload.get('retMsg', '')[:120]}"
                )
                return

            data = payload.get("result") or {}
            for row in data.get("rows") or []:
                yield row

            cursor = data.get("nextPageCursor")
            if not cursor:
                break
            await asyncio.sleep(TRANSFER_PAUSE_S)

        start = end + 1
        await asyncio.sleep(TRANSFER_PAUSE_S)


async def sync_bybit_transfers(
    api_key: str, api_secret: str,
    since_ts: int | None = None, until_ts: int | None = None,
) -> CexSyncResult:
    """Подтянуть ончейн-депозиты и выводы Bybit.

    Успешный депозит становится TRANSFER_IN, вывод — TRANSFER_OUT. Дальше
    rematch_transfers сопоставит их с ончейн-переводами кошельков: ввод на
    биржу ↔ исходящий перевод, вывод с биржи ↔ входящий перевод.
    """
    result = CexSyncResult(exchange="bybit")
    if since_ts is None:
        since_ts = int((time.time() - BYBIT_LOOKBACK_S) * 1000)
    if until_ts is None:
        until_ts = int(time.time() * 1000)
    if since_ts >= until_ts:
        return result

    client = await get_client()

    async for raw in _bybit_transfer_pages(
        client, "/v5/asset/deposit/query-record",
        api_key, api_secret, since_ts, until_ts, result,
    ):
        row = _bybit_deposit_to_parsed(raw)
        if row:
            result.saved += 1
            setattr(row, "_exchange", "bybit")
            yield_row(row, result)

    # withdrawType=0 — только ончейн-выводы. Внутренние (off-chain) переводы
    # между биржами не являются блокчейн-движением и здесь не нужны.
    async for raw in _bybit_transfer_pages(
        client, "/v5/asset/withdraw/query-record",
        api_key, api_secret, since_ts, until_ts, result,
        extra={"withdrawType": "0"},
    ):
        row = _bybit_withdraw_to_parsed(raw)
        if row:
            result.saved += 1
            setattr(row, "_exchange", "bybit")
            yield_row(row, result)

    return result


def _bybit_deposit_to_parsed(r: dict) -> ParsedRow | None:
    """Ончейн-депозит Bybit → TRANSFER_IN.

    Успех — status 3 (final success), 10012 (credited to funding pool) или
    70012 (депозит остался успешным после проверки rollback). Промежуточные
    и отменённые статусы пропускаем: иначе на балансе появится актив,
    который ещё не зачислен.
    """
    try:
        status = int(r.get("status") or -1)
    except (TypeError, ValueError):
        return None
    if status not in (3, 10012, 70012):
        return None

    coin = str(r.get("coin") or "").strip().upper()
    amount = _positive_amount(r.get("amount"))
    ts = _timestamp_s(r.get("successAt") or r.get("blockTime")
                      or r.get("txTime"))
    dep_id = str(r.get("id") or r.get("txID") or "").strip()
    if not coin or not amount or not ts or not dep_id:
        return None

    chain = str(r.get("chain") or "").strip()
    note = f"bybit deposit {coin}"
    if chain:
        note += f" ({chain})"
    return ParsedRow(
        ts=ts, asset=coin, tx_type=TxType.TRANSFER_IN,
        amount=amount, fiat_value_rub="", cost_basis_rub="",
        note=note, external_id=f"bybit:deposit:{dep_id}",
        source="bybit",
    )


def _bybit_withdraw_to_parsed(r: dict) -> ParsedRow | None:
    """Ончейн-вывод Bybit → TRANSFER_OUT.

    success и BlockchainConfirmed — финальные успешные статусы. Pending,
    SecurityCheck, Fail и отмены пропускаем. Комиссию не вычитаем из
    суммы: в учёте остаётся движение, которое биржа списала со счёта;
    допуск rematch_transfers (1%) покрывает разницу с зачислением.
    """
    status = str(r.get("status") or "").strip()
    if status not in ("success", "BlockchainConfirmed"):
        return None
    try:
        withdraw_type = int(r.get("withdrawType") or 0)
    except (TypeError, ValueError):
        withdraw_type = 0
    if withdraw_type != 0:
        return None

    coin = str(r.get("coin") or "").strip().upper()
    amount = _positive_amount(r.get("amount"))
    ts = _timestamp_s(r.get("createTime") or r.get("updateTime"))
    withdraw_id = str(r.get("withdrawId") or r.get("txID") or "").strip()
    if not coin or not amount or not ts or not withdraw_id:
        return None

    chain = str(r.get("chain") or "").strip()
    note = f"bybit withdraw {coin}"
    if chain:
        note += f" ({chain})"
    fee = _positive_amount(r.get("withdrawFee"))
    if fee:
        note += f", fee {fee} {coin}"
    return ParsedRow(
        ts=ts, asset=coin, tx_type=TxType.TRANSFER_OUT,
        amount=amount, fiat_value_rub="", cost_basis_rub="",
        note=note, external_id=f"bybit:withdraw:{withdraw_id}",
        source="bybit",
    )


def _positive_amount(value) -> str:
    """Вернуть сумму строкой, если она положительная, иначе пустую строку."""
    text = str(value or "").strip()
    try:
        if not text or float(text) <= 0:
            return ""
    except (TypeError, ValueError):
        return ""
    return text


def _signed_amount(value) -> str:
    """Вернуть ненулевую сумму со знаком (комиссия может быть ребейтом)."""
    text = str(value or "").strip()
    try:
        if not text or float(text) == 0:
            return ""
    except (TypeError, ValueError):
        return ""
    return text


def _timestamp_ms(value) -> int:
    """Timestamp в миллисекундах; секунды из старых полей тоже понимаем."""
    try:
        ts = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    if 0 < ts < 10_000_000_000:
        ts *= 1000
    return ts


def _timestamp_s(value) -> int:
    ts = _timestamp_ms(value)
    return ts // 1000 if ts else 0


# Хранилище «yielded» строк внутри CexSyncResult.saved (счётчик) и
# списке rows_external для последующей записи caller'ом.
def yield_row(row: ParsedRow, result: CexSyncResult) -> None:
    """Положить строку в result.rows для последующего извлечения."""
    if not hasattr(result, "rows"):
        result.rows = []  # type: ignore[attr-defined]
    result.rows.append(row)  # type: ignore[attr-defined]


# ------------------------------------------------------------------- OKX


async def _okx_trade_pages(
    client, api_key: str, api_secret: str, passphrase: str,
    since_ms: int, until_ms: int, result: CexSyncResult,
):
    """Страницы OKX fills-history через after/by time.

    fills-history отдаёт три месяца и сортирует записи по убыванию ts.
    Внутри страницы используем after=billId; если биржа не вернула billId,
    сдвигаем end на самую старую метку времени. Дедуп по tradeId защищает
    от повторов на стыке страниц.
    """
    seen: set[str] = set()
    end = int(until_ms)
    after = ""
    while int(since_ms) < end:
        params = {
            "instType": "SPOT",
            "limit": "100",
            "begin": str(int(since_ms)),
            "end": str(end),
        }
        if after:
            params["after"] = after
        headers = _okx_sign(api_key, api_secret, passphrase,
                            "GET", "/api/v5/trade/fills-history", params)
        try:
            resp = await client.get(
                f"{OKX_API}/api/v5/trade/fills-history",
                params=params,
                headers=headers,
                timeout=20.0,
            )
        except Exception as e:
            result.errors.append(f"okx сеть: {e}")
            return

        if resp.status_code == 429:
            await asyncio.sleep(5.0)
            result.errors.append("okx 429 (лимит)")
            return
        if resp.status_code != 200:
            result.errors.append(f"okx {resp.status_code}: {resp.text[:120]}")
            return

        try:
            payload = resp.json()
        except Exception:
            result.errors.append("okx: невалидный JSON")
            return

        if payload.get("code") != "0":
            result.errors.append(f"okx {payload.get('code')}: "
                                 f"{payload.get('msg', '')[:120]}")
            return

        data = payload.get("data") or []
        if not data:
            return

        for row in data:
            key = str(row.get("tradeId") or row.get("billId") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            yield row

        if len(data) < 100:
            return

        last = data[-1]
        next_after = str(last.get("billId") or "").strip()
        if next_after and next_after != after:
            after = next_after
            await asyncio.sleep(TRADE_PAUSE_S)
            continue

        # Нет billId или страница не сдвинулась — режем диапазон по времени.
        stamps = [int(r.get("ts") or 0) for r in data]
        oldest = min((ts for ts in stamps if ts), default=0)
        if not oldest:
            return
        next_end = oldest - 1
        if next_end >= end:
            return
        end = next_end
        after = ""
        await asyncio.sleep(TRADE_PAUSE_S)


async def sync_okx_trades(
    api_key: str, api_secret: str, passphrase: str,
    since_ts: int | None = None, until_ts: int | None = None,
) -> CexSyncResult:
    """Подтянуть сделки OKX (spot) — /api/v5/trade/fills-history."""
    result = CexSyncResult(exchange="okx")
    if since_ts is None:
        since_ts = int((time.time() - OKX_LOOKBACK_S) * 1000)
    if until_ts is None:
        until_ts = int(time.time() * 1000)
    if since_ts >= until_ts:
        return result

    client = await get_client()
    async for raw in _okx_trade_pages(
        client, api_key, api_secret, passphrase,
        since_ts, until_ts, result,
    ):
        row = _okx_row_to_parsed(raw)
        if row:
            result.saved += 1
            setattr(row, "_exchange", "okx")
            yield_row(row, result)

    return result


def _okx_sign(api_key: str, api_secret: str, passphrase: str,
              method: str, path: str, params: dict) -> dict[str, str]:
    """OKX: HMAC-SHA256 base64 от timestamp + method + path + body."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    sorted_q = urlencode(sorted(params.items()))
    prehash = f"{ts}{method.upper()}{path}?{sorted_q}"
    sig = hmac.new(api_secret.encode(), prehash.encode(),
                   hashlib.sha256).digest()
    import base64
    sig_b64 = base64.b64encode(sig).decode()
    return {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sig_b64,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }


def _okx_row_to_parsed(r: dict) -> ParsedRow | None:
    """OKX fill. Двойная запись — аналогично _bybit_row_to_parsed."""
    try:
        ts_ms = int(r.get("ts") or 0)
        if not ts_ms:
            return None
        ts = ts_ms // 1000
        inst_id = (r.get("instId") or "").strip().upper()
        if "-" not in inst_id:
            return None
        base, _, quote = inst_id.partition("-")
        side = (r.get("side") or "").strip().lower()
        qty = r.get("fillSz") or "0"
        price = r.get("fillPx") or "0"
        if not qty or float(qty) == 0:
            return None
        trade_id = str(r.get("tradeId") or r.get("billId") or "").strip()
        if not trade_id:
            trade_id = f"{ts_ms}:{inst_id}:{side}:{qty}:{price}"
        quote_amount = _fmt_amount(float(qty) * float(price))

        # OKX присылает fee отрицательным, когда комиссия списана, и
        # положительным, когда начислен ребейт. В ParsedRow знак обратный:
        # плюс — расход, минус — возврат.
        raw_fee = _signed_amount(r.get("fee"))
        fee = _fmt_amount(-float(raw_fee)) if raw_fee else ""
        fee_asset = str(r.get("feeCcy") or "").strip().upper()
        if fee and not fee_asset:
            fee_asset = base if side == "buy" else quote

        if side == "buy":
            return ParsedRow(
                ts=ts, asset=base, tx_type=TxType.BUY,
                amount=qty, fiat_value_rub="",
                cost_basis_rub="",
                note=f"okx buy {inst_id}",
                external_id=f"okx:{trade_id}",
                quote_asset=quote, quote_amount=quote_amount,
                fee=fee, fee_asset=fee_asset,
            )
        elif side == "sell":
            return ParsedRow(
                ts=ts, asset=base, tx_type=TxType.SELL,
                amount=qty, fiat_value_rub="",
                cost_basis_rub="",
                note=f"okx sell {inst_id}",
                external_id=f"okx:{trade_id}",
                quote_asset=quote, quote_amount=quote_amount,
                fee=fee, fee_asset=fee_asset,
            )
        return None
    except Exception as e:
        logger.info(f"okx row skip: {e}")
        return None


# ------------------------------------------------- OKX: переводы


async def _okx_transfer_pages(
    client, path: str, api_key: str, api_secret: str, passphrase: str,
    since_ms: int, until_ms: int, result: CexSyncResult, id_key: str,
):
    """Страницы OKX asset-API через after/before.

    OKX отдаёт историю в обратном хронологическом порядке. after — «раньше
    указанного ts», before — «позже указанного ts». Поэтому забираем
    диапазон [since, until], а затем сдвигаем after к последней строке.
    """
    seen: set[str] = set()
    # after/before эксклюзивны: +1/−1 включают граничные ts. Повторно
    # попавшие на стык страниц строки отсекаются по id.
    after = str(_timestamp_ms(until_ms) + 1)
    before = str(max(0, _timestamp_ms(since_ms) - 1))

    while True:
        params = {
            "limit": "100",
            "after": after,
            "before": before,
        }
        headers = _okx_sign(api_key, api_secret, passphrase,
                            "GET", path, params)
        try:
            resp = await client.get(
                f"{OKX_API}{path}",
                params=params,
                headers=headers,
                timeout=20.0,
            )
        except Exception as e:
            result.errors.append(f"okx сеть: {e}")
            return

        if resp.status_code == 429:
            await asyncio.sleep(5.0)
            result.errors.append("okx 429 (лимит)")
            return
        if resp.status_code != 200:
            result.errors.append(f"okx {resp.status_code}: {resp.text[:120]}")
            return

        try:
            payload = resp.json()
        except Exception:
            result.errors.append("okx: невалидный JSON")
            return

        if payload.get("code") != "0":
            result.errors.append(f"okx {payload.get('code')}: "
                                 f"{payload.get('msg', '')[:120]}")
            return

        data = payload.get("data") or []
        if not data:
            break

        for row in data:
            row_id = str(row.get(id_key) or "").strip()
            if row_id and row_id in seen:
                continue
            if row_id:
                seen.add(row_id)
            yield row

        # Неполная страница — история закончилась.
        if len(data) < 100:
            break
        last_ts = _timestamp_ms(data[-1].get("ts"))
        if not last_ts:
            break
        next_after = last_ts + 1
        if next_after >= int(after):
            # Строки с тем же ts не дают сдвинуться — дальше не уйдём.
            break
        after = str(next_after)
        await asyncio.sleep(TRANSFER_PAUSE_S)


async def sync_okx_transfers(
    api_key: str, api_secret: str, passphrase: str,
    since_ts: int | None = None, until_ts: int | None = None,
) -> CexSyncResult:
    """Подтянуть ончейн-депозиты и выводы OKX.

    type=4 — ончейн-операция; type=3 (внутренний перевод внутри OKX) не
    берём, чтобы не считать один и тот же перевод дважды. Депозит
    засчитывается со state 1 (credited) или 2 (success), вывод — только
    state 2 (success).
    """
    result = CexSyncResult(exchange="okx")
    if since_ts is None:
        since_ts = int((time.time() - OKX_LOOKBACK_S) * 1000)
    if until_ts is None:
        until_ts = int(time.time() * 1000)
    if since_ts >= until_ts:
        return result

    client = await get_client()

    async for raw in _okx_transfer_pages(
        client, "/api/v5/asset/deposit-history",
        api_key, api_secret, passphrase,
        since_ts, until_ts, result, id_key="depId",
    ):
        row = _okx_deposit_to_parsed(raw)
        if row:
            result.saved += 1
            setattr(row, "_exchange", "okx")
            yield_row(row, result)

    async for raw in _okx_transfer_pages(
        client, "/api/v5/asset/withdrawal-history",
        api_key, api_secret, passphrase,
        since_ts, until_ts, result, id_key="wdId",
    ):
        row = _okx_withdraw_to_parsed(raw)
        if row:
            result.saved += 1
            setattr(row, "_exchange", "okx")
            yield_row(row, result)

    return result


def _okx_deposit_to_parsed(r: dict) -> ParsedRow | None:
    """Ончейн-депозит OKX → TRANSFER_IN (state 1/2)."""
    if str(r.get("type") or "").strip() != "4":
        return None
    if str(r.get("state") or "").strip() not in ("1", "2"):
        return None

    coin = str(r.get("ccy") or "").strip().upper()
    amount = _positive_amount(r.get("amt"))
    ts = _timestamp_s(r.get("ts"))
    dep_id = str(r.get("depId") or r.get("txId") or "").strip()
    if not coin or not amount or not ts or not dep_id:
        return None

    chain = str(r.get("chain") or "").strip()
    note = f"okx deposit {coin}"
    if chain:
        note += f" ({chain})"
    return ParsedRow(
        ts=ts, asset=coin, tx_type=TxType.TRANSFER_IN,
        amount=amount, fiat_value_rub="", cost_basis_rub="",
        note=note, external_id=f"okx:deposit:{dep_id}",
        source="okx",
    )


def _okx_withdraw_to_parsed(r: dict) -> ParsedRow | None:
    """Ончейн-вывод OKX → TRANSFER_OUT (state 2)."""
    if str(r.get("type") or "").strip() != "4":
        return None
    if str(r.get("state") or "").strip() != "2":
        return None

    coin = str(r.get("ccy") or "").strip().upper()
    amount = _positive_amount(r.get("amt"))
    ts = _timestamp_s(r.get("ts"))
    withdraw_id = str(r.get("wdId") or r.get("txId") or "").strip()
    if not coin or not amount or not ts or not withdraw_id:
        return None

    chain = str(r.get("chain") or "").strip()
    note = f"okx withdraw {coin}"
    if chain:
        note += f" ({chain})"
    fee = _positive_amount(r.get("fee"))
    fee_ccy = str(r.get("feeCcy") or coin).strip().upper()
    if fee:
        note += f", fee {fee} {fee_ccy}"
    return ParsedRow(
        ts=ts, asset=coin, tx_type=TxType.TRANSFER_OUT,
        amount=amount, fiat_value_rub="", cost_basis_rub="",
        note=note, external_id=f"okx:withdraw:{withdraw_id}",
        source="okx",
    )


# ----------------------------------------------------------- watermark


def _get_stat(key: str) -> int:
    from utils.database import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM bot_stats WHERE key = ?", (key,),
        ).fetchone()
    return int(row["value"]) if row else 0


def _set_stat(key: str, ts_ms: int) -> None:
    from utils.database import get_db
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO bot_stats (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = MAX(CAST(value AS INTEGER), ?),
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, str(ts_ms), ts_ms),
        )


def get_cex_watermark(exchange: str) -> int:
    """Последний обработанный ts (миллисекунды) сделок — для инкремента."""
    return _get_stat(f"cex_sync_ts:{exchange}")


def set_cex_watermark(exchange: str, ts_ms: int) -> None:
    _set_stat(f"cex_sync_ts:{exchange}", ts_ms)


def get_cex_transfer_watermark(exchange: str) -> int:
    """Последний скан депозитов/выводов (ms).

    Отдельный ключ, чтобы не сдвигать инкремент сделок: у переводов своё
    перекрытие и свои статусы (позднее подтверждение).
    """
    return _get_stat(f"cex_transfer_ts:{exchange}")


def set_cex_transfer_watermark(exchange: str, ts_ms: int) -> None:
    _set_stat(f"cex_transfer_ts:{exchange}", ts_ms)
