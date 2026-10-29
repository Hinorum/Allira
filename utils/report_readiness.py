"""Готовность отчёта 3-НДФЛ: можно ли подавать цифры или ещё рано.

Сводка может посчитаться без ошибок и всё равно быть неполной: нет
котировки на дату сделки, не хватило FIFO-лотов, часть переводов ждёт
подтверждения, история биржи не дотягивает до начала года. Раньше это
оседало в общих «Замечаниях» рядом с налоговыми пояснениями, и отличить
блокер от справочной сноски было нельзя.

Модуль собирает только блокеры — причины, по которым отчёт нельзя
считать готовым. Всё остальное (например, пояснение про раздельные
шкалы майнера) остаётся в summary.warnings и на статус не влияет.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from utils.crypto_db import TxType
from utils.database import get_db


@dataclass
class ReportReadiness:
    """Итог проверки: готов ли отчёт и что именно мешает."""

    ready: bool
    blockers: list[str] = field(default_factory=list)
    # Сколько операций каждого вида попало в блокер — для короткой сводки.
    counts: dict[str, int] = field(default_factory=dict)


def _year_bounds(year: int) -> tuple[int, int]:
    start = int(time.mktime(time.strptime(f"{year}-01-01 00:00:00",
                                          "%Y-%m-%d %H:%M:%S")))
    end = int(time.mktime(time.strptime(f"{year + 1}-01-01 00:00:00",
                                        "%Y-%m-%d %H:%M:%S")))
    return start, end


def check_readiness(owner_id: int, year: int, summary,
                    snapshot=None) -> ReportReadiness:
    """Проверить полноту данных за год.

    summary — YearSummary из utils.tax_calc; snapshot — SourcesSnapshot
    из utils.sources (нужен для покрытия истории и наличия источников).
    Без snapshot проверяются только данные самого расчёта.
    """
    blockers: list[str] = []
    counts: dict[str, int] = {}

    skipped = list(getattr(summary, "skipped", []) or [])
    no_quote = [s for s in skipped if s.get("reason") == "no_quote"]
    if no_quote:
        counts["no_quote"] = len(no_quote)
        symbols = sorted({str(s.get("symbol") or "?") for s in no_quote})
        blockers.append(
            f"Нет котировок по {len(no_quote)} операциям "
            f"({', '.join(symbols[:4])}) — суммы в отчёте неполные."
        )

    missing_lots = [s for s in skipped if s.get("reason") == "missing_lots"]
    if missing_lots:
        counts["missing_lots"] = len(missing_lots)
        symbols = sorted({str(s.get("symbol") or "?")
                          for s in missing_lots})
        blockers.append(
            f"Не хватило лотов для {len(missing_lots)} списаний "
            f"({', '.join(symbols[:4])}) — вероятно, приход не записан."
        )

    start, end = _year_bounds(year)
    with get_db() as conn:
        unpriced = conn.execute(
            """
            SELECT COUNT(*) AS n FROM crypto_transactions
            WHERE owner_id = ? AND ts >= ? AND ts < ?
              AND tx_type IN (?, ?)
              AND (cost_basis_rub IS NULL OR cost_basis_rub = '')
              AND (fiat_value_rub IS NULL OR fiat_value_rub = '')
              AND (is_internal IS NULL OR is_internal = 0)
            """,
            (owner_id, start, end, TxType.BUY, TxType.TRANSFER_IN),
        ).fetchone()
        unconfirmed = conn.execute(
            """
            SELECT COUNT(*) AS n FROM crypto_transactions
            WHERE owner_id = ? AND ts >= ? AND ts < ?
              AND source = 'chain'
              AND (is_internal IS NULL OR is_internal = 0)
              AND (is_confirmed IS NULL OR is_confirmed = 0)
            """,
            (owner_id, start, end),
        ).fetchone()

    if int(unpriced["n"] or 0):
        counts["unpriced"] = int(unpriced["n"])
        blockers.append(
            f"{counts['unpriced']} покупок/вводов без рублёвой оценки — "
            "себестоимость лотов занижена."
        )
    if int(unconfirmed["n"] or 0):
        counts["unconfirmed"] = int(unconfirmed["n"])
        blockers.append(
            f"{counts['unconfirmed']} операций ждут подтверждения — "
            "проверь их на экране источников."
        )

    if snapshot is not None:
        coverage = list(getattr(snapshot, "coverage_warnings", []) or [])
        if coverage:
            counts["coverage"] = len(coverage)
            blockers.extend(coverage)
        if not getattr(snapshot, "has_sources", False):
            counts["no_sources"] = 1
            blockers.append(
                "Нет источников: не подключены ни кошельки, ни биржи."
            )

    return ReportReadiness(ready=not blockers, blockers=blockers,
                           counts=counts)
