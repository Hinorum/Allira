"""Хендлер автоимпорта: адреса + выписки → операции для налоговой базы.

Команды (только в ЛС владельца):
  /autoimport              — статус: кошельки, что нашлось, что требует проверки
  /autoimport (реплай на файл) — импорт выписки биржи (CSV или Excel) и
                            последующий скан кошельков

Кнопки:
  🔄 Сканировать — пересканировать адреса и записать операции
  💰 Налог      — посмотреть базу за текущий год

Главная идея: пользователь отдаёт адреса кошельков и выписки бирж, а бот
сам достраивает историю операций. То, что бот не смог классифицировать
однозначно, показывается списком с кнопками — поправить один тап.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from utils.common import escape_html

logger = logging.getLogger(__name__)

MAX_REVIEW_ROWS = 6


def _owner_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    return int(context.bot_data.get("CRYPTO_OWNER_ID") or 0)


async def _is_owner_chat(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user:
        return False
    user_id = update.effective_user.id
    creator_ids = context.bot_data.get("CREATOR_USER_IDS") or ()
    creator_username = (context.bot_data.get("CREATOR_USERNAME")
                        or "").lstrip("@").lower()
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


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Сканировать кошельки",
                                 callback_data="ai:scan"),
            InlineKeyboardButton("💰 Налог за год",
                                 callback_data="ai:tax"),
        ],
    ])


def _review_keyboard(reviews) -> InlineKeyboardMarkup:
    """Кнопки правки классификации — по одному тапу на операцию."""
    rows = []
    for r in reviews[:MAX_REVIEW_ROWS]:
        if not r.tx_id:
            continue
        rows.append([
            InlineKeyboardButton("↔️ Перемещение",
                                 callback_data=f"ai:fix:{r.tx_id}:internal"),
            InlineKeyboardButton("⬆️ Доход",
                                 callback_data=f"ai:fix:{r.tx_id}:income"),
            InlineKeyboardButton("⬇️ Реализация",
                                 callback_data=f"ai:fix:{r.tx_id}:disposal"),
        ])
    rows.append([
        InlineKeyboardButton("✅ Подтвердить всё",
                             callback_data="ai:confirm_all"),
    ])
    rows.append([
        InlineKeyboardButton("🔄 Сканировать кошельки",
                             callback_data="ai:scan"),
        InlineKeyboardButton("💰 Налог за год", callback_data="ai:tax"),
    ])
    return InlineKeyboardMarkup(rows)


# ------------------------------------------------------------- команда


async def auto_import_command(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """`/autoimport` — статус или импорт выписки."""
    if not await _is_owner_chat(update, context):
        return
    msg = update.message
    # Файл может прийти приложением к команде (подпись «/autoimport») или
    # реплаем на уже отправленный файл.
    document = msg.document if msg else None
    if not document and msg and msg.reply_to_message:
        document = msg.reply_to_message.document

    if document:
        await _import_document(update, context, document)
        return

    await _show_status(update, context)


async def _show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Что сейчас в учёте: кошельки, операции, что требует проверки."""
    from utils.auto_import import (DEFAULT_EVM_CHAIN_IDS, EVM_CHAIN_NAMES,
                                    _evm_chain_ids, pending_reviews)
    from utils.crypto_db import list_wallets

    owner = _owner_id(context)
    cfg = context.bot_data.get("BOT_CONFIG")
    wallets = list_wallets(owner)
    lines = ["<b>Автоимпорт</b>", ""]

    if not wallets:
        lines.append("Кошельков нет. Добавь адреса:")
        lines.append("<code>/wallet_add 0:…</code>")
        lines.append("<code>/wallet_add 0x…</code>")
    else:
        lines.append(f"Кошельков: <b>{len(wallets)}</b>")
        for w in wallets[:10]:
            lines.append(f"• {w.chain} {w.short_address}"
                         + (f" — {w.label}" if w.label else ""))
        if len(wallets) > 10:
            lines.append(f"…и ещё {len(wallets) - 10}")

    # Чем именно сканируем: без этого непонятно, почему по кошельку пусто.
    ton_key = bool(getattr(cfg, "tonapi_api_key", ""))
    evm_key = bool(getattr(cfg, "etherscan_api_key", ""))
    chain_ids = _evm_chain_ids(cfg) if cfg else list(DEFAULT_EVM_CHAIN_IDS)
    lines.append("")
    if any(w.chain == "evm" for w in wallets):
        if evm_key:
            names = ", ".join(EVM_CHAIN_NAMES.get(c, str(c))
                              for c in chain_ids)
            lines.append(f"🌐 EVM-сети: {names}")
        else:
            lines.append("⚠️ EVM-кошельки есть, но <code>ETHERSCAN_API_KEY</code> "
                         "не задан — их история не подтягивается")
    if any(w.chain == "ton" for w in wallets):
        lines.append("🌐 TON: TonAPI" + ("" if ton_key else " (без ключа)"))

    reviews = pending_reviews(owner)
    if reviews:
        lines.append("")
        lines.append(f"Требуют проверки: <b>{len(reviews)}</b>")
        for r in reviews[:MAX_REVIEW_ROWS]:
            lines.append(f"• {escape_html(r.message[:110])}")
        lines.append("")
        lines.append("Поправить — один тап под операцией.")
    else:
        lines.append("")
        lines.append("✅ Неоднозначных операций нет")

    lines.append("")
    lines.append("Пришли выписку биржи (CSV или Excel) реплаем на "
                 "<code>/autoimport</code> — разберу и запишу.")

    markup = _review_keyboard(reviews) if reviews else _menu_keyboard()
    await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                    reply_markup=markup)


