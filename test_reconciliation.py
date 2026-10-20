"""Функциональный тест: сверка «дебет-кредит» (reconciliation).

Сценарий (всё через парные CEX-записи, как их создаёт tasks.cex_sync):
  1. Покупка 1 BTC за 3000 USDT → Дт BTC / Кт USDT.
  2. Покупка 0.5 BTC за 1500 USDT → Дт BTC / Кт USDT.
  3. Продажа 1 BTC за 4000 USDT → Дт USDT / Кт BTC.
  4. Аирдроп 100 NOT (без quote-пары, с fiat_value) → Дт NOT / Кт доходы.

Ожидаемый результат:
  * Журнал: 4 проводки (каждая — готовая пара Дт/Кт; quote-ноги «шиты»
    в счета сделок) + аирдроп.
  * Все buy/sell — has_quote_pair=True.
  * Расчётные остатки: BTC = 0.5, NOT = 100, USDT = −500
    (−3000 −1500 +4000) — отрицательный USDT демонстрирует работу
    контроля check_negative_balances: USDT «пришёл» бы откуда-то ещё,
    если бы это была реальная история.
  * Остаточная себестоимость BTC = 150 000 RUB (FIFO: продан старый лот).
"""

import asyncio
import os
import sys
import tempfile
import time

TEST_DB = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = TEST_DB
sys.path.insert(0, ".")

# Кириллица, «Σ» и эмодзи в выводе против кодовой страницы cp866/cp1251
# консоли Windows роняют скрипт UnicodeEncodeError на финальном print.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


