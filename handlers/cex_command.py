"""Хендлеры CEX: статус подключения, ручной запуск синка.

Команды (только в ЛС владельца):
  /cex_status    — что подключено, когда был последний синк
  /cex_sync      — запустить синк немедленно

Логика синка живёт в tasks.cex_sync.sync_cex: её же использует кнопка
«Обновить всё» в /autoimport, поэтому здесь только вызов и форматирование.
"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from utils.cex_api import get_cex_transfer_watermark, get_cex_watermark
from utils.common import escape_html
from utils.sources import cex_connections

logger = logging.getLogger(__name__)


def _owner_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    return int(context.bot_data.get("CRYPTO_OWNER_ID") or 0)


async def _is_owner_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user:
        return False
    user_id = update.effective_user.id
    creator_ids = context.bot_data.get("CREATOR_USER_IDS") or ()
    creator_username = (context.bot_data.get("CREATOR_USERNAME") or "").lstrip("@").lower()
    username = (update.effective_user.username or "").lower()
    if creator_ids and user_id not in creator_ids:
        await update.message.reply_text("Команды доступны только владельцу бота.")
        return False
    if not creator_ids and username != creator_username:
        await update.message.reply_text("Команды доступны только владельцу бота.")
        return False
    if not _owner_id(context):
        await update.message.reply_text("Владелец не настроен.")
        return False
    return True


def cex_status_text(cfg) -> str:
    """Общий текст про биржи: и для /cex_status, и для экрана источников."""
    lines = ["<b>Биржи</b>", ""]
    for name, ok in cex_connections(cfg):
        if not ok:
            if name == "Bybit":
                keys = "<code>BYBIT_API_KEY</code> / <code>BYBIT_API_SECRET</code>"
            else:
                keys = ("<code>OKX_API_KEY</code> / <code>OKX_API_SECRET</code> / "
                        "<code>OKX_PASSPHRASE</code>")
            lines.append(f"⚪ <b>{name}</b> — ключи не заданы ({keys})")
            continue
        ts_ms = max(get_cex_watermark(name.lower()),
                    get_cex_transfer_watermark(name.lower()))
        if ts_ms:
            when = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts_ms / 1000))
            lines.append(f"✅ <b>{name}</b> — последний синк: {when}")
        else:
            lines.append(f"✅ <b>{name}</b> — ни разу не синкался")

    lines.append("")
    lines.append("Ключи read-only хранятся только в ENV, в чат их присылать "
                 "не нужно. Сделки, депозиты и выводы подтягиваются сами — "
                 "выписка не требуется.")
    return "\n".join(lines)


async def cex_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать состояние подключений к CEX."""
    if not await _is_owner_chat(update, context):
        return
    cfg = context.bot_data.get("BOT_CONFIG")
    await update.message.reply_text(cex_status_text(cfg), parse_mode="HTML")


async def cex_sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запустить синк с обеих подключённых бирж."""
    if not await _is_owner_chat(update, context):
        return
    cfg = context.bot_data.get("BOT_CONFIG")
    if not cfg or not any(ok for _, ok in cex_connections(cfg)):
        await update.message.reply_text(
            "Ни одна биржа не подключена. Задай ключи в .env или /cex_status."
        )
        return

    await update.message.reply_text("Запускаю синк… Результат придёт в этот чат.")
    asyncio.create_task(_cex_sync_task(context.bot, update.message.chat_id, cfg))


async def _cex_sync_task(bot, chat_id: int, cfg) -> None:
    """Длинная фоновая задача синка — без неё /cex_sync блокирует бота."""
    from tasks.cex_sync import sync_cex

    owner_id = int(getattr(cfg, "crypto_owner_id", 0) or 0)
    if not owner_id:
        try:
            await bot.send_message(chat_id, "Не задан владелец учёта.")
        except Exception:
            pass
        return

    report = await sync_cex(owner_id, cfg)
    if not report.configured:
        try:
            await bot.send_message(chat_id, "Ни одна биржа не подключена.")
        except Exception:
            pass
        return

    summary_lines: list[str] = ["<b>Синк CEX завершён:</b>"]
    for ex in report.exchanges:
        summary_lines.append(f"\n<b>{ex.title}</b>")
        summary_lines.append(f"  • новых: {ex.saved}, дублей: {ex.duplicates}")
        transfers_saved = getattr(ex, "transfers_saved", 0)
        if transfers_saved:
            summary_lines.append(f"  • из них переводов: {transfers_saved}")
        if ex.errors:
            summary_lines.append("  • ошибки:")
            for e in ex.errors[:5]:
                summary_lines.append(f"    · {escape_html(str(e)[:160])}")

    summary_lines.append(
        f"\nИтого: <b>{report.saved}</b> новых, {report.duplicates} дублей."
    )
    if report.rematched:
        summary_lines.append(
            f"Сопоставлено переводов: <b>{report.rematched}</b>."
        )
    try:
        await bot.send_message(chat_id, "\n".join(summary_lines),
                               parse_mode="HTML")
    except Exception as e:
        logger.error(f"cex_sync_task: send failed: {e}")
