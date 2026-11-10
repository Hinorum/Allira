"""Регрессии P0-набора: WAC, очистка памяти, /health, /backup, seed настроек.

Каждый сценарий проверяет исправление, которое легко «потерять» при
рефакторинге:

  1. FIFO и WAC дают разную себестоимость на одной и той же истории;
     WAC-остаток переоценивается по средней, а не по цене последнего лота.
  2. purge_stale_user_data не удаляет состояния без метки активности.
  3. mark_activity ставит метку на любом апдейте.
  4. /health отдаёт только {"status": "ok"} — без статистики и секретов.
  5. /backup делает согласованный снимок SQLite, пригодный для чтения.
  6. seed_tax_settings заполняет новую строку и не затирает ручные настройки.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from decimal import Decimal
from types import SimpleNamespace

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
    return int(time.mktime(time.strptime(value, "%Y-%m-%d %H:%M:%S")))


async def test_cost_basis_methods() -> None:
    """FIFO: 2 000 000; WAC: 2 250 000 на одной истории."""
    from utils.crypto_db import (
        Chain, TxType, add_transaction, add_wallet, ensure_asset,
        get_tax_settings, set_tax_settings,
    )
    from utils.tax_calc import compute_year_report, rebuild_lots

    print("\n1. FIFO и WAC на одной истории")
    owner = 990001
    year = 2025

    ensure_asset("BTC", "Bitcoin", chain=Chain.OTHER, decimals=8,
                 coingecko_id="bitcoin")
    wallet = add_wallet(owner, Chain.OTHER, "bc1qwacaddress00000000", "wac")

    add_transaction(
        owner_id=owner, ts=_ts("2025-01-15 10:00:00"), asset_symbol="BTC",
        tx_type=TxType.TRANSFER_IN, amount="1", wallet_id=wallet.id,
        cost_basis_rub="1000000", note="buy 1",
    )
    add_transaction(
        owner_id=owner, ts=_ts("2025-03-10 11:00:00"), asset_symbol="BTC",
        tx_type=TxType.TRANSFER_IN, amount="1", wallet_id=wallet.id,
        cost_basis_rub="2000000", note="buy 2",
    )
    add_transaction(
        owner_id=owner, ts=_ts("2025-06-20 12:00:00"), asset_symbol="BTC",
        tx_type=TxType.SELL, amount="1.5", wallet_id=wallet.id,
        fiat_value_rub="4500000", note="sell 1.5",
    )

    set_tax_settings(owner, cost_basis="fifo")
    fifo = await compute_year_report(owner, year)
    check(fifo.total_cost_rub == Decimal("2000000"),
          "FIFO: себестоимость 2 000 000", str(fifo.total_cost_rub))
    check(fifo.realized_gain_rub == Decimal("2500000"),
          "FIFO: прибыль 2 500 000", str(fifo.realized_gain_rub))

    set_tax_settings(owner, cost_basis="wac")
    check(get_tax_settings(owner).get("cost_basis") == "wac",
          "метод WAC сохранён в tax_settings")
    wac = await compute_year_report(owner, year)
    check(wac.total_cost_rub == Decimal("2250000"),
          "WAC: себестоимость 2 250 000", str(wac.total_cost_rub))
    check(wac.realized_gain_rub == Decimal("2250000"),
          "WAC: прибыль 2 250 000", str(wac.realized_gain_rub))

    # Отчёт за прошлый год не перезаписывает текущий баланс (см.
    # compute_year_report), поэтому остаток читаем после явной
    # пересборки — именно её делает /wallet_list и /balance.
    rebuild_lots(owner)

    from utils.database import get_db
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT qty_remaining, cost_per_unit_rub
            FROM tax_lots WHERE owner_id = ?
            """,
            (owner,),
        ).fetchone()
    qty = Decimal(str(row["qty_remaining"])) if row else Decimal(0)
    cost = Decimal(str(row["cost_per_unit_rub"])) if row else Decimal(0)
    check(qty == Decimal("50000000"),
          "WAC: осталось 0.5 BTC", str(qty))
    check(cost == Decimal("1500000"),
          "WAC: остаток переоценён по средней 1 500 000", str(cost))


