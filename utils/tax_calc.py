"""Расчёт налоговой базы по операциям с цифровой валютой.

Правовая рамка:
- НК РФ ред. 418-ФЗ от 29.11.2024 (действует с 01.01.2025).
- Ст. 38 НК РФ — цифровая валюта признаётся имуществом.
- Ст. 214 НК РФ — доход от продажи имущества облагается НДФЛ.
- Ст. 220 НК РФ — расходы на приобретение уменьшают базу (пп.2 п.2:
  имущественный вычет 250 000 ₽ к крипте НЕ применяется, письма Минфина
  № 03-04-05/9243 от 10.02.2022 и № 03-04-07/80764 от 08.11.2018).
- Ст. 224 НК РФ — прогрессивная шкала НДФЛ для резидентов.

Метод FIFO:
  Расходы на приобретение конкретной единицы = цена, по которой эта
  единица была получена первой из всех доступных лотов. Для учёта
  стоимости единицы актива пересчитываем в «цена за единицу в RUB» и
  при реализации снимаем сначала самый старый лот.

Прогрессивная шкала (резидент, ст. 224 НК РФ):
  до 2,4 млн      — 13%
  2,4 – 5 млн     — 15% с суммы превышения
  5 – 20 млн      — 18% с превышения
  20 – 50 млн     — 20% с превышения
  свыше 50 млн    — 22% с превышения
Нерезидент — 30% со всего дохода (п. 3 ст. 224 НК РФ).

Майнинг (ст. 217.1 НК РФ, доход в момент получения): доход по
рыночной котировке на дату получения, расходы — затраты на майнинг
(электричество, оборудование, амортизация) — в нашем MVP пока
принимаются нулевыми, реальные расходы пользователь добавит вручную
через /tx_add с типом mining + note.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict, field
from decimal import Decimal, ROUND_HALF_UP, getcontext
from typing import Optional

from utils.crypto_db import (
    Tx, TxType, _to_decimal, list_transactions,
)
from utils.database import get_db
from utils.price_oracle import get_asset_price_rub

# Decimal-контекст: 28 знаков хватает и для BTC в сатоши, и для RUB-копеек.
getcontext().prec = 28
logger = logging.getLogger(__name__)


# --------------------------------------------------------- шкалы НДФЛ


# Прогрессивная шкала резидента (ст. 224 НК РФ) — (порог_в_рублях, ставка).
# Последний элемент — потолок, выше которого ставка неизменна.
RESIDENT_BRACKETS: list[tuple[Decimal, Decimal]] = [
    (Decimal("2400000"), Decimal("0.13")),
    (Decimal("5000000"), Decimal("0.15")),
    (Decimal("20000000"), Decimal("0.18")),
    (Decimal("50000000"), Decimal("0.20")),
    (Decimal("9999999999999"), Decimal("0.22")),  # 22% с превышения 50 млн
]

NONRESIDENT_RATE = Decimal("0.30")


def compute_ndfl(taxable_income: Decimal, status: str) -> tuple[Decimal, list[dict]]:
    """Расчёт НДФЛ по прогрессивной шкале.

    Возвращает (итого_налог, разбивка по ступеням). taxable_income < 0 —
    возвращаем 0 и пустую разбивку (отрицательный убыток не уменьшает
    другие категории дохода — ст. 220 НК, п. 3).
    """
    if taxable_income <= 0:
        return Decimal(0), []

    if status == "nonresident":
        tax = taxable_income * NONRESIDENT_RATE
        return _round2(tax), [{
            "bracket": "нерезидент 30%",
            "base": str(taxable_income),
            "rate": "0.30",
            "tax": str(_round2(tax)),
        }]

    if status == "miner":
        # Майнеры облагаются по майнинговой шкале (пп.1.1 п.2 ст.224 НК):
        # 13/15/18/20/22 — но С ДОХОДА ОТ МАЙНИНГА, не суммарного.
        # Суммируем с обычной 13/15-шкалой для прочих доходов. Упрощение:
        # считаем всё по единой резидентской шкале, а разбивку маркируем
        # типом дохода. Для реальной практики этого хватает — основная
        # разница в учёте расходов на электричество.
        pass

    breakdown: list[dict] = []
    remaining = taxable_income
    prev_cap = Decimal(0)
    total_tax = Decimal(0)
    for cap, rate in RESIDENT_BRACKETS:
        if remaining <= 0:
            break
        # Сколько базы попадает в эту ступень.
        step_capacity = max(cap - prev_cap, Decimal(0))
        in_step = min(remaining, step_capacity)
        step_tax = in_step * rate
        total_tax += step_tax
        breakdown.append({
            "bracket": f"{_fmt_money(prev_cap)}–{_fmt_money(cap)}",
            "base": str(in_step),
            "rate": str(rate),
            "tax": str(_round2(step_tax)),
        })
        remaining -= in_step
        prev_cap = cap
        if rate == RESIDENT_BRACKETS[-1][1]:
            break

    return _round2(total_tax), breakdown


def _fmt_money(value: Decimal) -> str:
    return f"{value:,.0f}".replace(",", " ")


def _round2(value: Decimal) -> Decimal:
    """Округление до копеек по правилу HALF_UP (стандарт для налоговой)."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------- FIFO