# ------------------------------------------------------------- импорт выписки


async def _import_document(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            document):
    """Импорт выписки биржи из прикреплённого файла."""
    from utils.auto_import import import_statement

    msg = update.message
    name = document.file_name or "statement.csv"
    try:
        file = await context.bot.get_file(document.file_id)
        buf = await file.download_to_memory()
    except Exception as e:
        logger.error(f"/autoimport: файл не скачан: {e}", exc_info=True)
        await msg.reply_text(f"Не удалось скачать файл: {e}")
        return

    await msg.reply_text("Разбираю выписку…")
    result = await import_statement(_owner_id(context), buf.getvalue(), name)

    lines = ["<b>Выписка разобрана:</b>"]
    lines.append(f"• строк распознано: {result.rows}")
    lines.append(f"• проводок записано: {result.saved}")
    if result.duplicates:
        lines.append(f"• пропущено (уже было): {result.duplicates}")
    if result.errors:
        lines.append("")
        lines.append("<b>Что не удалось:</b>")
        for e in result.errors[:8]:
            lines.append(f"• {escape_html(e[:160])}")
    if result.review:
        lines.append("")
        lines.append("<b>Нужна проверка:</b>")
        for r in result.review[:MAX_REVIEW_ROWS]:
            lines.append(f"• {escape_html(r.message[:160])}")
        lines.append("")
        lines.append("Поправить — один тап под операцией.")

    from utils.auto_import import pending_reviews

    reviews = pending_reviews(_owner_id(context))
    markup = _review_keyboard(reviews) if reviews else _menu_keyboard()
    await msg.reply_text("\n".join(lines), parse_mode="HTML",
                         reply_markup=markup)

    # Сразу сканируем кошельки: выписка нужна, чтобы отличить вывод с биржи
    # от аирдропа — иначе перевод станет ложным доходом.
    await _run_scan(context, msg.chat_id)


# ------------------------------------------------------------- сканирование


