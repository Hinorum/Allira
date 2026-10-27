"""Хендлеры налоговой отчётности 3-НДФЛ по операциям с криптой.

Команды (только в ЛС создателя):
  /tax_setup                          — настроить ИНН/ФИО/статус
  /tax_setup_status                   — посмотреть текущие настройки
  /tax_cost <сумма> [год] [категория] — расходы, уменьшающие базу
  /tax_cost_del <id>                  — удалить расход
  /tax <год>                          — сводка за год + кнопки экспорта
  /tax_pdf <год>                      — PDF-отчёт 3-НДФЛ
  /tax_xlsx <год>                     — детальная XLSX-книга
  /tax_csv_income <год>               — CSV доходов
  /tax_csv_sale <год>                 — CSV реализаций

Алгоритм:
  1. compute_year_report() пересобирает FIFO-лоты и считает базу.
  2. Сводка летит одним сообщением с InlineKeyboard для экспорта.
  3. Кнопки вызывают tax_pdf_callback / tax_xlsx_callback / …
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime

from telegram import InputFile, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from utils.common import escape_html
from utils.crypto_db import (add_tax_expense, delete_tax_expense,
                             get_tax_settings, list_tax_expenses,
                             set_tax_settings)
from utils.price_oracle import format_rub
from utils.report_export import (render_disposal_csv, render_income_csv,
                                 render_pdf, render_xlsx)
from utils.tax_calc import cache_report, compute_year_report, load_cached_report

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
        await update.message.reply_text(
            "Владелец не настроен. Добавь CRYPTO_OWNER_ID или CREATOR_USER_IDS в .env."
        )
        return False
    return True


# --------------------------------------------------------- /tax_setup


async def tax_setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/tax_setup ИНН=<инн> ИМЯ=<фио> СТАТУС=<resident|miner|nonresident>`"""
    if not await _is_owner_chat(update, context):
        return
    args = context.args or []
    fields: dict = {}
    for a in args:
        if "=" not in a:
            continue
        k, v = a.split("=", 1)
        fields[k.strip().lower()] = v.strip()

    inn = fields.get("инн") or fields.get("inn")
    name = fields.get("имя") or fields.get("name") or fields.get("фио")
    status = (fields.get("статус") or fields.get("status") or "").lower()
    method = (fields.get("метод") or fields.get("method") or "").lower()

    allowed_status = {"resident", "miner", "nonresident"}
    allowed_method = {"fifo", "wac"}
    payload = {}
    if inn:
        payload["taxpayer_inn"] = inn
    if name:
        payload["taxpayer_name"] = name
    if status:
        if status not in allowed_status:
            await update.message.reply_text(
                f"Статус должен быть одним из: {sorted(allowed_status)}")
            return
        payload["tax_status"] = status
    if method:
        if method not in allowed_method:
            await update.message.reply_text(
                f"Метод должен быть одним из: {sorted(allowed_method)}")
            return
        payload["cost_basis"] = method

    if not payload:
        # Без аргументов — показать текущие настройки.
        await _send_tax_status(update, context)
        return

    set_tax_settings(_owner_id(context), **payload)
    await update.message.reply_text("Настройки обновлены.")
    await _send_tax_status(update, context)