@dataclass
class Lot:
    """Лот актива на балансе. FIFO: гасится строго по acquired_ts ASC."""
    id: int
    asset_id: int
    acquired_ts: int
    acquired_tx_id: int
    qty_remaining: Decimal
    cost_per_unit_rub: Decimal
    source: str


@dataclass
class DisposalEvent:
    """Результат реализации (продажи/списания) — для отчёта 3-НДФЛ."""
    tx_id: int
    ts: int
    asset_symbol: str
    qty: Decimal
    proceeds_rub: Decimal         # цена реализации × количество
    cost_rub: Decimal             # списанные расходы
    gain_rub: Decimal             # proceeds − cost
    note: str


@dataclass
class IncomeEvent:
    """Доход (аирдроп, майнинг, стейкинг)."""
    tx_id: int
    ts: int
    asset_symbol: str
    qty: Decimal
    income_rub: Decimal           # рыночная котировка × количество
    tx_type: str
    note: str


@dataclass
class YearSummary:
    year: int
    income_events: list[IncomeEvent] = field(default_factory=list)
    disposal_events: list[DisposalEvent] = field(default_factory=list)
    total_income_rub: Decimal = Decimal(0)
    total_cost_rub: Decimal = Decimal(0)
    realized_gain_rub: Decimal = Decimal(0)
    ndfl_total: Decimal = Decimal(0)
    ndfl_breakdown: list[dict] = field(default_factory=list)
    taxable_income_rub: Decimal = Decimal(0)
    warnings: list[str] = field(default_factory=list)
    # Найденные расходы пользователя (если он их ввёл вручную).
    manual_costs: Decimal = Decimal(0)
    # Ошибки в данных: пропущенные котировки, отрицательный итог.
    skipped: list[dict] = field(default_factory=list)


# --------------------------------------------------------- ядро


def rebuild_lots(owner_id: int, up_to_ts: Optional[int] = None) -> int:
    """Пересборка FIFO-лотов по всем транзакциям до up_to_ts.

    ВАЖНО: пишет ТЕКУЩИЙ остаток (что лежит на балансе) в tax_lots,
    но НЕ гасит их по ходу: это нужно делать in-memory во время
    расчёта отчёта, иначе более ранние списания съедят лоты раньше,
    чем их увидит расчёт более поздних списаний в том же году.

    Возвращает количество оставшихся (непогашенных) лотов.
    """
    active = _walk_fifo(owner_id, up_to_ts=up_to_ts, persist_balance=True)
    return sum(len(v) for v in active.values())


