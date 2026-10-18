"""Кольцевой буфер последних логов для сервиса управления.

Заводится один RecentLogHandler на корневой logger: он держит последние
N отформатированных строк в памяти (без записи на диск — диска на
бесплатном хостинге мало, а для диагностики нужны только свежие строки).

Веб-панель отдаёт их в /admin/api/logs, админ-меню — командой «Логи».
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

# Уровни, которые буферизуем (WARNING несём всегда — в нём причины падений).
DEFAULT_MIN_LEVEL = logging.INFO

_buffer: "RecentLogHandler | None" = None
_lock = threading.Lock()


class RecentLogHandler(logging.Handler):
    """Хранит последние capacity строк лога в deque."""

    def __init__(self, capacity: int = 400,
                 min_level: int = DEFAULT_MIN_LEVEL):
        super().__init__(level=min_level)
        self.capacity = capacity
        self._lines: deque[dict] = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            "%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # traceback'и режем: полные стеки есть в основном логе, в буфер
            # берём только первые строки, чтобы не тащить килобайты.
            msg = self.format(record)
            if record.exc_info:
                first = str(record.exc_info[1])
                msg = f"{msg} | {first}"
            with _lock:
                self._lines.append({
                    "ts": int(record.created),
                    "level": record.levelname,
                    "logger": record.name,
                    "text": msg[:500],
                })
        except Exception:
            pass  # логгер не должен падать из-за буфера

    def tail(self, limit: int = 150, level: str = "") -> list[dict]:
        with _lock:
            lines = list(self._lines)
        if level:
            wanted = {l.strip().upper() for l in level.split(",") if l.strip()}
            lines = [l for l in lines if l["level"] in wanted]
        return lines[-limit:]


def setup_log_buffer(application=None) -> "RecentLogHandler":
    """Повесить буфер на корневой logger (один раз)."""
    global _buffer
    with _lock:
        if _buffer is None:
            _buffer = RecentLogHandler()
            logging.getLogger().addHandler(_buffer)
    return _buffer


def recent_logs(limit: int = 150, level: str = "") -> list[dict]:
    """Последние строки лога (пусто, если буфер ещё не заведён)."""
    if _buffer is None:
        return []
    return _buffer.tail(limit=limit, level=level)
