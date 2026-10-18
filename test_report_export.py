"""Тест генерации PDF/XLSX/CSV из смоделированного summary."""
import asyncio
import io
import os
import sys
import tempfile
import time

TEST_DB = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = TEST_DB
sys.path.insert(0, ".")


async def main() -> int:
    import utils.database as db
    db.DB_PATH = TEST_DB
    db.init_db()

    from utils.crypto_db import Chain, TxType, add_transaction, add_wallet, ensure_asset
    from utils.tax_calc import compute_year_report
    from utils.report_export import render_pdf, render_xlsx, render_income_csv, render_disposal_csv

    owner = 999002
    year = 2025
    ensure_asset("BTC", "Bitcoin", chain=Chain.OTHER, decimals=8, coingecko_id="bitcoin")
    w = add_wallet(owner, Chain.OTHER, "bc1qtestfakeaddress", "test")
    t1 = int(time.mktime(time.strptime("2025-02-01 10:00:00", "%Y-%m-%d %H:%M:%S")))
    t2 = int(time.mktime(time.strptime("2025-08-15 12:00:00", "%Y-%m-%d %H:%M:%S")))
    add_transaction(owner_id=owner, ts=t1, asset_symbol="BTC", tx_type=TxType.TRANSFER_IN,
                    amount="0.5", wallet_id=w.id, cost_basis_rub="2500000")
    add_transaction(owner_id=owner, ts=t2, asset_symbol="BTC", tx_type=TxType.SELL,
                    amount="0.5", wallet_id=w.id, fiat_value_rub="3000000")

    summary = await compute_year_report(owner, year)
    settings = {"taxpayer_inn": "7707083893", "taxpayer_name": "Иванов И.И.",
                "tax_status": "resident", "cost_basis": "fifo"}

    pdf_bytes = render_pdf(summary, settings)
    xlsx_bytes = render_xlsx(summary, settings)
    income_csv = render_income_csv(summary)
    disposal_csv = render_disposal_csv(summary)

    print(f"PDF: {len(pdf_bytes)} bytes")
    print(f"XLSX: {len(xlsx_bytes)} bytes")
    print(f"Income CSV: {len(income_csv)} bytes ({income_csv.decode('utf-8-sig')[:200]})")
    print(f"Disposal CSV: {len(disposal_csv)} bytes ({disposal_csv.decode('utf-8-sig')[:200]})")

    errors = []
    if not pdf_bytes.startswith(b"%PDF"):
        errors.append(f"PDF не начинается с %PDF: {pdf_bytes[:8]!r}")
    # ZIP-сигнатура XLSX: PK\x03\x04
    if not xlsx_bytes.startswith(b"PK\x03\x04"):
        errors.append(f"XLSX не начинается с PK: {xlsx_bytes[:4]!r}")
    if len(income_csv) < 20:
        errors.append("income CSV подозрительно мал")
    if len(disposal_csv) < 20:
        errors.append("disposal CSV подозрительно мал")

    # Пишем файлы для глаз.
    with open("test_tax_report.pdf", "wb") as f:
        f.write(pdf_bytes)
    with open("test_tax_report.xlsx", "wb") as f:
        f.write(xlsx_bytes)
    with open("test_income.csv", "wb") as f:
        f.write(income_csv)
    with open("test_disposal.csv", "wb") as f:
        f.write(disposal_csv)

    if not errors:
        print("✅ Все форматы сгенерированы успешно")
        rc = 0
    else:
        print("❌ ОШИБКИ:", errors)
        rc = 1

    from utils import database as _db
    _db.close_all()
    try:
        os.unlink(TEST_DB)
    except OSError:
        pass
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))