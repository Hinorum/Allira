"""Тесты режиссуры длины ответа (utils/answer_length).

Проверяется то, из-за чего чат выглядел простынёй:

  1. Сценарий длины выбирается по природе запроса: «привет» — коротко,
     «расскажи подробно» — развёрнуто, «что такое X» — средне.
  2. В директиве есть конкретное число: модель держит цифру лучше, чем
     «будь кратко».
  3. Вступление («Конечно!», «Хороший вопрос») вырезается.
  4. Хвост-вопрос («Хочешь, расскажу подробнее?») вырезается.
  5. Лишние фразы режутся по границе предложения, а не по символам.
  6. Код и развёрнутые ответы не режутся.
  7. Если модель в прошлый раз разлилась, следующий план жмём сильнее.
"""

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


def main() -> int:
    print("=" * 60)
    print("Тест режиссуры длины ответа")
    print("=" * 60)

    from utils.answer_length import (LONG, MEDIUM, SHORT, count_sentences,
                                     direct, plan_for, strip_preamble,
                                     strip_trailing_offer, trim_to_plan)

    # --- 1. Сценарии.
    check(plan_for("привет").mode == SHORT, "«привет» → коротко")
    check(plan_for("как дела?").mode == SHORT, "«как дела?» → коротко")
    check(plan_for("расскажи подробно, как работает стейкинг").mode == LONG,
          "«расскажи подробно» → развёрнуто")
    check(plan_for("что такое стейкинг?").mode == MEDIUM,
          "«что такое X?» → средне")
    check(plan_for("BTC сегодня").mode == MEDIUM, "Обычная реплика → средне")

    # --- 2. В директиве есть число.
    plan = plan_for("что такое стейкинг?")
    check(any(ch.isdigit() for ch in plan.directive),
          "В директиве есть конкретное число", plan.directive)
    check("ДЛИНА ОТВЕТА" in plan.directive, "Директива помечена как длина")
    check(plan.max_sentences >= 2 and plan.max_chars >= 200,
          "У сценария разумные границы")

    # --- 3. Вступления.
    check(strip_preamble("Конечно! BTC растёт.") == "BTC растёт.",
          "«Конечно!» вырезано", strip_preamble("Конечно! BTC растёт."))
    check(strip_preamble("Хороший вопрос. Смотри сюда.") == "Смотри сюда.",
          "«Хороший вопрос» вырезано",
          strip_preamble("Хороший вопрос. Смотри сюда."))
    check(strip_preamble("С удовольствием! Вот что важно.") == "Вот что важно.",
          "«С удовольствием!» вырезано")
    check(strip_preamble("BTC растёт.") == "BTC растёт.",
          "Обычный ответ не тронут")

    # --- 4. Хвосты.
    check(strip_trailing_offer("Дивергенция. Хочешь, расскажу подробнее?")
          == "Дивергенция", "Хвост-вопрос вырезан")
    check(strip_trailing_offer("Дивергенция. Если хочешь, могу подробнее.")
          == "Дивергенция", "Хвост-оферта вырезана")
    check(strip_trailing_offer("Дивергенция.") == "Дивергенция.",
          "Ответ без хвоста не тронут")

    # --- 5. Подрезка по границе предложения.
    long_answer = " ".join(f"Фраза номер {i}." for i in range(1, 13))
    plan = plan_for("что такое стейкинг?")
    trimmed, was_trimmed = trim_to_plan(long_answer, plan)
    check(was_trimmed, "Длинный ответ подрезан")
    check(count_sentences(trimmed) <= plan.max_sentences,
          "Фраз не больше сценария",
          str(count_sentences(trimmed)))
    check(trimmed.endswith("…"), "Обрыв помечен многоточием", trimmed[-20:])
    check(trimmed.startswith("Фраза номер 1."),
          "Режем с начала, а не с середины")

    # --- 6. Код и развёрнутый ответ не режем.
    with_code = "Вот код:\n```python\nprint(1)\n```\nИ конец."
    out, trimmed_code = trim_to_plan(with_code, plan)
    check(not trimmed_code and "print(1)" in out,
          "Ответ с кодом не режется")
    long_plan = plan_for("расскажи подробно")
    out2, trimmed_long = trim_to_plan(long_answer, long_plan)
    check(not trimmed_long, "Развёрнутый ответ не режется",
          str(len(out2)))

    # --- 7. Обратная связь: предыдущая «излишка» жмёт план.
    tight = plan_for("что такое стейкинг?", prev_overflow=2)
    loose = plan_for("что такое стейкинг?")
    check(tight.max_sentences < loose.max_sentences,
          "После излишки план жмётся",
          f"{tight.max_sentences} vs {loose.max_sentences}")
    check("короче" in tight.directive.lower(),
          "В директиве сказано, что в прошлый раз было длинно")

    # --- 8. Полная склейка.
    answer = ("Конечно! Разберём по полочкам. "
              + " ".join(f"Фраза {i}." for i in range(1, 9))
              + " Хочешь, расскажу подробнее?")
    result, overflow = direct(answer, plan)
    check(not result.startswith("Конечно"), "Вступление убрано в склейке",
          result[:40])
    check("Хочешь" not in result, "Хвост-вопрос убран в склейке")
    check(count_sentences(result) <= plan.max_sentences,
          "Склейка держит сценарий", str(count_sentences(result)))
    check(overflow > 0, "Склейка сообщает, на сколько фраж ответ вылез",
          str(overflow))

    # --- 9. Склейка не ломает короткий ответ.
    short_answer = "BTC на грани. Жди выстрела."
    result2, overflow2 = direct(short_answer, plan)
    check(result2 == short_answer, "Короткий ответ не тронут", result2)
    check(overflow2 == 0, "Короткий ответ не считается излишкой")

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
    sys.exit(main())
