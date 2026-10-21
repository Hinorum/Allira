"""Тесты правок в учёте, налогах и админ-панели.

Проверяются баги, найденные при разборе кода:

  Учёт и налоги:
   1. CSV-импорт не пишет quote-валюту (USDT) в рублёвые поля.
   2. Импорт переводит quote в рубли по курсу на дату сделки.
   3. Дубликаты не склеиваются между владельцами учёта.
   4. Дубль считается дублем, а не «сохранённой» записью.
   5. Не-числовая или отрицательная денежная сумма не попадает в базу.
   6. Доходный актив не получает себестоимость, равную своему доходу.
   7. Майнер: доход от майнинга считается по отдельной шкале.

  Админ-панель:
   8. Токен проверяется строгим равенством (подстрока не проходит).
   9. Без токена панель отдаёт 404.
  10. Отрицательный limit не снимает ограничение выборки.
  11. Статус фоновых задач не пустой (next_t — datetime, а не число).
  12. Меню владельца закрыто, если владелец не настроен.
"""

import asyncio
import json
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


OWNER = 424242


async def main() -> int:
    import utils.database as db

    db.DB_PATH = TEST_DB
    db.init_db()

    from utils.crypto_db import (
        add_transaction, ensure_asset, find_duplicate, get_transaction,
        CryptoError, TxType,
    )
    from utils.csv_import import parse_csv, _safe_mul, _split_bybit, _split_okx
    from tasks import cex_sync

    # --------------------------------------------- 1. CSV: quote ≠ рубли
    print("1. CSV-импорт: quote-валюта не попадает в рублёвые поля")
    csv_bybit = (
        b"utcTime,symbol,side,qty,price,fee,orderType\n"
        b"2025-01-15 10:00:00,BTCUSDT,Buy,0.5,60000,10,Limit\n"
        b"2025-01-16 11:00:00,BTCUSDT,Sell,0.5,80000,12,Limit\n"
    )
    rows, errors = parse_csv(csv_bybit)
    check(not errors, "CSV разобран без ошибок", str(errors))
    check(len(rows) == 2, "обе строки распознаны", str(len(rows)))
    buy = rows[0]
    check(buy.tx_type == TxType.BUY, "покупка помечена как BUY", buy.tx_type)
    check(buy.quote_asset == "USDT", "quote-валюта извлечена", buy.quote_asset)
    check(buy.quote_amount == "30000", "сумма в quote посчитана", buy.quote_amount)
    check(buy.cost_basis_rub == "", "рублёвая себестоимость пуста", buy.cost_basis_rub)
    check(buy.fiat_value_rub == "", "рублёвая выручка пуста", buy.fiat_value_rub)
    sell = rows[1]
    check(sell.fiat_value_rub == "", "у продажи рублёвая выручка пуста", sell.fiat_value_rub)

    check(_split_bybit("BTCUSDT") == ("BTC", "USDT"), "разбор пары Bybit")
    check(_split_okx("BTC-USDT-SWAP") == ("BTC", "USDT"), "разбор пары OKX")
    check(_safe_mul("0.1", "3") == "0.3", "умножение без float-мусора", _safe_mul("0.1", "3"))

    # --------------------------------- 2. Импорт переводит quote в рубли
    print("2. Импорт: quote переводится в рубли по курсу")
    original_price = cex_sync.get_asset_price_rub

    async def fake_price(asset, ts):
        return 80.0 if str(asset).upper() == "USDT" else None

    cex_sync.get_asset_price_rub = fake_price
    try:
        saved, dups = await cex_sync.persist_cex_rows(OWNER, rows, "csv")
    finally:
        cex_sync.get_asset_price_rub = original_price
    check(saved == 4, "сохранены 2 сделки и 2 парные ноги", str(saved))
    check(dups == 0, "дублей нет", str(dups))

    with db.get_db() as conn:
        tx_rows = conn.execute(
            "SELECT t.tx_type, t.fiat_value_rub, t.cost_basis_rub, t.source, "
            "a.symbol FROM crypto_transactions t "
            "JOIN crypto_assets a ON a.id=t.asset_id ORDER BY t.id"
        ).fetchall()
    by_type = {r["symbol"] + ":" + r["tx_type"]: r for r in tx_rows}
    buy_row = by_type.get("BTC:buy")
    check(buy_row is not None, "покупка записана")
    if buy_row:
        check(buy_row["cost_basis_rub"] == "2400000.00",
              "себестоимость в рублях (0.5×60000×80)", str(buy_row["cost_basis_rub"]))
    sell_row = by_type.get("BTC:sell")
    if sell_row:
        check(sell_row["fiat_value_rub"] == "3200000.00",
              "выручка в рублях (0.5×80000×80)", str(sell_row["fiat_value_rub"]))
    check("USDT:trade_out" in by_type, "парная нога quote-валюты на покупке")
    check("USDT:trade_in" in by_type, "парная нога quote-валюты на продаже")
    check(all(r["source"] == "bybit" for r in tx_rows),
          "источник строки уважается", str({r["source"] for r in tx_rows}))

    # ------------------------------------------ 3. Дедуп по владельцу
    print("3. Дедупликация не склеивает владельцев")
    other_owner = OWNER + 1
    add_transaction(
        owner_id=other_owner, ts=1736924400, asset_symbol="ETH",
        tx_type=TxType.AIRDROP, amount="1", source="manual",
        note="чужой",
    )
    dup_id = find_duplicate(other_owner, "bybit", rows[0].external_id)
    check(dup_id is None,
          "у другого владельца нет дубля с тем же external_id", str(dup_id))
    own_dup = find_duplicate(OWNER, "bybit", rows[0].external_id)
    check(own_dup is not None, "у своего владельца дубль находится", str(own_dup))

    # -------------------------------- 4. Дубль считается дублем
    print("4. Повторный импорт считается дублем")
    saved2, dups2 = await cex_sync.persist_cex_rows(OWNER, rows, "csv")
    check(dups2 == 2, "повторный импорт дал два дубля", str(dups2))
    check(saved2 == 0, "повторный импорт ничего не сохранил", str(saved2))
    with db.get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM crypto_transactions WHERE owner_id=?",
            (OWNER,),
        ).fetchone()["c"]
    check(total == 4, "в базе по-прежнему 4 записи", str(total))

    # --------------------------------- 5. Валидация денежных полей
    print("5. Денежные поля валидируются")
    for bad, label in (("abc", "не-число"), ("-100", "отрицательное")):
        try:
            add_transaction(
                owner_id=OWNER, ts=1736924400, asset_symbol="BTC",
                tx_type=TxType.AIRDROP, amount="1",
                fiat_value_rub=bad, source="manual",
            )
            check(False, f"{label} отклоняется", f"{bad!r} принято")
        except CryptoError:
            check(True, f"{label} отклоняется")

    tx = add_transaction(
        owner_id=OWNER, ts=1736924400, asset_symbol="BTC",
        tx_type=TxType.AIRDROP, amount="1",
        fiat_value_rub="8 500", source="manual",
    )
    check(tx is not None and "8500" in str(tx.fiat_value_rub),
          "сумма с пробелом нормализуется", str(getattr(tx, "fiat_value_rub", None)))

    # -------------------------- 6. Доходный актив: нулевая себестоимость
    print("6. Доходный актив не получает себестоимость = своему доходу")
    from utils.tax_calc import _lot_cost_total
    from decimal import Decimal

    check(_lot_cost_total(TxType.AIRDROP, Decimal(0), Decimal("100000")) == 0,
          "у аирдропа себестоимость нулевая")
    check(_lot_cost_total(TxType.BUY, Decimal(0), Decimal("100000")) == Decimal("100000"),
          "у покупки себестоимость = цена")
    check(_lot_cost_total(TxType.AIRDROP, Decimal("5000"), Decimal("100000")) == Decimal("5000"),
          "явно заданная себестоимость приоритетна")

    # --------------------------------- 7. Майнер: отдельная шкала
    print("7. Майнер: раздельные шкалы")
    from utils.tax_calc import compute_ndfl, compute_ndfl_split

    tax_miner, breakdown = compute_ndfl_split(
        Decimal("3000000"), Decimal("3000000"), "miner")
    tax_common, _ = compute_ndfl(Decimal("6000000"), "miner")
    check(tax_miner == Decimal("804000.00"),
          "по раздельным шкалам 804 000", str(tax_miner))
    check(tax_common == Decimal("882000.00"),
          "по общей шкале было бы 882 000", str(tax_common))
    check(tax_miner < tax_common, "раздельные шкалы дают меньше налога")
    scales = {s.get("scale") for s in breakdown}
    check(scales == {"майнинг", "основная"},
          "в разбивке видны обе шкалы", str(scales))

    # ------------------------------------------ 8-10. Веб-панель
    print("8. Веб-панель: токен, 404, ограничения выборки")
    from utils import web_panel

    class MockHandler:
        def __init__(self, path, headers=None, body=b""):
            self.path = path
            self.headers = dict(headers or {})
            self.rfile = __import__("io").BytesIO(body)
            self.status = 0
            self.response = b""

        def _respond(self, status, body, content_type="text/plain"):
            self.status = status
            self.response = body.encode("utf-8")

    class StubConfig:
        bot_token = "x"
        crypto_owner_id = OWNER
        openrouter_api_key = ""
        tonapi_api_key = ""
        toncenter_api_key = ""
        marketapp_api_key = ""
        marketapp_wallet = ""
        bybit_api_key = ""
        bybit_api_secret = ""
        okx_api_key = ""
        okx_api_secret = ""
        okx_passphrase = ""
        admin_chat_id = ""
        creator_user_ids = ()
        news_channel_id = ""
        port = 10000
        taxpayer_inn = ""
        tax_status = "resident"
        cost_basis_method = "fifo"
        image_prompt_model = ""
        default_model = ""
        lane_model = ""
        vision_model = ""
        fallback_model = ""

    web_panel.configure(StubConfig(), None, time.time(), "secret-token")

    h = MockHandler("/admin/api/status", {"X-Admin-Token": "xxsecret-tokenyy"})
    web_panel.handle_get(h)
    check(h.status == 401, "токен-подстрока не проходит", str(h.status))

    h = MockHandler("/admin/api/status", {"X-Admin-Token": "Bearer secret-token"})
    web_panel.handle_get(h)
    check(h.status == 200, "Bearer-префикс принимается", str(h.status))

    h = MockHandler("/admin/api/status", {"X-Admin-Token": "secret"})
    web_panel.handle_get(h)
    check(h.status == 401, "усечённый токен не проходит", str(h.status))

    web_panel.configure(StubConfig(), None, time.time(), "")
    h = MockHandler("/admin")
    web_panel.handle_get(h)
    check(h.status == 404, "без токена панель отдаёт 404", str(h.status))
    h = MockHandler("/admin/api/status", {"X-Admin-Token": "anything"})
    web_panel.handle_get(h)
    check(h.status == 401, "без токена API отдаёт 401", str(h.status))

    web_panel.configure(StubConfig(), None, time.time(), "secret-token")
    check(web_panel._clamp(-1, 1, 200) == 1, "отрицательный limit → нижняя граница")
    check(web_panel._clamp(10_000, 1, 200) == 200, "огромный limit → верхняя граница")
    check(web_panel._clamp(50, 1, 200) == 50, "обычный limit без изменений")

    # ------------------------------------- 11. Статус фоновых задач
    print("9. Статус фоновых задач")
    from datetime import datetime, timezone
    from utils import bot_admin

    class FakeJob:
        def __init__(self, name, next_t):
            self.name = name
            self.next_t = next_t

    class FakeJobQueue:
        def __init__(self, jobs):
            self._jobs = jobs

        def jobs(self):
            return self._jobs

    class FakeApplication:
        def __init__(self, jobs):
            self.job_queue = FakeJobQueue(jobs)

    upcoming = datetime.now(timezone.utc) + __import__("datetime").timedelta(seconds=90)
    app = FakeApplication([
        FakeJob("cex_sync_hourly", upcoming),
        FakeJob("crypto_jetton_sync", None),
    ])
    jobs = bot_admin.jobs_status(app)
    check(len(jobs) == 2, "задачи видны", str(jobs))
    check(jobs[0]["next_run_in_s"] is not None,
          "время следующего запуска посчитано", str(jobs))
    if jobs[0]["next_run_in_s"] is not None:
        check(80 <= jobs[0]["next_run_in_s"] <= 100,
              "до запуска примерно 90 секунд", str(jobs[0]["next_run_in_s"]))

    # --------------------------------- 12. Меню владельца закрыто
    print("10. Меню владельца")
    from handlers import admin_menu

    class FakeUser:
        def __init__(self, uid, username=None):
            self.id = uid
            self.username = username
            self.first_name = "Юзер"

    class FakeUpdate:
        def __init__(self, user):
            self.effective_user = user
            self.message = None

    class FakeCtx:
        def __init__(self, bot_data):
            self.bot_data = bot_data

    # Ни ID, ни username не настроены — доступа быть не должно ни у кого.
    ctx = FakeCtx({"CREATOR_USER_IDS": (), "CREATOR_USERNAME": ""})
    allowed = await admin_menu._is_owner_chat(FakeUpdate(FakeUser(1)), ctx)
    check(not allowed, "без настройки владельца доступ закрыт")

    # Настроен только username — аккаунт БЕЗ username не проходит.
    ctx = FakeCtx({"CREATOR_USER_IDS": (), "CREATOR_USERNAME": "hinorum"})
    allowed = await admin_menu._is_owner_chat(FakeUpdate(FakeUser(1, None)), ctx)
    check(not allowed, "аккаунт без username не проходит")
    allowed = await admin_menu._is_owner_chat(FakeUpdate(FakeUser(1, "hinorum")), ctx)
    check(allowed, "владелец по username проходит")

    # Настроен только ID — username не важен.
    ctx = FakeCtx({"CREATOR_USER_IDS": (7,), "CREATOR_USERNAME": ""})
    allowed = await admin_menu._is_owner_chat(FakeUpdate(FakeUser(7, None)), ctx)
    check(allowed, "владелец по ID проходит")
    allowed = await admin_menu._is_owner_chat(FakeUpdate(FakeUser(8, None)), ctx)
    check(not allowed, "чужой ID не проходит")

    # ------------------------------------------- не-числовая сумма в меню
    print("11. Не-числовая сумма не роняет меню операций")
    check(admin_menu._rub_cell("8 500") != "", "сумма с пробелом показывается")
    check("abc" in admin_menu._rub_cell("abc"), "не-число показывается как есть")
    check(admin_menu._rub_cell("") == "", "пустая сумма не даёт хвост")

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