def _walk_fifo(
    owner_id: int,
    *,
    up_to_ts: Optional[int] = None,
    persist_balance: bool = False,
) -> dict[int, list[Lot]]:
    """Чистый проход по операциям в хронологическом порядке с возвратом
    активных лотов. Не трогает tax_lots на диске, если persist_balance=False.

    Внутренний контракт: cost_per_unit_rub вычисляется на момент открытия
    лота и далее не меняется. Если пользователь вручную ввёл fiat_value_rub
    в приходе — используется он. Если нет — cost_per_unit = 0 и лот
    существует, но при списании даёт нулевую себестоимость (что в случае
    аирдропа и соответствует НК: приход равен доходу, расходы нулевые).
    """
    with get_db() as conn:
        if up_to_ts is None:
            sql = ("SELECT * FROM crypto_transactions WHERE owner_id = ? "
                   "ORDER BY ts ASC, id ASC")
            args: tuple = (owner_id,)
        else:
            sql = ("SELECT * FROM crypto_transactions WHERE owner_id = ? "
                   "AND ts < ? ORDER BY ts ASC, id ASC")
            args = (owner_id, up_to_ts)
        rows = conn.execute(sql, args).fetchall()

    active: dict[int, list[Lot]] = {}
    next_lot_id = 0
    for row in rows:
        tx_id = row["id"]
        asset_id = row["asset_id"]
        tx_type = row["tx_type"]
        amount = _to_decimal(row["amount_raw"])
        decimals = _asset_decimals(asset_id)
        scale = Decimal(10) ** decimals
        cost_basis = _to_decimal(row["cost_basis_rub"]) if row["cost_basis_rub"] else Decimal(0)
        fiat_value = _to_decimal(row["fiat_value_rub"]) if row["fiat_value_rub"] else Decimal(0)

        if tx_type in TxType.lot_in_types():
            # Себестоимость прихода (за 1 единицу актива):
            cost_total = cost_basis if cost_basis > 0 else fiat_value
            qty_human = amount / scale
            cost_per_unit = cost_total / qty_human if qty_human > 0 else Decimal(0)
            next_lot_id -= 1  # in-memory id, чтобы не пересекаться с persisted
            active.setdefault(asset_id, []).append(Lot(
                id=next_lot_id,
                asset_id=asset_id,
                acquired_ts=int(row["ts"]),
                acquired_tx_id=tx_id,
                qty_remaining=amount,
                cost_per_unit_rub=cost_per_unit,
                source=row["source"] or "manual",
            ))

        elif tx_type in TxType.lot_out_types():
            lots = active.get(asset_id, [])
            to_consume = amount
            i = 0
            while i < len(lots) and to_consume > 0:
                head = lots[i]
                take = min(head.qty_remaining, to_consume)
                head.qty_remaining -= take
                to_consume -= take
                if head.qty_remaining <= 0:
                    lots.pop(i)
                else:
                    i += 1

        # transfer_out намеренно не трогает активы: в MVP считаем, что
        # обе записи (transfer_in на получателе и transfer_out у отправителя)
        # вводятся вручную и net-effect нулевой.

    if persist_balance:
        with get_db() as conn:
            conn.execute("DELETE FROM tax_lots WHERE owner_id = ?", (owner_id,))
            for asset_id, lots in active.items():
                for lot in lots:
                    if lot.qty_remaining <= 0:
                        continue
                    conn.execute(
                        """
                        INSERT INTO tax_lots
                            (owner_id, asset_id, acquired_ts, acquired_tx_id,
                             qty_remaining, cost_per_unit_rub, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (owner_id, asset_id, lot.acquired_ts, lot.acquired_tx_id,
                         str(lot.qty_remaining), str(lot.cost_per_unit_rub),
                         lot.source),
                    )
    return active


async def compute_year_report(owner_id: int, year: int) -> YearSummary:
    """Полный пересчёт и сборка событий за год.

    Шаги:
      1. Загрузить все транзакции до конца года включительно.
      2. Пройтись по операциям года в хронологическом порядке ОДИН РАЗ:
         на каждой операции классифицировать (доход/списание), посчитать
         доход/расход/прибыль, И ТУТ ЖЕ обновить in-memory FIFO-лоты.
         Это критично: если сначала «высушить» все лоты через _walk_fifo,
         а потом снова итерировать для расчёта — поздние приходы (аирдроп
         после продажи) окажутся в лотах и будут использованы для FIFO
         более ранней продажи. Перерасход → завышенный расход → заниженная
         прибыль.
      3. Подтянуть пропущенные котировки через price_oracle.
      4. Посчитать НДФЛ по прогрессивной шкале.
      5. Сохранить финальные лоты в tax_lots (persist).
    """
    summary = YearSummary(year=year)

    # Все операции ДО конца года — нужны для построения лотов, которые
    # действуют на момент операций года.
    year_end = _year_end(year)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM crypto_transactions "
            "WHERE owner_id = ? AND ts < ? "
            "ORDER BY ts ASC, id ASC",
            (owner_id, year_end),
        ).fetchall()
    txs = [
        Tx(
            id=r["id"], owner_id=r["owner_id"], ts=int(r["ts"]),
            wallet_id=r["wallet_id"], asset_id=r["asset_id"],
            tx_type=r["tx_type"], direction=r["direction"],
            amount_raw=r["amount_raw"],
            fiat_value_rub=r["fiat_value_rub"],
            cost_basis_rub=r["cost_basis_rub"],
            note=r["note"] or "", source=r["source"] or "manual",
            external_id=r["external_id"],
        ) for r in rows
    ]
    txs.sort(key=lambda t: (t.ts, t.id))

    asset_cache: dict[int, tuple[str, int]] = {}

    def get_asset_meta(asset_id: int):
        if asset_id in asset_cache:
            return asset_cache[asset_id]
        with get_db() as conn:
            row = conn.execute(
                "SELECT symbol, decimals FROM crypto_assets WHERE id = ?",
                (asset_id,),
            ).fetchone()
        meta = (row["symbol"] if row else "?", int(row["decimals"]) if row else 9)
        asset_cache[asset_id] = meta
        return meta

    # Один проход: обновляем лоты и события одновременно.
    active: dict[int, list[Lot]] = {}
    year_start_ts = _year_start(year)

    for tx in txs:
        symbol, decimals = get_asset_meta(tx.asset_id)
        scale = Decimal(10) ** decimals
        amount_raw = _to_decimal(tx.amount_raw)
        amount_human = amount_raw / scale
        tx_year = tx.ts >= year_start_ts

        if tx.tx_type in TxType.lot_in_types():
            # Лот открываем всегда — даже без котировки. Иначе FIFO
            # «потеряет» этот приход и при продаже даст нулевую себестоимость.
            cost_total = _to_decimal(tx.cost_basis_rub) if tx.cost_basis_rub else Decimal(0)
            if cost_total == 0 and tx.fiat_value_rub:
                cost_total = _to_decimal(tx.fiat_value_rub)
            qty_human = amount_human
            cost_per_unit = (cost_total / qty_human) if qty_human > 0 else Decimal(0)
            active.setdefault(tx.asset_id, []).append(Lot(
                id=-tx.id,  # отрицательный id — отличаем от persisted
                asset_id=tx.asset_id,
                acquired_ts=tx.ts,
                acquired_tx_id=tx.id,
                qty_remaining=amount_raw,
                cost_per_unit_rub=cost_per_unit,
                source=tx.source or "manual",
            ))

            if tx_year and tx.tx_type in TxType.taxable_income_types():
                # Доход для 3-НДФЛ. Внутренние переводы, покупки (BUY) и
                # конвертации (TRADE_IN) здесь НЕ считаются доходом: выручка
                # отражена на парной стороне сделки (SELL/SWAP_OUT).
                income = _resolve_fiat(tx)
                if income is None and cost_total > 0:
                    # На входе была задана стоимость — используем её.
                    income = cost_total
                if income is None:
                    market = await get_asset_price_rub(symbol, tx.ts)
                    if market is None:
                        summary.warnings.append(
                            f"нет котировки {symbol} на {_fmt_date(tx.ts)} (tx #{tx.id})"
                        )
                        summary.skipped.append({
                            "tx_id": tx.id, "reason": "no_quote", "symbol": symbol,
                        })
                        continue
                    income = amount_human * _price_to_decimal(market)
                summary.income_events.append(IncomeEvent(
                    tx_id=tx.id,
                    ts=tx.ts,
                    asset_symbol=symbol,
                    qty=amount_human,
                    income_rub=_round2(income),
                    tx_type=tx.tx_type,
                    note=tx.note,
                ))

        elif tx.tx_type in TxType.lot_out_types():
            # FIFO-себестоимость ДО открытия новых лотов (это текущая операция).
            cost, shortfall = _fifo_cost_inplace(
                active.get(tx.asset_id, []), amount_raw, scale
            )

            if tx_year and tx.tx_type in TxType.disposal_types():
                proceeds = _resolve_fiat(tx)
                if proceeds is None:
                    market = await get_asset_price_rub(symbol, tx.ts)
                    if market is None:
                        summary.warnings.append(
                            f"нет котировки {symbol} на {_fmt_date(tx.ts)} (tx #{tx.id})"
                        )
                        summary.skipped.append({
                            "tx_id": tx.id, "reason": "no_quote", "symbol": symbol,
                        })
                        continue
                    proceeds = amount_human * _price_to_decimal(market)

                gain = proceeds - cost
                if shortfall > 0:
                    summary.warnings.append(
                        f"tx #{tx.id} ({symbol}): не хватило лотов для "
                        f"списания ({shortfall} минимальных единиц) — "
                        "вероятно, приход не записан в бота"
                    )
                summary.disposal_events.append(DisposalEvent(
                    tx_id=tx.id,
                    ts=tx.ts,
                    asset_symbol=symbol,
                    qty=amount_human,
                    proceeds_rub=_round2(proceeds),
                    cost_rub=_round2(cost),
                    gain_rub=_round2(gain),
                    note=tx.note,
                ))
            # Если tx вне года — лот всё равно списываем, чтобы баланс
            # # на начало года был корректным.

        # transfer_out намеренно не трогает активы.

    # Сохраняем итоговое состояние лотов в tax_lots.
    _persist_lots(owner_id, active)

    summary.total_income_rub = _round2(
        sum((e.income_rub for e in summary.income_events), Decimal(0))
    )
    summary.total_cost_rub = _round2(
        sum((e.cost_rub for e in summary.disposal_events), Decimal(0))
    )
    summary.realized_gain_rub = _round2(
        sum((e.gain_rub for e in summary.disposal_events), Decimal(0))
    )

    taxable = summary.realized_gain_rub + summary.total_income_rub - summary.manual_costs
    if taxable < 0:
        summary.warnings.append(
            "убыток от реализации превысил доходы — взаимозачёт с "
            "другими категориями НК не допускает"
        )
        taxable = Decimal(0)
    summary.taxable_income_rub = _round2(taxable)

    status = _tax_status(owner_id)
    ndfl, breakdown = compute_ndfl(taxable, status)
    summary.ndfl_total = ndfl
    summary.ndfl_breakdown = breakdown

    return summary


def _year_start(year: int) -> int:
    return int(time.mktime(time.strptime(f"{year}-01-01 00:00:00",
                                          "%Y-%m-%d %H:%M:%S")))


def _persist_lots(owner_id: int, active: dict[int, list[Lot]]) -> None:
    """Сбросить tax_lots и записать текущий остаток из in-memory."""
    with get_db() as conn:
        conn.execute("DELETE FROM tax_lots WHERE owner_id = ?", (owner_id,))
        for asset_id, lots in active.items():
            for lot in lots:
                if lot.qty_remaining <= 0:
                    continue
                conn.execute(
                    """
                    INSERT INTO tax_lots
                        (owner_id, asset_id, acquired_ts, acquired_tx_id,
                         qty_remaining, cost_per_unit_rub, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (owner_id, asset_id, lot.acquired_ts, lot.acquired_tx_id,
                     str(lot.qty_remaining), str(lot.cost_per_unit_rub),
                     lot.source),
                )


def _fifo_cost_inplace(lots: list[Lot], to_consume: Decimal,
                        scale: Decimal) -> tuple[Decimal, Decimal]:
    """FIFO-расход по in-memory списку лотов. Мутирует переданный список.

    Возвращает (cost_rub, shortfall). shortfall > 0 — не хватило лотов
    (докупка мимо учёта), себестоимость по недосписанному принимается
    нулевой — пользователь увидит это в warnings.
    """
    total_cost = Decimal(0)
    i = 0
    while i < len(lots) and to_consume > 0:
        head = lots[i]
        take = min(head.qty_remaining, to_consume)
        total_cost += (take / scale) * head.cost_per_unit_rub
        head.qty_remaining -= take
        to_consume -= take
        if head.qty_remaining <= 0:
            lots.pop(i)
        else:
            i += 1
    return total_cost, to_consume


def _asset_decimals(asset_id: int) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT decimals FROM crypto_assets WHERE id = ?", (asset_id,)
        ).fetchone()
    return int(row["decimals"] or 9) if row else 9


