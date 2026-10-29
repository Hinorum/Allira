"""Тесты готовности отчёта 3-НДФЛ.

Проверяется, что статус «готов / не готов» отличает блокеры от обычных
пояснений:

  1. Чистые данные + полное покрытие истории → отчёт готов.
  2. Нет котировки, не хватило лотов, покупка без рублёвой оценки,
     неподтверждённая операция, неполное покрытие биржи → не готов,
     и в причинах есть человеческая формулировка.
  3. Неподтверждённая операция вне отчётного года не блокирует отчёт.
  4. Нехватка лотов действительно появляется из расчёта FIFO, а не
     только из ручного списка skipped.
"""

import asyncio
import os
import sys
import tempfile
import time

TEST_DB = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = TEST_DB
sys.path.insert(0, ".")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

FAILURES: list[str] = []


def check(cond: bool, name: str, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def ts(year: int, month: int, day: int) -> int:
    return int(time.mktime(time.strptime(
        f"{year}-{month:02d}-{day:02d} 12:00:00", "%Y-%m-%d %H:%M:%S"
    )))


def full_snapshot(year: int = 2025):
    from utils.sources import SourcesSnapshot
    return SourcesSnapshot(
        year=year, wallets=[object()], cex=[("Bybit", True)],
        pending=0, ops_year=1,
        coverage=[("Bybit", ts(year - 1, 3, 1) * 1000)],
    )


async def main() -> int:
    import utils.database as db

    db.DB_PATH = TEST_DB
    db.init_db()

    from utils.crypto_db import TxType, add_transaction
    from utils.report_readiness import check_readiness
    from utils.tax_calc import YearSummary, compute_year_report

    print("=" * 60)
    print("Тест готовности отчёта 3-НДФЛ")
    print("=" * 60)

    # ---------------------------------------------- 1. чистые данные
    print("1. Чистые данные — отчёт готов")
    owner = 8101
    summary = YearSummary(year=2025)
    readiness = check_readiness(owner, 2025, summary, full_snapshot())
    check(readiness.ready, "нет блокеров", str(readiness.blockers))
    check(not readiness.blockers, "список причин пуст",
          str(readiness.blockers))

    # ------------------------------------------------ 2. блокеры
    print("2. Каждый вид неполноты делает отчёт неготовым")
    summary_no_quote = YearSummary(year=2025)
    summary_no_quote.skipped.append({
        "tx_id": 1, "reason": "no_quote", "symbol": "BTC",
    })
    ready = check_readiness(owner, 2025, summary_no_quote, full_snapshot())
    check(not ready.ready and any("Нет котировок" in b
                                  for b in ready.blockers),
          "нет котировки — блокер", str(ready.blockers))

    summary_lots = YearSummary(year=2025)
    summary_lots.skipped.append({
        "tx_id": 2, "reason": "missing_lots", "symbol": "ETH",
        "shortfall": "100",
    })
    ready = check_readiness(owner, 2025, summary_lots, full_snapshot())
    check(not ready.ready and any("Не хватило лотов" in b
                                  for b in ready.blockers),
          "нехватка лотов — блокер", str(ready.blockers))

    add_transaction(owner, ts(2025, 4, 1), "BTC", TxType.BUY, "1",
                    source="manual")
    ready = check_readiness(owner, 2025, YearSummary(year=2025),
                            full_snapshot())
    check(not ready.ready and any("без рублёвой оценки" in b
                                  for b in ready.blockers),
          "покупка без оценки — блокер", str(ready.blockers))

    add_transaction(owner, ts(2025, 5, 1), "TON", TxType.AIRDROP, "1",
                    source="chain", fiat_value_rub="100")
    with db.get_db() as conn:
        conn.execute(
            "UPDATE crypto_transactions SET is_confirmed = 0 "
            "WHERE owner_id = ? AND ts = ?",
            (owner, ts(2025, 5, 1)),
        )
    add_transaction(owner, ts(2024, 5, 1), "TON", TxType.AIRDROP, "1",
                    source="chain", fiat_value_rub="100")
    with db.get_db() as conn:
        conn.execute(
            "UPDATE crypto_transactions SET is_confirmed = 0 "
            "WHERE owner_id = ? AND ts = ?",
            (owner, ts(2024, 5, 1)),
        )
    ready = check_readiness(owner, 2025, YearSummary(year=2025),
                            full_snapshot())
    check(any("ждут подтверждения" in b for b in ready.blockers),
          "неподтверждённая операция года — блокер", str(ready.blockers))
    check(not any("2024" in b for b in ready.blockers),
          "операция вне года не блокирует", str(ready.blockers))

    from utils.sources import SourcesSnapshot
    partial = SourcesSnapshot(
        year=2025, wallets=[], cex=[("OKX", True)], pending=0, ops_year=1,
        coverage=[("OKX", ts(2025, 8, 1) * 1000)],
    )
    ready = check_readiness(owner, 2025, YearSummary(year=2025), partial)
    check(any("не покрыто" in b for b in ready.blockers),
          "неполное покрытие — блокер", str(ready.blockers))
    check(ready.counts.get("coverage") == 1, "покрытие посчитано",
          str(ready.counts))

    no_sources = SourcesSnapshot(
        year=2025, wallets=[], cex=[("Bybit", False)], pending=0, ops_year=0,
        coverage=[],
    )
    ready = check_readiness(owner, 2025, YearSummary(year=2025), no_sources)
    check(any("Нет источников" in b for b in ready.blockers),
          "без источников отчёт не готов", str(ready.blockers))

    # ------------------------- 3. нехватка лотов из реального расчёта
    print("3. FIFO-расчёт сам сообщает о нехватке лотов")
    owner2 = 8102
    add_transaction(owner2, ts(2025, 6, 1), "BTC", TxType.SELL, "1",
                    fiat_value_rub="1000", source="manual")
    report = await compute_year_report(owner2, 2025)
    check(any(s.get("reason") == "missing_lots" for s in report.skipped),
          "расчёт записал missing_lots", str(report.skipped))
    ready = check_readiness(owner2, 2025, report, full_snapshot())
    check(not ready.ready, "отчёт неготов", str(ready.blockers))
    check(any("Не хватило лотов" in b for b in ready.blockers),
          "причина названа", str(ready.blockers))

    print()
    if FAILURES:
        print(f"❌ Провалено проверок: {len(FAILURES)}")
        for name in FAILURES:
            print(f"   • {name}")
        return 1
    print("✅ Все проверки пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
