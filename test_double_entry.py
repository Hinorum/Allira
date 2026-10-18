"""Функциональный тест: журнал проводок дебет/кредит (utils.double_entry).

Сценарий (все даты внутри 2025 года):
  1. BUY 1 BTC за 1 000 000 RUB       → Д 41.01.BTC 1M / К 51 1M
  2. AIRDROP 0.5 BTC, оценка 1.5M     → Д 41.01.BTC 1.5M / К 91.01 1.5M
  3. SELL 1.5 BTC за 4 500 000 RUB
     FIFO-себестоимость = 1.0×1M + 0.5×3M = 2.5M, прибыль 2M
                                       → Д 51 4.5M / К 41.01.BTC 2.5M / К 91.01 2M
  4. BUY 2 TON за 100 RUB             → Д 41.01.TON 100 / К 51 100
  5. TRANSFER_OUT 1 TON (своё)        → Д 41.01.TON 50 / К 41.01.TON 50
  6. TRANSFER_IN 1 TON (своё)         → Д 41.01.TON 50 / К 41.01.TON 50
  7. SELL 1 TON за 80 RUB
     FIFO: лот из п.4 списан на 50 → прибыль 30
                                       → Д 51 80 / К 41.01.TON 50 / К 91.01 30

Проверяем:
  * журнал сведён: Σдебет == Σкредит == 7 000 280
  * обороты по счетам (91.01: кредит 3 500 030 и т.д.)
  * BUY не даёт дохода для 3-НДФЛ (income_events = 1, только аирдроп)
  * CSV-экспорт журнала содержит итоговую строку со сверкой
"""

import asyncio
import os
import sys
import tempfile
import time

# Windows-консоль cp1251 не знает Σ/✅ — переключаем вывод на UTF-8.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Изолированная БД для теста.
TEST_DB = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = TEST_DB
sys.path.insert(0, ".")