def _resolve_fiat(tx: Tx) -> Optional[Decimal]:
    """Что в транзакции: вручную введённая сумма или ничего (None)."""
    if tx.fiat_value_rub:
        v = _to_decimal(tx.fiat_value_rub)
        if v > 0:
            return v
    return None


def _price_to_decimal(price: float) -> Decimal:
    """Рыночная котировка приходит из price_oracle как float.

    Decimal * float в Python не поддерживается — умножение падает с
    TypeError, и весь годовой отчёт (/tax, /pnl, выгрузки) не считается
    целиком. Приводим цену к Decimal через str: str даёт десятичную
    запись без двоичного хвоста, а дальше _round2 округляет до копеек.
    """
    return Decimal(str(price))


def _tax_status(owner_id: int) -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT tax_status FROM tax_settings WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()
    return (row["tax_status"] if row and row["tax_status"] else "resident")


def _fmt_date(ts: int) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def _year_end(year: int) -> int:
    return int(time.mktime(time.strptime(f"{year + 1}-01-01 00:00:00",
                                    "%Y-%m-%d %H:%M:%S")))


def summary_to_dict(summary: YearSummary) -> dict:
    """JSON-кэш отчёта. Для tax_reports.payload."""
    return {
        "year": summary.year,
        "total_income_rub": str(summary.total_income_rub),
        "total_cost_rub": str(summary.total_cost_rub),
        "realized_gain_rub": str(summary.realized_gain_rub),
        "taxable_income_rub": str(summary.taxable_income_rub),
        "ndfl_total": str(summary.ndfl_total),
        "ndfl_breakdown": summary.ndfl_breakdown,
        "warnings": summary.warnings,
        "skipped": summary.skipped,
        "income_events": [asdict(e) for e in summary.income_events],
        "disposal_events": [asdict(e) for e in summary.disposal_events],
    }


def cache_report(owner_id: int, summary: YearSummary) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO tax_reports (owner_id, year, payload, generated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(owner_id, year) DO UPDATE SET
                payload = excluded.payload,
                generated_at = CURRENT_TIMESTAMP
            """,
            (owner_id, summary.year, json.dumps(
                summary_to_dict(summary), ensure_ascii=False, default=str)),
        )


def load_cached_report(owner_id: int, year: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT payload, generated_at FROM tax_reports "
            "WHERE owner_id = ? AND year = ?",
            (owner_id, year),
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
        data["generated_at"] = row["generated_at"]
        return data
    except (TypeError, ValueError):
        return None