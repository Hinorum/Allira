"""Тесты отчётов Marketapp для чужих кошельков.

Раньше /marketapptop и /marketappgifts разрешали привязку платежей к NFT
только для кошелька создателя (is_owner): по адресу друга отчёт показывал
общую сумму и «привязка доступна только для своего». Лента Marketapp на
самом деле платформенная и фильтруется по src/dst, поэтому привязка
работает для любого кошелька.

Проверяется:

  1. Без адреса чужой пользователь получает подсказку, а не отчёт по
     кошельку создателя; создатель по-прежнему получает свой из конфига.
  2. Привязка через collect_rent_events запускается для не-владельца.
  3. «Примерка по цене» (my-rented) остаётся только для владельца: она
     отдаёт аренды владельца API-ключа, а не чужого кошелька.
  4. Если привязка не удалась, причина больше не врёт про «только для
     своего».
  5. Сам топ по событиям чужого кошелька строится и называет адрес.
"""

import asyncio
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

OWNER = "0:" + "a" * 64
OTHER = "0:" + "b" * 64


def check(cond: bool, name: str, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class FakeUser:
    def __init__(self, user_id: int, username: str = ""):
        self.id = user_id
        self.username = username


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, user: FakeUser):
        self.effective_user = user
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, args: list[str], bot_data: dict):
        self.args = args
        self.bot_data = bot_data


class FakeBot:
    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)


def _event(ts: int, nano: str = "1500000000", addr: str = "") -> dict:
    return {
        "ts": ts,
        "src": "0:" + "1" * 64,
        "dst": OTHER,
        "price_nano": nano,
        "nft_address": addr,
        "nft_name": "Gift" if addr else "",
        "duration": 86400,
        "category": "gifts",
        "wallet": OTHER,
    }


