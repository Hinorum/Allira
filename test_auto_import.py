"""Тесты автоимпорта: адреса + выписки → налоговая база.

Проверяется главное — то, ради чего всё затевалось:

  1. Входящий перевод с неизвестного адреса — это доход (аирдроп) и он
     попадает в базу.
  2. Перевод между своими кошельками — перемещение: ни дохода, ни лота.
  3. Вывод с биржи, найденный по выписке, — тоже перемещение. Иначе покупка
     на бирже плюс вывод на кошелёк давали бы ложный доход в размере всей
     покупки — самый дорогой вид ошибки в этом боте.
  4. Обмен (ушёл актив — пришёл другой) — реализация уходящего и приобретение
     пришедшего, а не «аирдроп» (один обмен не должен облагаться дважды).
  5. Исходящий перевод на неизвестный адрес — реализация.
  6. Перемещения не открывают лотов: актив учитывается один раз.
  7. Сквозной сценарий «купил на бирже → вывел на кошелёк»: налоговая база
     нулевая, актив на балансе один.
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

PRICES = {"BTC": 100000.0, "USDT": 100.0, "TON": 500.0, "ETH": 4000.0}


def check(cond: bool, name: str, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _patch_prices():
    """Котировки — заглушка: тест не ходит в сеть."""
    from utils import auto_import, tax_calc
    import tasks.cex_sync as cex_sync

    async def fake_price(symbol: str, ts: int):
        return PRICES.get((symbol or "").upper())

    auto_import.get_asset_price_rub = fake_price
    tax_calc.get_asset_price_rub = fake_price
    cex_sync.get_asset_price_rub = fake_price


def _move(asset: str, amount: str, direction: str, ts: int,
          counterparty: str, group: str = "g1", decimals: int = 8,
          chain: str = "ton"):
    from utils.chain_scan import ChainMove
    return ChainMove(
        ts=ts, chain=chain, address="0:mine", asset=asset, amount=amount,
        decimals=decimals, direction=direction, counterparty=counterparty,
        tx_hash=group, group=group, amount_raw=int(float(amount) * 10 ** decimals),
    )


async def main() -> int:
    import utils.database as db
    db.DB_PATH = TEST_DB
    db.init_db()
    _patch_prices()

    from utils.auto_import import (classify_moves, import_statement,
                                    persist_moves, rematch_transfers)
    from utils.chain_scan import ChainMove
    from utils.crypto_db import (TxType, add_transaction, add_wallet,
                                 list_transactions)
    from utils.reconciliation import compute_balances
    from utils.tax_calc import compute_year_report

    owner = 777001
    year = 2025
    ts_march = int(time.mktime(time.strptime("2025-03-05 10:00:00",
                                              "%Y-%m-%d %H:%M:%S")))
    ts_may = int(time.mktime(time.strptime("2025-05-11 12:00:00",
                                            "%Y-%m-%d %H:%M:%S")))

    print("=" * 60)
    print("Тест автоимпорта")
    print("=" * 60)

    wallet = add_wallet(owner, "ton", "0:mine", "мой")
    other = add_wallet(owner, "ton", "0:other", "второй")

    # --- 1. Неизвестный контрагент → доход.
    moves = [_move("BTC", "0.1", "in", ts_march, "0:stranger")]
    classified = await classify_moves(owner, moves)
    check(classified[0].kind == "income", "Неизвестный входящий → доход",
          classified[0].kind)
    saved, dups, review = await persist_moves(owner, classified,
                                              wallet_id=wallet.id)
    check(saved == 1, "Доход записан", f"saved={saved}")
    txs = list_transactions(owner, year=year)
    income_rows = [t for t in txs if t.tx_type == TxType.AIRDROP]
    check(len(income_rows) == 1, "Аирдроп в базе", str(len(income_rows)))
    if income_rows:
        check(income_rows[0].fiat_value_rub == "10000.00",
              "Доход оценён по рынку (0.1 × 100 000 ₽)",
              str(income_rows[0].fiat_value_rub))
    check(any(r.kind == "airdrop" for r in review),
          "Операция попала в список на проверку", str([r.kind for r in review]))

    # --- 2. Перевод между своими кошельками → перемещение.
    moves = [_move("ETH", "1", "in", ts_may, other.address)]
    classified = await classify_moves(owner, moves)
    check(classified[0].kind == "internal", "Свой кошелёк → перемещение",
          classified[0].kind)
    saved, dups, review = await persist_moves(owner, classified,
                                              wallet_id=wallet.id)
    internal_rows = [t for t in list_transactions(owner, year=year)
                     if t.is_internal]
    check(len(internal_rows) == 1, "Перемещение помечено внутренним",
          str(len(internal_rows)))
    check(not any(t.tx_type == TxType.AIRDROP and t.asset_id != 0
                   for t in internal_rows),
          "Перемещение не стало доходом")

    # --- 3. Вывод с биржи по выписке → перемещение, а не доход.
    # В выписке есть вывод 0.1 BTC 05.03 — он и есть источник перевода.
    statement = (
        "utcTime,symbol,side,qty,price,fee\n"
        "2025-03-05 09:00:00,BTCUSDT,Buy,0.1,60000,0.1\n"
    )
    # Вынос делаем вручную: в формате выписки биржи вывода нет отдельной
    # строки «вывод» в Bybit-экспорте сделок.
    add_transaction(
        owner_id=owner, ts=ts_march, wallet_id=wallet.id,
        asset_symbol="BTC", tx_type=TxType.TRANSFER_OUT, amount="0.1",
        note="bybit: вывод BTC", source="bybit",
        external_id="bybit:withdraw:1",
    )
    moves = [_move("BTC", "0.1", "in", ts_march, "0:bybit")]
    classified = await classify_moves(owner, moves)
    check(classified[0].kind == "internal",
          "Вывод с биржи → перемещение, а не доход", classified[0].kind)
    check(classified[0].matched_tx > 0, "Сопоставлено с записью из выписки",
          str(classified[0].matched_tx))
    await persist_moves(owner, classified, wallet_id=wallet.id)

    # --- 4. Обмен: ушёл BTC — пришёл USDT.
    swap_out = _move("BTC", "0.05", "out", ts_may, "0:dex", group="swap1")
    swap_in = _move("USDT", "5000", "in", ts_may, "0:dex", group="swap1",
                    decimals=6)
    classified = await classify_moves(owner, [swap_out, swap_in])
    kinds = {c.kind for c in classified}
    check("swap_out" in kinds and "swap_in" in kinds,
          "Обмен распознан как обмен", str(kinds))
    check("income" not in kinds, "Обмен не считается доходом", str(kinds))
    await persist_moves(owner, classified, wallet_id=wallet.id)
    swap_rows = [t for t in list_transactions(owner, year=year)
                 if t.tx_type == TxType.TRADE_IN]
    check(len(swap_rows) == 1, "Приобретение в обмене записано",
          str(len(swap_rows)))
    if swap_rows:
        check(swap_rows[0].cost_basis_rub == "5000.00",
              "Себестоимость полученного = отданная сумма (0.05 × 100 000)",
              str(swap_rows[0].cost_basis_rub))

    # --- 5. Исходящий перевод на неизвестный адрес → реализация.
    moves = [_move("TON", "10", "out", ts_may, "0:stranger2", group="g5")]
    classified = await classify_moves(owner, moves)
    check(classified[0].kind == "disposal", "Исходящий → реализация",
          classified[0].kind)
    await persist_moves(owner, classified, wallet_id=wallet.id)

    # --- 6. Перемещения не открывают лотов.
    balances = {b.symbol: b.qty for b in compute_balances(owner)}
    check(balances.get("ETH", 0) == 0,
          "Перевод между кошельками не добавил актив", str(balances.get("ETH")))
    check(balances.get("BTC") == Decimal_eq(0.05),
          "BTC учтён один раз: 0.1 приход − 0.05 обмен",
          str(balances.get("BTC")))

    # --- 7. Сквозной сценарий: покупка на бирже → вывод на кошелёк.
    owner2 = 777002
    wallet2 = add_wallet(owner2, "ton", "0:mine2", "мой")
    bybit_csv = (
        "utcTime,symbol,side,qty,price,fee\n"
        "2025-03-01 10:00:00,BTCUSDT,Buy,1,60000,0.1\n"
    )
    res = await import_statement(owner2, bybit_csv.encode(), "bybit.csv")
    check(res.rows == 1, "Выписка разобрана", str(res.rows))
    check(res.saved == 2, "Покупка записана с парной ногой", str(res.saved))
    # Вывод с биржи — отдельной записью (в экспорте сделок его нет).
    add_transaction(
        owner_id=owner2, ts=ts_march, asset_symbol="BTC",
        tx_type=TxType.TRANSFER_OUT, amount="1", note="bybit: вывод BTC",
        source="bybit", external_id="bybit:withdraw:1",
    )
    # Зачисление на кошелёк — из сети.
    moves = [_move("BTC", "1", "in", ts_march, "0:bybit", group="w1")]
    classified = await classify_moves(owner2, moves)
    check(classified[0].kind == "internal",
          "Зачисление после вывода — перемещение", classified[0].kind)
    await persist_moves(owner2, classified, wallet_id=wallet2.id)

    balances2 = {b.symbol: b.qty for b in compute_balances(owner2)}
    check(balances2.get("BTC") == Decimal_eq(1),
          "BTC на балансе ровно один", str(balances2.get("BTC")))
    summary = await compute_year_report(owner2, year)
    check(summary.total_income_rub == 0,
          "Ложного дохода от вывода нет",
          f"доход={summary.total_income_rub}")
    check(summary.taxable_income_rub == 0, "Налоговая база нулевая",
          str(summary.taxable_income_rub))
    check(summary.ndfl_total == 0, "НДФЛ нулевой",
          str(summary.ndfl_total))

    # --- 8. Пересопоставление: выписка приходит позже перевода.
    owner3 = 777003
    wallet3 = add_wallet(owner3, "ton", "0:mine3", "мой")
    moves = [_move("BTC", "0.5", "in", ts_march, "0:bybit", group="w2")]
    classified = await classify_moves(owner3, moves)
    await persist_moves(owner3, classified, wallet_id=wallet3.id)
    rows = list_transactions(owner3, year=year)
    check(any(t.tx_type == TxType.AIRDROP for t in rows),
          "До выписки перевод считался доходом")
    add_transaction(
        owner_id=owner3, ts=ts_march, asset_symbol="BTC",
        tx_type=TxType.TRANSFER_OUT, amount="0.5", note="вывод с биржи",
        source="bybit", external_id="bybit:withdraw:2",
    )
    changed = rematch_transfers(owner3)
    check(changed == 1, "Пересопоставление сработало", str(changed))
    rows = list_transactions(owner3, year=year)
    check(not any(t.tx_type == TxType.AIRDROP for t in rows),
          "Ложный доход исчез после выписки")
    summary3 = await compute_year_report(owner3, year)
    check(summary3.total_income_rub == 0, "База обнулилась",
          str(summary3.total_income_rub))

    print()
    if not FAILURES:
        print("OK: ALL CHECKS PASSED")
        code = 0
    else:
        print(f"FAIL: {len(FAILURES)} проверок не прошли:")
        for f in FAILURES:
            print(f"  - {f}")
        code = 1

    from utils import database as _db
    _db.close_all()
    try:
        os.unlink(TEST_DB)
    except OSError:
        pass
    return code


def Decimal_eq(value):
    from decimal import Decimal
    return Decimal(str(value))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
