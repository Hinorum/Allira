"""Границы налогового года по московскому времени.

Раньше каждый модуль считал начало года через ``time.mktime`` — то есть
по локальной таймзоне процесса. На Render выставлен ``TZ=Europe/Moscow``,
но локальный запуск и тесты могли идти в UTC или другой зоне, и сделка
31.12 в 23:30 МСК попадала в следующий год. Для 3-НДФЛ даты берутся по
МСК, поэтому границы фиксируем явно.

Москва живёт в UTC+3 круглый год: переход на летнее время отменён
в 2014 году. Фиксированный offset не требует пакета ``tzdata`` и
одинаково работает на Windows, Linux и в контейнере.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

MOSCOW_TZ = timezone(timedelta(hours=3))


def moscow_year_start(year: int) -> int:
    """Unix-время 00:00:00 1 января года по МСК."""
    return int(datetime(year, 1, 1, tzinfo=MOSCOW_TZ).timestamp())


def moscow_year_end(year: int) -> int:
    """Unix-время 00:00:00 1 января следующего года по МСК.

    Именно ``<`` на этой границе отделяет операции года от следующего:
    ``year_start <= ts < year_end``.
    """
    return moscow_year_start(year + 1)
