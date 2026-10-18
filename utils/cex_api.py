"""Клиенты CEX (Bybit, OKX) для авто-импорта сделок.

Только read-only: ключи дают доступ к истории сделок, но не к выводу.
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

# Безконечный лимит «торгов» Bybit'а — за последние 2 года. OKX — 3 месяца
# для спота через trade/fills (для расширения нужен /api/v5/trade/fills-history
# через OKX-плагин). На текущий момент ограничиваемся этими окнами.
BYBIT_LOOKBACK_S = 365 * 86400
OKX_LOOKBACK_S = 90 * 86400


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

    client = await get_client()
    cursor: str | None = None
    while True:
        params = {
            "category": "spot",
            "limit": "100",
            "startTime": str(since_ts),
            "endTime": str(until_ts),
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
            break

        if resp.status_code == 429:
            await asyncio.sleep(5.0)
            result.errors.append("bybit 429 (лимит)")
            break
        if resp.status_code != 200:
            result.errors.append(f"bybit {resp.status_code}: {resp.text[:120]}")
            break

        try:
            payload = resp.json()
        except Exception:
            result.errors.append("bybit: невалидный JSON")
            break

        if payload.get("retCode") != 0:
            result.errors.append(f"bybit retCode {payload.get('retCode')}: "
                                 f"{payload.get('retMsg', '')[:120]}")
            break

        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            break

        for r in rows:
            row = _bybit_row_to_parsed(r)
            if row:
                result.saved += 1
                # Передаём наверх — caller сам запишет через add_transaction
                # с дедупликацией по external_id.
                setattr(row, "_exchange", "bybit")
                yield_row(row, result)

        cursor = (payload.get("result") or {}).get("nextPageCursor")
        if not cursor:
            break
        await asyncio.sleep(0.4)

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
        exec_id = r.get("execId", "")

        if side == "buy":
            return ParsedRow(
                ts=ts, asset=base, tx_type=TxType.BUY,
                amount=qty, fiat_value_rub="",
                cost_basis_rub="",  # заполнит cex_sync по курсу quote→RUB
                note=f"bybit buy {symbol}",
                external_id=f"bybit:{exec_id}",
                quote_asset=quote, quote_amount=quote_amount,
            )
        elif side == "sell":
            return ParsedRow(
                ts=ts, asset=base, tx_type=TxType.SELL,
                amount=qty, fiat_value_rub="",  # заполнит cex_sync
                cost_basis_rub="",
                note=f"bybit sell {symbol}",
                external_id=f"bybit:{exec_id}",
                quote_asset=quote, quote_amount=quote_amount,
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


# Хранилище «yielded» строк внутри CexSyncResult.saved (счётчик) и
# списке rows_external для последующей записи caller'ом.
def yield_row(row: ParsedRow, result: CexSyncResult) -> None:
    """Положить строку в result.rows для последующего извлечения."""
    if not hasattr(result, "rows"):
        result.rows = []  # type: ignore[attr-defined]
    result.rows.append(row)  # type: ignore[attr-defined]


# ------------------------------------------------------------------- OKX


async def sync_okx_trades(
    api_key: str, api_secret: str, passphrase: str,
    since_ts: int | None = None, until_ts: int | None = None,
) -> CexSyncResult:
    """Подтянуть сделки OKX (spot) — /api/v5/trade/fills."""
    result = CexSyncResult(exchange="okx")
    if since_ts is None:
        since_ts = int((time.time() - OKX_LOOKBACK_S) * 1000)
    if until_ts is None:
        until_ts = int(time.time() * 1000)

    client = await get_client()
    while True:
        params = {
            "instType": "SPOT",
            "limit": "100",
            "begin": str(since_ts),
            "end": str(until_ts),
        }
        headers = _okx_sign(api_key, api_secret, passphrase,
                            "GET", "/api/v5/trade/fills", params)
        try:
            resp = await client.get(
                f"{OKX_API}/api/v5/trade/fills",
                params=params,
                headers=headers,
                timeout=20.0,
            )
        except Exception as e:
            result.errors.append(f"okx сеть: {e}")
            break

        if resp.status_code == 429:
            await asyncio.sleep(5.0)
            result.errors.append("okx 429 (лимит)")
            break
        if resp.status_code != 200:
            result.errors.append(f"okx {resp.status_code}: {resp.text[:120]}")
            break

        try:
            payload = resp.json()
        except Exception:
            result.errors.append("okx: невалидный JSON")
            break

        if payload.get("code") != "0":
            result.errors.append(f"okx {payload.get('code')}: "
                                 f"{payload.get('msg', '')[:120]}")
            break

        rows = payload.get("data") or []
        if not rows:
            break

        for r in rows:
            row = _okx_row_to_parsed(r)
            if row:
                result.saved += 1
                setattr(row, "_exchange", "okx")
                yield_row(row, result)

        # OKX пагинирует через after/before; упрощённо — break.
        break

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
        trade_id = r.get("tradeId", "")
        quote_amount = _fmt_amount(float(qty) * float(price))

        if side == "buy":
            return ParsedRow(
                ts=ts, asset=base, tx_type=TxType.BUY,
                amount=qty, fiat_value_rub="",
                cost_basis_rub="",
                note=f"okx buy {inst_id}",
                external_id=f"okx:{trade_id}",
                quote_asset=quote, quote_amount=quote_amount,
            )
        elif side == "sell":
            return ParsedRow(
                ts=ts, asset=base, tx_type=TxType.SELL,
                amount=qty, fiat_value_rub="",
                cost_basis_rub="",
                note=f"okx sell {inst_id}",
                external_id=f"okx:{trade_id}",
                quote_asset=quote, quote_amount=quote_amount,
            )
        return None
    except Exception as e:
        logger.info(f"okx row skip: {e}")
        return None


# ----------------------------------------------------------- watermark


def get_cex_watermark(exchange: str) -> int:
    """Последний обработанный ts (миллисекунды) для биржи — для инкремента."""
    from utils.database import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM bot_stats WHERE key = ?",
            (f"cex_sync_ts:{exchange}",),
        ).fetchone()
    return int(row["value"]) if row else 0


def set_cex_watermark(exchange: str, ts_ms: int) -> None:
    from utils.database import get_db
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO bot_stats (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = MAX(CAST(value AS INTEGER), ?),
                updated_at = CURRENT_TIMESTAMP
            """,
            (f"cex_sync_ts:{exchange}", str(ts_ms), ts_ms),
        )