"""Тесты модерации: /ban, /unban и /kick с очисткой сообщений.

Проверяется:
  1. /ban — только для ADMIN_USER_IDS, бан по id и по реплаю, защита админа.
  2. /unban — снимает флаг в БД и бан в чате (если команда пришла в группе).
  3. /kick — перманентный бан через ban_chat_member(revoke_messages=True):
     юзер не может вернуться, его сообщения удаляются, флаг is_banned ставится.
  4. Ошибки Telegram (нет прав, не удалось разбанить) показываются понятно.
"""

import asyncio
import os
import sys
import tempfile
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


class FakeUser:
    def __init__(self, user_id: int, first_name=None, username=None):
        self.id = user_id
        self.first_name = first_name
        self.username = username


class FakeChat:
    def __init__(self, chat_id: int = -100, chat_type: str = "supergroup"):
        self.id = chat_id
        self.type = chat_type
        self.username = None


class FakeMessage:
    def __init__(self, user, chat, reply_to_message=None):
        self.from_user = user
        self.chat = chat
        self.chat_id = chat.id
        self.reply_to_message = reply_to_message
        self.message_id = 1
        self.text = ""
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeBot:
    def __init__(self, bot_id: int = 999):
        self.id = bot_id
        self.calls = []
        self.fail_ban = None
        self.fail_unban = None

    async def ban_chat_member(self, chat_id, user_id, revoke_messages=None):
        self.calls.append(("ban", chat_id, user_id, revoke_messages))
        if self.fail_ban:
            raise self.fail_ban

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=None):
        self.calls.append(("unban", chat_id, user_id, only_if_banned))
        if self.fail_unban:
            raise self.fail_unban


class FakeContext:
    def __init__(self, admin_ids=(42,), args=(), bot=None):
        self.bot_data = {"ADMIN_USER_IDS": list(admin_ids)}
        self.args = list(args)
        self.bot = bot or FakeBot()
        self.user_data = {}


def make_update(admin_id=42, args=(), reply_user=None,
                chat_type="supergroup", chat_id=-100, bot=None):
    chat = FakeChat(chat_id, chat_type)
    replied = None
    if reply_user is not None:
        replied = FakeMessage(reply_user, chat)
    message = FakeMessage(FakeUser(admin_id, "Админ"), chat, replied)
    update = SimpleNamespace(message=message)
    context = FakeContext(args=args, bot=bot)
    return update, context, message


