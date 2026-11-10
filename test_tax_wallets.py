"""Регрессии налогового периода и лотов по кошелькам.

Проверяются правки, которые легко потерять при следующем рефакторинге:

  1. Границы налогового года считаются по МСК, а не по локальной зоне
     процесса. Сделка 31.12 в 23:30 МСК относится к старому году.
  2. Перемещение между своими кошельками переносит лот в кошелёк-получатель,
     не открывая второй лот и не теряя актив.
  3. /wallet_list видит остаток в конкретном кошельке, а не общий остаток
     владельца в каждом.
  4. Отчёт за прошлый год не перезаписывает текущий баланс tax_lots.
  5. Смена FIFO ↔ WAC сбрасывает кэш отчётов; снапшот другого метода
     не выдаётся за актуальный.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal

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


def _ts(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp())


def test_moscow_year() -> None:
    from datetime import timedelta

    from utils.tax_period import MOSCOW_TZ, moscow_year_end, moscow_year_start

    print("\n1. Границы года по МСК")
    start_2025 = moscow_year_start(2025)
    check(
        datetime.fromtimestamp(start_2025, MOSCOW_TZ)
        == datetime(2025, 1, 1, tzinfo=MOSCOW_TZ),
        "начало 2025 — 00:00 МСК",
        str(datetime.fromtimestamp(start_2025, MOSCOW_TZ)),
    )
    check(
        start_2025 == int(datetime(2024, 12, 31, 21, tzinfo=timezone.utc).timestamp()),
        "00:00 МСК = 21:00 UTC предыдущего дня",
        str(start_2025),
    )
    check(moscow_year_end(2025) == moscow_year_start(2026),
          "конец года = начало следующего")

    # 31.12.2024 23:30 МСК — ещё 2024 год, хотя в UTC это 20:30.
    edge_old = int(datetime(2024, 12, 31, 23, 30,
                            tzinfo=timezone(timedelta(hours=3))).timestamp())
    edge_new = int(datetime(2025, 1, 1, 0, 30,
                            tzinfo=timezone(timedelta(hours=3))).timestamp())
    check(edge_old < start_2025, "23:30 31.12 остаётся в старом году")
    check(start_2025 <= edge_new < moscow_year_end(2025),
          "00:30 01.01 попадает в новый год")


def test_wallet_lots() -> None:
    from utils.crypto_db import (
        Chain, TxType, add_transaction, add_wallet, ensure_asset,
    )
    from utils.database import get_db
    from utils.tax_calc import rebuild_lots
    from handlers.crypto_ledger import unassigned_holdings, wallet_holdings

    print("\n2. Перемещение лота между кошельками")
    owner = 991001
    ensure_asset("TST", "Test Coin", chain=Chain.OTHER, decimals=9)
    source = add_wallet(owner, Chain.OTHER, "tst-source-address", "source")
    target = add_wallet(owner, Chain.OTHER, "tst-target-address", "target")

    add_transaction(
        owner_id=owner, ts=_ts("2025-02-01 10:00:00"), asset_symbol="TST",
        tx_type=TxType.TRANSFER_IN, amount="2", wallet_id=source.id,
        cost_basis_rub="1000", note="buy 2",
    )
    add_transaction(
        owner_id=owner, ts=_ts("2025-03-01 10:00:00"), asset_symbol="TST",
        tx_type=TxType.TRANSFER_OUT, amount="1.5", wallet_id=source.id,
        is_internal=True, note="move out",
    )
    add_transaction(
        owner_id=owner, ts=_ts("2025-03-01 10:05:00"), asset_symbol="TST",
        tx_type=TxType.TRANSFER_IN, amount="1.5", wallet_id=target.id,
        is_internal=True, note="move in",
    )

    rebuild_lots(owner)
    by_wallet = wallet_holdings(owner)
    check(source.id in by_wallet and
          by_wallet[source.id][0][1] == Decimal("0.5"),
          "в кошельке-отправителе осталось 0.5",
          str(by_wallet.get(source.id)))
    check(target.id in by_wallet and
          by_wallet[target.id][0][1] == Decimal("1.5"),
          "в кошельке-получателе 1.5",
          str(by_wallet.get(target.id)))
    check(unassigned_holdings(owner) == [], "все лоты привязаны к кошелькам",
          str(unassigned_holdings(owner)))

    with get_db() as conn:
        rows = conn.execute(
            "SELECT wallet_id, qty_remaining, cost_per_unit_rub "
            "FROM tax_lots WHERE owner_id = ?",
            (owner,),
        ).fetchall()
    target_rows = [r for r in rows if r["wallet_id"] == target.id]
    check(len(target_rows) == 1, "в получателе один лот", str(len(target_rows)))
    if target_rows:
        check(Decimal(str(target_rows[0]["qty_remaining"]))
              == Decimal("1500000000"),
              "лот перенесён в минимальных единицах",
              str(target_rows[0]["qty_remaining"]))
        check(Decimal(str(target_rows[0]["cost_per_unit_rub"]))
              == Decimal("500"),
              "себестоимость лота сохранена",
              str(target_rows[0]["cost_per_unit_rub"]))


async def test_past_report_keeps_lots() -> None:
    from utils.crypto_db import Chain, TxType, add_transaction, ensure_asset
    from utils.database import get_db
    from utils.tax_calc import compute_year_report, rebuild_lots

    print("\n3. Отчёт за прошлый год не трогает текущий баланс")
    owner = 991002
    ensure_asset("OLD", "Old Coin", chain=Chain.OTHER, decimals=0)
    add_transaction(
        owner_id=owner, ts=_ts("2021-05-01 12:00:00"), asset_symbol="OLD",
        tx_type=TxType.TRANSFER_IN, amount="3", cost_basis_rub="300",
        note="buy 3",
    )
    add_transaction(
        owner_id=owner, ts=_ts("2022-05-01 12:00:00"), asset_symbol="OLD",
        tx_type=TxType.TRANSFER_IN, amount="2", cost_basis_rub="400",
        note="buy 2 more",
    )
    rebuild_lots(owner)
    with get_db() as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM tax_lots WHERE owner_id = ?",
            (owner,),
        ).fetchone()["n"]
    check(before == 2, "до отчёта два лота", str(before))

    # 2020 год — заведомо прошлый; операций в нём нет.
    summary = await compute_year_report(owner, 2020)
    check(summary.year == 2020 and not summary.income_events,
          "отчёт за пустой прошлый год посчитан")
    with get_db() as conn:
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM tax_lots WHERE owner_id = ?",
            (owner,),
        ).fetchone()["n"]
    check(after == 2, "текущие лоты не перезаписаны срезом прошлого года",
          str(after))


def test_report_cache_method() -> None:
    from utils.crypto_db import set_tax_settings
    from utils.tax_calc import (
        YearSummary, cache_report, load_cached_report,
    )

    print("\n4. Кэш отчёта привязан к методу расчёта")
    owner = 991003
    summary = YearSummary(year=2020, cost_basis="fifo")
    cache_report(owner, summary)
    check(load_cached_report(owner, 2020) is not None,
          "FIFO-снапшот прочитан")
    check(load_cached_report(owner, 2020, expected_method="fifo") is not None,
          "FIFO-снапшот подходит для FIFO")
    check(load_cached_report(owner, 2020, expected_method="wac") is None,
          "FIFO-снапшот не выдаётся за WAC")

    changed = set_tax_settings(owner, cost_basis="wac")
    check(changed is True, "смена FIFO → WAC отмечена", str(changed))
    check(load_cached_report(owner, 2020) is None,
          "кэш сброшен при смене метода")
    again = set_tax_settings(owner, cost_basis="wac")
    check(again is False, "повторное сохранение того же метода — не смена",
          str(again))


async def main() -> int:
    import utils.database as db
    db.DB_PATH = TEST_DB
    db.init_db()

    print("=" * 60)
    print("Налоговый период и лоты по кошелькам")
    print("=" * 60)

    test_moscow_year()
    test_wallet_lots()
    await test_past_report_keeps_lots()
    test_report_cache_method()

    print()
    if FAILURES:
        print(f"❌ Провалено проверок: {len(FAILURES)}")
        for name in FAILURES:
            print(f"   — {name}")
        return 1
    print("✅ Все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