def test_purge_and_activity() -> None:
    """Старые профили удаляются, состояния без метки — нет."""
    import main

    print("\n2. Очистка user_data и метка активности")

    class FakeApplication:
        def __init__(self):
            self.user_data = {}
            self.dropped: list[int] = []

        def drop_user_data(self, user_id):
            self.dropped.append(user_id)
            self.user_data.pop(user_id, None)

    now = time.time()
    app = FakeApplication()
    app.user_data[1] = {main.LAST_SEEN_KEY: now}                    # свежий
    app.user_data[2] = {main.LAST_SEEN_KEY: now - 8 * 86400}        # старый
    app.user_data[3] = {"wizard": "address"}                        # без метки
    app.user_data[4] = "not-a-dict"                                 # мусор

    context = SimpleNamespace(application=app)
    asyncio.run(main.purge_stale_user_data(context))

    check(1 in app.user_data, "свежий профиль сохранён")
    check(2 not in app.user_data and app.dropped == [2],
          "старый профиль удалён", str(app.dropped))
    check(3 in app.user_data and app.user_data[3]["wizard"] == "address",
          "состояние без метки не удалено")
    check(4 in app.user_data, "не-dict запись не тронута")

    from handlers.message_handler import LAST_SEEN_KEY, mark_activity

    data: dict = {}
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42))
    asyncio.run(mark_activity(update, SimpleNamespace(user_data=data)))
    marker = data.get(LAST_SEEN_KEY)
    check(isinstance(marker, (int, float)) and abs(marker - time.time()) < 5,
          "mark_activity ставит метку", str(marker))

    empty = {}
    asyncio.run(mark_activity(SimpleNamespace(effective_user=None),
                              SimpleNamespace(user_data=empty)))
    check(LAST_SEEN_KEY not in empty, "апдейт без юзера не создаёт метку")


def test_health_is_minimal() -> None:
    """Публичный /health не отдаёт статистику и тексты ошибок."""
    print("\n3. Минимальный /health")
    import main

    payload = main._build_health_payload()
    check(payload == {"status": "ok"},
          "/health содержит только status", str(payload))
    check("total_users" not in payload and "rent_events" not in payload
          and "http_error" not in payload,
          "/health без диагностики и секретов")


def test_backup_is_readable() -> None:
    """Снимок базы открывается и содержит те же операции."""
    from handlers.backup_command import create_backup
    from utils import database

    print("\n4. Резервная копия SQLite")
    with database.get_db() as conn:
        source_tx = conn.execute(
            "SELECT COUNT(*) AS n FROM crypto_transactions"
        ).fetchone()["n"]

    backup_path = create_backup()
    try:
        check(os.path.exists(backup_path) and os.path.getsize(backup_path) > 0,
              "файл копии создан")
        with sqlite3.connect(backup_path) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            backup_tx = conn.execute(
                "SELECT COUNT(*) FROM crypto_transactions"
            ).fetchone()[0]
        check(integrity == "ok", "PRAGMA integrity_check = ok", integrity)
        check(backup_tx == source_tx,
              "число операций в копии совпадает", f"{backup_tx} != {source_tx}")
    finally:
        try:
            os.unlink(backup_path)
        except OSError:
            pass


def test_seed_tax_settings() -> None:
    """Env заполняет только новую строку, ручные настройки не затираются."""
    from utils.crypto_db import get_tax_settings, seed_tax_settings, set_tax_settings

    print("\n5. Начальные настройки из env")
    owner = 990005

    seed_tax_settings(owner, cost_basis="wac", tax_status="resident",
                      taxpayer_inn="770000000001")
    seeded = get_tax_settings(owner)
    check(seeded.get("cost_basis") == "wac",
          "новая строка получила cost_basis=wac", str(seeded.get("cost_basis")))
    check(seeded.get("taxpayer_inn") == "770000000001",
          "новая строка получила ИНН")

    set_tax_settings(owner, cost_basis="fifo", taxpayer_inn="770000000002")
    seed_tax_settings(owner, cost_basis="wac", taxpayer_inn="770000000003")
    kept = get_tax_settings(owner)
    check(kept.get("cost_basis") == "fifo",
          "повторный seed не затирает ручной FIFO", str(kept.get("cost_basis")))
    check(kept.get("taxpayer_inn") == "770000000002",
          "повторный seed не затирает ручной ИНН", str(kept.get("taxpayer_inn")))


def main() -> int:
    import utils.database as db

    db.DB_PATH = TEST_DB
    db.init_db()

    print("=" * 60)
    print("Регрессии P0: WAC, purge, /health, /backup, seed")
    print("=" * 60)

    asyncio.run(test_cost_basis_methods())
    test_purge_and_activity()
    test_health_is_minimal()
    test_backup_is_readable()
    test_seed_tax_settings()

    db.close_all()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(TEST_DB + suffix)
        except OSError:
            pass

    print()
    if FAILURES:
        print(f"❌ ПРОВАЛЕНО ПРОВЕРОК: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        return 1

    print("✅ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
