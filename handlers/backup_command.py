"""Резервная копия SQLite-базы по команде /backup.

Бесплатный Render не даёт постоянного диска: при деплое `allira.db`
пересоздаётся, и вся история операций пропадает. Пока внешний бэкап
(Litestream/платный диск) не настроен, владелец может забрать копию в
Telegram — это лучше, чем ничего.

Копия делается через sqlite3.Connection.backup(), а не копированием файла:
в режиме WAL часть данных лежит в `-wal`, и обычный `copy` легко даёт
битую или устаревшую базу.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
from datetime import datetime

from telegram import InputFile, Update
from telegram.ext import ContextTypes

from utils import database
from utils.permissions import require_owner

logger = logging.getLogger(__name__)

# Bot API не принимает документы больше 50 МБ. Оставляем запас на заголовки
# и служебные данные, иначе большая база обрывается на середине отправки.
MAX_BACKUP_BYTES = 45 * 1024 * 1024


def create_backup(db_path: str | None = None) -> str:
    """Сделать согласованный снимок базы и вернуть путь к временному файлу.

    Вызывающий обязан удалить файл после отправки.
    """
    source_path = db_path or database.DB_PATH
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"файл базы не найден: {source_path}")

    fd, dest_path = tempfile.mkstemp(prefix="allira_backup_", suffix=".db")
    os.close(fd)
    source = sqlite3.connect(source_path)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    except Exception:
        try:
            os.unlink(dest_path)
        except OSError:
            pass
        raise
    finally:
        source.close()
    return dest_path


async def backup_command(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/backup` — отправить владельцу копию базы документом."""
    if not await require_owner(update, context):
        return

    await update.message.reply_text("Готовлю резервную копию базы…")
    try:
        path = await asyncio.to_thread(create_backup)
    except Exception as e:
        logger.error(f"backup: {e}", exc_info=True)
        await update.message.reply_text(
            f"Не удалось сделать резервную копию: {e}"
        )
        return

    try:
        size = os.path.getsize(path)
        if size > MAX_BACKUP_BYTES:
            await update.message.reply_text(
                "База больше 45 МБ — Telegram не примет такой документ. "
                "Скопируй файл с диска сервиса или настрой Litestream."
            )
            return
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        await update.message.reply_document(
            InputFile(path, filename=f"allira_backup_{stamp}.db"),
            caption=(
                f"Резервная копия allira.db · {size / 1024 / 1024:.1f} МБ\n"
                "Храни вне Telegram: файл содержит историю операций."
            ),
        )
    except Exception as e:
        logger.error(f"backup send: {e}", exc_info=True)
        await update.message.reply_text(
            f"Копия создана, но отправить её не удалось: {e}"
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
