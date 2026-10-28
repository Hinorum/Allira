"""Тесты источников отчёта и единого обновления.

Проверяется:

  1. Список адресов разбирается в любом виде: строки, запятые, маркеры,
     подписи до и после адреса.
  2. Пакетное добавление не падает на мусоре: валидные адреса пишутся,
     дубли и опечатки попадают в отчёт.
  3. Снимок источников считает кошельки, биржи, операции за год и
     спорные проводки; подсказка «чего не хватает» не врёт.
  4. sync_all собирает один отчёт из кошельков, бирж и сопоставления,
     а сбой источника не отменяет остальные.
  5. sync_cex берёт окно в миллисекундах эпохи, а не время event loop
     (раньше из-за этого since_ms был неверным).
  6. Приём списка адресов перехватывает текст только когда его ждёт,
     и не отдаёт адреса ИИ-чату.
"""

import asyncio
import os
import sys
import tempfile
import time
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

TON_A = "0:" + "a" * 64
TON_B = "0:" + "b" * 64
TON_C = "0:" + "c" * 64
TON_D = "0:" + "d" * 64
EVM_A = "0x" + "1" * 40
EVM_B = "0x" + "2" * 40
EVM_C = "0x" + "3" * 40
EVM_D = "0x" + "4" * 40
BTC_A = "bc1q" + "a" * 38
TRON_A = "T" + "1" * 33


def check(cond: bool, name: str, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def make_cfg(**over):
    base = dict(
        crypto_owner_id=1,
        bybit_api_key="", bybit_api_secret="",
        okx_api_key="", okx_api_secret="", okx_passphrase="",
    )
    base.update(over)
    return SimpleNamespace(**base)


class FakeUser:
    def __init__(self, uid=1, username="owner"):
        self.id = uid
        self.username = username
        self.first_name = "Владелец"


class FakeMessage:
    def __init__(self, text="", chat_id=1):
        self.text = text
        self.chat_id = chat_id
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=1)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=2)


class FakeContext:
    def __init__(self, owner):
        self.bot_data = {
            "CRYPTO_OWNER_ID": owner,
            "CREATOR_USER_IDS": (1,),
            "BOT_CONFIG": None,
        }
        self.user_data = {}
        self.bot = FakeBot()
        self.args = []


def _update(message):
    return SimpleNamespace(message=message, effective_user=FakeUser(),
                           callback_query=None)


