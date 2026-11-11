"""Регрессии ИИ-чата и автопостинга.

Проверяем то, что раньше ломалось тихо:

  1. очередь LLM снимает запрос после MAX_WAIT_SECONDS, а не держит вечно;
  2. circuit breaker не вызывает заблокированную основную модель;
  3. канал/анонимный админ без user_data не роняет память диалога;
  4. автопостинг учитывает время последнего поста после рестарта;
  5. ответ Pollinations проверяется на сигнатуру картинки, а не только
     на код 200 и размер;
  6. set_stat пишет абсолютное значение, а не приращение.
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


class FakeResponse:
    def __init__(self, content: str = "ok", status_code: int = 200):
        self.status_code = status_code
        self._content = content
        self.text = content

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class NoUserDataContext:
    """Контекст канала/анонимного админа: user_data отсутствует."""

    user_data = None


async def test_queue_timeout() -> None:
    print("1. Таймаут ожидания в очереди LLM")
    from utils.llm_queue import LlmQueue, QueueTimeout

    queue = LlmQueue(workers=1, max_queue=10, max_wait=0.05)

    async def slow():
        await asyncio.sleep(0.2)
        return "slow"

    first = asyncio.create_task(queue.submit(slow))
    await asyncio.sleep(0.01)

    timed_out = False
    try:
        await queue.submit(lambda: asyncio.sleep(0.01, result="fast"))
    except QueueTimeout:
        timed_out = True

    check(timed_out, "ожидание сверх лимита даёт QueueTimeout")
    check(await first == "slow", "первая задача всё равно завершилась")
    check(await queue.submit(lambda: asyncio.sleep(0, result="after")) == "after",
          "после таймаута очередь продолжает работать")
    check(queue.stats().timed_out >= 1,
          "таймаут учтён в статистике", str(queue.stats()))

    await queue.shutdown()


async def test_circuit_breaker() -> None:
    print("2. Circuit breaker пропускает сбойную основную модель")
    import utils.ai_responses as ai

    primary = "test/primary"
    ai._model_failures.clear()
    ai._model_last_failure.clear()
    ai.response_cache.clear()
    ai._model_failures[primary] = ai.CIRCUIT_BREAKER_THRESHOLD
    ai._model_last_failure[primary] = time.time()

    calls: list[str] = []

    async def fake_call(payload, headers, timeout=25.0, on_queue=None):
        calls.append(payload["model"])
        return FakeResponse(f"ответ от {payload['model']}")

    original = ai._call_openrouter
    ai._call_openrouter = fake_call
    try:
        answer = await ai.get_llm_response(
            "уникальный вопрос для проверки breaker",
            "системный промпт",
            primary,
            "test-key",
        )
    finally:
        ai._call_openrouter = original
        ai._model_failures.clear()
        ai._model_last_failure.clear()
        ai.response_cache.clear()

    check(bool(answer), "ответ получен через фолбэк", answer)
    check(bool(calls) and calls[0] != primary,
          "заблокированная модель не вызывается первой", str(calls))


async def test_user_data_none() -> None:
    print("3. Канал и анонимный админ без user_data")
    import handlers.message_handler as mh

    context = NoUserDataContext()
    check(mh._get_history(context) == [], "история без user_data пуста")
    mh._append_history(context, "user", "привет", "allira")
    check(mh.reset_dialog(context) == 0, "сброс диалога без user_data безопасен")

    class User:
        id = 1

    class Update:
        effective_user = User()

    try:
        await mh.mark_activity(Update(), context)
        activity_ok = True
    except Exception:
        activity_ok = False
    check(activity_ok, "mark_activity без user_data не падает")

    class NormalContext:
        user_data: dict = {}

    check(mh._get_history(NormalContext()) == [],
          "обычный user_data по-прежнему работает")


def test_autopost_schedule() -> None:
    print("4. Расписание автопостинга после рестарта")
    from tasks import autoposting

    class FakeJobQueue:
        def __init__(self):
            self.kwargs = None

        def get_jobs_by_name(self, name):
            return []

        def run_repeating(self, callback, **kwargs):
            self.kwargs = kwargs

    class FakeApp:
        def __init__(self):
            self.job_queue = FakeJobQueue()

    original_get = autoposting._sync_get_stat
    original_interval = autoposting.get_smart_interval
    autoposting.get_smart_interval = lambda: 43200
    try:
        autoposting._sync_get_stat = lambda key: int(time.time()) - 3600
        app = FakeApp()
        autoposting.setup_autoposting(app)
        first = app.job_queue.kwargs["first"]
        check(39000 <= first <= 39600,
              "после недавнего поста ждём остаток интервала", str(first))
        check(app.job_queue.kwargs["interval"] == 43200,
              "интервал не изменился", str(app.job_queue.kwargs))

        autoposting._sync_get_stat = lambda key: 0
        fresh = FakeApp()
        autoposting.setup_autoposting(fresh)
        first_fresh = fresh.job_queue.kwargs["first"]
        check(60 <= first_fresh <= 300,
              "без истории первый пост через 1-5 минут", str(first_fresh))
    finally:
        autoposting._sync_get_stat = original_get
        autoposting.get_smart_interval = original_interval


def test_image_signatures() -> None:
    print("5. Проверка сигнатуры картинки Pollinations")
    from tasks.autoposting import _looks_like_image

    check(_looks_like_image(b"\xff\xd8\xff\xe0" + b"0" * 2000),
          "JPEG распознан")
    check(_looks_like_image(b"\x89PNG\r\n\x1a\n" + b"0" * 2000),
          "PNG распознан")
    check(_looks_like_image(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"0" * 2000),
          "WebP распознан")
    check(_looks_like_image(b"GIF89a" + b"0" * 2000),
          "GIF распознан")
    check(not _looks_like_image(b"<html><body>error</body></html>" + b" " * 2000),
          "HTML-страница не считается картинкой")
    check(not _looks_like_image(b""), "пустой ответ не считается картинкой")


async def test_set_stat() -> None:
    print("6. Абсолютная запись счётчика")
    from utils.database import get_stat, init_db, set_stat

    init_db()
    await set_stat("test_abs_stat", 123)
    check(await get_stat("test_abs_stat") == 123,
          "set_stat записал значение")
    await set_stat("test_abs_stat", 7)
    check(await get_stat("test_abs_stat") == 7,
          "set_stat перезаписал, а не прибавил")


async def main() -> int:
    print("=" * 60)
    print("Тесты устойчивости ИИ-чата и автопостинга")
    print("=" * 60)

    await test_queue_timeout()
    await test_circuit_breaker()
    await test_user_data_none()
    test_autopost_schedule()
    test_image_signatures()
    await test_set_stat()

    print()
    if FAILURES:
        print(f"❌ Провалено проверок: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("✅ Все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