async def main() -> int:
    from utils import database as db

    db.DB_PATH = TEST_DB
    db.init_db()

    from handlers import moderation as mod
    from telegram.error import BadRequest

    # ---------------------------------------------------------- 1. /ban
    print("1. /ban и /unban")
    upd, ctx, msg = make_update(admin_id=7, args=["555"])
    await mod.ban_command(upd, ctx)
    check("только администраторам" in msg.replies[0], "не-админу отказ", str(msg.replies))
    check(not await db.is_user_banned(555), "не-админ не забанил")

    upd, ctx, msg = make_update(args=["555"])
    await mod.ban_command(upd, ctx)
    check(await db.is_user_banned(555), "бан по id сохраняется в БД")
    check("забанен" in msg.replies[0], "ответ о бане", str(msg.replies))

    upd, ctx, msg = make_update(reply_user=FakeUser(777, "Vasya"))
    await mod.ban_command(upd, ctx)
    check(await db.is_user_banned(777), "бан по реплаю")
    check("Vasya" in msg.replies[0], "имя из реплая в ответе", str(msg.replies))

    upd, ctx, msg = make_update(args=["42"])
    await mod.ban_command(upd, ctx)
    check(not await db.is_user_banned(42), "админа забанить нельзя")
    check("нельзя" in msg.replies[0], "отказ по админу", str(msg.replies))

    upd, ctx, msg = make_update(args=["abc"])
    await mod.ban_command(upd, ctx)
    check("числом" in msg.replies[0], "нечисловой id отклонён", str(msg.replies))

    upd, ctx, msg = make_update(args=["555"])
    await mod.unban_command(upd, ctx)
    check(not await db.is_user_banned(555), "разбан по id снимает флаг в БД")
    check(ctx.bot.calls == [("unban", -100, 555, True)],
          "разбан в группе снимает бан в чате", str(ctx.bot.calls))
    check("разбанен" in msg.replies[0], "ответ о разбане", str(msg.replies))

    await db.set_user_banned(778, True)
    upd, ctx, msg = make_update(args=["778"], chat_type="private")
    await mod.unban_command(upd, ctx)
    check(not await db.is_user_banned(778), "разбан в личке снимает флаг")
    check(ctx.bot.calls == [], "в личке Telegram API не вызывается", str(ctx.bot.calls))

    await db.set_user_banned(779, True)
    upd, ctx, msg = make_update(args=["779"], bot=FakeBot())
    ctx.bot.fail_unban = BadRequest("not enough rights to restrict/unrestrict chat member")
    await mod.unban_command(upd, ctx)
    check(not await db.is_user_banned(779), "флаг снят даже при ошибке Telegram")
    check("разбанен в боте" in msg.replies[0], "ошибка снятия бана показана", str(msg.replies))
    check("not enough rights" in msg.replies[0], "причина передана", str(msg.replies))

    # --------------------------------------------------------- 2. /kick
    print("2. /kick — проверки доступа и цели")
    upd, ctx, msg = make_update(admin_id=7, args=["555"])
    await mod.kick_command(upd, ctx)
    check("только администраторам" in msg.replies[0], "не-админу отказ", str(msg.replies))
    check(ctx.bot.calls == [], "не-админ не дёргает Telegram API", str(ctx.bot.calls))

    upd, ctx, msg = make_update(chat_type="private", args=["555"])
    await mod.kick_command(upd, ctx)
    check("только в группе" in msg.replies[0], "в личке отказ", str(msg.replies))
    check(ctx.bot.calls == [], "в личке API не вызывается", str(ctx.bot.calls))

    upd, ctx, msg = make_update(args=["42"])
    await mod.kick_command(upd, ctx)
    check("Администратора забанить нельзя" in msg.replies[0],
          "админа забанить нельзя", str(msg.replies))
    check(ctx.bot.calls == [], "админ не тронут", str(ctx.bot.calls))

    upd, ctx, msg = make_update(args=["999"])
    await mod.kick_command(upd, ctx)
    check("Себя забанить нельзя" in msg.replies[0], "бота забанить нельзя", str(msg.replies))
    check(ctx.bot.calls == [], "бота не тронули", str(ctx.bot.calls))

    upd, ctx, msg = make_update(args=[])
    await mod.kick_command(upd, ctx)
    check("Кого?" in msg.replies[0], "пустая цель — подсказка", str(msg.replies))

    upd, ctx, msg = make_update(args=["abc"])
    await mod.kick_command(upd, ctx)
    check("числом" in msg.replies[0], "нечисловой id отклонён", str(msg.replies))

    # --------------------------------------------- 3. /kick — успешный путь
    print("3. /kick — перманентный бан и очистка сообщений")
    upd, ctx, msg = make_update(reply_user=FakeUser(555, "Spamer"))
    await mod.kick_command(upd, ctx)
    check(
        ctx.bot.calls == [("ban", -100, 555, True)],
        "только ban(revoke_messages=True), без разбана",
        str(ctx.bot.calls),
    )
    check("забанен навсегда" in msg.replies[0] and "сообщения удалены" in msg.replies[0],
          "ответ о перманентном бане", str(msg.replies))
    check(await db.is_user_banned(555), "бан продублирован в БД")

    upd, ctx, msg = make_update(args=["556"])
    await mod.kick_command(upd, ctx)
    check(
        ctx.bot.calls == [("ban", -100, 556, True)],
        "бан по id без разбана",
        str(ctx.bot.calls),
    )
    check(await db.is_user_banned(556), "бан по id продублирован в БД")

    # --------------------------------------------------- 4. ошибки Telegram
    print("4. /kick — ошибки Telegram")
    upd, ctx, msg = make_update(args=["557"], bot=FakeBot())
    ctx.bot.fail_ban = BadRequest("not enough rights to restrict/unrestrict chat member")
    await mod.kick_command(upd, ctx)
    check("Не удалось забанить" in msg.replies[0], "ошибка бана показана", str(msg.replies))
    check("not enough rights" in msg.replies[0], "причина передана", str(msg.replies))
    check([c[0] for c in ctx.bot.calls] == ["ban"], "unban не вызывался", str(ctx.bot.calls))
    check(not await db.is_user_banned(557), "при ошибке Telegram флаг не ставится")

    db.close_all()
    try:
        os.unlink(TEST_DB)
    except OSError:
        pass

    print()
    if FAILURES:
        print(f"❌ Провалено проверок: {len(FAILURES)}")
        for name in FAILURES:
            print(f"   - {name}")
        return 1
    print("✅ Все проверки прошли")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
