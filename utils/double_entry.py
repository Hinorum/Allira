"""Журнал проводок (двойная запись: дебет ↔ кредит) по операциям с криптой.

Зачем: для сверки и налоговой отчётности каждая операция должна отражаться
сбалансированно — сумма дебета равна сумме кредита. Модель однострочной
записи (crypto_transactions) хранит одну строку на операцию; здесь из неё
строятся бухгалтерские проводки по адаптированному плану счетов.

План счетов (персональный учёт физлица по операциям с цифровой валютой):

  41.01.{АКТИВ} — Криптовалюта на балансе (аналитика по каждому активу)
  51             — Фиатные средства: руб.-эквивалент поступлений/расходов
                   по сделкам (выручка при продаже, расход при покупке)
  91.01          — Прочие доходы
  91.02          — Прочие расходы

Схемы проводок (валюта оценки — RUB, по курсу на дату операции):

  Доход (аирдроп, майнинг, стейкинг, награда…):
      Д 41.01.{АКТИВ}   К 91.01                     на рыночную стоимость

  Покупка (buy):
      Д 41.01.{АКТИВ}   К 51                        на документированную
                                                    себестоимость в RUB

  Реализация (продажа, обмен, оплата) — трёхсоставная:
      Д 51   К 41.01.{АКТИВ}   К 91.01              выручка = себестоимость
                                                    + прибыль
      при убытке вместо К 91.01 — Д 91.02.

  Внутреннее перемещение между своими кошельками (transfer_in / transfer_out):
      Д 41.01.{АКТИВ}   К 41.01.{АКТИВ}             сальдо не меняется

Себестоимость реализации и внутренних переводов списывается по FIFO — той же
хронологии, что в utils.tax_calc, но независимым проходом (журнал не должен
падать, если налоговый расчёт не добежал до конца).

Сверка: каждая проводка строится сбалансированной по построению; дополнительно
проверяется Σдебет == Σкредит по журналу целиком. Операции, для которых не
нашлось ни ручной цены, ни котировки, попадают в список unpriced и оцениваются
нулём — в /journal они видны как «без оценки», чтобы сверка не теряла их.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from utils.crypto_db import (
    Tx, TxType, _to_decimal, _tx_from_row, _year_end, _year_start,
)
from utils.database import get_db
from utils.price_oracle import get_asset_price_rub

logger = logging.getLogger(__name__)

ACCOUNT_SETTLEMENT = "51"
ACCOUNT_INCOME = "91.01"
ACCOUNT_EXPENSE = "91.02"

ACCOUNT_NAMES: dict[str, str] = {
    ACCOUNT_SETTLEMENT: "Фиатные средства (руб.-эквивалент сделок)",
    ACCOUNT_INCOME: "Прочие доходы",
    ACCOUNT_EXPENSE: "Прочие расходы",
}


def asset_account(symbol: str) -> str:
    """Счёт учёта актива: 41.01.BTC, 41.01.TON, …"""
    return f"41.01.{symbol}"


def account_name(account: str) -> str:
    if account.startswith("41.01."):
        return f"Криптовалюта {account[6:]}"
    return ACCOUNT_NAMES.get(account, account)


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------- дата-классы


@dataclass
class Posting:
    """Одна строка проводки: счёт и суммы дебета/кредита (ровно одна > 0)."""
    account: str
    debit: Decimal
    credit: Decimal


@dataclass
class JournalEntry:
    """Проводка одной операции (все её составные части)."""
    tx_id: int
    ts: int
    tx_type: str
    asset_symbol: str
    qty: Decimal
    note: str
    postings: list[Posting] = field(default_factory=list)
    unpriced: bool = False

    @property
    def total_debit(self) -> Decimal:
        return sum((p.debit for p in self.postings), Decimal(0))


@dataclass
class AccountTurnover:
    """Оборот счёта за период."""
    account: str
    debit: Decimal = Decimal(0)
    credit: Decimal = Decimal(0)

    @property
    def balance(self) -> Decimal:
        """Сальдо: >0 — дебетовое, <0 — кредитное."""
        return self.debit - self.credit


@dataclass
class Journal:
    """Построенный журнал: проводки за период + обороты по счетам."""
    year: Optional[int]
    entries: list[JournalEntry] = field(default_factory=list)
    turnovers: dict[str, AccountTurnover] = field(default_factory=dict)
    # tx_id операций, оценить которые не удалось (нет цены и котировки).
    unpriced: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_debit(self) -> Decimal:
        return sum((e.total_debit for e in self.entries), Decimal(0))

    @property
    def total_credit(self) -> Decimal:
        total = Decimal(0)
        for e in self.entries:
            total += sum((p.credit for p in e.postings), Decimal(0))
        return total

    @property
    def balanced(self) -> bool:
        return self.total_debit == self.total_credit


# --------------------------------------------------------- построение


async def build_journal(owner_id: int, year: Optional[int] = None) -> Journal:
    """Построить журнал проводок за год (или за всё время при year=None).

    Проходит по всем операциям хронологически, ведёт FIFO-лоты в памяти
    (те же правила, что в utils.tax_calc) и генерирует проводки.
    """
    journal = Journal(year=year)
    year_start = _year_start(year) if year else 0
    year_end = _year_end(year) if year else None

    with get_db() as conn:
        if year_end:
            rows = conn.execute(
                "SELECT * FROM crypto_transactions "
                "WHERE owner_id = ? AND ts < ? ORDER BY ts ASC, id ASC",
                (owner_id, year_end),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM crypto_transactions "
                "WHERE owner_id = ? ORDER BY ts ASC, id ASC",
                (owner_id,),
            ).fetchall()
    txs = [_tx_from_row(r) for r in rows]

    asset_cache: dict[int, tuple[str, int]] = {}

    def asset_meta(asset_id: int) -> tuple[str, int]:
        if asset_id in asset_cache:
            return asset_cache[asset_id]
        with get_db() as conn:
            row = conn.execute(
                "SELECT symbol, decimals FROM crypto_assets WHERE id = ?",
                (asset_id,),
            ).fetchone()
        meta = (row["symbol"] if row else "?", int(row["decimals"] or 9) if row else 9)
        asset_cache[asset_id] = meta
        return meta

    # FIFO-лоты (in-memory): те же правила, что в tax_calc.
    active: dict[int, list[Lot]] = {}
    next_lot_id = 0

    for tx in txs:
        symbol, decimals = asset_meta(tx.asset_id)
        scale = Decimal(10) ** decimals
        amount_raw = _to_decimal(tx.amount_raw)
        amount_human = amount_raw / scale
        in_year = tx.ts >= year_start
        t = tx.tx_type

        # ------------------------------------------------------------ приход
        if t in TxType.income_types():
            cost_total = (_to_decimal(tx.cost_basis_rub)
                          or _to_decimal(tx.fiat_value_rub))
            cost_per_unit = (cost_total / amount_human) if amount_human > 0 else Decimal(0)
            next_lot_id -= 1
            active.setdefault(tx.asset_id, []).append(_Lot(
                id=next_lot_id, asset_id=tx.asset_id, acquired_ts=tx.ts,
                acquired_tx_id=tx.id, qty_remaining=amount_raw,
                cost_per_unit_rub=cost_per_unit, source=tx.source or "manual",
            ))

            if in_year:
                value, unpriced = await _income_valuation(tx, symbol, amount_human)
                if unpriced:
                    journal.unpriced.append(tx.id)
                    journal.warnings.append(
                        f"tx #{tx.id} ({symbol}): нет оценки дохода "
                        "(ни цены, ни котировки) — проводка по нулевой сумме"
                    )
                entry = _entry(journal, tx, symbol, amount_human, unpriced)
                _add_posting(journal, entry, asset_account(symbol),
                             debit=value, credit=Decimal(0))
                _add_posting(journal, entry, ACCOUNT_INCOME,
                             debit=Decimal(0), credit=value)

        # ------------------------------------------------------- приобретение
        elif t in TxType.acquisition_types():
            cost = _acquisition_cost(tx)
            if cost is None:
                cost = await _market_value(symbol, amount_human, tx.ts)
                unpriced = cost is None
                if unpriced:
                    cost = Decimal(0)
                    journal.unpriced.append(tx.id)
                    journal.warnings.append(
                        f"tx #{tx.id} ({symbol}): покупка без себестоимости "
                        "и котировки — расход не отражён"
                    )
            else:
                unpriced = False
            cost = _round2(cost)
            cost_per_unit = (cost / amount_human) if amount_human > 0 else Decimal(0)
            next_lot_id -= 1
            active.setdefault(tx.asset_id, []).append(_Lot(
                id=next_lot_id, asset_id=tx.asset_id, acquired_ts=tx.ts,
                acquired_tx_id=tx.id, qty_remaining=amount_raw,
                cost_per_unit_rub=cost_per_unit, source=tx.source or "manual",
            ))

            if in_year:
                entry = _entry(journal, tx, symbol, amount_human, unpriced)
                _add_posting(journal, entry, asset_account(symbol),
                             debit=cost, credit=Decimal(0))
                _add_posting(journal, entry, ACCOUNT_SETTLEMENT,
                             debit=Decimal(0), credit=cost)

        # ------------------------------------------- перевод между своими (вх.)
        elif t == TxType.TRANSFER_IN:
            cost_total = (_to_decimal(tx.cost_basis_rub)
                          or _to_decimal(tx.fiat_value_rub))
            cost_per_unit = (cost_total / amount_human) if amount_human > 0 else Decimal(0)
            next_lot_id -= 1
            active.setdefault(tx.asset_id, []).append(_Lot(
                id=next_lot_id, asset_id=tx.asset_id, acquired_ts=tx.ts,
                acquired_tx_id=tx.id, qty_remaining=amount_raw,
                cost_per_unit_rub=cost_per_unit, source=tx.source or "manual",
            ))
            if in_year:
                # Внутреннее перемещение: обе стороны одного счёта, сальдо
                # не меняется — стоимость переносится по FIFO-себестоимости.
                cost = _round2((cost_per_unit * amount_human))
                entry = _entry(journal, tx, symbol, amount_human, False)
                _add_posting(journal, entry, asset_account(symbol),
                             debit=cost, credit=Decimal(0))
                _add_posting(journal, entry, asset_account(symbol),
                             debit=Decimal(0), credit=cost)

        # --------------------------------------------------------- реализация
        elif t in TxType.disposal_types():
            cost, shortfall = _consume_fifo(
                active.get(tx.asset_id, []), amount_raw, scale
            )
            if shortfall > 0:
                journal.warnings.append(
                    f"tx #{tx.id} ({symbol}): не хватило лотов для списания — "
                    "приход, вероятно, не записан в бота"
                )
            proceeds, unpriced = await _disposal_proceeds(tx, symbol, amount_human)
            if unpriced:
                journal.unpriced.append(tx.id)
                journal.warnings.append(
                    f"tx #{tx.id} ({symbol}): нет оценки выручки "
                    "(ни цены, ни котировки) — проводка по нулевой выручке"
                )
            cost_r = _round2(cost)
            proceeds_r = _round2(proceeds)
            gain = proceeds_r - cost_r
            if in_year:
                entry = _entry(journal, tx, symbol, amount_human, unpriced)
                _add_posting(journal, entry, ACCOUNT_SETTLEMENT,
                             debit=proceeds_r, credit=Decimal(0))
                _add_posting(journal, entry, asset_account(symbol),
                             debit=Decimal(0), credit=cost_r)
                if gain > 0:
                    _add_posting(journal, entry, ACCOUNT_INCOME,
                                 debit=Decimal(0), credit=gain)
                elif gain < 0:
                    _add_posting(journal, entry, ACCOUNT_EXPENSE,
                                 debit=-gain, credit=Decimal(0))

        # ------------------------------------------- перевод между своими (исх.)
        elif t == TxType.TRANSFER_OUT:
            cost, shortfall = _consume_fifo(
                active.get(tx.asset_id, []), amount_raw, scale
            )
            if shortfall > 0:
                journal.warnings.append(
                    f"tx #{tx.id} ({symbol}): перевод наружу без хвоста лотов — "
                    "остаток уменьшен для баланса"
                )
            if in_year:
                cost_r = _round2(cost)
                entry = _entry(journal, tx, symbol, amount_human, False)
                _add_posting(journal, entry, asset_account(symbol),
                             debit=cost_r, credit=Decimal(0))
                _add_posting(journal, entry, asset_account(symbol),
                             debit=Decimal(0), credit=cost_r)

    return journal


# --------------------------------------------------------- вспомогательное


class _Lot:
    """Лот FIFO — копия utils.tax_calc.Lot (изолирована, чтобы не тянуть
    внутренности налогового расчёта в бухгалтерский журнал)."""

    def __init__(self, *, id: int, asset_id: int, acquired_ts: int,
                 acquired_tx_id: int, qty_remaining: Decimal,
                 cost_per_unit_rub: Decimal, source: str):
        self.id = id
        self.asset_id = asset_id
        self.acquired_ts = acquired_ts
        self.acquired_tx_id = acquired_tx_id
        self.qty_remaining = qty_remaining
        self.cost_per_unit_rub = cost_per_unit_rub
        self.source = source


def _consume_fifo(lots: list[_Lot], to_consume: Decimal,
                  scale: Decimal) -> tuple[Decimal, Decimal]:
    """Списать to_consume (в мин. единицах) с лотов по FIFO.

    Возвращает (себестоимость_rub, недостача). Недостача > 0 — лотов не
    хватило; недосписанная часть считается с нулевой себестоимостью.
    """
    total_cost = Decimal(0)
    i = 0
    remaining = to_consume
    while i < len(lots) and remaining > 0:
        head = lots[i]
        take = min(head.qty_remaining, remaining)
        total_cost += (take / scale) * head.cost_per_unit_rub
        head.qty_remaining -= take
        remaining -= take
        if head.qty_remaining <= 0:
            lots.pop(i)
        else:
            i += 1
    return total_cost, remaining


def _entry(journal: Journal, tx: Tx, symbol: str, qty: Decimal,
           unpriced: bool) -> JournalEntry:
    entry = JournalEntry(
        tx_id=tx.id, ts=tx.ts, tx_type=tx.tx_type, asset_symbol=symbol,
        qty=qty, note=tx.note, unpriced=unpriced,
    )
    journal.entries.append(entry)
    return entry


def _add_posting(journal: Journal, entry: JournalEntry, account: str,
                 *, debit: Decimal, credit: Decimal) -> None:
    if debit == 0 and credit == 0:
        return  # нулевая проводка (например, внутреннее перемещение без цены)
    entry.postings.append(Posting(account=account, debit=debit, credit=credit))
    tr = journal.turnovers.setdefault(account, AccountTurnover(account))
    tr.debit += debit
    tr.credit += credit


def _acquisition_cost(tx: Tx) -> Optional[Decimal]:
    """Документированная себестоимость покупки: cost_basis, иначе fiat."""
    for raw in (tx.cost_basis_rub, tx.fiat_value_rub):
        if raw:
            v = _to_decimal(raw)
            if v > 0:
                return v
    return None


async def _market_value(symbol: str, qty: Decimal, ts: int) -> Optional[Decimal]:
    try:
        price = await get_asset_price_rub(symbol, ts)
    except Exception as e:
        logger.warning(f"double_entry: котировка {symbol} не получена: {e}")
        return None
    if price is None:
        return None
    return _round2(Decimal(str(price)) * qty)


async def _income_valuation(tx: Tx, symbol: str,
                            qty: Decimal) -> tuple[Decimal, bool]:
    """Оценка дохода: ручная цена, иначе котировка на дату.

    Возвращает (сумма_rub, без_оценки).
    """
    if tx.fiat_value_rub:
        v = _to_decimal(tx.fiat_value_rub)
        if v > 0:
            return _round2(v), False
    if tx.cost_basis_rub:
        v = _to_decimal(tx.cost_basis_rub)
        if v > 0:
            return _round2(v), False
    market = await _market_value(symbol, qty, tx.ts)
    if market is None:
        return Decimal(0), True
    return market, False


async def _disposal_proceeds(tx: Tx, symbol: str,
                             qty: Decimal) -> tuple[Decimal, bool]:
    """Выручка реализации: ручная цена, иначе котировка на дату."""
    if tx.fiat_value_rub:
        v = _to_decimal(tx.fiat_value_rub)
        if v > 0:
            return v, False
    market = await _market_value(symbol, qty, tx.ts)
    if market is None:
        return Decimal(0), True
    return market, False


# ------------------------------------------------------------- экспорт


def render_journal_csv(journal: Journal) -> bytes:
    """CSV журнала проводок: одна строка на одну проводку (счёт).

    Формат — «бухгалтерская книга»: дата, операция, счёт, сумма дебета,
    сумма кредита. Итоговая строка закрывает журнал суммами и признаком
    сверки. Кодировка utf-8-sig (BOM) — чтобы Excel в РФ открывал кириллицу.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "Дата", "Tx ID", "Операция", "Актив", "Кол-во",
        "Счёт", "Наименование счёта", "Дебет (RUB)", "Кредит (RUB)",
        "Примечание",
    ])
    for e in journal.entries:
        date = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.ts))
        for p in e.postings:
            writer.writerow([
                date, e.tx_id, TxType.label(e.tx_type), e.asset_symbol,
                f"{e.qty:.8f}".rstrip("0").rstrip("."),
                p.account, account_name(p.account),
                f"{p.debit:.2f}" if p.debit else "",
                f"{p.credit:.2f}" if p.credit else "",
                e.note,
            ])
    writer.writerow([
        "", "", "", "", "", "ИТОГО", "",
        f"{journal.total_debit:.2f}", f"{journal.total_credit:.2f}",
        "дебет=кредит ✓" if journal.balanced else "НЕСВЕДЕНО ✗",
    ])
    return buf.getvalue().encode("utf-8-sig")
