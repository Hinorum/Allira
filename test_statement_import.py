"""Тесты разбора выписок бирж (utils/statement_import).

Проверяется то, от чего зависит налоговая база:
  * выписка читается и в CSV, и в Excel, и через «;», и с запятыми как
    десятичным разделителем;
  * заголовки находятся сами: русские, английские, произвольный порядок
    колонок; «Order Type» не перебивает колонку «Side»;
  * покупка и продажа распознаются, quote-валюта отделяется от базовой;
  * ввод и вывод с биржи распознаются как перемещения, а не как доход;
  * отменённые сделки не попадают в базу;
  * ключ external_id стабилен — повторный импорт не создаёт дублей.
"""

import io
import os
import sys
import tempfile

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


BYBIT_CSV = """utcTime,symbol,side,qty,price,fee
2025-03-01 10:00:00,BTCUSDT,Buy,0.5,60000,0.1
2025-04-10 12:30:00,BTCUSDT,Sell,0.2,70000,0.04
"""

OKX_CSV = """cTime,instId,side,fillSz,fillPx,fee
2025-03-01 10:00:00,BTC-USDT,buy,0.5,60000,-0.1
2025-04-10 12:30:00,BTC-USDT,sell,0.2,70000,-0.04
"""

# Русская выписка, произвольный порядок колонок.
RU_CSV = """Торговая пара;Цена;Дата и время;Тип операции;Количество
BTC/USDT;60000;2025-03-01 10:00:00;Покупка;0,5
ETHUSDT;3000;2025-05-02 11:00:00;Продажа;1,25
"""

# Binance-подобный экспорт: есть и «Order Type», и «Side».
BINANCE_CSV = """Date(UTC),Pair,Order Type,Side,Amount,Price,Total,Status
2025-03-01 10:00:00,BTCUSDT,LIMIT,Buy,0.5,60000,30000,Filled
2025-04-10 12:30:00,BTCUSDT,LIMIT,Sell,0.2,70000,14000,Filled
2025-05-01 09:00:00,BTCUSDT,LIMIT,Buy,1.0,72000,72000,Cancelled
"""

TRANSFERS_CSV = """Date,Type,Coin,Amount,Status
2025-03-05 08:00:00,Deposit,USDT,1000,Success
2025-03-06 08:00:00,Withdrawal,BTC,0.5,Success
2025-03-07 08:00:00,Fee,USDT,0.5,Success
"""