async def auto_import_callback(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    """Кнопки автоимпорта."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("ai:fix:"):
        await _handle_fix(query, context, data)
        return

    if data == "ai:confirm_all":
        await _handle_confirm_all(query, context)
        return

    if data == "ai:scan":
        await query.message.reply_text("Сканирую кошельки…")
        await _run_scan(context, query.message.chat_id)
        return

    if data == "ai:tax":
        from handlers.crypto_tax import _send_tax_summary
        await _send_tax_summary(query.message.chat_id, context,
                                 _current_year())
        return


async def _handle_confirm_all(query, context) -> None:
    """Подтвердить все спорные операции разом."""
    from utils.auto_import import confirm_all, pending_reviews

    owner = _owner_id(context)
    count = confirm_all(owner)
    reviews = pending_reviews(owner)
    lines = [f"✅ Подтверждено операций: <b>{count}</b>", ""]
    if reviews:
        lines.append(f"Осталось на проверку: <b>{len(reviews)}</b>")
        for r in reviews[:MAX_REVIEW_ROWS]:
            lines.append(f"• {escape_html(r.message[:110])}")
    else:
        lines.append("✅ Неоднозначных операций нет")
    try:
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=_review_keyboard(reviews) if reviews
            else _menu_keyboard(),
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise


async def _handle_fix(query, context, data: str) -> None:
    """Правка классификации операции по кнопке."""
    from utils.auto_import import reclassify

    parts = data.split(":")
    if len(parts) != 4:
        return
    try:
        tx_id = int(parts[2])
    except ValueError:
        return
    kind = parts[3]

    ok, message = await reclassify(_owner_id(context), tx_id, kind)
    if not ok:
        await query.answer(f"Не получилось: {message}", show_alert=True)
        return

    # После правки перерисовываем список: операция уходит из проверки.
    from utils.auto_import import pending_reviews
    from utils.crypto_db import list_wallets

    owner = _owner_id(context)
    reviews = pending_reviews(owner)
    lines = [f"✅ {escape_html(message)}", ""]
    if reviews:
        lines.append(f"Осталось на проверку: <b>{len(reviews)}</b>")
        for r in reviews[:MAX_REVIEW_ROWS]:
            lines.append(f"• {escape_html(r.message[:110])}")
    else:
        lines.append("✅ Неоднозначных операций нет")
    lines.append("")
    lines.append("Налоговая база пересчитана автоматически.")

    try:
        await query.edit_message_text("\n".join(lines), parse_mode="HTML",
                                       reply_markup=_review_keyboard(reviews)
                                       if reviews else _menu_keyboard())
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise


async def _run_scan(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Фоновое сканирование: сеть бывает медленной, чат не должен висеть."""
    asyncio.create_task(_scan_task(context.bot, chat_id, context.bot_data))


async def _scan_task(bot, chat_id: int, bot_data: dict):
    from utils.auto_import import run

    try:
        cfg = (bot_data or {}).get("BOT_CONFIG")
        owner_id = int(getattr(cfg, "crypto_owner_id", 0) or 0)
        if not owner_id:
            await bot.send_message(chat_id, "Не задан владелец учёта "
                                         "(CRYPTO_OWNER_ID).")
            return
        result = await run(owner_id, cfg)
        lines = ["<b>Скан кошельков завершён:</b>"]
        lines.append(f"• найдено переводов: {result.moves}")
        lines.append(f"• записано операций: {result.saved}")
        if result.duplicates:
            lines.append(f"• уже было: {result.duplicates}")
        if result.internal:
            lines.append(f"• перемещений (не влияют на базу): {result.internal}")
        if result.errors:
            lines.append("")
            lines.append("<b>Ошибки:</b>")
            for e in result.errors[:5]:
                lines.append(f"• {escape_html(e[:160])}")
        if result.review:
            lines.append("")
            lines.append("<b>Нужна проверка:</b>")
            for r in result.review[:MAX_REVIEW_ROWS]:
                lines.append(f"• {escape_html(r.message[:160])}")
        lines.append("")
        lines.append("Поправить классификацию — один тап под операцией "
                     "в списке <code>/autoimport</code>.")
        await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
        await _send_tax_digest(bot, chat_id, owner_id)
    except Exception as e:
        logger.error(f"auto_import scan failed: {e}", exc_info=True)
        try:
            await bot.send_message(chat_id, f"Сбой сканирования: {e}")
        except Exception:
            pass


def _current_year() -> int:
    import time
    return time.localtime().tm_year


async def _send_tax_digest(bot, chat_id: int, owner_id: int,
                           year: Optional[int] = None):
    """Короткая сводка по базе: сколько дохода, база и налог."""
    from utils.price_oracle import format_rub
    from utils.tax_calc import compute_year_report

    year = year or _current_year()
    if not owner_id:
        return
    try:
        summary = await compute_year_report(owner_id, year)
    except Exception as e:
        logger.warning(f"auto_import: сводка не собралась: {e}")
        return

    lines = [
        f"<b>База за {year} год:</b>",
        f"• доход: {format_rub(float(summary.total_income_rub))}",
        f"• расходы по реализации: {format_rub(float(summary.total_cost_rub))}",
        f"• налоговая база: {format_rub(float(summary.taxable_income_rub))}",
        f"• <b>НДФЛ: {format_rub(float(summary.ndfl_total))}</b>",
    ]
    if summary.warnings:
        lines.append("")
        lines.append("Замечания: " + escape_html(summary.warnings[0][:160]))
    try:
        await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"auto_import: сводка не отправлена: {e}")