async def main() -> int:
    import utils.database as db
    db.DB_PATH = TEST_DB
    db.init_db()

    from decimal import Decimal

    from utils.crypto_db import Chain, TxType, add_transaction, ensure_asset
    from utils.double_entry import (ACCOUNT_EXPENSE, ACCOUNT_INCOME,
                                    ACCOUNT_SETTLEMENT, asset_account,
                                    build_journal, render_journal_csv)
    from utils.tax_calc import compute_year_report

    owner = 999002
    year = 2025
    ensure_asset("BTC", "Bitcoin", chain=Chain.OTHER, decimals=8,
                 coingecko_id="bitcoin")
    ensure_asset("TON", "Toncoin", chain=Chain.TON, decimals=9,
                 coingecko_id="the-open-network")

    def ts(s: str) -> int:
        return int(time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S")))

    # 1. Покупка 1 BTC за 1 000 000 RUB.
    add_transaction(owner_id=owner, ts=ts("2025-01-15 10:00:00"),
                    asset_symbol="BTC", tx_type=TxType.BUY, amount="1.0",
                    cost_basis_rub="1000000", note="bought 1 BTC")
    # 2. Аирдроп 0.5 BTC с оценкой 1 500 000.
    add_transaction(owner_id=owner, ts=ts("2025-02-01 09:00:00"),
                    asset_symbol="BTC", tx_type=TxType.AIRDROP, amount="0.5",
                    fiat_value_rub="1500000", note="airdrop")
    # 3. Продажа 1.5 BTC за 4 500 000 RUB.
    add_transaction(owner_id=owner, ts=ts("2025-03-10 12:00:00"),
                    asset_symbol="BTC", tx_type=TxType.SELL, amount="1.5",
                    fiat_value_rub="4500000", note="sold 1.5 BTC")
    # 4. Покупка 2 TON за 100 RUB.
    add_transaction(owner_id=owner, ts=ts("2025-04-01 10:00:00"),
                    asset_symbol="TON", tx_type=TxType.BUY, amount="2.0",
                    cost_basis_rub="100", note="bought 2 TON")
    # 5. Перевод 1 TON на свой второй кошелёк.
    add_transaction(owner_id=owner, ts=ts("2025-04-02 10:00:00"),
                    asset_symbol="TON", tx_type=TxType.TRANSFER_OUT, amount="1.0",
                    cost_basis_rub="50", note="to my ledger wallet")
    add_transaction(owner_id=owner, ts=ts("2025-04-02 10:05:00"),
                    asset_symbol="TON", tx_type=TxType.TRANSFER_IN, amount="1.0",
                    cost_basis_rub="50", note="from my trade wallet")
    # 7. Продажа 1 TON за 80 RUB (FIFO: лот по 50 → прибыль 30).
    add_transaction(owner_id=owner, ts=ts("2025-05-01 10:00:00"),
                    asset_symbol="TON", tx_type=TxType.SELL, amount="1.0",
                    fiat_value_rub="80", note="sold 1 TON")

    journal = await build_journal(owner, year)

    print("=" * 60)
    print(f"Тест журнала проводок за {year}")
    print("=" * 60)
    for e in journal.entries:
        date = time.strftime("%d.%m.%Y %H:%M", time.localtime(e.ts))
        print(f"#{e.tx_id} {date} {TxType.label(e.tx_type)} "
              f"{e.qty} {e.asset_symbol}")
        for p in e.postings:
            print(f"    Д {p.account} {p.debit} / К {p.account} {p.credit}")
    print()
    for account in sorted(journal.turnovers):
        tr = journal.turnovers[account]
        print(f"  {account}: обр.Д {tr.debit} обр.К {tr.credit} "
              f"сальдо {tr.balance}")
    print()
    print(f"Σ дебет: {journal.total_debit}")
    print(f"Σ кредит: {journal.total_credit}")
    print(f"Сведено: {journal.balanced}")
    print(f"Warnings: {journal.warnings}")
    print()

    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    check(journal.balanced, f"журнал должен сходиться: "
          f"{journal.total_debit} != {journal.total_credit}")
    check(journal.total_debit == Decimal("7000280.00"),
          f"Σ дебет = 7 000 280, не {journal.total_debit}")
    check(journal.total_credit == Decimal("7000280.00"),
          f"Σ кредит = 7 000 280, не {journal.total_credit}")
    check(len(journal.entries) == 7, f"7 проводок, не {len(journal.entries)}")
    check(not journal.unpriced, f"оценивать нечего не должно было: {journal.unpriced}")

    # Обороты по счетам.
    btc = journal.turnovers[asset_account("BTC")]
    check(btc.debit == Decimal("2500000.00") and btc.credit == Decimal("2500000.00"),
          f"41.01.BTC: обороты 2.5M/2.5M, не {btc.debit}/{btc.credit}")
    income = journal.turnovers[ACCOUNT_INCOME]
    check(income.credit == Decimal("3500030.00"),
          f"91.01 кредит = 3 500 030 (1.5M аирдроп + 2M + 30 прибыли), "
          f"не {income.credit}")
    settlement = journal.turnovers[ACCOUNT_SETTLEMENT]
    check(settlement.debit == Decimal("4500080.00"),
          f"51 обр.Д = 4 500 080, не {settlement.debit}")
    check(settlement.credit == Decimal("1000100.00"),
          f"51 обр.К = 1 000 100, не {settlement.credit}")
    ton = journal.turnovers[asset_account("TON")]
    check(ton.balance == Decimal("50.00"),
          f"сальдо 41.01.TON = 50 (1 TON × 50), не {ton.balance}")
    check(ACCOUNT_EXPENSE not in journal.turnovers,
          "убытков нет — счёт 91.02 не должен открываться")

    # BUY не должен порождать доход для 3-НДФЛ.
    summary = await compute_year_report(owner, year)
    check(len(summary.income_events) == 1,
          f"доходное событие только одно (аирдроп), не {len(summary.income_events)}")
    check(summary.total_income_rub == 1500000,
          f"доход 1 500 000, не {summary.total_income_rub}")

    # CSV-экспорт: итоговая строка и заголовок на месте.
    csv_bytes = render_journal_csv(journal)
    csv_text = csv_bytes.decode("utf-8-sig")
    check("Дебет (RUB)" in csv_text, "в CSV нет заголовка")
    check("дебет=кредит ✓" in csv_text, "в CSV нет итога со сверкой")
    check(len(csv_bytes) > 100, "CSV подозрительно мал")

    if not errors:
        print("✅ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    else:
        print("❌ ОШИБКИ:")
        for e in errors:
            print(f"  - {e}")

    from utils import database as _db
    _db.close_all()
    try:
        os.unlink(TEST_DB)
    except OSError:
        pass
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
