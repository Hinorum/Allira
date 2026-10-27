"""Хендлер автоимпорта: источники → один прогон → налоговая база.

Экран `/autoimport` отвечает на три вопроса без знания команд:

  • что уже отдано боту — кошельки и биржи;
  • чего не хватает до отчёта — одна строка, а не список команд;
  • что сделать сейчас — «Добавить кошельки» или «Обновить всё».

Адреса принимаются одним сообщением: каждый с новой строки, через запятую
или с маркером списка; сеть определяется по виду адреса. После добавления
история подтягивается сама — отдельный заход в /autoimport не нужен.

Кнопки:
  ➕ Добавить кошельки — включить приём списка адресов
  🔄 Обновить всё      — кошельки + биржи + сопоставление переводов
  🔌 Биржи             — что подключено и когда был синк
  💰 Налог за год      — сводка 3-НДФЛ
  📥 Источники         — этот экран

Экран рассчитан на личный чат с ботом: адреса и суммы не стоит
показывать в общем чате.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from utils.common import escape_html

logger = logging.getLogger(__name__)

MAX_REVIEW_ROWS = 6
MAX_WALLETS_SHOWN = 8


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


# ------------------------------------------------------------- клавиатуры


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Добавить кошельки",
                                 callback_data="ai:wallets"),
            InlineKeyboardButton("🔄 Обновить всё",
                                 callback_data="ai:sync_all"),
        ],
        [
            InlineKeyboardButton("🔌 Биржи", callback_data="ai:cex"),
            InlineKeyboardButton("💰 Налог за год", callback_data="ai:tax"),
        ],
        [
            InlineKeyboardButton("📥 Источники и готовность",
                                 callback_data="ai:status"),
        ],
    ])


def _wait_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Отмена", callback_data="ai:wait_cancel")],
    ])


def _cex_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить всё", callback_data="ai:sync_all")],
        [InlineKeyboardButton("⬅️ Источники", callback_data="ai:status")],
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
        InlineKeyboardButton("🔄 Обновить всё", callback_data="ai:sync_all"),
        InlineKeyboardButton("📥 Источники", callback_data="ai:status"),
    ])
    rows.append([
        InlineKeyboardButton("💰 Налог за год", callback_data="ai:tax"),
    ])
    return InlineKeyboardMarkup(rows)


def _wallets_prompt() -> str:
    return (
        "➕ <b>Добавить кошельки</b>\n\n"
        "Пришли адреса одним сообщением — каждый с новой строки, через "
        "запятую или списком. Сеть определю по виду адреса.\n\n"
        "Можно с подписью:\n"
        "<code>0:abc… основной</code>\n"
        "<code>0x123… биржа</code>\n\n"
        "Поддерживаются TON, EVM, Bitcoin, TRON и Solana. "
        "Когда пришлёшь — сразу подтяну историю."
    )


def _start_wait_wallets(context: ContextTypes.DEFAULT_TYPE) -> None:
    from utils.sources import AWAIT_WALLETS_KEY

    context.user_data[AWAIT_WALLETS_KEY] = True


# ------------------------------------------------------------- экран статуса


def _build_status(context: ContextTypes.DEFAULT_TYPE,
                  year: Optional[int] = None):
    """Текст и клавиатура экрана источников — без отправки в Telegram."""
    from utils.auto_import import pending_reviews
    from utils.sources import sources_snapshot

    owner = _owner_id(context)
    cfg = context.bot_data.get("BOT_CONFIG")
    snap = sources_snapshot(owner, cfg, year)
    reviews = pending_reviews(owner)

    lines = [f"<b>Источники и готовность</b> · {snap.year}", ""]

    if snap.wallets:
        lines.append(f"👛 Кошельки: <b>{len(snap.wallets)}</b>")
        for w in snap.wallets[:MAX_WALLETS_SHOWN]:
            suffix = f" — {escape_html(w.label)}" if w.label else ""
            lines.append(
                f"• <code>{w.chain}</code> {escape_html(w.short_address)}{suffix}"
            )
        if len(snap.wallets) > MAX_WALLETS_SHOWN:
            lines.append(f"…и ещё {len(snap.wallets) - MAX_WALLETS_SHOWN}")
    else:
        lines.append("👛 Кошельков нет")

    lines.append(f"🔌 Биржи: {snap.cex_line}")
    lines.append(f"🧾 Операций за {snap.year}: <b>{snap.ops_year}</b>")
    lines.append(f"⚠️ Ждут проверки: <b>{snap.pending}</b>")

    if snap.missing:
        lines.append("")
        lines.append("<b>Что дальше</b>")
        for item in snap.missing:
            lines.append(f"• {escape_html(item)}")
        if not snap.has_sources:
            lines.append("")
            lines.append(
                "Пришли адреса одним сообщением — по одному в строке. "
                "Сеть определю сам."
            )
        elif not snap.ops_year:
            lines.append("• Нажми «Обновить всё» — подтяну историю.")
    else:
        lines.append("")
        lines.append("✅ Всё готово: считай налог или выгружай отчёт.")

    markup = (_review_keyboard(reviews) if reviews else _menu_keyboard())
    return "\n".join(lines), markup


async def _show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = _build_status(context)
    await update.message.reply_text(text, parse_mode="HTML",
                                    reply_markup=markup)


async def _send_status(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    text, markup = _build_status(context)
    await context.bot.send_message(chat_id=chat_id, text=text,
                                   parse_mode="HTML", reply_markup=markup)


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

    # Сразу обновляем всё: выписка нужна, чтобы отличить вывод с биржи
    # от аирдропа, а без сопоставления перевод остался бы ложным доходом.
    await start_sync(context, msg.chat_id,
                     "🔄 Обновляю кошельки и биржи… Это может занять минуту.")


# ------------------------------------------------------------- кнопки


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

    if data in ("ai:sync_all", "ai:scan"):
        await start_sync(context, query.message.chat_id,
                         "🔄 Обновляю кошельки и биржи… Это может занять минуту.")
        return

    if data == "ai:wallets":
        _start_wait_wallets(context)
        await query.message.reply_text(_wallets_prompt(), parse_mode="HTML",
                                       reply_markup=_wait_keyboard())
        return

    if data == "ai:wait_cancel":
        from utils.sources import AWAIT_WALLETS_KEY

        context.user_data.pop(AWAIT_WALLETS_KEY, None)
        try:
            await query.edit_message_text(
                "Ок, отменил добавление кошельков.",
                reply_markup=_menu_keyboard(),
            )
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.warning(f"ai:wait_cancel: {e}")
        return

    if data == "ai:cex":
        from handlers.cex_command import cex_status_text

        text = (cex_status_text(context.bot_data.get("BOT_CONFIG"))
                + "\n\nСинхронизировать сейчас — кнопка «Обновить всё».")
        await query.message.reply_text(text, parse_mode="HTML",
                                       reply_markup=_cex_keyboard())
        return

    if data == "ai:status":
        await _send_status(query.message.chat_id, context)
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


# ------------------------------------------------------------- синхронизация


async def start_sync(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                     notice: Optional[str] = None) -> None:
    """Запускает общий прогон в фоне: чат не должен ждать сеть."""
    if notice:
        try:
            await context.bot.send_message(chat_id=chat_id, text=notice,
                                           parse_mode="HTML")
        except Exception as e:
            logger.warning(f"start_sync: уведомление не отправлено: {e}")
    asyncio.create_task(_sync_task(context.bot, chat_id, context.bot_data))


async def _sync_task(bot, chat_id: int, bot_data: dict):
    from utils.sync_pipeline import sync_all

    try:
        cfg = (bot_data or {}).get("BOT_CONFIG")
        owner_id = int(getattr(cfg, "crypto_owner_id", 0) or 0)
        if not owner_id:
            await bot.send_message(chat_id, "Не задан владелец учёта "
                                            "(CRYPTO_OWNER_ID).")
            return

        report = await sync_all(owner_id, cfg)
        lines = ["<b>Обновление завершено</b>", ""]

        if report.wallets:
            lines.append(f"👛 Кошельки: {report.wallets}")
            lines.append(f"• новых операций: <b>{report.saved}</b>")
            if report.duplicates:
                lines.append(f"• уже было: {report.duplicates}")
            if report.internal:
                lines.append(f"• перемещений (не влияют на базу): "
                             f"{report.internal}")
        else:
            lines.append("👛 Кошельков нет — добавь адреса.")

        for ex in report.cex_exchanges:
            lines.append(f"🔌 {ex.title}: <b>{ex.saved}</b> новых"
                         + (f", дублей: {ex.duplicates}" if ex.duplicates else ""))
            for err in ex.errors[:3]:
                lines.append(f"  · {escape_html(str(err)[:140])}")

        if report.rematched:
            lines.append(f"🔗 Сопоставлено переводов: <b>{report.rematched}</b>")
        if report.review:
            lines.append(f"⚠️ Новых спорных: <b>{report.review}</b>")

        if report.errors:
            lines.append("")
            lines.append("<b>Ошибки:</b>")
            for e in report.errors[:5]:
                lines.append(f"• {escape_html(str(e)[:160])}")

        lines.append("")
        if report.review:
            lines.append("Проверь спорные операции: от них зависит база.")
        else:
            lines.append("Дальше: «Налог за год» или /tax.")

        await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML",
                               reply_markup=_menu_keyboard())
        await _send_tax_digest(bot, chat_id, owner_id)
    except Exception as e:
        logger.error(f"auto_import sync failed: {e}", exc_info=True)
        try:
            await bot.send_message(chat_id, f"Сбой обновления: {e}")
        except Exception:
            pass


# ------------------------------------------------------------- приём адресов


async def wallet_bulk_text(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> None:
    """Принимает список адресов, если бот его ждёт (после кнопки/команды).

    Регистрируется в отдельной группе до ИИ-чата. Если ожидания нет —
    молча выходим, и обычный текст обрабатывается как раньше.
    """
    from utils.sources import (AWAIT_WALLETS_KEY, add_wallets_from_text)

    if not context.user_data.get(AWAIT_WALLETS_KEY):
        return

    msg = update.message
    if not msg or not msg.text:
        return

    owner = _owner_id(context)
    if not owner:
        context.user_data.pop(AWAIT_WALLETS_KEY, None)
        await msg.reply_text("Владелец не настроен.")
        raise ApplicationHandlerStop

    result = add_wallets_from_text(owner, msg.text)
    # Мусор без единого адреса — не повод выключать режим: человек мог
    # прислать подпись отдельной строкой или опечататься.
    if not (result.added or result.duplicates):
        await msg.reply_text(
            "Не нашёл ни одного адреса. Пришли ещё раз — по одному в "
            "строке — или нажми «Отмена».",
            reply_markup=_wait_keyboard(),
        )
        raise ApplicationHandlerStop

    context.user_data.pop(AWAIT_WALLETS_KEY, None)

    lines = []
    if result.added:
        lines.append(f"✅ Добавлено кошельков: <b>{len(result.added)}</b>")
        for w in result.added[:MAX_WALLETS_SHOWN]:
            suffix = f" — {escape_html(w.label)}" if w.label else ""
            lines.append(
                f"• <code>{w.chain}</code> {escape_html(w.short_address)}{suffix}"
            )
        if len(result.added) > MAX_WALLETS_SHOWN:
            lines.append(f"…и ещё {len(result.added) - MAX_WALLETS_SHOWN}")
    if result.duplicates:
        if lines:
            lines.append("")
        lines.append(f"🔁 Уже были: <b>{len(result.duplicates)}</b>")
    if result.invalid:
        if lines:
            lines.append("")
        lines.append(f"⚠️ Не разобрал: <b>{len(result.invalid)}</b>")
        for address, why in result.invalid[:5]:
            lines.append(
                f"• <code>{escape_html(str(address)[:80])}</code> — "
                f"{escape_html(str(why)[:120])}"
            )

    await msg.reply_text("\n".join(lines), parse_mode="HTML")

    if result.added:
        await start_sync(
            context, msg.chat_id,
            "🔄 Подтягиваю историю по новым кошелькам… Это может занять минуту.",
        )
    else:
        await _send_status(msg.chat_id, context)

    # Текст уже обработан: не отдаём его ИИ-чату.
    raise ApplicationHandlerStop


# ------------------------------------------------------------- налоговая сводка


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