async def main() -> int:
    import utils.database as db
    db.DB_PATH = TEST_DB
    db.init_db()

    from utils.crypto_db import TxType, add_transaction, ensure_asset
    from utils.reconciliation import (build_journal, check_pairing,
                                      compute_balances,
                                      check_negative_balances)

    owner = 999002
    year = 2025

    ensure_asset("BTC", "Bitcoin", decimals=8, coingecko_id="bitcoin")
    ensure_asset("USDT", "Tether", decimals=6, coingecko_id="tether")
    ensure_asset("NOT", "Notcoin", decimals=9, coingecko_id="notcoin")

    t1 = int(time.mktime(time.strptime("2025-02-01 10:00:00", "%Y-%m-%d %H:%M:%S")))
    t2 = int(time.mktime(time.strptime("2025-03-01 10:00:00", "%Y-%m-%d %H:%M:%S")))
    t3 = int(time.mktime(time.strptime("2025-05-01 10:00:00", "%Y-%m-%d %H:%M:%S")))
    t4 = int(time.mktime(time.strptime("2025-06-01 10:00:00", "%Y-%m-%d %H:%M:%S")))

    # 1. Покупка 1 BTC за 3000 USDT — двойная запись.
    add_transaction(owner_id=owner, ts=t1, asset_symbol="BTC",
                    tx_type=TxType.BUY, amount="1",
                    cost_basis_rub="300000", note="bybit buy BTCUSDT",
                    source="bybit", external_id="bybit:exec1")
    add_transaction(owner_id=owner, ts=t1, asset_symbol="USDT",
                    tx_type=TxType.TRADE_OUT, amount="3000",
                    note="bybit buy BTCUSDT [quote]",
                    source="bybit", external_id="bybit:exec1:quote")
    # 2. Покупка 0.5 BTC за 1500 USDT.
    add_transaction(owner_id=owner, ts=t2, asset_symbol="BTC",
                    tx_type=TxType.BUY, amount="0.5",
                    cost_basis_rub="150000", note="bybit buy BTCUSDT",
                    source="bybit", external_id="bybit:exec2")
    add_transaction(owner_id=owner, ts=t2, asset_symbol="USDT",
                    tx_type=TxType.TRADE_OUT, amount="1500",
                    note="bybit buy BTCUSDT [quote]",
                    source="bybit", external_id="bybit:exec2:quote")
    # 3. Продажа 1 BTC за 4000 USDT.
    add_transaction(owner_id=owner, ts=t3, asset_symbol="BTC",
                    tx_type=TxType.SELL, amount="1",
                    fiat_value_rub="400000", note="bybit sell BTCUSDT",
                    source="bybit", external_id="bybit:exec3")
    add_transaction(owner_id=owner, ts=t3, asset_symbol="USDT",
                    tx_type=TxType.TRADE_IN, amount="4000",
                    note="bybit sell BTCUSDT [quote]",
                    source="bybit", external_id="bybit:exec3:quote")
    # 4. Аирдроп 100 NOT без quote-пары (с fiat).
    add_transaction(owner_id=owner, ts=t4, asset_symbol="NOT",
                    tx_type=TxType.AIRDROP, amount="100",
                    fiat_value_rub="500", note="airdrop")

    # --- Журнал проводок.
    entries = build_journal(owner, year)
    print("=" * 60)
    print(f"Тест reconciliation за {year}")
    print("=" * 60)
    print(f"Проводок: {len(entries)}")
    for e in entries:
        print(f"  {time.strftime('%d.%m.%Y', time.localtime(e.ts))} "
              f"Дт {e.dr_account} / Кт {e.cr_account} :: {e.kind} "
              f"{e.qty} {e.asset} @ {e.rub} RUB "
              f"(пара={'да' if e.has_quote_pair else 'НЕТ'})")

    issues = check_pairing(entries)
    balances = compute_balances(owner)
    neg = check_negative_balances(balances)
    print("\nБалансы (расчёт):")
    for b in balances:
        print(f"  {b.symbol}: {b.qty} (себестоимость {b.cost_rub} RUB)")
    print(f"\nIssues pairing: {[i.message for i in issues]}")
    print(f"Issues negative: {[i.message for i in neg]}")

    errors: list[str] = []

    # Журнал: каждая проводка — УЖЕ пара Дт/Кт (quote-нога «вшита» в
    # счета сделки), поэтому 4 проводки: 3 сделки + аирдроп.
    if len(entries) != 4:
        errors.append(f"ожидалось 4 проводки, получено {len(entries)}")

    # Все сделки парные.
    unpaired = [i for i in issues if i.kind == "unpaired"]
    if unpaired:
        errors.append(f"неожиданные непарные операции: {[i.message for i in unpaired]}")

    # Нет no_quote: у всех income/sell/spend есть RUB.
    no_quote = [i for i in issues if i.kind == "no_quote"]
    if no_quote:
        errors.append(f"операции без RUB-оценки: {[i.message for i in no_quote]}")

    # Балансы.
    by_symbol = {b.symbol: b.qty for b in balances}
    if by_symbol.get("BTC") != 0.5:
        errors.append(f"BTC balance должен быть 0.5, не {by_symbol.get('BTC')}")
    if by_symbol.get("USDT") != -500:
        errors.append(f"USDT balance должен быть −500, не {by_symbol.get('USDT')}")
    if by_symbol.get("NOT") != 100:
        errors.append(f"NOT balance должен быть 100, не {by_symbol.get('NOT')}")

    # USDT отрицательный → контроль отрицательных остатков должен сработать.
    if not any(i.symbol == "USDT" for i in neg):
        errors.append("ожидалось предупреждение об отрицательном USDT")

    # FIFO-себестоимость BTC: продан 1 BTC из лота 02.01 по 300 000 RUB.
    btc_row = next((b for b in balances if b.symbol == "BTC"), None)
    if btc_row and btc_row.cost_rub != 150000:
        errors.append(f"остаточная себестоимость BTC должна быть 150000, "
                      f"не {btc_row.cost_rub}")

    print()
    if not errors:
        print("OK: ALL CHECKS PASSED")
        code = 0
    else:
        print("FAIL:")
        for e in errors:
            print(f"  - {e}")
        code = 1

    from utils import database as _db
    _db.close_all()
    try:
        os.unlink(TEST_DB)
    except OSError:
        pass
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
