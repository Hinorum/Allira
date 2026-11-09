"""Последовательный запуск всех standalone-тестов репозитория.

Тесты в проекте — не pytest-функции, а обычные скрипты: каждый сам
создаёт временную БД и печатает отчёт. Параллельно их запускать нельзя:
скрипты делят переменные окружения и файлы в рабочей папке.

Запуск из корня репозитория:

    python -X utf8 tests/run_all.py

Код возврата — 1, если хотя бы один тест упал.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def discover() -> list[Path]:
    """Все test_*.py в корне репозитория, по алфавиту."""
    return sorted(ROOT.glob("test_*.py"))


def main() -> int:
    tests = discover()
    if not tests:
        print("Не найдено ни одного test_*.py")
        return 1

    print(f"Запускаю {len(tests)} тестов из {ROOT}")
    print("=" * 60)

    failures: list[tuple[str, int, float]] = []
    started_all = time.perf_counter()

    for index, path in enumerate(tests, start=1):
        print(f"[{index}/{len(tests)}] {path.name}")
        started = time.perf_counter()
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(path)],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.perf_counter() - started
        if result.returncode == 0:
            print(f"    OK ({elapsed:.1f} c)")
        else:
            print(f"    FAIL (код {result.returncode}, {elapsed:.1f} c)")
            failures.append((path.name, result.returncode, elapsed))
        print("-" * 60)

    total = time.perf_counter() - started_all
    print()
    if failures:
        print(f"❌ Упало тестов: {len(failures)} из {len(tests)}")
        for name, code, elapsed in failures:
            print(f"  - {name}: код {code}, {elapsed:.1f} c")
        print(f"Общее время: {total:.1f} c")
        return 1

    print(f"✅ Все тесты прошли: {len(tests)} за {total:.1f} c")
    return 0


if __name__ == "__main__":
    sys.exit(main())