async def _send_tax_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_tax_settings(_owner_id(context))
    text = (
        "<b>Настройки для 3-НДФЛ:</b>\n"
        f"  ИНН: <code>{escape_html(settings.get('taxpayer_inn') or '—')}</code>\n"
        f"  Плательщик: <code>{escape_html(settings.get('taxpayer_name') or '—')}</code>\n"
        f"  Статус: <b>{escape_html(settings.get('tax_status') or 'resident')}</b>\n"
        f"  Метод себестоимости: <b>{escape_html(settings.get('cost_basis') or 'fifo')}</b>\n"
        f"  Валюта отчёта: <b>{escape_html(settings.get('reporting_currency') or 'RUB')}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def tax_setup_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_owner_chat(update, context):
        return
    await _send_tax_status(update, context)


# ------------------------------------------------- /tax_cost — расходы


async def tax_cost_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/tax_cost <сумма> [год] [категория] [комментарий]`

    Прочие расходы, уменьшающие налоговую базу: электричество у пула,
    комиссии бирж, амортизация оборудования. Без них база считалась бы
    завышенной (у майнера — на всю сумму добычи).

    Без аргументов — список расходов.
    """
    if not await _is_owner_chat(update, context):
        return
    owner = _owner_id(context)
    args = list(context.args or [])

    if not args:
        await _send_expense_list(update, context, owner)
        return

    amount_raw = ""
    year = datetime.now().year
    category = ""
    note = ""

    if "=" in args[0]:
        # Форма с ключами: СУММА=50000 ГОД=2025 КАТЕГОРИЯ=электричество
        fields: dict = {}
        for a in args:
            if "=" not in a:
                continue
            k, v = a.split("=", 1)
            fields[k.strip().lower()] = v.strip()
        amount_raw = fields.get("сумма") or fields.get("amount") or fields.get("sum") or ""
        year_raw = fields.get("год") or fields.get("year") or ""
        category = fields.get("категория") or fields.get("category") or ""
        note = fields.get("заметка") or fields.get("note") or ""
        if year_raw.isdigit():
            year = int(year_raw)
    else:
        amount_raw = args[0]
        if len(args) > 1 and args[1].isdigit() and len(args[1]) == 4:
            year = int(args[1])
            category = args[2] if len(args) > 2 else ""
            note = " ".join(args[3:]) if len(args) > 3 else ""
        else:
            note = " ".join(args[1:])

    amount = _parse_money(amount_raw)
    if amount is None:
        await update.message.reply_text(
            "Сумма расхода не понята. Пример: "
            "<code>/tax_cost 50000 2025 электричество за март</code>",
            parse_mode="HTML",
        )
        return

    try:
        exp_id = add_tax_expense(owner, year, amount, category, note)
    except Exception as e:
        await update.message.reply_text(f"Не записал: {e}")
        return

    await update.message.reply_text(
        f"✅ Расход #{exp_id} записан: <b>{format_rub(float(amount))}</b> "
        f"({year}"
        + (f", {escape_html(category)}" if category else "")
        + "). База пересчитана при следующем /tax.",
        parse_mode="HTML",
    )
    await _send_tax_summary(update.message.chat_id, context, year)


async def tax_cost_del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/tax_cost_del <id>` — удалить расход."""
    if not await _is_owner_chat(update, context):
        return
    args = list(context.args or [])
    if not args or not args[0].isdigit():
        await update.message.reply_text("Нужен id расхода: <code>/tax_cost_del 3</code>",
                                        parse_mode="HTML")
        return
    ok = delete_tax_expense(int(args[0]), _owner_id(context))
    if ok:
        await update.message.reply_text("✅ Расход удалён.")
    else:
        await update.message.reply_text("Такого расхода нет.")


async def _send_expense_list(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              owner: int) -> None:
    expenses = list_tax_expenses(owner)
    if not expenses:
        await update.message.reply_text(
            "Расходов пока нет.\n\n"
            "Пример: <code>/tax_cost 50000 2025 электричество</code>\n"
            "<i>Расходы уменьшают налоговую базу: ст. 221 НК РФ.</i>",
            parse_mode="HTML",
        )
        return
    lines = ["<b>Расходы, уменьшающие базу:</b>"]
    for e in expenses:
        cat = f" — {escape_html(e['category'])}" if e["category"] else ""
        note = f" ({escape_html(e['note'][:60])})" if e["note"] else ""
        lines.append(f"• #{e['id']} {e['year']} — "
                     f"{format_rub(float(e['amount_rub']))}{cat}{note}")
    lines.append("")
    lines.append("Удалить: <code>/tax_cost_del &lt;id&gt;</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def _parse_money(value: str):
    """'50 000,50' → 50000.5; None, если не число."""
    s = (value or "").strip().replace(" ", "").replace(" ", "")
    s = s.replace(",", ".")
    if not s:
        return None
    try:
        num = float(s)
    except ValueError:
        return None
    if num <= 0:
        return None
    return num


# ------------------------------- форма настройки ФНС (по шагам)

SETUP_KEY = "tax_setup_wizard"

SETUP_STATUSES = [
    ("resident", "🇷🇺 Резидент"),
    ("miner", "⛏ Майнер"),
    ("nonresident", "🌍 Нерезидент"),
]


def _setup_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    state = context.user_data.get(SETUP_KEY)
    if not isinstance(state, dict):
        state = {}
        context.user_data[SETUP_KEY] = state
    return state


async def start_tax_setup_wizard(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пошаговая форма: ИНН → ФИО → статус.

    Раньше настройка требовала синтаксиса `ИНН=… ИМЯ=… СТАТУС=…`, который
    надо было помнить и не опечатать. Теперь это три вопроса подряд.
    """
    state = _setup_state(context)
    state.clear()
    state["step"] = "inn"
    state["owner"] = _owner_id(context)
    await message.reply_text(
        "🧾 <b>Настройка отчёта 3-НДФЛ</b>\n\n"
        "Напиши ИНН — 12 цифр. Он печатается в шапке отчёта.",
        parse_mode="HTML",
    )


async def tax_setup_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текстовый ввод в форме настройки."""
    state = context.user_data.get(SETUP_KEY)
    if not isinstance(state, dict) or not state.get("step"):
        return
    message = getattr(update, "message", None)
    if message is None:
        return
    text = ((getattr(message, "text", None) or "").strip())
    if not text:
        return

    step = state.get("step")
    if step == "inn":
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) != 12:
            await message.reply_text(
                "ИНН — это 12 цифр. Проверь и пришли ещё раз."
            )
            raise ApplicationHandlerStop
        state["inn"] = digits
        state["step"] = "name"
        await message.reply_text(
            "ИНН принят. Теперь ФИО плательщика — как в налоговой декларации.",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

    if step == "name":
        state["name"] = text[:120]
        state["step"] = "status"
        await message.reply_text(
            "ФИО принято. Остался статус налогоплательщика:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(label, callback_data=f"taxsetup:status:{value}")]
                for value, label in SETUP_STATUSES
            ]),
        )
        raise ApplicationHandlerStop

    raise ApplicationHandlerStop


async def tax_setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Кнопки формы настройки."""
    query = update.callback_query
    if not query or not (query.data or "").startswith("taxsetup:"):
        return False
    action = (query.data or "")[9:]

    if action == "start":
        await query.answer()
        await start_tax_setup_wizard(query.message, context)
        return True

    if action.startswith("status:"):
        value = action[7:]
        state = _setup_state(context)
        owner = state.get("owner") or _owner_id(context)
        payload = {}
        if state.get("inn"):
            payload["taxpayer_inn"] = state["inn"]
        if state.get("name"):
            payload["taxpayer_name"] = state["name"]
        if value in {v for v, _ in SETUP_STATUSES}:
            payload["tax_status"] = value
        if payload:
            set_tax_settings(owner, **payload)
        context.user_data.pop(SETUP_KEY, None)
        await query.answer("Готово")
        settings = get_tax_settings(owner)
        await query.message.reply_text(
            "✅ <b>Настройки сохранены</b>\n\n"
            f"ИНН: <code>{escape_html(settings.get('taxpayer_inn') or '—')}</code>\n"
            f"Плательщик: <code>{escape_html(settings.get('taxpayer_name') or '—')}</code>\n"
            f"Статус: <b>{escape_html(settings.get('tax_status') or 'resident')}</b>\n\n"
            f"Теперь отчёт: <code>/tax</code>",
            parse_mode="HTML",
        )
        return True

    return True


# --------------------------------------------------------- /tax


async def tax_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/tax [год]` — сводка + инлайн-кнопки экспорта."""
    if not await _is_owner_chat(update, context):
        return
    args = context.args or []
    year = int(args[0]) if args and args[0].isdigit() else datetime.now().year
    owner = _owner_id(context)
    settings = get_tax_settings(owner)

    from utils.sources import sources_snapshot

    snapshot = sources_snapshot(owner, context.bot_data.get("BOT_CONFIG"),
                                year)

    # Без ИНН и ФИО отчёт для ФНС неполон. Не молчим: прямо говорим, чего
    # не хватает, и даём кнопку — до настройки был отдельный заход через
    # /tax_setup с синтаксисом ИНН=… ИМЯ=…, о котором надо было помнить.
    if not (settings.get("taxpayer_inn") and settings.get("taxpayer_name")):
        await update.message.reply_text(
            "⚠️ <b>Для отчёта не хватает данных</b>\n\n"
            f"ИНН: {escape_html(settings.get('taxpayer_inn') or '—')}\n"
            f"Плательщик: {escape_html(settings.get('taxpayer_name') or '—')}\n\n"
            f"{escape_html(snapshot.summary_line)}\n\n"
            "Заполни — и отчёт будет готов к подаче. Считаю за это время.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✍️ Заполнить ИНН и ФИО",
                                      callback_data="taxsetup:start")],
                [InlineKeyboardButton("📥 Источники",
                                      callback_data="ai:status")],
            ]),
        )

    await update.message.reply_text(
        f"Считаю отчёт за {year}… Это занимает до минуты."
    )
    await _send_tax_summary(update.message.chat_id, context, year)


async def _send_tax_summary(chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                             year: int) -> None:
    """Считает сводку за год и отправляет её с кнопками экспорта."""
    owner = _owner_id(context)
    summary = await compute_year_report(owner, year)
    cache_report(owner, summary)

    current_year = datetime.now().year
    years = [current_year]
    if current_year - 1 not in years:
        years.append(current_year - 1)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 PDF", callback_data=f"taxpdf:{year}"),
            InlineKeyboardButton("📊 XLSX", callback_data=f"taxxlsx:{year}"),
        ],
        [
            InlineKeyboardButton("CSV доходы", callback_data=f"taxcsvi:{year}"),
            InlineKeyboardButton("CSV реализация", callback_data=f"taxcsvs:{year}"),
        ],
        [
            InlineKeyboardButton("⚖️ Сверить с сетью",
                                 callback_data=f"taxrecon:{year}"),
        ],
        [
            InlineKeyboardButton("📥 Источники", callback_data="ai:status"),
        ],
        [
            InlineKeyboardButton(f"📅 {years[0]}", callback_data=f"taxyear:{years[0]}"),
            InlineKeyboardButton(f"📅 {years[1]}", callback_data=f"taxyear:{years[1]}"),
        ],
    ])

    from utils.sources import sources_snapshot

    snapshot = sources_snapshot(owner, context.bot_data.get("BOT_CONFIG"),
                                year)
    text = (
        f"<b>Сводка 3-НДФЛ за {year}:</b>\n\n"
        f"Доход (аирдропы, майнинг…): <b>{format_rub(float(summary.total_income_rub))}</b>\n"
        f"Себестоимость реализованных: <b>{format_rub(float(summary.total_cost_rub))}</b>\n"
        f"Прибыль от реализации: <b>{format_rub(float(summary.realized_gain_rub))}</b>\n"
    )
    if summary.manual_costs:
        text += (f"Прочие расходы (ст. 221 НК): "
                 f"<b>−{format_rub(float(summary.manual_costs))}</b>\n")
    text += (
        f"Налоговая база: <b>{format_rub(float(summary.taxable_income_rub))}</b>\n"
        f"<b>НДФЛ к уплате:</b> <b>{format_rub(float(summary.ndfl_total))}</b>\n\n"
        f"Доходов: {len(summary.income_events)} | Реализаций: {len(summary.disposal_events)}\n"
        f"{escape_html(snapshot.summary_line)}\n"
    )
    if not summary.income_events and not summary.disposal_events:
        text += (
            "\n<i>Операций за этот год нет. Записать первую: "
            "<code>/addtx</code> — или пришли список операций обычным "
            "текстом, я разберу его сам.</i>\n"
        )
    if summary.warnings:
        text += "\n<b>Замечания:</b>\n"
        for w in summary.warnings[:8]:
            text += f"• {escape_html(w[:200])}\n"
        if len(summary.warnings) > 8:
            text += f"…и ещё {len(summary.warnings) - 8}\n"

    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                   reply_markup=keyboard)


# --------------------------------------------------------- экспорт


async def tax_export_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback от /tax — выгрузка PDF/XLSX/CSV, смена года, сверка."""
    query = update.callback_query
    await query.answer()
    if not query.data or ":" not in query.data:
        return
    kind, year = query.data.split(":", 1)
    try:
        year_int = int(year)
    except ValueError:
        return
    owner = _owner_id(context)

    # Смена года — пересчитываем сводку, не пересобирая PDF.
    if kind == "taxyear":
        await _send_tax_summary(query.message.chat_id, context, year_int)
        return

    # Сверка с сетью прямо из отчёта: раньше до неё надо было доходить
    # отдельной командой, и шаг «а не разошёлся ли учёт» часто
    # пропускали.
    if kind == "taxrecon":
        await query.message.reply_text(
            f"Сверяю дебет-кредит за {year_int}… Загружаю балансы сети и бирж."
        )
        from handlers.reconcile_command import _format_report
        from utils.reconciliation import reconcile

        config = context.bot_data.get("BOT_CONFIG")
        report = await reconcile(owner, year_int, config=config, with_actual=True)
        await query.message.reply_text(_format_report(report), parse_mode="HTML")
        return

    summary = await compute_year_report(owner, year_int)
    settings = get_tax_settings(owner)

    chat_id = query.message.chat_id

    try:
        if kind == "taxpdf":
            payload = render_pdf(summary, settings)
            file = InputFile(io.BytesIO(payload),
                            filename=f"tax_report_{year_int}.pdf")
            await context.bot.send_document(
                chat_id=chat_id,
                document=file,
                caption=f"3-НДФЛ по крипте · {year_int} · "
                        f"НДФЛ: {format_rub(float(summary.ndfl_total))}",
            )
        elif kind == "taxxlsx":
            payload = render_xlsx(summary, settings)
            file = InputFile(io.BytesIO(payload),
                            filename=f"tax_report_{year_int}_details.xlsx")
            await context.bot.send_document(
                chat_id=chat_id,
                document=file,
                caption=f"Детальная XLSX по {year_int}.",
            )
        elif kind == "taxcsvi":
            payload = render_income_csv(summary)
            file = InputFile(io.BytesIO(payload),
                            filename=f"income_{year_int}.csv")
            await context.bot.send_document(
                chat_id=chat_id,
                document=file,
                caption=f"Доходы по {year_int} ({len(summary.income_events)} строк).",
            )
        elif kind == "taxcsvs":
            payload = render_disposal_csv(summary)
            file = InputFile(io.BytesIO(payload),
                            filename=f"disposal_{year_int}.csv")
            await context.bot.send_document(
                chat_id=chat_id,
                document=file,
                caption=f"Реализации по {year_int} ({len(summary.disposal_events)} строк).",
            )
    except Exception as e:
        logger.error(f"tax_export_callback: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Не удалось сформировать файл: <code>{escape_html(str(e)[:200])}</code>",
            parse_mode="HTML",
        )


# --------------------------------------------------------- прямые команды


async def tax_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/tax_pdf [год]`."""
    if not await _is_owner_chat(update, context):
        return
    args = context.args or []
    year = int(args[0]) if args and args[0].isdigit() else datetime.now().year
    owner = _owner_id(context)
    await update.message.reply_text(f"Готовлю PDF за {year}…")
    summary = await compute_year_report(owner, year)
    settings = get_tax_settings(owner)
    payload = render_pdf(summary, settings)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(payload),
                           filename=f"tax_report_{year}.pdf"),
        caption=f"3-НДФЛ по крипте за {year}. "
                f"НДФЛ: {format_rub(float(summary.ndfl_total))}",
    )


async def tax_xlsx_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/tax_xlsx [год]`."""
    if not await _is_owner_chat(update, context):
        return
    args = context.args or []
    year = int(args[0]) if args and args[0].isdigit() else datetime.now().year
    owner = _owner_id(context)
    await update.message.reply_text(f"Готовлю XLSX за {year}…")
    summary = await compute_year_report(owner, year)
    settings = get_tax_settings(owner)
    payload = render_xlsx(summary, settings)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(payload),
                           filename=f"tax_report_{year}_details.xlsx"),
        caption=f"Детальный отчёт за {year}.",
    )