def main() -> int:
    from utils.crypto_db import TxType
    from utils.statement_import import (detect_format, map_columns,
                                        parse_statement, split_pair)

    print("=" * 60)
    print("Тест разбора выписок бирж")
    print("=" * 60)

    # --- Bybit.
    rows, errors = parse_statement(BYBIT_CSV.encode(), "bybit.csv")
    check(len(rows) == 2, "Bybit: 2 строки", f"получено {len(rows)} {errors}")
    if rows:
        check(rows[0].asset == "BTC", "Bybit: базовый актив BTC", rows[0].asset)
        check(rows[0].tx_type == TxType.BUY, "Bybit: покупка", rows[0].tx_type)
        check(rows[0].amount == "0.5", "Bybit: количество 0.5", rows[0].amount)
        check(rows[0].quote_asset == "USDT", "Bybit: quote USDT",
              rows[0].quote_asset)
        check(rows[0].quote_amount == "30000", "Bybit: сумма 30000",
              rows[0].quote_amount)
        check(rows[0].source == "bybit", "Bybit: источник bybit", rows[0].source)
        check(rows[1].tx_type == TxType.SELL, "Bybit: продажа", rows[1].tx_type)
        check(rows[0].fiat_value_rub == "" and rows[0].cost_basis_rub == "",
              "Bybit: рубли не подставлены вручную (переведёт price_oracle)")

    # --- OKX.
    rows, errors = parse_statement(OKX_CSV.encode(), "okx.csv")
    check(len(rows) == 2, "OKX: 2 строки", f"получено {len(rows)} {errors}")
    if rows:
        check(rows[0].asset == "BTC" and rows[0].quote_asset == "USDT",
              "OKX: пара BTC/USDT", f"{rows[0].asset}/{rows[0].quote_asset}")
        check(rows[0].tx_type == TxType.BUY, "OKX: покупка", rows[0].tx_type)
        check(rows[1].tx_type == TxType.SELL, "OKX: продажа", rows[1].tx_type)

    # --- Русская выписка через «;» и запятую как десятичный разделитель.
    rows, errors = parse_statement(RU_CSV.encode(), "ru.csv")
    check(len(rows) == 2, "Русская выписка: 2 строки",
          f"получено {len(rows)} {errors}")
    if rows:
        check(rows[0].asset == "BTC", "RU: актив BTC", rows[0].asset)
        check(rows[0].amount == "0.5", "RU: «0,5» → 0.5", rows[0].amount)
        check(rows[0].quote_asset == "USDT", "RU: quote USDT", rows[0].quote_asset)
        check(rows[0].tx_type == TxType.BUY, "RU: «Покупка» → buy",
              rows[0].tx_type)
        check(rows[1].tx_type == TxType.SELL, "RU: «Продажа» → sell",
              rows[1].tx_type)

    # --- Binance: «Order Type» не должен перебивать «Side».
    rows, errors = parse_statement(BINANCE_CSV.encode(), "binance.csv")
    check(len(rows) == 2, "Binance: отменённая сделка отброшена",
          f"получено {len(rows)} {errors}")
    if rows:
        check(rows[0].tx_type == TxType.BUY, "Binance: Buy → buy",
              rows[0].tx_type)
        check(rows[1].tx_type == TxType.SELL, "Binance: Sell → sell",
              rows[1].tx_type)
        check(rows[0].quote_amount == "30000", "Binance: сумма из Total",
              rows[0].quote_amount)

    # --- Вводы и выводы — перемещения, а не доход.
    rows, errors = parse_statement(TRANSFERS_CSV.encode(), "transfers.csv")
    kinds = {r.tx_type for r in rows}
    check(TxType.TRANSFER_IN in kinds, "Ввод распознан как перевод",
          str(kinds))
    check(TxType.TRANSFER_OUT in kinds, "Вывод распознан как перевод",
          str(kinds))
    for r in rows:
        if r.tx_type in (TxType.TRANSFER_IN, TxType.TRANSFER_OUT):
            check(not r.quote_asset and not r.quote_amount,
                  "Перевод без quote-ноги", f"{r.asset} {r.quote_asset}")

    # --- Excel.
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["utcTime", "symbol", "side", "qty", "price"])
        ws.append(["2025-03-01 10:00:00", "ETHUSDT", "Buy", "2", "3000"])
        buf = io.BytesIO()
        wb.save(buf)
        rows, errors = parse_statement(buf.getvalue(), "bybit.xlsx")
        check(len(rows) == 1, "Excel: строка прочитана",
              f"получено {len(rows)} {errors}")
        if rows:
            check(rows[0].asset == "ETH" and rows[0].amount == "2",
                  "Excel: актив и количество", f"{rows[0].asset} {rows[0].amount}")
    except ImportError:
        print("  ⚠ openpyxl нет — проверка Excel пропущена")

    # --- Разбор пары.
    check(split_pair("BTCUSDT") == ("BTC", "USDT"), "Пара BTCUSDT")
    check(split_pair("BTC-USDT") == ("BTC", "USDT"), "Пара BTC-USDT")
    check(split_pair("ETHBTC") == ("ETH", "BTC"), "Пара ETHBTC")

    # --- Колонки находятся сами; «Order Type» не перебивает «Side».
    cols = map_columns(["Order Type", "Side", "Amount", "Price"])
    check(cols.get("side") == 1, "map_columns: Side не перебит Order Type",
          str(cols))
    check(cols.get("amount") == 2, "map_columns: количество найдено", str(cols))

    # --- Определение формата.
    check(detect_format(["utcTime", "symbol", "side"]) == "bybit",
          "detect_format: bybit")
    check(detect_format(["cTime", "instId", "side"]) == "okx",
          "detect_format: okx")
    check(detect_format(["ts", "asset", "type"]) == "internal",
          "detect_format: внутренний")
    check(detect_format(["Дата и время", "Торговая пара", "Тип операции"])
          == "generic", "detect_format: универсальный")

    # --- Ключ дедупликации стабилен.
    first, _ = parse_statement(BYBIT_CSV.encode(), "bybit.csv")
    second, _ = parse_statement(BYBIT_CSV.encode(), "bybit.csv")
    check(bool(first) and bool(second)
          and first[0].external_id == second[0].external_id,
          "external_id стабилен между прогонами")

    # --- Пустой файл.
    rows, errors = parse_statement(b"", "empty.csv")
    check(not rows and errors, "Пустой файл: понятная ошибка", str(errors))

    print()
    if not FAILURES:
        print("OK: ALL CHECKS PASSED")
        code = 0
    else:
        print(f"FAIL: {len(FAILURES)} проверок не прошли:")
        for f in FAILURES:
            print(f"  - {f}")
        code = 1

    try:
        os.unlink(TEST_DB)
    except OSError:
        pass
    return code


if __name__ == "__main__":
    sys.exit(main())
