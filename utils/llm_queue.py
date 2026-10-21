"""Очередь запросов к LLM.

Зачем: на бесплатном тарифе OpenRouter несколько одновременных запросов
к одной и той же модели упираются в 429 — и пользователь получает
«Слишком много запросов! Дай мне минутку передохнуть...».

Раньше стоял `asyncio.Semaphore(4)`: запросы проходили одновременно, по
четыре. Порядок обслуживания у семафора был честным, но никто не знал,
что запрос стоит в очереди — юзер видел только «печатает…» и через пол
минуты получал отказ.

Здесь:
  * обслуживание строго FIFO — кто раньше спросил, тот раньше получил;
  * одновременных запросов меньше, чем воркеров по умолчанию не
    запускается: два параллельных вызова не давят лимит бесплатного
    тарифа так, как четыре;
  * ожидающий знает свою позицию и может получить внятный отказ, если
    очередь переполнена.

Очередь общая для всех точек входа: чат, инлайн-режим, автопостинг. По
этой причине пост в канал ждёт за живыми вопросами пользователей, а не
забирает лимит себе.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Сколько запросов к LLM обслуживается одновременно.
# Два — компромисс: 429 на бесплатном тарифе случается уже при трёх
# одновременных запросах к одной модели, а один поток совсем не
# использует процессор (запрос идёт по сети).
WORKERS = 2

# Сколько запросов может ждать. Больше — отказ с внятным сообщением:
# ждать полчаса бесполезно, лучше сказать «позже». Считаются и ждущие,
# и те, что уже в работе.
MAX_QUEUE = 32

# Больше этого времени ждать бессмысленно — пользователь уйдёт.
MAX_WAIT_SECONDS = 120


@dataclass
class _Job:
    factory: Callable[[], Awaitable[Any]]
    future: "asyncio.Future"
    on_wait: Optional[Callable[[int], Awaitable[None]]] = None
    enqueued_at: float = 0.0
    notified: bool = False


@dataclass
class QueueStats:
    waiting: int = 0
    active: int = 0
    processed: int = 0
    rejected: int = 0
    workers: int = WORKERS
    max_queue: int = MAX_QUEUE
    extra: dict = field(default_factory=dict)


class QueueFull(Exception):
    """Очередь переполнена — обслужить не скоро."""


class LlmQueue:
    """FIFO-очередь с пулом воркеров."""

    def __init__(self, workers: int = WORKERS, max_queue: int = MAX_QUEUE):
        self._workers_count = max(1, workers)
        self._max_queue = max_queue
        self._queue: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self._active = 0
        self._processed = 0
        self._rejected = 0

    # ------------------------------------------------------- жизненный цикл

    def _ensure_started(self) -> None:
        """Ленивый старт: очередь поднимается при первом же запросе.

        Раньше лимит задавался вручную в main.py, и при первом обращении
        к LLM до инициализации семафор вовсе не существовал бы.
        """
        if self._started:
            return
        self._started = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._started = False
            return
        self._tasks = [
            loop.create_task(self._worker(i), name=f"llm-queue-{i}")
            for i in range(self._workers_count)
        ]
        logger.info(f"Очередь LLM поднята: воркеров {self._workers_count}, "
                    f"максимум в очереди {self._max_queue}")

    async def shutdown(self) -> None:
        """Остановить воркеров (на деплое/остановке сервиса)."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        self._started = False

    # ---------------------------------------------------------- обслуживание

    async def _worker(self, index: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job.future.done():
                    continue
                self._active += 1
                try:
                    result = await job.factory()
                    if not job.future.done():
                        job.future.set_result(result)
                except asyncio.CancelledError:
                    if not job.future.done():
                        job.future.cancel()
                    raise
                except Exception as e:
                    if not job.future.done():
                        job.future.set_exception(e)
                finally:
                    self._active -= 1
                    self._processed += 1
            finally:
                self._queue.task_done()

    async def submit(
        self,
        factory: Callable[[], Awaitable[Any]],
        on_wait: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> Any:
        """Поставить задачу в очередь и дождаться результата.

        on_wait(position) вызывается, если за запросом уже есть работа:
        position — номер в очереди (1 — ты первый в ожидании).
        """
        self._ensure_started()
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        # Граница считается по всем заявкам в системе (ждущие + в работе),
        # а не по одной очереди: заявка, которую воркер вот-вот заберёт,
        # тоже занимает место. Иначе «в очереди» считалось бы на воркеров
        # больше, чем настроено.
        if self._queue.qsize() + self._active >= self._max_queue:
            self._rejected += 1
            raise QueueFull(
                f"очередь запросов переполнена ({self._max_queue})"
            )

        position = self._queue.qsize() + 1
        job = _Job(
            factory=factory,
            future=future,
            on_wait=on_wait,
            enqueued_at=loop.time(),
        )
        await self._queue.put(job)

        if on_wait is not None and position > 1:
            try:
                await on_wait(position)
            except Exception as e:
                logger.debug(f"on_wait не отработал: {e}")

        return await future

    # ------------------------------------------------------------- статистика

    def stats(self) -> QueueStats:
        return QueueStats(
            waiting=self._queue.qsize(),
            active=self._active,
            processed=self._processed,
            rejected=self._rejected,
            workers=self._workers_count,
            max_queue=self._max_queue,
        )


# Общая очередь на всё приложение.
llm_queue = LlmQueue()
