"""Функциональный тест: FIFO и НДФЛ на смоделированной истории.

Сценарий: покупаем 1 BTC за 1 000 000 RUB, потом ещё 1 BTC за 2 000 000 RUB,
продаём 1.5 BTC за 4 500 000 RUB, получаем аирдроп 0.5 BTC на дату продажи.

Ожидаемый результат (FIFO):
  * Себестоимость проданных 1.5 BTC = 1.0 * 1 000 000 + 0.5 * 2 000 000
                                       = 1 000 000 + 1 000 000 = 2 000 000
  * Прибыль от реализации = 4 500 000 − 2 000 000 = 2 500 000
  * Доход от аирдропа = 0.5 BTC × рыночная котировка (для теста возьмём
    ту же цену, что у реализации = 3 000 000 за 1 BTC)
    → 0.5 × 3 000 000 = 1 500 000
  * Налоговая база = 2 500 000 + 1 500 000 = 4 000 000
  * НДФЛ: 13% от 2.4 млн + 15% от (4 - 2.4) = 312 000 + 240 000 = 552 000
"""

import asyncio
import os
import sys
import tempfile
import time

# Изолированная БД для теста.
TEST_DB = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = TEST_DB
# Отключаем сетевые вызовы — налог считается по мануальным fiat_value.
sys.path.insert(0, ".")

# Тесты печатают кириллицу, «Σ» и эмодзи. Консоль Windows по умолчанию
# отдаёт cp866/cp1251, где этих символов нет, и скрипт падает с
# UnicodeEncodeError уже на финальном print — проверки при этом успешно
# прошли. Переключаем вывод на UTF-8, а если поток не даёт перенастроить
# кодировку — просим заменять непечатаемое, чтобы тест дошёл до конца.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


