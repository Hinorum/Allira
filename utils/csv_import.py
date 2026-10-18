"""Импорт выписок бирж в формате CSV.

Поддерживаемые источники:
  * Bybit (trades export) — колонки: utcTime, symbol, side, qty, price,
    fee, …
  * OKX (orders history) — cTime, instId, side, fillSz, fillPx, …
  * Универсальный «внутренний» формат — то, что сам бот может экспорти-
    ровать и перечитать. Колонки: ts (ISO), asset, type, amount,
    fiat_rub, cost_basis_rub, note.

Парсер берёт заголовок, опознаёт формат и нормализует в общий вид:
  (ts, asset_symbol, tx_type, amount, fiat_value_rub, cost_basis_rub, note)
Затем эти записи передаются в add_transaction() с source='bybit' / 'okx'
и external_id — защита от дублей уже встроена в crypto_db.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass
from typing import IO, Iterable

from utils.crypto_db import TxType
from utils.price_oracle import normalize_iso_date

logger = logging.getLogger(__name__)


@dataclass
class ParsedRow:
    ts: int
    asset: str
    tx_type: str
    amount: str
    fiat_value_rub: str
    cost_basis_rub: str
    note: str
    external_id: str
    # Валюта расчётов сделки (quote, обычно USDT) и сумма в этой валюте.
    # Нужны, чтобы в tasks/cex_sync перевести реальную цену сделки в RUB
    # по курсу ЦБ на дату — рыночная котировка для покупки лишь приближение.
    quote: str = ""
    quote_value: str = ""


# ----------------------------------------------------- распознавание


def detect_format(headers: list[str]) -> str:
    """По заголовкам CSV — определить источник."""
    h = {x.strip().lower(): i for i, x in enumerate(headers)}
    if "utctime" in h and "symbol" in h:
        return "bybit"
    if "ctime" in h and "instid" in h:
        return "okx"
    if "ts" in h and "asset" in h and "type" in h:
        return "internal"
    return "unknown"


# ----------------------------------------------------- парсеры


def parse_csv(content: bytes, source_hint: str = "") -> tuple[list[ParsedRow], list[str]]:
    """Распарсить CSV-файл в список ParsedRow.

    Возвращает (rows, errors). errors — список сообщений для пользователя
    (например, «строка 5: неизвестный символ»).
    """
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows: list[list[str]] = list(reader)
    if not rows:
        return [], ["пустой файл"]
    headers = rows[0]
    fmt = source_hint or detect_format(headers)
    if fmt == "bybit":
        return _parse_bybit(rows[1:])
    if fmt == "okx":
        return _parse_okx(rows[1:])
    if fmt == "internal":
        return _parse_internal(rows[1:])
    return [], [f"не удалось определить формат (заголовки: {headers!r})"]


def _parse_bybit(body: Iterable[list[str]]) -> tuple[list[ParsedRow], list[str]]:
    rows: list[ParsedRow] = []
    errors: list[str] = []
    for i, r in enumerate(body, start=2):
        if len(r) < 6:
            continue
        try:
            ts = _parse_bybit_ts(r[0])
        except Exception:
            errors.append(f"строка {i}: не парсится время {r[0]!r}")
            continue
        symbol = _symbol_from_bybit(r[1])
        if not symbol:
            errors.append(f"строка {i}: не парсится symbol {r[1]!r}")
            continue
        side = r[2].strip().lower()
        qty = _decimal_str(r[3])
        price = _decimal_str(r[4])
        if not qty or float(qty) == 0:
            continue
        quote_value = str(_safe_mul(qty, price))
        # Цену сделки кладём в quote/quote_value; RUB-конвертацию делает
        # utils.cex_api.enrich_rub_prices (курс ЦБ на дату) — здесь
        # cost/fiat остаются пустыми, чтобы USDT-цена не попала в RUB-поля.
        if side == "buy":
            # Покупка: крипта приходит, фиат уходит. Тип BUY открывает
            # FIFO-лёт с документированной себестоимостью и НЕ даёт дохода
            # для НДФЛ (в отличие от старого TRANSFER_IN, из-за которого
            # себестоимость терялась и при последующей продаже завышалась
            # прибыль).
            tx_type = TxType.BUY
        elif side == "sell":
            tx_type = TxType.SELL
        else:
            errors.append(f"строка {i}: неизвестный side {side!r}")
            continue
        rows.append(ParsedRow(
            ts=ts, asset=symbol, tx_type=tx_type,
            amount=qty, fiat_value_rub="",
            cost_basis_rub="",
            note=f"bybit {side} {r[1]}",
            external_id=f"bybit:{ts}:{r[1]}:{side}:{qty}",
            quote=_quote_from_bybit(r[1]),
            quote_value=quote_value,
        ))
    return rows, errors


def _parse_okx(body: Iterable[list[str]]) -> tuple[list[ParsedRow], list[str]]:
    rows: list[ParsedRow] = []
    errors: list[str] = []
    for i, r in enumerate(body, start=2):
        if len(r) < 7:
            continue
        try:
            ts = int(time.mktime(time.strptime(r[0].strip(),
                                                "%Y-%m-%d %H:%M:%S")))
        except Exception:
            try:
                ts = int(float(r[0]))
            except Exception:
                errors.append(f"строка {i}: не парсится время {r[0]!r}")
                continue
        symbol = _symbol_from_okx(r[1])
        if not symbol:
            errors.append(f"строка {i}: не парсится instId {r[1]!r}")
            continue
        side = r[2].strip().lower()
        qty = _decimal_str(r[3])
        price = _decimal_str(r[4])
        if not qty or float(qty) == 0:
            continue
        quote_value = str(_safe_mul(qty, price))
        # Цена сделки — в quote/quote_value; RUB делает enrich_rub_prices.
        if side == "buy":
            tx_type = TxType.BUY  # приобретение с документированной ценой
        elif side == "sell":
            tx_type = TxType.SELL
        else:
            errors.append(f"строка {i}: неизвестный side {side!r}")
            continue
        # BTC-USDT → quote = USDT (вторая часть instId через дефис).
        inst = r[1].strip().upper()
        quote = inst.partition("-")[2] if "-" in inst else ""
        rows.append(ParsedRow(
            ts=ts, asset=symbol, tx_type=tx_type,
            amount=qty, fiat_value_rub="",
            cost_basis_rub="",
            note=f"okx {side} {r[1]}",
            external_id=f"okx:{ts}:{r[1]}:{side}:{qty}",
            quote=quote,
            quote_value=quote_value,
        ))
    return rows, errors


def _parse_internal(body: Iterable[list[str]]) -> tuple[list[ParsedRow], list[str]]:
    rows: list[ParsedRow] = []
    errors: list[str] = []
    for i, r in enumerate(body, start=2):
        if len(r) < 4:
            continue
        ts_raw = r[0].strip()
        try:
            ts = int(float(ts_raw))
        except Exception:
            iso = normalize_iso_date(ts_raw)
            if iso:
                ts = int(time.mktime(time.strptime(iso, "%Y-%m-%d")))
            else:
                errors.append(f"строка {i}: не парсится ts {ts_raw!r}")
                continue
        asset = r[1].strip().upper()
        tx_type = r[2].strip()
        if tx_type not in TxType.all():
            errors.append(f"строка {i}: неизвестный type {tx_type!r}")
            continue
        amount = _decimal_str(r[3])
        if not amount:
            errors.append(f"строка {i}: пустое amount")
            continue
        fiat = _decimal_str(r[4]) if len(r) > 4 else ""
        cost = _decimal_str(r[5]) if len(r) > 5 else ""
        note = r[6].strip() if len(r) > 6 else ""
        external_id = f"csv:{ts}:{asset}:{tx_type}:{amount}"
        rows.append(ParsedRow(
            ts=ts, asset=asset, tx_type=tx_type,
            amount=amount, fiat_value_rub=fiat,
            cost_basis_rub=cost, note=note,
            external_id=external_id,
        ))
    return rows, errors


# ----------------------------------------------------- хелперы


def _symbol_from_bybit(symbol: str) -> str:
    """BTCUSDT → BTC, ETHUSDT → ETH, TONUSDT → TON."""
    s = symbol.strip().upper()
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH", "EUR", "TON"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[:-len(quote)]
    return ""


def _quote_from_bybit(symbol: str) -> str:
    """BTCUSDT → USDT (валюта расчётов сделки)."""
    s = symbol.strip().upper()
    base = _symbol_from_bybit(s)
    return s[len(base):] if base else ""


def _symbol_from_okx(inst_id: str) -> str:
    """BTC-USDT → BTC."""
    s = inst_id.strip().upper()
    if "-" in s:
        base, _, _quote = s.partition("-")
        return base
    return ""


def _decimal_str(value: str) -> str:
    """Безопасно: пустое/нечисловое → ''. Иначе строка в '0.123456'."""
    s = (value or "").strip().replace(",", ".")
    if not s:
        return ""
    try:
        float(s)
    except ValueError:
        return ""
    return s


def _safe_mul(a: str, b: str) -> float:
    try:
        return float(a) * float(b)
    except (TypeError, ValueError):
        return 0.0


def _parse_bybit_ts(value: str) -> int:
    """Bybit отдаёт '2024-08-15 12:34:56' (UTC) или миллисекунды."""
    s = value.strip()
    if s.isdigit():
        ts = int(s)
        if ts > 10_000_000_000:  # мс
            ts //= 1000
        return ts
    return int(time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S")))