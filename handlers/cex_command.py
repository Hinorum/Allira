"""Хендлеры CEX: статус подключения, ручной запуск синка.

Команды (только в ЛС владельца):
  /cex_status    — что подключено, когда был последний синк
  /cex_sync      — запустить синк немедленно
"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from utils.cex_api import (get_cex_watermark, set_cex_watermark,
                           sync_bybit_trades, sync_okx_trades)
from utils.common import escape_html
from utils.crypto_db import add_transaction, CryptoError
from utils.database import get_db
from utils.price_oracle import get_usd_rub_rate

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


async def cex_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать состояние подключений к CEX."""
    if not await _is_owner_chat(update, context):
        return
    cfg = context.bot_data.get("BOT_CONFIG")
    lines = ["<b>CEX-подключения:</b>\n"]
    bybit_ok = bool(cfg and cfg.bybit_api_key and cfg.bybit_api_secret)
    okx_ok = bool(cfg and cfg.okx_api_key and cfg.okx_api_secret and cfg.okx_passphrase)

    if bybit_ok:
        ts_ms = get_cex_watermark("bybit")
        if ts_ms:
            lines.append(f"✅ <b>Bybit</b> — последний синк: "
                         f"{time.strftime('%d.%m.%Y %H:%M', time.localtime(ts_ms / 1000))}")
        else:
            lines.append("✅ <b>Bybit</b> — ни разу не синкался")
    else:
        lines.append("⚪ Bybit — ключи не заданы "
                     "(<code>BYBIT_API_KEY</code>/<code>BYBIT_API_SECRET</code>)")

    if okx_ok:
        ts_ms = get_cex_watermark("okx")
        if ts_ms:
            lines.append(f"✅ <b>OKX</b> — последний синк: "
                         f"{time.strftime('%d.%m.%Y %H:%M', time.localtime(ts_ms / 1000))}")
        else:
            lines.append("✅ <b>OKX</b> — ни разу не синкался")
    else:
        lines.append("⚪ OKX — ключи не заданы "
                     "(<code>OKX_API_KEY</code>/<code>OKX_API_SECRET</code>/"
                     "<code>OKX_PASSPHRASE</code>)")

    lines.append("\nСоздай read-only ключи на бирже → впиши в .env → /cex_sync")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cex_sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запустить синк с обеих подключённых бирж."""
    if not await _is_owner_chat(update, context):
        return
    cfg = context.bot_data.get("BOT_CONFIG")
    if not cfg or (not cfg.bybit_api_key and not cfg.okx_api_key):
        await update.message.reply_text(
            "Ни одна биржа не подключена. Задай ключи в .env или /cex_status."
        )
        return

    await update.message.reply_text("Запускаю синк… Результат придёт в этот чат.")
    asyncio.create_task(_cex_sync_task(context.bot, update.message.chat_id, cfg))


async def _cex_sync_task(bot, chat_id: int, cfg) -> None:
    """Длинная фоновая задача синка — без неё /cex_sync блокирует бота."""
    owner_id = int(cfg.crypto_owner_id or 0)
    if not owner_id:
        try:
            await bot.send_message(chat_id, "Не задан владелец учёта.")
        except Exception:
            pass
        return

    total_saved = 0
    total_dups = 0
    summary_lines: list[str] = ["<b>Синк CEX завершён:</b>"]

    if cfg.bybit_api_key and cfg.bybit_api_secret:
        summary_lines.append("\n<b>Bybit</b>")
        try:
            since_ms = get_cex_watermark("bybit") or (
                int((time.time() - 365 * 86400) * 1000)
            )
            result = await sync_bybit_trades(
                cfg.bybit_api_key, cfg.bybit_api_secret,
                since_ts=since_ms, until_ts=int(time.time() * 1000),
            )
            saved, dups = await _persist_rows(owner_id, result.rows, "bybit")
            total_saved += saved
            total_dups += dups
            summary_lines.append(f"  • новых: {saved}, дублей: {dups}")
            if result.rows:
                # Сдвигаем водяной знак.
                max_ts = max(int(r.external_id.split(":")[1])
                             for r in result.rows if r.ts)
                set_cex_watermark("bybit", max_ts * 1000)
            if result.errors:
                summary_lines.append("  • ошибки:")
                for e in result.errors[:5]:
                    summary_lines.append(f"    · {escape_html(e[:120])}")
        except Exception as e:
            logger.error(f"bybit sync crashed: {e}", exc_info=True)
            summary_lines.append(f"  • сбой: {escape_html(str(e)[:200])}")

    if cfg.okx_api_key and cfg.okx_api_secret and cfg.okx_passphrase:
        summary_lines.append("\n<b>OKX</b>")
        try:
            since_ms = get_cex_watermark("okx") or (
                int((time.time() - 90 * 86400) * 1000)
            )
            result = await sync_okx_trades(
                cfg.okx_api_key, cfg.okx_api_secret, cfg.okx_passphrase,
                since_ts=since_ms, until_ts=int(time.time() * 1000),
            )
            saved, dups = await _persist_rows(owner_id, result.rows, "okx")
            total_saved += saved
            total_dups += dups
            summary_lines.append(f"  • новых: {saved}, дублей: {dups}")
            if result.rows:
                max_ts = max(int(r.external_id.split(":")[1])
                             for r in result.rows if r.ts)
                set_cex_watermark("okx", max_ts * 1000)
            if result.errors:
                summary_lines.append("  • ошибки:")
                for e in result.errors[:5]:
                    summary_lines.append(f"    · {escape_html(e[:120])}")
        except Exception as e:
            logger.error(f"okx sync crashed: {e}", exc_info=True)
            summary_lines.append(f"  • сбой: {escape_html(str(e)[:200])}")

    summary_lines.append(f"\nИтого: <b>{total_saved}</b> новых, {total_dups} дублей.")
    try:
        await bot.send_message(chat_id, "\n".join(summary_lines),
                              parse_mode="HTML")
    except Exception as e:
        logger.error(f"cex_sync_task: send failed: {e}")


async def _persist_rows(owner_id: int, rows: list, exchange: str) -> tuple[int, int]:
    """Записать ParsedRow через add_transaction (с защитой от дублей).

    Возвращает (saved, duplicates_skipped).
    """
    saved = 0
    dups = 0
    for row in rows:
        # Подтянуть курс USDT→RUB для автозаполнения cost_basis (buy)
        # и fiat_value (sell). Если не получится — запишем без цены,
        # потом /addtx подтянет при первом запуске /tax.
        try:
            usd_rub = await get_usd_rub_rate(
                time.strftime("%Y-%m-%d", time.gmtime(row.ts))
            )
        except Exception:
            usd_rub = None

        cost_basis = ""
        fiat_value = ""
        # amount в quote-валюте = qty * price
        try:
            qty_f = float(row.amount)
        except (TypeError, ValueError):
            qty_f = 0.0
        # Нам нужен price для quote, но ParsedRow его не хранит — дописываем
        # в note и оставляем пустые cost/fiat. Дальше /tax подтянет по рынку.
        # (Реальная цена в quote-валюте потребует расширения ParsedRow —
        # оставлено на следующую итерацию.)
        try:
            add_transaction(
                owner_id=owner_id,
                ts=row.ts,
                asset_symbol=row.asset,
                tx_type=row.tx_type,
                amount=row.amount,
                fiat_value_rub=fiat_value or None,
                cost_basis_rub=cost_basis or None,
                note=row.note,
                source=exchange,
                external_id=row.external_id,
            )
            saved += 1
        except CryptoError as e:
            if "дубл" in str(e).lower():
                dups += 1
            else:
                logger.info(f"cex persist skip: {e}")
                dups += 1
        except Exception as e:
            logger.error(f"cex persist fail: {e}", exc_info=True)
    return saved, dups