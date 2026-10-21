"""Тесты очереди запросов и нового меню бота.

Проверяются:

  Очередь:
   1. Обслуживание строго FIFO — кто раньше спросил, тот раньше получил.
   2. Одновременных запросов не больше числа воркеров.
   3. Ожидающий получает свою позицию в очереди.
   4. Переполненная очередь даёт внятный отказ, а не молчание.
   5. Ошибка в задаче не роняет очередь.

  Меню:
   6. В главном меню есть все разделы, кнопки на месте.
   7. Раздел открывается и показывает команды.
   8. Быстрое действие вызывает нужную команду с пустыми аргументами.
   9. В меню «/» команды отсортированы: полезное вперёд.
"""

import asyncio
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


async def main() -> int:
    # ------------------------------------------------------------- очередь
    print("1. Очередь запросов: порядок и лимиты")
    from utils.llm_queue import LlmQueue, QueueFull

    queue = LlmQueue(workers=2, max_queue=10)
    order: list[str] = []
    active = 0
    max_active = 0

    async def job(tag: str, delay: float = 0.05):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(delay)
            order.append(tag)
            return tag
        finally:
            active -= 1

    started = time.monotonic()
    results = await asyncio.gather(*[
        queue.submit(lambda tag=tag: job(tag)) for tag in ("a", "b", "c", "d", "e")
    ])
    elapsed = time.monotonic() - started

    check(results == ["a", "b", "c", "d", "e"], "все задачи выполнены", str(results))
    check(order == ["a", "b", "c", "d", "e"],
          "порядок обслуживания — FIFO", str(order))
    check(max_active <= 2,
          "одновременных задач не больше числа воркеров", str(max_active))
    check(elapsed >= 0.1, "задачи шли последовательно, а не все сразу",
          f"{elapsed:.3f}с")

    print("2. Позиция в очереди")
    positions: list[int] = []

    async def slow_job():
        await asyncio.sleep(0.25)
        return "ok"

    async def on_wait(position: int) -> None:
        positions.append(position)

    tasks = [
        asyncio.create_task(queue.submit(slow_job, on_wait=on_wait))
        for _ in range(3)
    ]
    await asyncio.sleep(0.05)
    await asyncio.gather(*tasks)
    check(bool(positions), "уведомление о позиции пришло", str(positions))
    check(all(p >= 1 for p in positions),
          "позиция нумеруется с единицы", str(positions))
    check(len(set(positions)) >= 2,
          "позиции разные — очередь растёт", str(positions))

    print("3. Переполнение очереди")
    small = LlmQueue(workers=1, max_queue=2)

    async def hang():
        await asyncio.sleep(2)
        return "x"

    async def on_wait2(position: int) -> None:
        pass

    pending = [asyncio.create_task(small.submit(hang, on_wait=on_wait2))
               for _ in range(3)]
    await asyncio.sleep(0.05)
    overflowed = False
    try:
        await small.submit(hang)
    except QueueFull:
        overflowed = True
    check(overflowed, "переполнение даёт QueueFull, а не молчание")
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    print("4. Ошибка в задаче не роняет очередь")
    broken = LlmQueue(workers=1, max_queue=5)

    async def boom():
        raise RuntimeError("модель упала")

    failed = False
    try:
        await broken.submit(boom)
    except RuntimeError:
        failed = True
    check(failed, "ошибка задачи доходит до вызывающего")

    async def fine():
        return "ok"

    check(await broken.submit(fine) == "ok",
          "после ошибки очередь продолжает работать")

    stats = broken.stats()
    check(stats.processed >= 1, "счётчик обработанных растёт", str(stats))
    await broken.shutdown()
    await queue.shutdown()
    await small.shutdown()

    # --------------------------------------------------------------- меню
    print("5. Главное меню")
    from handlers import start_command as sc

    check(len(sc.MAIN_MENU.inline_keyboard) >= 4,
          "в меню несколько рядов кнопок",
          str(len(sc.MAIN_MENU.inline_keyboard)))
    check(len(sc.MENU_SECTIONS) == 6,
          "шесть разделов", str(len(sc.MENU_SECTIONS)))

    home_labels = [
        btn.text for row in sc.MAIN_MENU.inline_keyboard for btn in row
    ]
    check(any("Портфель" in t for t in home_labels),
          "раздел «Портфель» на виду", str(home_labels))
    check(any("Помощь" in t or "инструкция" in t for t in home_labels),
          "доступ к справке", str(home_labels))

    for section_id, section in sc.MENU_SECTIONS.items():
        markup = sc._section_markup(section_id)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        check(any("Главное меню" in t for t in labels),
              f"в разделе «{section_id}» есть возврат назад", str(labels))
        check(bool(section.get("text")),
              f"в разделе «{section_id}» есть описание")

    print("6. Раздел открывается по кнопке")

    class FakeQuery:
        def __init__(self, data):
            self.data = data
            self.answers = []
            self.edits = []

        async def answer(self, *args, **kwargs):
            self.answers.append(args)

        async def edit_message_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

    class FakeCtx:
        bot_data = {}

    query = FakeQuery("menu:portfolio")
    await sc.menu_callback(type("U", (), {"callback_query": query})(), FakeCtx())
    check(bool(query.edits), "раздел открылся", str(query.edits)[:80])
    if query.edits:
        text, kwargs = query.edits[0]
        check("Портфель" in text, "в разделе правильный текст", text[:80])
        check(kwargs.get("reply_markup") is not None,
              "у раздела есть кнопки")

    query = FakeQuery("menu:home")
    await sc.menu_callback(type("U", (), {"callback_query": query})(), FakeCtx())
    check(bool(query.edits) and "Привет" in query.edits[0][0],
          "возврат в главное меню работает")

    print("7. Быстрое действие вызывает команду")

    called = {}

    async def fake_balance(update, context):
        called["text"] = update.message.text
        called["args"] = list(context.args or [])
        called["user"] = update.message.from_user
        await update.message.reply_text("Портфель пуст")

    original = sc.commands()
    sc._COMMAND_MAP = {**original, "balance": fake_balance}
    try:
        class FakeChat:
            id = -100999

        class FakeUser:
            id = 4242
            username = "tester"

        class FakeBot:
            def __init__(self):
                self.sent = []

            async def send_message(self, text, **kwargs):
                self.sent.append((text, kwargs))
                return type("M", (), {"message_id": 1})()

        bot = FakeBot()
        query = FakeQuery("quick:balance")
        query.message = type("M", (), {"chat": FakeChat(), "message_id": 5})()
        query.from_user = FakeUser()
        ctx = FakeCtx()
        ctx.bot = bot
        ctx.args = ["лишний", "мусор"]

        await sc.quick_action_callback(
            type("U", (), {"callback_query": query})(), ctx)

        check(called.get("text") == "/balance",
              "команда вызвана с правильным текстом", str(called))
        check(called.get("args") == [],
              "аргументы прошлой команды сброшены", str(called.get("args")))
        check(called.get("user") is not None, "владелец передан")
        check(bool(bot.sent), "ответ ушёл в чат", str(bot.sent)[:80])
    finally:
        sc._COMMAND_MAP = original

    print("8. Кнопки меню ведут на существующие команды")
    known = set(sc.commands().keys())
    for section_id, section in sc.MENU_SECTIONS.items():
        for label, command in section.get("buttons") or []:
            if command == "clear":
                continue
            check(command in known,
                  f"раздел «{section_id}»: команда {command} существует")

    print("9. Порядок команд в меню «/»")
    import main as main_module

    names = [name for name, _ in main_module.BOT_COMMANDS]
    check(len(names) == len(set(names)), "нет повторов команд", str(names))
    check(names[0] == "start", "первой идёт /start", str(names[:3]))
    check(names.index("balance") < names.index("marketapptop"),
          "часто используемое выше редкого", str(names[:8]))
    check(len(names) <= 100, "команд не больше лимита Telegram", str(len(names)))
    for name, desc in main_module.BOT_COMMANDS:
        check(len(name) <= 32, f"имя команды {name} в лимите")
        check(len(desc) <= 256, f"описание команды {name} в лимите")

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