async def main() -> int:
    import handlers.wallet_command as wcmd

    print("=" * 60)
    print("Тест отчётов Marketapp для чужих кошельков")
    print("=" * 60)

    # ------------------------------------------ 1. выбор кошелька
    print("1. Кошелёк по умолчанию — только у создателя")
    bot_data = {
        "CREATOR_USER_IDS": (100,),
        "CREATOR_USERNAME": "boss",
        "MARKETAPP_WALLET": OWNER,
    }
    usage = "Использование: /marketapptop [дней] [адрес кошелька]"

    creator = FakeUpdate(FakeUser(100, "boss"))
    resolved = await wcmd._resolve_wallet(
        creator, FakeContext([], bot_data), "marketapptop", [], usage
    )
    check(resolved == (OWNER, True), "создатель без адреса — свой кошелёк",
          str(resolved))

    creator_by_name = FakeUpdate(FakeUser(101, "boss"))
    resolved = await wcmd._resolve_wallet(
        creator_by_name, FakeContext([], bot_data), "marketapptop", [], usage
    )
    check(resolved == (OWNER, True), "создатель по username — свой кошелёк",
          str(resolved))

    friend = FakeUpdate(FakeUser(200, "friend"))
    resolved = await wcmd._resolve_wallet(
        friend, FakeContext([], bot_data), "marketapptop", [], usage
    )
    check(resolved is None, "чужой без адреса — отчёта нет", str(resolved))
    check(any("Укажи адрес" in r for r in friend.message.replies),
          "чужому подсказали адрес", str(friend.message.replies))

    friend_with_addr = FakeUpdate(FakeUser(200, "friend"))
    resolved = await wcmd._resolve_wallet(
        friend_with_addr, FakeContext([OTHER], bot_data),
        "marketapptop", [OTHER], usage
    )
    check(resolved == (OTHER, False), "чужой с адресом — его кошелёк",
          str(resolved))

    open_bot = FakeUpdate(FakeUser(300, "anyone"))
    resolved = await wcmd._resolve_wallet(
        open_bot, FakeContext([], {
            "CREATOR_USER_IDS": (), "CREATOR_USERNAME": "",
            "MARKETAPP_WALLET": OWNER,
        }), "marketapptop", [], usage,
    )
    check(resolved == (OWNER, True),
          "без настроек создателя ограничения нет", str(resolved))

    # ------------------------------- 2. привязка чужого кошелька
    print("2. Привязка NFT для чужого кошелька")
    original_get = wcmd.get_all_rent_events
    original_collect = wcmd.collect_rent_events
    original_enrich = wcmd._enrich_by_price
    original_sync = wcmd.sync_rent_from_blockchain
    original_busy = wcmd.LINKAGE_BUSY["running"]

    rows: list[dict] = [_event(1_750_000_000)]
    collect_calls: list[tuple] = []
    enrich_calls: list[str] = []
    sync_calls: list[str] = []

    async def fake_get(wallet):
        return [dict(r) for r in rows]

    async def fake_collect(token, wallet, page_budget=None):
        collect_calls.append((token, wallet, page_budget))
        return 0

    async def fake_enrich(token, wallet):
        enrich_calls.append(wallet)
        return 0

    async def fake_sync(wallet, max_pages=50, from_scratch=False):
        sync_calls.append(wallet)
        return 0

    wcmd.get_all_rent_events = fake_get
    wcmd.collect_rent_events = fake_collect
    wcmd._enrich_by_price = fake_enrich
    wcmd.sync_rent_from_blockchain = fake_sync
    wcmd.LINKAGE_BUSY["running"] = False
    try:
        # Не-владелец: лента должна запуститься, my-rented — нет.
        collect_calls.clear()
        enrich_calls.clear()
        rows[:] = [_event(1_750_000_000)]

        async def link_other(token, wallet, page_budget=None):
            collect_calls.append((token, wallet, page_budget))
            rows[0]["nft_address"] = "0:" + "c" * 64
            rows[0]["nft_name"] = "Friend gift"
            return 1

        wcmd.collect_rent_events = link_other
        bot = FakeBot()
        events = await wcmd._ensure_rent_data(
            bot, 1, OTHER, "token", is_owner=False
        )
        check(len(collect_calls) == 1, "лента Marketapp вызвана",
              str(collect_calls))
        check(collect_calls[0][1] == OTHER, "лента читалась по чужому адресу",
              str(collect_calls))
        check(not enrich_calls, "my-rented для чужого не вызывался",
              str(enrich_calls))
        check(bool(events and events[0].get("nft_address")),
              "чужой платёж привязан к NFT", str(events))
        check(any("Привязываю" in m for m in bot.messages),
              "пользователю сказали про привязку", str(bot.messages))

        # Владелец: если лента не помогла, включается запасная примерка.
        collect_calls.clear()
        enrich_calls.clear()
        rows[:] = [_event(1_750_000_100)]
        wcmd.collect_rent_events = fake_collect

        async def enrich_owner(token, wallet):
            enrich_calls.append(wallet)
            rows[0]["nft_address"] = "0:" + "d" * 64
            rows[0]["nft_name"] = "Owner gift"
            return 1

        wcmd._enrich_by_price = enrich_owner
        bot = FakeBot()
        events = await wcmd._ensure_rent_data(
            bot, 2, OWNER, "token", is_owner=True
        )
        check(len(collect_calls) == 1, "владельцу лента тоже читается",
              str(collect_calls))
        check(enrich_calls == [OWNER], "владельцу доступна примерка по цене",
              str(enrich_calls))
        check(bool(events and events[0].get("nft_address")),
              "владельцу платёж привязан", str(events))

        # Без токена привязки нет ни у кого.
        collect_calls.clear()
        enrich_calls.clear()
        wcmd.collect_rent_events = fake_collect
        wcmd._enrich_by_price = fake_enrich
        rows[:] = [_event(1_750_000_200)]
        bot = FakeBot()
        events = await wcmd._ensure_rent_data(
            bot, 3, OTHER, "", is_owner=False
        )
        check(not collect_calls and not enrich_calls,
              "без ключа Marketapp привязки нет",
              f"collect={collect_calls}, enrich={enrich_calls}")
        check(len(events) == 1, "события остаются в отчёте", str(events))
    finally:
        wcmd.get_all_rent_events = original_get
        wcmd.collect_rent_events = original_collect
        wcmd._enrich_by_price = original_enrich
        wcmd.sync_rent_from_blockchain = original_sync
        wcmd.LINKAGE_BUSY["running"] = original_busy

    # ------------------------------------- 3. причина без привязки
    print("3. Причина отказа не врёт про «только для своего»")
    reason_no_key = wcmd._no_linkage_reason("")
    check("MARKETAPP_API_KEY" in reason_no_key, "без ключа назван ключ",
          reason_no_key)
    reason_no_match = wcmd._no_linkage_reason("token")
    check("Marketapp не вернул совпадений" in reason_no_match,
          "без совпадений названа причина", reason_no_match)
    check("только для своего" not in reason_no_match,
          "чужой кошелёк больше не ограничение", reason_no_match)

    # ----------------------------------------- 4. топ по чужому
    print("4. Топ строится по событиям чужого кошелька")
    addr = "0:" + "e" * 64
    events = [
        _event(1_750_000_300, nano="3000000000", addr=addr),
        _event(1_750_000_400, nano="1000000000", addr=addr),
        _event(1_750_000_500, nano="500000000", addr=""),
    ]
    rows_top, summary = wcmd._top_rows(events, 0)
    check(len(rows_top) == 1 and rows_top[0][0] == addr,
          "подарок один, непривязанный не попал", str(rows_top))
    check(rows_top[0][1]["nano"] == 4_000_000_000,
          "сумма по подарку сложилась", str(rows_top))
    check(summary["unlinked_count"] == 1, "непривязанный посчитан в сводке",
          str(summary))
    report = wcmd._build_top_report(OTHER, rows_top, summary, days=30)
    check(OTHER in report, "в отчёте адрес чужого кошелька", report[:200])
    check("ТОП подарков" in report, "заголовок топа на месте", report[:200])

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