async def main() -> int:
    import utils.database as db

    db.DB_PATH = TEST_DB
    db.init_db()

    from utils.sources import (
        AWAIT_WALLETS_KEY, add_wallets_from_text, parse_wallet_lines,
        sources_snapshot,
    )
    from utils.crypto_db import (TxType, add_transaction, add_wallet,
                                 list_wallets)

    print("=" * 60)
    print("Тест источников и общего обновления")
    print("=" * 60)

    # ------------------------------------------------- 1. разбор списка
    print("1. Разбор списка адресов")
    text = (
        f"- {TON_A} основной\n"
        f"{EVM_A} | биржа\n"
        f"основной2 {BTC_A}\n"
        f"1) {TRON_A}\n"
    )
    parsed = parse_wallet_lines(text)
    check(len(parsed) == 4, "четыре адреса из четырёх строк", str(parsed))
    check(parsed[0].address == TON_A and parsed[0].label == "основной",
          "подпись после адреса", str(parsed[0]))
    check(parsed[1].address == EVM_A and parsed[1].label == "биржа",
          "подпись через вертикальную черту", str(parsed[1]))
    check(parsed[2].address == BTC_A and parsed[2].label == "основной2",
          "подпись перед адресом", str(parsed[2]))
    check(parsed[3].address == TRON_A, "маркер списка «1)» убран",
          str(parsed[3]))

    comma = parse_wallet_lines(f"{EVM_A}, {EVM_B}; {EVM_C}")
    check([p.address for p in comma] == [EVM_A, EVM_B, EVM_C],
          "запятая и точка с запятой — разделители", str(comma))
    check(parse_wallet_lines("") == [], "пустой текст — пустой список")

    # --------------------------------------------- 2. пакетное добавление
    print("2. Пакетное добавление")
    owner = 2001
    result = add_wallets_from_text(owner, (
        f"{TON_A} основной\n"
        f"{EVM_A} | биржа\n"
        f"{TON_A} повтор\n"
        "просто текст\n"
    ))
    check(len(result.added) == 2, "два валидных адреса записаны",
          str([w.address for w in result.added]))
    check(len(result.duplicates) == 1, "повтор внутри сообщения — дубль",
          str(result.duplicates))
    check(len(result.invalid) == 1, "мусор попал в ошибки",
          str(result.invalid))
    check(len(list_wallets(owner)) == 2, "в базе ровно два кошелька",
          str(len(list_wallets(owner))))
    labels = sorted(w.label for w in list_wallets(owner))
    check(labels == ["биржа", "основной"], "подписи сохранены", str(labels))

    again = add_wallets_from_text(owner, f"{TON_A}\n{EVM_A}")
    check(not again.added and len(again.duplicates) == 2,
          "повторный ввод не создаёт второй кошелёк", str(again))

    # ------------------------------------------------- 3. снимок источников
    print("3. Снимок источников")
    owner = 3001
    empty_cfg = make_cfg(crypto_owner_id=owner)
    snap = sources_snapshot(owner, empty_cfg, year=2025)
    check(not snap.has_sources, "без кошельков и ключей источников нет")
    check(any("Нет источников" in m for m in snap.missing),
          "подсказка про источники", str(snap.missing))

    wallet = add_wallet(owner, "ton", TON_B, "мой")
    ts_2025 = int(time.mktime(time.strptime("2025-06-01 12:00:00",
                                            "%Y-%m-%d %H:%M:%S")))
    ts_2024 = int(time.mktime(time.strptime("2024-06-01 12:00:00",
                                            "%Y-%m-%d %H:%M:%S")))
    add_transaction(owner, ts_2025, "TON", TxType.AIRDROP, "1",
                    source="chain", wallet_id=wallet.id)
    add_transaction(owner, ts_2024, "TON", TxType.AIRDROP, "5",
                    source="chain", wallet_id=wallet.id)

    bybit_cfg = make_cfg(crypto_owner_id=owner,
                         bybit_api_key="key", bybit_api_secret="secret")
    snap = sources_snapshot(owner, bybit_cfg, year=2025)
    check(len(snap.wallets) == 1, "кошелёк виден", str(len(snap.wallets)))
    check(snap.ops_year == 1, "операции считаются только за нужный год",
          str(snap.ops_year))
    check(snap.pending == 2, "спорные проводки посчитаны", str(snap.pending))
    check(snap.cex_line == "Bybit ✅, OKX ⚪", "биржи в строке статуса",
          snap.cex_line)
    check("Bybit ✅" in snap.summary_line and "спорных — 2" in snap.summary_line,
          "строка для /tax собрана", snap.summary_line)
    check(any("ждут проверки" in m for m in snap.missing),
          "подсказка про проверку", str(snap.missing))

    # Внутреннее перемещение не должно попадать в «спорные».
    add_transaction(owner, ts_2025, "TON", TxType.TRANSFER_OUT, "1",
                    source="chain", wallet_id=wallet.id, is_internal=True)
    snap2 = sources_snapshot(owner, bybit_cfg, year=2025)
    check(snap2.pending == 2, "внутренние перемещения не считаются спорными",
          str(snap2.pending))

    # ------------------------------------------- 3b. покрытие истории
    print("3b. Покрытие истории бирж")
    from utils.cex_api import get_cex_coverage, set_cex_coverage

    check(snap2.coverage_line == "Bybit — ещё не загружена",
          "без синка покрытие неизвестно", snap2.coverage_line)
    check(any("история ещё не загружена" in m for m in snap2.missing),
          "подсказка загрузить историю", str(snap2.missing))

    early_ms = int(time.mktime(time.strptime("2024-03-01 00:00:00",
                                             "%Y-%m-%d %H:%M:%S"))) * 1000
    set_cex_coverage("bybit", early_ms)
    check(get_cex_coverage("bybit") == early_ms, "покрытие записано",
          str(get_cex_coverage("bybit")))

    later_ms = int(time.mktime(time.strptime("2025-06-01 00:00:00",
                                             "%Y-%m-%d %H:%M:%S"))) * 1000
    set_cex_coverage("bybit", later_ms)
    check(get_cex_coverage("bybit") == early_ms,
          "покрытие не сдвигается вперёд", str(get_cex_coverage("bybit")))

    snap3 = sources_snapshot(owner, bybit_cfg, year=2025)
    check("Bybit — с 01.03.2024" in snap3.coverage_line,
          "дата покрытия в строке", snap3.coverage_line)
    check(not snap3.coverage_warnings,
          "начало 2025 покрыто — предупреждений нет",
          str(snap3.coverage_warnings))

    okx_mid = int(time.mktime(time.strptime("2025-06-01 00:00:00",
                                            "%Y-%m-%d %H:%M:%S"))) * 1000
    set_cex_coverage("okx", okx_mid)
    both_cfg = make_cfg(crypto_owner_id=owner,
                        bybit_api_key="key", bybit_api_secret="secret",
                        okx_api_key="k", okx_api_secret="s",
                        okx_passphrase="p")
    snap4 = sources_snapshot(owner, both_cfg, year=2025)
    check(any("OKX" in w and "не покрыто" in w
              for w in snap4.coverage_warnings),
          "OKX не дотягивает до начала года", str(snap4.coverage_warnings))
    check(not any("Bybit" in w for w in snap4.coverage_warnings),
          "предупреждение только про неполную биржу",
          str(snap4.coverage_warnings))
    check(any("OKX" in m and "не покрыто" in m for m in snap4.missing),
          "предупреждение попало в «что дальше»", str(snap4.missing))

    # ---------------------------------------------------- 4. sync_all
    print("4. Единый прогон sync_all")
    owner = 4001
    add_wallet(owner, "ton", TON_C, "для синка")
    cfg = make_cfg(crypto_owner_id=owner,
                   bybit_api_key="key", bybit_api_secret="secret")

    import utils.auto_import as auto_import
    import utils.sync_pipeline as pipeline
    import tasks.cex_sync as cex_sync

    seen = {}
    original_run = auto_import.run
    original_rematch = auto_import.rematch_transfers
    original_sync_cex = cex_sync.sync_cex

    async def fake_run(owner_id, config, wallets=None):
        seen["owner"] = owner_id
        seen["wallets"] = len(wallets or [])
        return SimpleNamespace(moves=5, saved=3, duplicates=1, internal=2,
                               review=["a", "b"], errors=["мелкая ошибка"])

    async def fake_sync_cex(owner_id, config):
        ex = SimpleNamespace(name="bybit", title="Bybit", saved=4,
                             duplicates=2, errors=[])
        return SimpleNamespace(saved=4, duplicates=2, exchanges=[ex])

    auto_import.run = fake_run
    auto_import.rematch_transfers = lambda owner_id: 7
    cex_sync.sync_cex = fake_sync_cex
    try:
        report = await pipeline.sync_all(owner, cfg)
    finally:
        auto_import.run = original_run
        auto_import.rematch_transfers = original_rematch
        cex_sync.sync_cex = original_sync_cex

    check(seen.get("owner") == owner, "владелец передан в скан", str(seen))
    check(seen.get("wallets") == 1, "скан получил список кошельков", str(seen))
    check(report.wallets == 1 and report.moves == 5, "кошельки в отчёте",
          str(report))
    check(report.saved == 3 and report.duplicates == 1, "операции в отчёте",
          str(report))
    check(report.internal == 2 and report.review == 2,
          "перемещения и спорные в отчёте", str(report))
    check(report.cex_saved == 4 and report.cex_duplicates == 2,
          "биржи в отчёте", str(report))
    check(report.rematched == 7, "сопоставление запущено", str(report.rematched))
    check(report.errors == ["мелкая ошибка"],
          "ошибка источника не потерялась", str(report.errors))

    # -------------------------------------------------- 5. since_ms CEX
    print("5. Окно CEX в миллисекундах эпохи")
    owner = 5001
    cfg = make_cfg(crypto_owner_id=owner,
                   bybit_api_key="key", bybit_api_secret="secret")
    captured = {}

    original_watermark = cex_sync.get_cex_watermark
    original_trades = cex_sync.sync_bybit_trades
    original_transfers = cex_sync.sync_bybit_transfers
    original_transfer_watermark = cex_sync.get_cex_transfer_watermark
    original_set_transfer_watermark = cex_sync.set_cex_transfer_watermark

    from utils.cex_api import CexSyncResult

    async def fake_trades(key, secret, since_ts=None, until_ts=None):
        captured["since"] = since_ts
        captured["until"] = until_ts
        return CexSyncResult(exchange="bybit", rows=[])

    async def fake_transfers(key, secret, since_ts=None, until_ts=None):
        captured["transfer_since"] = since_ts
        return CexSyncResult(exchange="bybit", rows=[])

    cex_sync.get_cex_watermark = lambda name: 0
    cex_sync.sync_bybit_trades = fake_trades
    cex_sync.sync_bybit_transfers = fake_transfers
    cex_sync.get_cex_transfer_watermark = lambda name: 0
    cex_sync.set_cex_transfer_watermark = (
        lambda name, ts: captured.__setitem__("transfer_wm", ts)
    )
    try:
        report = await cex_sync.sync_cex(owner, cfg)
    finally:
        cex_sync.get_cex_watermark = original_watermark
        cex_sync.sync_bybit_trades = original_trades
        cex_sync.sync_bybit_transfers = original_transfers
        cex_sync.get_cex_transfer_watermark = original_transfer_watermark
        cex_sync.set_cex_transfer_watermark = original_set_transfer_watermark

    expected = int((time.time() - 730 * 86400) * 1000)
    since = captured.get("since") or 0
    check(since > 1_000_000_000_000,
          "since_ms — эпоха в миллисекундах, а не время loop", str(since))
    check(abs(since - expected) < 120_000,
          "since_ms — примерно два года назад", f"{since} vs {expected}")
    check((captured.get("until") or 0) > since,
          "until_ms позже since_ms", str(captured))
    check((captured.get("transfer_wm") or 0) > 1_000_000_000_000,
          "водяной знак переводов сдвинут", str(captured.get("transfer_wm")))
    check(len(report.exchanges) == 1 and report.exchanges[0].name == "bybit",
          "отчёт по одной бирже", str(report.exchanges))
    check(report.saved == 0, "пустой ответ не даёт записей", str(report.saved))

    # Ошибка биржи должна попасть в отчёт, а не уронить sync_cex.
    async def failing_trades(key, secret, since_ts=None, until_ts=None):
        return CexSyncResult(exchange="bybit", errors=["boom"])

    cex_sync.get_cex_watermark = lambda name: 0
    cex_sync.sync_bybit_trades = failing_trades
    cex_sync.sync_bybit_transfers = fake_transfers
    cex_sync.get_cex_transfer_watermark = lambda name: 0
    cex_sync.set_cex_transfer_watermark = (
        lambda name, ts: captured.__setitem__("transfer_wm2", ts)
    )
    try:
        report = await cex_sync.sync_cex(owner, cfg)
    finally:
        cex_sync.get_cex_watermark = original_watermark
        cex_sync.sync_bybit_trades = original_trades
        cex_sync.sync_bybit_transfers = original_transfers
        cex_sync.get_cex_transfer_watermark = original_transfer_watermark
        cex_sync.set_cex_transfer_watermark = original_set_transfer_watermark
    check(report.errors == ["boom"], "ошибка биржи в отчёте",
          str(report.errors))

    # ------------------------------------- 6. приём списка адресов
    print("6. Приём списка адресов в чате")
    import handlers.auto_import as ai
    from telegram.ext import ApplicationHandlerStop

    owner = 6001
    calls = []
    original_start = ai.start_sync

    async def fake_start(context, chat_id, notice=None):
        calls.append((chat_id, notice))

    ai.start_sync = fake_start
    try:
        # Без ожидания текст не перехватывается.
        ctx = FakeContext(owner)
        msg = FakeMessage(TON_A)
        try:
            await ai.wallet_bulk_text(_update(msg), ctx)
            raised = False
        except ApplicationHandlerStop:
            raised = True
        check(not raised and not msg.replies,
              "обычный текст не перехватывается", str(msg.replies))

        # С ожиданием — добавляет и запускает синк.
        ctx = FakeContext(owner)
        ctx.user_data[AWAIT_WALLETS_KEY] = True
        msg = FakeMessage(f"{TON_D} основной\n{EVM_D} | биржа")
        try:
            await ai.wallet_bulk_text(_update(msg), ctx)
            raised = False
        except ApplicationHandlerStop:
            raised = True
        check(raised, "текст с адресами перехвачен")
        check(bool(msg.replies) and "Добавлено" in msg.replies[0][0],
              "бот отчитался о добавлении", str(msg.replies[:1]))
        check(not ctx.user_data.get(AWAIT_WALLETS_KEY),
              "режим ожидания выключен")
        check(len(calls) == 1, "синк запущен автоматически", str(calls))
        check(len(list_wallets(owner)) == 2, "оба адреса в базе",
              str(len(list_wallets(owner))))

        # Чужой адрес нельзя молча вернуть как «добавленный».
        cross = add_wallets_from_text(owner, TON_B)
        check(not cross.added and cross.invalid
              and "другому владельцу" in cross.invalid[0][1],
              "чужой кошелёк не выдаётся за свой", str(cross))

        # Мусор: режим не выключаем, чтобы можно было повторить.
        ctx = FakeContext(owner)
        ctx.user_data[AWAIT_WALLETS_KEY] = True
        msg = FakeMessage("просто текст без адреса")
        try:
            await ai.wallet_bulk_text(_update(msg), ctx)
            raised = False
        except ApplicationHandlerStop:
            raised = True
        check(raised and "Не нашёл" in msg.replies[0][0],
              "на мусор попросил повторить", str(msg.replies[:1]))
        check(bool(ctx.user_data.get(AWAIT_WALLETS_KEY)),
              "режим ожидания сохранён")
    finally:
        ai.start_sync = original_start

    # /wallet_add без аргументов включает тот же режим.
    print("7. /wallet_add без аргументов")
    from handlers import crypto_ledger

    ctx = FakeContext(7001)
    msg = FakeMessage("")
    await crypto_ledger.wallet_add_command(_update(msg), ctx)
    check(bool(ctx.user_data.get(AWAIT_WALLETS_KEY)),
          "включён приём списка адресов")
    check(bool(msg.replies) and "Добавить кошельки" in msg.replies[0][0],
          "бот попросил список", str(msg.replies[:1]))

    print()
    if FAILURES:
        print(f"❌ Провалено проверок: {len(FAILURES)}")
        for name in FAILURES:
            print(f"   • {name}")
        return 1
    print("✅ Все проверки пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
