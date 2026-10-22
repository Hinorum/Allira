"""Тесты пути «от операции до отчёта».

Проверяется сокращение пути:

  Ввод операции:
   1. /addtx без аргументов запускает пошаговую форму, а не справку.
   2. Свободный текст вместо команды предлагает разобрать список.
   3. Разбор ответа модели: валидные операции берутся, мусор отбрасывается.
   4. Цифры не разъезжаются: «2.4 млн» → 2400000, «0.5» → 0.5.
   5. Даты разбираются в любом написании, плохие — отбрасываются.
   6. Показ предпросмотра показывает, что именно будет записано.

  Отчёт:
   7. Без ИНН и ФИО бот прямо говорит, чего не хватает, и даёт кнопку.
   8. В сводке есть кнопки выгрузки, сверки и смены года.
   9. Форма настройки ФНС идёт по шагам и сохраняет значения.
  10. Разбор списка записывает операции в базу.
"""

import asyncio
import json
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


class FakeUser:
    def __init__(self, uid=1, username="owner"):
        self.id = uid
        self.username = username
        self.first_name = "Владелец"


class FakeMessage:
    def __init__(self, chat_id=-100):
        self.chat_id = chat_id
        self.message_id = 1
        self.text = ""
        self.replies = []
        self.edits = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return type("M", (), {"message_id": 99})()

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeQuery:
    def __init__(self, data, message=None, user=None):
        self.data = data
        self.message = message or FakeMessage()
        self.from_user = user or FakeUser()
        self.answers = []

    async def answer(self, text="", **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.message.edits.append((text, kwargs))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return type("M", (), {"message_id": 100})()

    async def send_document(self, **kwargs):
        self.sent.append(kwargs)


class FakeContext:
    def __init__(self, bot_data=None):
        self.bot_data = bot_data or {}
        self.bot_data.setdefault("CRYPTO_OWNER_ID", 555)
        self.bot_data.setdefault("CREATOR_USER_IDS", (1,))
        self.bot_data.setdefault("DEFAULT_MODEL", "test")
        self.bot_data.setdefault("OPENROUTER_API_KEY", "test")
        self.user_data = {}
        self.args = []
        self.bot = FakeBot()


async def main() -> int:
    import utils.database as db

    db.DB_PATH = TEST_DB
    db.init_db()

    from handlers import crypto_tax, tx_wizard
    from handlers.tx_wizard import (
        _parse_operations_payload, format_preview, normalize_type, parse_date,
    )

    # ------------------------------------------------------ 1. форма вместо справки
    print("1. /addtx без аргументов открывает форму")
    ctx = FakeContext()
    message = FakeMessage()
    update = type("U", (), {"message": message, "callback_query": None,
                             "effective_user": FakeUser()})()
    await tx_wizard.start_wizard(update, ctx)
    check(bool(message.replies), "форма открылась", str(message.replies)[:80])
    if message.replies:
        text, kwargs = message.replies[0]
        check("тип операции" in text, "спрашивает тип операции", text[:80])
        markup = kwargs.get("reply_markup")
        check(markup is not None and len(markup.inline_keyboard) >= 3,
              "есть кнопки типов")
    check(ctx.user_data.get(tx_wizard.WIZARD_KEY, {}).get("step") == "type",
          "состояние мастера сохранено")

    # ------------------------------------------ 2. свободный текст → разбор
    print("2. Свободный текст предлагает разобрать список")
    ctx = FakeContext()
    message = FakeMessage()
    update = type("U", (), {"message": message, "callback_query": None,
                             "effective_user": FakeUser()})()
    await tx_wizard.start_bulk_flow(
        update, ctx, "15.01 получил 0.5 BTC, потом продал за 2.4 млн",
    )
    check(bool(message.replies), "бот предложил разбор", str(message.replies)[:80])
    if message.replies:
        _, kwargs = message.replies[0]
        markup = kwargs.get("reply_markup")
        labels = [b.text for row in markup.inline_keyboard for b in row] if markup else []
        check(any("Разобрать" in t for t in labels), "есть кнопка разбора", str(labels))

    # ------------------------------------------------ 3-4. разбор ответа модели
    print("3. Разбор ответа модели")
    payload = json.dumps({"operations": [
        {"type": "airdrop", "asset": "btc", "amount": "0.5",
         "date": "15.01.2026", "price_rub": "", "note": "за аирдроп"},
        {"type": "продал", "asset": "BTC", "amount": "0,3",
         "date": "20.06.2026", "price_rub": "2400000", "note": ""},
        {"type": "ерунда", "asset": "BTC", "amount": "1", "date": "", "price_rub": "", "note": ""},
        {"type": "sell", "asset": "", "amount": "1", "date": "", "price_rub": "", "note": ""},
        {"type": "sell", "asset": "BTC", "amount": "много", "date": "", "price_rub": "", "note": ""},
        "не объект",
    ]})
    ops = _parse_operations_payload(payload)
    check(len(ops) == 2, "остались только корректные операции", str(len(ops)))
    check(ops[0]["asset"] == "BTC", "символ в верхнем регистре", ops[0]["asset"])
    check(ops[1]["amount"] == "0.3", "запятая превращена в точку", ops[1]["amount"])
    check(ops[0]["type"] == "airdrop", "тип распознан")
    check(ops[1]["type"] == "sell", "человеческое «продал» → sell", ops[1]["type"])
    check(ops[1]["price_rub"] == "2400000", "цена сохранена", ops[1]["price_rub"])

    fenced = "```json\n" + payload + "\n```"
    check(len(_parse_operations_payload(fenced)) == 2,
          "JSON в код-блоке тоже разбирается")
    check(_parse_operations_payload("не json вовсе") == [],
          "мусор даёт пустой список")
    check(_parse_operations_payload('{"operations": []}') == [],
          "пустой список операций")

    print("4. Типы и даты")
    check(normalize_type("Аирдроп") == "airdrop", "русский тип")
    check(normalize_type("продал") == "sell", "синоним типа")
    check(normalize_type("airdrop") == "airdrop", "английский тип")
    check(normalize_type("ерунда") is None, "неизвестный тип → None")
    check(parse_date("15.01.2026") is not None, "дата ДД.ММ.ГГГГ")
    check(parse_date("2026-01-15") is not None, "дата ГГГГ-ММ-ДД")
    check(parse_date("сегодня") is not None, "«сегодня»")
    check(parse_date("вчера") is not None, "«вчера»")
    check(parse_date("ерунда") is None, "плохая дата → None")
    check(parse_date("32.13.2026") is None, "несуществующая дата → None")

    # --------------------------------------------------- 6. предпросмотр
    print("5. Предпросмотр")
    preview = format_preview(ops)
    check("Так я понял" in preview, "заголовок предпросмотра", preview[:60])
    check("0.5" in preview, "видно количество", preview[:200])
    check("2400000" in preview, "видно цену", preview[:200])
    check("2" in preview and "операций" in preview, "видно счётчик", preview[-80:])

    # ------------------------------------------- 10. массовая запись в базу
    print("6. Массовая запись")
    from utils.database import get_db

    ctx = FakeContext()
    query = FakeQuery("wiz:bulk_write")
    await tx_wizard.write_bulk(ctx, ops, query)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT tx_type, amount_raw FROM crypto_transactions"
        ).fetchall()
    check(len(rows) == 2, "обе операции записаны", str(len(rows)))
    types = {r["tx_type"] for r in rows}
    check(types == {"airdrop", "sell"}, "типы верные", str(types))

    # ---------------------------------------- 7-8. отчёт: кнопки и подсказка
    print("7. Отчёт: кнопки и подсказка про ИНН")
    from utils.crypto_db import get_tax_settings, set_tax_settings

    owner = 555
    set_tax_settings(owner, taxpayer_inn="", taxpayer_name="")
    settings = get_tax_settings(owner)
    check(not settings.get("taxpayer_inn"), "ИНН пуст", str(settings))

    ctx = FakeContext()
    message = FakeMessage()
    update = type("U", (), {"message": message, "callback_query": None,
                             "effective_user": FakeUser()})()
    # Подменяем расчёт — он ходит в сеть за котировками.
    import utils.tax_calc as tax_calc

    class FakeSummary:
        year = 2026
        total_income_rub = 0
        total_cost_rub = 0
        realized_gain_rub = 0
        taxable_income_rub = 0
        ndfl_total = 0
        income_events = []
        disposal_events = []
        warnings = []
        ndfl_breakdown = []
        skipped = []
        manual_costs = 0
        expenses = []

    async def fake_report(owner_id, year):
        return FakeSummary()

    original_report = tax_calc.compute_year_report
    crypto_tax.compute_year_report = fake_report
    try:
        await crypto_tax.tax_command(update, ctx)
    finally:
        crypto_tax.compute_year_report = original_report

    check(len(message.replies) >= 2, "бот ответил", str(len(message.replies)))
    if message.replies:
        first_text = message.replies[0][0]
        check("не хватает данных" in first_text,
              "предупредил, что не хватает ИНН", first_text[:120])
        markup = message.replies[0][1].get("reply_markup")
        labels = [b.text for row in markup.inline_keyboard for b in row] if markup else []
        check(any("ИНН" in t for t in labels), "дал кнопку заполнения ИНН", str(labels))

    summary_text = ctx.bot.sent[-1][1]
    check("Сводка 3-НДФЛ" in summary_text, "прислал сводку", summary_text[:80])
    check("Операций за этот год нет" in summary_text,
          "объяснил, что операций нет", summary_text[:200])
    markup = ctx.bot.sent[-1][2].get("reply_markup")
    labels = [b.text for row in markup.inline_keyboard for b in row] if markup else []
    check(any("PDF" in t for t in labels), "есть кнопка PDF", str(labels))
    check(any("XLSX" in t for t in labels), "есть кнопка XLSX", str(labels))
    check(any("CSV" in t for t in labels), "есть кнопки CSV", str(labels))
    check(any("Сверить" in t for t in labels), "есть кнопка сверки", str(labels))
    check(any(t.startswith("📅") for t in labels), "есть кнопки смены года", str(labels))

    # ---------------------------------------- 9. форма настройки ФНС
    print("8. Форма настройки ФНС")
    from telegram.ext import ApplicationHandlerStop

    ctx = FakeContext()
    message = FakeMessage()
    await crypto_tax.start_tax_setup_wizard(message, ctx)
    check(bool(message.replies), "форма открылась")
    check(ctx.user_data.get(crypto_tax.SETUP_KEY, {}).get("step") == "inn",
          "первый шаг — ИНН")

    # Неверный ИНН: форма не должна переходить дальше.
    bad = FakeMessage()
    bad.text = "123"
    try:
        await crypto_tax.tax_setup_text_handler(
            type("U", (), {"message": bad})(), ctx,
        )
    except ApplicationHandlerStop:
        pass
    check(bool(bad.replies), "на неверный ИНН ответил", str(bad.replies)[:80])
    check(ctx.user_data[crypto_tax.SETUP_KEY].get("step") == "inn",
          "шаг не сдвинулся", str(ctx.user_data.get(crypto_tax.SETUP_KEY)))

    # Ввод ИНН
    message2 = FakeMessage()
    message2.text = "770708389300"
    try:
        await crypto_tax.tax_setup_text_handler(
            type("U", (), {"message": message2})(), ctx,
        )
        stopped = False
    except ApplicationHandlerStop:
        stopped = True
    check(stopped, "обработка прервана — текст не уйдёт в ИИ-чат")
    check(ctx.user_data[crypto_tax.SETUP_KEY].get("inn") == "770708389300",
          "ИНН сохранён", str(ctx.user_data.get(crypto_tax.SETUP_KEY)))
    check(ctx.user_data[crypto_tax.SETUP_KEY].get("step") == "name",
          "перешли к ФИО", str(ctx.user_data.get(crypto_tax.SETUP_KEY)))

    # ФИО
    message4 = FakeMessage()
    message4.text = "Иванов Иван Иванович"
    try:
        await crypto_tax.tax_setup_text_handler(
            type("U", (), {"message": message4})(), ctx,
        )
    except ApplicationHandlerStop:
        pass
    check(ctx.user_data[crypto_tax.SETUP_KEY].get("name") == "Иванов Иван Иванович",
          "ФИО сохранено", str(ctx.user_data.get(crypto_tax.SETUP_KEY)))


    # Статус кнопкой
    query = FakeQuery("taxsetup:status:miner")
    await crypto_tax.tax_setup_callback(
        type("U", (), {"callback_query": query})(), ctx,
    )
    settings = get_tax_settings(555)
    check(settings.get("taxpayer_inn") == "770708389300", "ИНН в базе", str(settings))
    check(settings.get("taxpayer_name") == "Иванов Иван Иванович", "ФИО в базе")
    check(settings.get("tax_status") == "miner", "статус в базе", str(settings))
    check(crypto_tax.SETUP_KEY not in ctx.user_data, "состояние формы убрано")

    # ---------------------------------------- 10. полный проход по мастеру
    print("9. Полный проход по мастеру: кнопки → текст → запись")
    ctx = FakeContext()
    message = FakeMessage()
    await tx_wizard.start_wizard(
        type("U", (), {"message": message, "callback_query": None,
                       "effective_user": FakeUser()})(),
        ctx,
    )

    async def press(data: str):
        query = FakeQuery(data)
        handled = await tx_wizard.wizard_callback(
            type("U", (), {"callback_query": query})(), ctx,
        )
        return query, handled

    async def type_text(text: str):
        msg = FakeMessage()
        msg.text = text
        try:
            await tx_wizard.wizard_text_handler(
                type("U", (), {"message": msg})(), ctx,
            )
            stopped = False
        except ApplicationHandlerStop:
            stopped = True
        return msg, stopped

    from telegram.ext import ApplicationHandlerStop

    query, handled = await press("wiz:type:airdrop")
    check(handled and ctx.user_data[tx_wizard.WIZARD_KEY].get("type") == "airdrop",
          "шаг 1: тип выбран кнопкой")
    check(bool(query.message.edits), "шаг 1: показали следующий вопрос",
          str(query.message.edits)[:80])

    msg, stopped = await type_text("btc")
    check(stopped, "шаг 2: текст перехвачен")
    check(ctx.user_data[tx_wizard.WIZARD_KEY].get("asset") == "BTC",
          "шаг 2: символ в верхнем регистре",
          str(ctx.user_data[tx_wizard.WIZARD_KEY]))

    msg, stopped = await type_text("0.5")
    check(ctx.user_data[tx_wizard.WIZARD_KEY].get("amount") == "0.5",
          "шаг 3: количество принято", str(ctx.user_data[tx_wizard.WIZARD_KEY]))

    await press("wiz:date_today")
    check(ctx.user_data[tx_wizard.WIZARD_KEY].get("step") == "price",
          "шаг 4: дата кнопкой", str(ctx.user_data[tx_wizard.WIZARD_KEY]))

    msg, stopped = await type_text("2000000")
    check(ctx.user_data[tx_wizard.WIZARD_KEY].get("price") == "2000000",
          "шаг 5: цена принята", str(ctx.user_data[tx_wizard.WIZARD_KEY]))

    msg, stopped = await type_text("тестовый аирдроп")
    check(ctx.user_data[tx_wizard.WIZARD_KEY].get("note") == "тестовый аирдроп",
          "шаг 6: комментарий принят", str(ctx.user_data[tx_wizard.WIZARD_KEY]))
    check(ctx.user_data[tx_wizard.WIZARD_KEY].get("step") == "confirm",
          "дошли до подтверждения", str(ctx.user_data[tx_wizard.WIZARD_KEY]))

    await press("wiz:confirm")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT tx_type, amount_raw, fiat_value_rub, note "
            "FROM crypto_transactions ORDER BY id DESC LIMIT 1"
        ).fetchall()
    check(len(rows) == 1, "операция записана", str(len(rows)))
    if rows:
        check(rows[0]["tx_type"] == "airdrop", "тип записан", rows[0]["tx_type"])
        check(rows[0]["fiat_value_rub"] == "2000000", "цена записана",
              str(rows[0]["fiat_value_rub"]))
        check(rows[0]["note"] == "тестовый аирдроп", "комментарий записан",
              str(rows[0]["note"]))
    check(tx_wizard.WIZARD_KEY not in ctx.user_data, "состояние мастера убрано")

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
