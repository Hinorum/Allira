"""Единый прогон обновления: кошельки → биржи → сопоставление переводов.

Раньше это были три отдельных действия: `/autoimport` сканировал сети,
`/cex_sync` тянул сделки, а сопоставление переводов запускалось только
внутри скана кошельков. Из-за этого после одного лишь синка биржи вывод
на кошелёк оставался «доходом», пока человек не догадается ещё раз
открыть `/autoimport`.

Теперь у всех входов одна точка: `sync_all()`. Она не бросает исключения
наружу — сбой одного источника не отменяет остальные, а попадает в отчёт.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    """Что получилось за один прогон — для короткой сводки в чате."""

    wallets: int = 0
    moves: int = 0
    saved: int = 0
    duplicates: int = 0
    internal: int = 0
    review: int = 0
    rematched: int = 0
    cex_saved: int = 0
    cex_duplicates: int = 0
    cex_exchanges: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_saved(self) -> int:
        return self.saved + self.cex_saved


async def sync_all(owner_id: int, cfg) -> SyncReport:
    """Сканирует кошельки, синкает биржи и пересопоставляет переводы.

    `cfg` — BotConfig (или совместимый объект). Без кошельков сетевой скан
    пропускается: биржи при этом всё равно обновляются.
    """
    from tasks.cex_sync import sync_cex
    from utils.auto_import import rematch_transfers, run
    from utils.crypto_db import list_wallets

    report = SyncReport()
    owner_wallets = list_wallets(owner_id)
    report.wallets = len(owner_wallets)

    if owner_wallets:
        try:
            scan = await run(owner_id, cfg, wallets=owner_wallets)
            report.moves = scan.moves
            report.saved = scan.saved
            report.duplicates = scan.duplicates
            report.internal = scan.internal
            report.review = len(scan.review)
            report.errors.extend(scan.errors)
        except Exception as e:
            logger.error(f"sync_all: кошельки: {e}", exc_info=True)
            report.errors.append(f"кошельки: {e}")

    try:
        cex = await sync_cex(owner_id, cfg)
        report.cex_saved = cex.saved
        report.cex_duplicates = cex.duplicates
        report.cex_exchanges = cex.exchanges
        # sync_cex сам сопоставляет переводы после депозитов/выводов;
        # сохраняем его счётчик, чтобы сводка не показывала 0.
        report.rematched = getattr(cex, "rematched", 0)
        report.errors.extend(getattr(cex, "errors", []))
    except Exception as e:
        logger.error(f"sync_all: биржи: {e}", exc_info=True)
        report.errors.append(f"биржи: {e}")

    # Сопоставление обязательно после любого источника: оно переводит
    # ложные «доходы» в перемещения, если появилась вторая сторона перевода.
    try:
        report.rematched += rematch_transfers(owner_id)
    except Exception as e:
        logger.error(f"sync_all: сопоставление: {e}", exc_info=True)
        report.errors.append(f"сопоставление: {e}")

    return report