async def tax_csv_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/tax_csv <доход|реализация> [год]`."""
    if not await _is_owner_chat(update, context):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Использование:\n"
            "<code>/tax_csv income [год]</code> — доходы\n"
            "<code>/tax_csv disposal [год]</code> — реализация",
            parse_mode="HTML",
        )
        return
    kind = args[0].lower()
    year_args = args[1:]
    year = int(year_args[0]) if year_args and year_args[0].isdigit() else datetime.now().year
    owner = _owner_id(context)
    summary = await compute_year_report(owner, year)
    if kind in ("income", "доход", "доходы"):
        payload = render_income_csv(summary)
        file = InputFile(io.BytesIO(payload),
                         filename=f"income_{year}.csv")
        cap = f"Доходы за {year} ({len(summary.income_events)} строк)."
    elif kind in ("disposal", "sale", "продажа", "реализация"):
        payload = render_disposal_csv(summary)
        file = InputFile(io.BytesIO(payload),
                         filename=f"disposal_{year}.csv")
        cap = f"Реализации за {year} ({len(summary.disposal_events)} строк)."
    else:
        await update.message.reply_text(
            "Неизвестный тип. Используй <code>income</code> или "
            "<code>disposal</code>.", parse_mode="HTML")
        return
    await update.message.reply_document(document=file, caption=cap)