async def main() -> int:
    # Перенастроим БД на тестовый файл ДО импорта, чтобы открылся правильный.
    import utils.database as db
    db.DB_PATH = TEST_DB
    db.init_db()

    from utils.crypto_db import (
        Chain, TxType, add_transaction, add_wallet, ensure_asset,
    )
    from utils.tax_calc import compute_year_report, compute_ndfl

    owner = 999001
    year = 2025

    # 1. Создаём актив BTC и кошелёк.
    ensure_asset("BTC", "Bitcoin", chain=Chain.OTHER, decimals=8,
                 coingecko_id="bitcoin")
    wallet = add_wallet(owner, Chain.OTHER, "bc1qtestfakeaddress0000", "test wallet")

    # Фиксированные даты.
    t_buy1 = int(time.mktime(time.strptime("2025-01-15 10:00:00", "%Y-%m-%d %H:%M:%S")))
    t_buy2 = int(time.mktime(time.strptime("2025-03-10 11:00:00", "%Y-%m-%d %H:%M:%S")))
    t_sell = int(time.mktime(time.strptime("2025-06-20 12:00:00", "%Y-%m-%d %H:%M:%S")))
    t_air  = int(time.mktime(time.strptime("2025-07-01 09:00:00", "%Y-%m-%d %H:%M:%S")))

    # 2. Покупка 1 BTC за 1 000 000 RUB (TRANSFER_IN + cost_basis).
    add_transaction(
        owner_id=owner, ts=t_buy1, asset_symbol="BTC",
        tx_type=TxType.TRANSFER_IN, amount="1.0",
        wallet_id=wallet.id, cost_basis_rub="1000000",
        note="bought 1 BTC",
    )
    # 3. Покупка ещё 1 BTC за 2 000 000 RUB.
    add_transaction(
        owner_id=owner, ts=t_buy2, asset_symbol="BTC",
        tx_type=TxType.TRANSFER_IN, amount="1.0",
        wallet_id=wallet.id, cost_basis_rub="2000000",
        note="bought 1 BTC more",
    )
    # 4. Продажа 1.5 BTC за 4 500 000 RUB.
    add_transaction(
        owner_id=owner, ts=t_sell, asset_symbol="BTC",
        tx_type=TxType.SELL, amount="1.5",
        wallet_id=wallet.id, fiat_value_rub="4500000",
        note="sold 1.5 BTC",
    )
    # 5. Аирдроп 0.5 BTC на 1 июля по курсу 3 000 000/BTC → доход 1 500 000.
    add_transaction(
        owner_id=owner, ts=t_air, asset_symbol="BTC",
        tx_type=TxType.AIRDROP, amount="0.5",
        wallet_id=wallet.id, fiat_value_rub="1500000",
        note="airdrop 0.5 BTC at 3M/BTC",
    )

    # Прогон расчёта.
    summary = await compute_year_report(owner, year)

    print("=" * 60)
    print(f"Тест FIFO + НДФЛ за {year}")
    print("=" * 60)
    print(f"Доход (income_events): {len(summary.income_events)}")
    for ev in summary.income_events:
        print(f"  + {ev.asset_symbol} {ev.qty} @ {ev.income_rub} RUB (tx {ev.tx_id})")
    print(f"Реализация (disposal_events): {len(summary.disposal_events)}")
    for ev in summary.disposal_events:
        print(f"  - {ev.asset_symbol} {ev.qty}: "
              f"выручка {ev.proceeds_rub}, расход {ev.cost_rub}, "
              f"прибыль {ev.gain_rub} (tx {ev.tx_id})")
    print()
    print(f"Σ доход: {summary.total_income_rub} RUB")
    print(f"Σ расход: {summary.total_cost_rub} RUB")
    print(f"Σ прибыль: {summary.realized_gain_rub} RUB")
    print(f"Налоговая база: {summary.taxable_income_rub} RUB")
    print(f"НДФЛ: {summary.ndfl_total} RUB")
    for step in summary.ndfl_breakdown:
        print(f"  {step['bracket']}: база {step['base']}, "
              f"ставка {step['rate']}, налог {step['tax']}")
    print()
    print(f"Warnings: {summary.warnings}")
    print(f"Skipped: {summary.skipped}")
    print()

    # Проверки.
    errors: list[str] = []

    if summary.total_income_rub != 1500000:
        errors.append(f"доход должен быть 1 500 000, не {summary.total_income_rub}")
    if summary.total_cost_rub != 2000000:
        errors.append(f"расход должен быть 2 000 000, не {summary.total_cost_rub}")
    if summary.realized_gain_rub != 2500000:
        errors.append(f"прибыль должна быть 2 500 000, не {summary.realized_gain_rub}")
    if summary.taxable_income_rub != 4000000:
        errors.append(f"база должна быть 4 000 000, не {summary.taxable_income_rub}")
    # НДФЛ: 13% × 2 400 000 = 312 000; 15% × 1 600 000 = 240 000; итого 552 000
    if summary.ndfl_total != 552000:
        errors.append(f"НДФЛ должен быть 552 000, не {summary.ndfl_total}")

    if not errors:
        print("✅ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    else:
        print("❌ ОШИБКИ:")
        for e in errors:
            print(f"  - {e}")

    # Закрываем соединения в тестовом потоке, иначе Windows держит файл.
    from utils import database as _db
    _db.close_all()
    try:
        os.unlink(TEST_DB)
    except OSError:
        pass  # на Windows файл может быть ещё занят — оставим
    return 0 if not errors else 1


async def regression_market_quote() -> int:
    """Регресс: рыночная котировка — float, количество — Decimal.

    Раньше строка `income = amount_human * market` роняла весь годовой
    отчёт (/tax, /pnl, выгрузки PDF/XLSX/CSV) с TypeError, как только у
    транзакции не было вручную введённой цены. Сценарий повторяет
    обычный случай: аирдроп без fiat_value_rub, оценка по котировке.
    """
    import utils.database as db
    db.DB_PATH = TEST_DB
    db.init_db()

    from utils.crypto_db import (
        Chain, TxType, add_transaction, add_wallet, ensure_asset,
    )
    import utils.tax_calc as tax_calc

    owner = 999002
    year = 2026

    ensure_asset("ETH", "Ethereum", chain=Chain.OTHER, decimals=18,
                 coingecko_id="ethereum")
    wallet = add_wallet(owner, Chain.OTHER, "0xtestfakeaddress0000", "test wallet")
    t_air = int(time.mktime(time.strptime("2026-03-01 12:00:00", "%Y-%m-%d %H:%M:%S")))

    # Ни cost_basis_rub, ни fiat_value_rub: цена должна взяться из котировки.
    add_transaction(
        owner_id=owner, ts=t_air, asset_symbol="ETH",
        tx_type=TxType.AIRDROP, amount="2",
        wallet_id=wallet.id,
        note="airdrop без ручной цены",
    )

    # Подменяем сетевую котировку на float — ровно тот тип, который
    # возвращает price_oracle.get_asset_price_rub.
    async def fake_price(symbol: str, ts: int) -> float:
        return 250_000.0

    original = tax_calc.get_asset_price_rub
    tax_calc.get_asset_price_rub = fake_price

    errors: list[str] = []
    summary = None
    try:
        summary = await tax_calc.compute_year_report(owner, year)
    except TypeError as exc:
        errors.append(f"рыночная котировка роняет отчёт: {exc}")
    finally:
        tax_calc.get_asset_price_rub = original

    print()
    print("=" * 60)
    print("Регресс: рыночная котировка вместо ручной цены")
    print("=" * 60)

    if summary is not None:
        print(f"Доход: {summary.total_income_rub} RUB (ожидаем 500 000)")
        print(f"Warnings: {summary.warnings}")
        print(f"Skipped: {summary.skipped}")
        print()
        # 2 ETH × 250 000 = 500 000
        if summary.total_income_rub != 500000:
            errors.append(f"доход должен быть 500 000, не {summary.total_income_rub}")

    if not errors:
        print("✅ РЫНОЧНЫЙ ПУТЬ СЧИТАЕТСЯ")
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


async def regression_expenses() -> int:
    """Прочие расходы уменьшают базу (ст. 221 НК РФ).

    Раньше поле manual_costs в YearSummary было объявлено, но никогда не
    заполнялось: расходы майнера (электричество) и комиссии не попадали
    в базу вообще, и налог считался завышенным на всю их сумму.
    """
    import utils.database as db
    db.DB_PATH = TEST_DB
    db.init_db()

    from utils.crypto_db import (
        Chain, TxType, add_tax_expense, add_transaction, add_wallet,
        ensure_asset, total_tax_expenses,
    )
    import utils.tax_calc as tax_calc

    owner = 999003
    year = 2026

    ensure_asset("ETH", "Ethereum", chain=Chain.OTHER, decimals=18,
                 coingecko_id="ethereum")
    wallet = add_wallet(owner, Chain.OTHER, "0xexpenseaddress00000", "test")
    ts = int(time.mktime(time.strptime("2026-04-01 12:00:00",
                                        "%Y-%m-%d %H:%M:%S")))
    add_transaction(
        owner_id=owner, ts=ts, asset_symbol="ETH", tx_type=TxType.AIRDROP,
        amount="1", wallet_id=wallet.id, fiat_value_rub="200000",
        note="airdrop",
    )

    errors: list[str] = []
    summary = await tax_calc.compute_year_report(owner, year)
    if summary.manual_costs != 0:
        errors.append(f"без расходов manual_costs должен быть 0, "
                      f"а он {summary.manual_costs}")
    base_without = summary.taxable_income_rub
    if base_without != 200000:
        errors.append(f"база без расходов должна быть 200 000, "
                      f"а она {base_without}")

    exp_id = add_tax_expense(owner, year, "50000", "электричество",
                             "пул за апрель")
    if total_tax_expenses(owner, year) != 50000:
        errors.append("сумма расходов не посчиталась")

    summary = await tax_calc.compute_year_report(owner, year)
    if summary.manual_costs != 50000:
        errors.append(f"расходы должны быть 50 000, а {summary.manual_costs}")
    if summary.taxable_income_rub != 150000:
        errors.append(f"база должна уменьшиться до 150 000, "
                      f"а она {summary.taxable_income_rub}")
    if not summary.expenses:
        errors.append("расходы должны попасть в отчёт (расшифровка)")

    print()
    print("=" * 60)
    print("Расходы, уменьшающие базу")
    print("=" * 60)
    print(f"Доход: {summary.total_income_rub} RUB")
    print(f"Расходы: {summary.manual_costs} RUB")
    print(f"База: {summary.taxable_income_rub} RUB")

    if not errors:
        print("✅ РАСХОДЫ УМЕНЬШАЮТ БАЗУ")
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
    rc = asyncio.run(main())
    rc |= asyncio.run(regression_market_quote())
    rc |= asyncio.run(regression_expenses())
    sys.exit(rc)