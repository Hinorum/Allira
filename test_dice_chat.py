"""Тесты хендлера турнира на кубиках и хендлера общения в чате.

Проверяются правки после разбора кода:

  Турнир:
   1. Не-админ, нажавший кнопку режима, не ломает турнирную доску.
   2. Счётчик переигровок — на каждый матч, а не общий на раунд.
   3. Бросок, который засчитан, не помечается как ошибка.
   4. Имя игрока без first_name не превращается в «None».
   5. Словарь последних бросков не растёт бесконечно.

  Чат:
   6. Кэш картинок не съедает память инстанса.
   7. Сервисная ошибка не попадает в историю диалога.
   8. Бот не отвечает на собственные сообщения.
   9. Пустой bot_username не ломает вырезание упоминания.
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


def check(cond: bool, name: str, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# --------------------------------------------------------------- заглушки


class FakeUser:
    def __init__(self, user_id: int, first_name=None, username=None):
        self.id = user_id
        self.first_name = first_name
        self.username = username


class FakeMessage:
    def __init__(self, message_id=1, chat_id=-100):
        self.message_id = message_id
        self.chat_id = chat_id
        self.text = ""
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)

    async def edit_text(self, text, **kwargs):
        self.edited = text


class FakeQuery:
    """callback_query: пишет ответы в answers и правки в edits."""

    def __init__(self, user, message, data="x"):
        self.from_user = user
        self.message = message
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text="", show_alert=False, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class FakeBot:
    def __init__(self, bot_id=777):
        self.id = bot_id
        self.sent = []
        self.dice_values = []
        self.fail_send_message = False

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_send_message:
            raise RuntimeError("Telegram недоступен")
        self.sent.append((chat_id, text))

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.sent.append((chat_id, text))

    async def send_dice(self, chat_id, emoji=None, **kwargs):
        value = self.dice_values.pop(0) if self.dice_values else 3
        message = FakeMessage()
        message.dice = type("D", (), {"value": value})()
        return message

    async def send_chat_action(self, **kwargs):
        pass

    async def pin_chat_message(self, **kwargs):
        pass

    async def unpin_chat_message(self, **kwargs):
        pass

    async def get_file(self, file_id):
        raise RuntimeError("сеть недоступна")


class FakeContext:
    def __init__(self, bot_data=None):
        self.bot_data = bot_data or {}
        self.bot = FakeBot()
        self.user_data = {}

    async def send_chat_action(self, **kwargs):
        pass


class FakeUpdate:
    def __init__(self, callback_query=None, message=None):
        self.callback_query = callback_query
        self.message = message


def chat_context(bot_username: str = "allira_bot") -> FakeContext:
    """Контекст чата с минимальным набором bot_data."""
    return FakeContext({
        "bot_username": bot_username,
        "CREATOR_USER_IDS": (777,),
        "DEFAULT_MODEL": "test-model",
        "LANE_MODEL": "test-lane",
        "VISION_MODEL": "test-vision",
        "OPENROUTER_API_KEY": "test-key",
    })


def chat_message(text: str = "аллира, привет", user_id: int = 5) -> FakeMessage:
    """Сообщение в группе от обычного юзера."""
    message = FakeMessage(chat_id=-100)
    message.text = text
    message.from_user = FakeUser(user_id, "Юзер")
    message.sender_chat = None
    message.chat = type("C", (), {"id": -100, "username": None})()
    return message


async def main() -> int:
    from handlers import dice_tournament as dt

    # ---------------------------------------- 1. не-админ не ломает доску
    print("1. Отказ не-админу не ломает турнирную доску")
    chat_id = -1001
    dt._tournament_states.clear()
    state = dt._default_state(chat_id)
    state.update({
        "active": True,
        "registration_open": False,
        "tournament_mode": None,
        "current_round": 0,
        "admin_user_id": 100,
        "players": {
            100: {"user_id": 100, "username": "Админ", "total_score": 0,
                  "is_eliminated": False, "eliminated_round": 0},
            200: {"user_id": 200, "username": "Чужой", "total_score": 0,
                  "is_eliminated": False, "eliminated_round": 0},
        },
    })
    dt._tournament_states[chat_id] = state

    msg = FakeMessage(chat_id=chat_id)
    query = FakeQuery(FakeUser(200, "Чужой"), msg, "set_tournament_mode:pair_match")
    await dt.set_tournament_mode(FakeUpdate(callback_query=query), FakeContext())

    check(query.edits == [], "доска не редактируется отказом", str(query.edits))
    check(
        any("Только админ" in a for a in query.answers),
        "отказ показан тостом",
        str(query.answers),
    )
    check(
        dt._tournament_states[chat_id].get("tournament_mode") is None,
        "режим не подменился",
    )
    check(
        dt._tournament_states[chat_id].get("round_matches") == [],
        "матчи не создались",
    )

    # -------------------------------- 2. счётчик переигровок на каждый матч
    print("2. Счётчик переигровок — на матч")
    dt._tournament_states.clear()
    state = dt._default_state(chat_id)
    state.update({
        "active": True,
        "registration_open": False,
        "tournament_mode": "pair_match",
        "current_round": 1,
        "admin_user_id": 100,
        "round_matches": [(1, 2), (3, 4)],
        "player_rolls_in_round": {1: 4, 2: 4, 3: 5, 4: 5},
        "active_players_in_round": set(),
        "players": {
            uid: {"user_id": uid, "username": f"P{uid}", "total_score": 0,
                  "is_eliminated": False, "eliminated_round": 0}
            for uid in (1, 2, 3, 4)
        },
    })
    dt._tournament_states[chat_id] = state

    ctx = FakeContext()
    await dt.announce_round_results(ctx, chat_id)
    st = dt._tournament_states[chat_id]
    check(
        st["rematch_counts"] == {"1:2": 1, "3:4": 1},
        "у каждого матча свой счётчик",
        str(st.get("rematch_counts")),
    )
    check(
        len(st["round_matches"]) == 2,
        "обе пары поставлены на переигровку",
        str(st["round_matches"]),
    )

    # Вторая ничья в том же матче — счётчик растёт только у своей пары.
    # Пара 3:4 в этот раз не заворачивается: 4 выигрывает сразу.
    st["rematch_counts"] = {"1:2": 1, "3:4": 1}
    st["player_rolls_in_round"] = {1: 6, 2: 6, 3: 2, 4: 6}
    st["active_players_in_round"] = set()
    st["round_matches"] = [(1, 2), (3, 4)]
    for uid in (3, 4):
        st["players"][uid]["is_eliminated"] = False
    await dt.announce_round_results(ctx, chat_id)
    st = dt._tournament_states[chat_id]
    check(
        st["rematch_counts"].get("1:2") == 2,
        "счётчик своей пары растёт",
        str(st.get("rematch_counts")),
    )
    check(
        st["rematch_counts"].get("3:4") is None,
        "у решённой пары счётчик убран",
        str(st.get("rematch_counts")),
    )
    check(
        st["players"][3].get("is_eliminated") is True,
        "проигравший пары 3:4 выбыл",
    )
    check(
        st["players"][1].get("is_eliminated") is False,
        "пара 1:2 ещё играет переигровку",
    )

    # ------------------------------------ 3. засчитанный бросок ≠ ошибка
    print("3. Засчитанный бросок не помечается ошибкой")
    dt._tournament_states.clear()
    dt.last_roll_times.clear()
    state = dt._default_state(chat_id)
    state.update({
        "active": True,
        "registration_open": False,
        "tournament_mode": "pair_match",
        "current_round": 1,
        "admin_user_id": 100,
        "round_matches": [(100, 200)],
        "active_players_in_round": {100},
        "player_rolls_in_round": {},
        "tournament_message_id": 5,
        "players": {
            100: {"user_id": 100, "username": "Админ", "total_score": 0,
                  "is_eliminated": False, "eliminated_round": 0},
            200: {"user_id": 200, "username": "Другой", "total_score": 0,
                  "is_eliminated": False, "eliminated_round": 0},
        },
    })
    dt._tournament_states[chat_id] = state

    ctx = FakeContext()
    ctx.bot.fail_send_message = True
    msg = FakeMessage(chat_id=chat_id)
    query = FakeQuery(FakeUser(100, "Админ"), msg, "make_tournament_roll")
    await dt.make_tournament_roll(FakeUpdate(callback_query=query), ctx)

    st = dt._tournament_states[chat_id]
    check(st["player_rolls_in_round"].get(100) is not None,
          "бросок засчитан несмотря на ошибку отправки")
    check(
        not any("Попробуй снова" in a for a in query.answers),
        "игроку не сказано «брось заново»",
        str(query.answers),
    )
    check(
        any("засчитан" in a.lower() for a in query.answers),
        "игроку сказано, что бросок засчитан",
        str(query.answers),
    )

    # ------------------------------------------- 4. имя без first_name
    print("4. Имя игрока без first_name")
    check(dt._display_name(FakeUser(1, None, "nick")) == "nick",
          "берётся username, если first_name пуст")
    check(dt._display_name(FakeUser(1, None, None)) == "User_1",
          "нет ни имени, ни ника — заглушка с id")
    check(dt._display_name(None) == "Игрок", "нет юзера — «Игрок»")
    check("None" not in dt._display_name(FakeUser(1, None, None)),
          "строки «None» не остаётся")

    # --------------------------------- 5. словарь бросков не растёт бесконечно
    print("5. Ограничение словаря последних бросков")
    dt.last_roll_times.clear()
    for i in range(dt.MAX_ROLL_HISTORY + 200):
        dt._remember_roll(i, float(i))
    check(
        len(dt.last_roll_times) <= dt.MAX_ROLL_HISTORY,
        "словарь ограничен",
        str(len(dt.last_roll_times)),
    )
    check(
        dt.last_roll_times.get(dt.MAX_ROLL_HISTORY + 199) is not None,
        "свежая запись на месте",
    )
    check(
        dt.last_roll_times.get(0) is None,
        "самая старая запись вытеснена",
    )

    # ------------------------------------------------------- чат: память
    print("6. Кэш картинок не съедает память")
    from handlers import message_handler as mh

    check(mh.MAX_IMAGE_BYTES <= 4 * 1024 * 1024,
          "потолок картинки не больше 4 МБ",
          str(mh.MAX_IMAGE_BYTES))
    check(mh._image_cache.maxsize <= 8,
          "кэш картинок маленький",
          str(mh._image_cache.maxsize))
    # 8 записей по 4 МБ в base64 — это ~43 МБ, а не сотни мегабайт
    worst_case = mh._image_cache.maxsize * (mh.MAX_IMAGE_BYTES * 4 / 3) / (1024 * 1024)
    check(worst_case < 64,
          "худший случай по RAM меньше 64 МБ",
          f"{worst_case:.1f} МБ")

    # ------------------------------------------ 7. сервисные ошибки в истории
    print("7. Сервисная ошибка не попадает в историю")
    from utils.ai_responses import ERR_TECHNICAL, is_service_error

    check(is_service_error(ERR_TECHNICAL), "ошибка распознаётся как сервисная")

    from telegram import Update

    ctx = chat_context()
    ctx.bot.id = 777
    ctx.user_data[mh.HISTORY_KEY] = []
    message = chat_message()
    update = Update(update_id=3, message=message)

    import utils.ai_responses as ai

    original_llm = mh.get_llm_response
    original_snapshot = mh.get_market_snapshot
    original_rate = mh.check_rate_limit
    original_upsert = mh.upsert_user
    original_log = mh.log_message
    original_ban = mh.is_user_banned

    async def fake_llm_error(user_prompt, system_prompt, model, api_key, history=None, images=None):
        return ERR_TECHNICAL

    async def fake_snapshot():
        return ""

    async def fake_rate(*args, **kwargs):
        return True

    async def fake_upsert(*args, **kwargs):
        return None

    async def fake_log(*args, **kwargs):
        return None

    async def fake_ban(user_id):
        return False

    mh.get_llm_response = fake_llm_error
    mh.get_market_snapshot = fake_snapshot
    mh.check_rate_limit = fake_rate
    mh.upsert_user = fake_upsert
    mh.log_message = fake_log
    mh.is_user_banned = fake_ban
    original_chance = mh.RESPONSE_CHANCE
    mh.RESPONSE_CHANCE = 1.0
    try:
        await mh._process_message(update, ctx, is_private=False)
    finally:
        mh.RESPONSE_CHANCE = original_chance
        mh.get_llm_response = original_llm
        mh.get_market_snapshot = original_snapshot
        mh.check_rate_limit = original_rate
        mh.upsert_user = original_upsert
        mh.log_message = original_log
        mh.is_user_banned = original_ban

    history = ctx.user_data.get(mh.HISTORY_KEY) or []
    check(
        all(turn["content"] != ERR_TECHNICAL for turn in history),
        "ошибка не попала в историю",
        str(history),
    )
    check(not history, "история осталась пустой", str(history))
    # Ответ должен был уйти юзеру — иначе проверка выше проходит вхолостую,
    # потому что до LLM дело просто не дошло.
    check(bool(message.replies), "ответ дошёл до юзера", str(message.replies))

    # ------------------------------------------ 8. бот не отвечает себе
    print("8. Бот не отвечает на собственные сообщения")
    from telegram import Update
    from telegram.ext import ContextTypes

    ctx = chat_context()
    ctx.bot.id = 777
    message = chat_message("аллира, что там с рынком?", user_id=777)
    message.from_user = FakeUser(777, "AlliraCryptoQueen_bot")
    update = Update(update_id=1, message=message)
    await mh._process_message(update, ctx, is_private=False)
    check(
        not ctx.bot.sent and not message.replies,
        "бот молчит на собственное сообщение",
        str(ctx.bot.sent),
    )

    # --------------------------------- 9. пустой bot_username не ломает текст
    print("9. Пустой bot_username")
    ctx = chat_context(bot_username="")
    ctx.bot.id = 777
    message = chat_message()
    update = Update(update_id=2, message=message)
    # Подменяем LLM, чтобы поймать сам промпт.
    captured = {}

    async def fake_llm(user_prompt, system_prompt, model, api_key, history=None, images=None):
        captured["prompt"] = user_prompt
        return "ответ"

    import utils.ai_responses as ai

    original_llm = mh.get_llm_response
    original_snapshot = mh.get_market_snapshot
    original_rate = mh.check_rate_limit
    original_upsert = mh.upsert_user
    original_log = mh.log_message
    original_ban = mh.is_user_banned

    async def fake_snapshot():
        return ""

    async def fake_rate(*args, **kwargs):
        return True

    async def fake_upsert(*args, **kwargs):
        return None

    async def fake_log(*args, **kwargs):
        return None

    async def fake_ban(user_id):
        return False

    async def fake_ban(user_id):
        return False

    mh.get_llm_response = fake_llm
    mh.get_market_snapshot = fake_snapshot
    mh.check_rate_limit = fake_rate
    mh.upsert_user = fake_upsert
    mh.log_message = fake_log
    mh.is_user_banned = fake_ban
    try:
        mh.RESPONSE_CHANCE = 1.0
        await mh._process_message(update, ctx, is_private=False)
    finally:
        mh.get_llm_response = original_llm
        mh.get_market_snapshot = original_snapshot
        mh.check_rate_limit = original_rate
        mh.upsert_user = original_upsert
        mh.log_message = original_log
        mh.is_user_banned = original_ban

    check(bool(captured.get("prompt")), "промпт собран", str(captured)[:120])
    check(
        captured.get("prompt", "").strip().startswith("аллира"),
        "текст не порезан пустой регуляркой",
        repr(captured.get("prompt", "")[:80]),
    )

    # Обычный ответ, наоборот, обязан попасть в историю.
    ctx2 = chat_context(bot_username="")
    ctx2.user_data[mh.HISTORY_KEY] = []
    message2 = chat_message()
    update2 = Update(update_id=4, message=message2)

    async def fake_llm_ok(user_prompt, system_prompt, model, api_key, history=None, images=None):
        return "Нормальный ответ"

    mh.get_llm_response = fake_llm_ok
    mh.get_market_snapshot = fake_snapshot
    mh.check_rate_limit = fake_rate
    mh.upsert_user = fake_upsert
    mh.log_message = fake_log
    mh.is_user_banned = fake_ban
    mh.RESPONSE_CHANCE = 1.0
    try:
        await mh._process_message(update2, ctx2, is_private=False)
    finally:
        mh.RESPONSE_CHANCE = original_chance
        mh.get_llm_response = original_llm
        mh.get_market_snapshot = original_snapshot
        mh.check_rate_limit = original_rate
        mh.upsert_user = original_upsert
        mh.log_message = original_log
        mh.is_user_banned = original_ban

    history2 = ctx2.user_data.get(mh.HISTORY_KEY) or []
    check(
        any(turn["content"] == "Нормальный ответ" for turn in history2),
        "обычный ответ попал в историю",
        str(history2),
    )
    check(
        any(turn["role"] == "user" for turn in history2),
        "реплика юзера попала в историю",
        str(history2),
    )

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
