"""Тесты «живого» чата: разметка, индикатор, быстрые ответы, голос ошибок.

Проверяется то, что делает чат «живым», а не списком текста:

  1. Markdown ответа превращается в Telegram HTML: `*курсив*`, `**жирный**`,
     код, ссылки. Раньше промпты требовали разметку, а ответы уходили
     обычным текстом — юзер видел asterиски.
  2. Чужой HTML из ответа модели экранируется и не может попасть в чат.
  3. Длинный ответ режется на части так, что HTML-теги не рвутся.
  4. Ошибка провайдера показывается голосом персонажа, а не текстом
     «Все модели временно недоступны».
  5. Под ответом есть быстрые кнопки, и каждая ведёт на существующую
     команду.
  6. В промпт крипто-персонажа попадают живые данные рынка с правилом
     «только эти цифры».
  7. Индикатор «печатает…» продлевается, пока модель думает.
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


async def main() -> int:
    print("=" * 60)
    print("Тест живого чата")
    print("=" * 60)

    # --- 1. Разметка.
    from utils.text_format import (split_text, strip_html,
                                   to_telegram_chunks, to_telegram_html)

    html = to_telegram_html("**жирно** и *курсивно* и `код`")
    check("<b>жирно</b>" in html, "Жирный → <b>", html)
    check("<i>курсивно</i>" in html, "Курсив → <i>", html)
    check("<code>код</code>" in html, "Код → <code>", html)

    html = to_telegram_html("```python\nprint(1)\n```")
    check("<pre>" in html and "print(1)" in html, "Блок кода → <pre>", html)

    html = to_telegram_html("[CoinGecko](https://coingecko.com)")
    check('<a href="https://coingecko.com">CoinGecko</a>' in html,
          "Ссылка → <a>", html)

    # --- 2. Безопасность: чужой HTML экранируется.
    html = to_telegram_html("<script>alert(1)</script>")
    check("<script>" not in html and "&lt;script&gt;" in html,
          "Чужой HTML экранирован", html)

    # --- 3. Разбиение длинного ответа.
    long_text = "Абзац про крипту. " * 400
    chunks = to_telegram_chunks(long_text)
    check(len(chunks) > 1, "Длинный ответ режется на части", str(len(chunks)))
    check(all(len(c) <= 4096 for c in chunks),
          "Каждая часть влезает в лимит Telegram",
          str(max(len(c) for c in chunks)))
    check(all(c.count("<b>") == c.count("</b>") for c in chunks),
          "Теги не рвутся между частями")
    check("".join(strip_html(c) for c in chunks).count("Абзац") == 400,
          "Ни одно слово не потерялось при разбиении")

    # Разрыв по границе абзаца, а не по середине слова.
    parts = split_text("Первый абзац.\n\nВторой абзац.", limit=20)
    check(len(parts) == 2, "Разбиение идёт по абзацам", str(parts))

    # --- 4. Ошибки голосом персонажа.
    from utils.ai_responses import (ERR_NO_MODELS, ERR_TECHNICAL,
                                    PERSONA_ERRORS, is_service_error,
                                    persona_service_error)

    allira = persona_service_error(ERR_NO_MODELS, "allira")
    lane = persona_service_error(ERR_NO_MODELS, "lane")
    check(allira != ERR_NO_MODELS, "Ошибка переведена на язык Аллиры", allira)
    check(lane != ERR_NO_MODELS, "Ошибка переведена на язык Лэйн", lane)
    check(allira != lane, "У персонажей разные формулировки")
    check(is_service_error(ERR_NO_MODELS), "Служебная ошибка опознаётся")
    check(persona_service_error("обычный ответ", "allira") == "обычный ответ",
          "Обычный ответ не трогаем")
    check(set(PERSONA_ERRORS) == {"allira", "lane"},
          "Ошибки описаны для обоих персонажей")

    # --- 5. Быстрые ответы.
    import handlers.message_handler as mh

    keyboard = mh._quick_keyboard("allira")
    check(keyboard is not None, "Клавиатура быстрых ответов есть")
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    check(len(labels) >= 3, "Быстрых ответов несколько", str(labels))
    datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    check(all(d and d.startswith("quick:") for d in datas),
          "Кнопки ведут на команды", str(datas))

    from handlers.start_command import commands
    cmds = commands()
    missing = [d.split(":", 1)[1] for d in datas
               if d.split(":", 1)[1] not in cmds]
    check(not missing, "Все быстрые ответы ведут на существующие команды",
          str(missing))

    # --- 6. Живые данные рынка в промпте.
    from utils.market import MARKET_FACTS_RULES

    check("ТОЛЬКО эти цифры" in MARKET_FACTS_RULES,
          "В правилах рынка есть запрет выдумывать цифры")
    check("не инструкции" in MARKET_FACTS_RULES,
          "Данные помечены как данные, а не инструкции")

    # --- 7. Индикатор «печатает…» продлевается.
    calls = {"n": 0}

    class FakeBot:
        async def send_chat_action(self, chat_id, action):
            calls["n"] += 1

    stop = asyncio.Event()

    async def refresher():
        await mh._keep_typing(FakeBot(), 1, stop)

    task = asyncio.create_task(refresher())
    await asyncio.sleep(mh.TYPING_REFRESH_SECONDS * 1.4)
    stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    check(calls["n"] >= 2, "Индикатор продлевается, а не показывается один раз",
          str(calls["n"]))

    # --- 8. Прямой вопрос в группе не остаётся без ответа.
    check(mh._looks_like_question("аллира, что будет с биткоином?"),
          "Вопрос со знаком «?»")
    check(mh._looks_like_question("аллира сколько стоит TON"),
          "Вопрос после имени персонажа")
    check(mh._looks_like_question("@AlliraCryptoQueen_bot как дела"),
          "Вопрос после упоминания бота")
    check(not mh._looks_like_question("привет всем"),
          "Обычная реплика — не вопрос")
    check(mh.QUESTION_CHANCE > mh.RESPONSE_CHANCE,
          "На вопрос шанс ответить выше, чем на болтовню")

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
    sys.exit(asyncio.run(main()))
