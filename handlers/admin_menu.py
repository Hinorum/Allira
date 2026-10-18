"""Telegram-админ-меню: /admin с инлайн-кнопками.

Второй интерфейс сервиса управления поверх utils/bot_admin.py (веб-панель —
utils/web_panel.py; логика общая, здесь только рендер в Telegram):

  /admin              — главное меню (кнопки ниже);
  📊 Статус           — аптайм, фоновые задачи, размер БД, крипто-сводка;
  👛 Кошельки         — список с кнопками вкл/выкл;
  📒 Операции         — последние 15 операций;
  🔄 Синк бирж        — ручной запуск синка Bybit/OKX;
  📱 Скан TON         — ручной скан jetton-переводов;
  ⚖️ Сверка Дт/Кт    — полная сверка за год (с факт. балансами);
  ⚙️ Настройки ФНС    — ИНН/ФИО/статус/метод;
  📜 Логи             — последние строки лога (жёлтые/красные).

Доступ — только владельцу (тот же guard, что в reconcile_command).
Ручной ввод (добавление кошельков/операций) оставлен существующим
командам /wallet_add, /addtx — в инлайне собирать форму неудобно,
а они уже покрывают этот сценарий.
"""

from __future__ import annotations

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from utils import bot_admin
from utils.common import escape_html
from utils.log_buffer import recent_logs
from utils.price_oracle import format_rub

logger = logging.getLogger(__name__)

BACK_BUTTON = InlineKeyboardMarkup([[
    InlineKeyboardButton("« Меню", callback_data="adm:menu"),
]])


def _owner_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    return int(context.bot_data.get("CRYPTO_OWNER_ID") or 0)


async def _is_owner_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Guard: отвечаем только владельцу (CREATOR_USER_IDS / @username)."""
    if not update.effective_user:
        return False
    user_id = update.effective_user.id
    creator_ids = context.bot_data.get("CREATOR_USER_IDS") or ()
    creator_username = (context.bot_data.get("CREATOR_USERNAME") or "").lstrip("@").lower()
    username = (update.effective_user.username or "").lower()
    if creator_ids and user_id not in creator_ids:
        if update.message:
            await update.message.reply_text("Команды доступны только владельцу бота.")
        return False
    if not creator_ids and username != creator_username:
        if update.message:
            await update.message.reply_text("Команды доступны только владельцу бота.")
        return False
    return True


def _main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статус", callback_data="adm:status"),
         InlineKeyboardButton("📒 Операции", callback_data="adm:tx")],
        [InlineKeyboardButton("👛 Кошельки", callback_data="adm:wallets"),
         InlineKeyboardButton("⚙️ Настройки ФНС", callback_data="adm:tax")],
        [InlineKeyboardButton("🔄 Синк бирж", callback_data="adm:sync_cex"),
         InlineKeyboardButton("📱 Скан TON", callback_data="adm:sync_jetton")],
        [InlineKeyboardButton("⚖️ Сверка Дт/Кт", callback_data="adm:reconcile"),
         InlineKeyboardButton("📜 Логи", callback_data="adm:logs")],
    ])


# ------------------------------------------------------------- рендер


def admin_status_text(application, config, start_time: float) -> str:
    st = bot_admin.full_status(application, config, start_time)
    db_stats = st["database"]
    crypto = st["crypto"]
    lines = [
        f"<b>📊 Статус бота</b> ({st['time']})",
        f"Аптайм: <b>{escape_html(st['uptime'])}</b>",
        "",
        f"БД: {db_stats['size_bytes'] / 1024:.0f} КБ, "
        f"пользователей {db_stats['counts'].get('users', 0)}, "
        f"сообщений {db_stats['counts'].get('messages', 0)}",
    ]
    if crypto.get("enabled"):
        lines.append(
            f"Операций: <b>{crypto['tx_count']}</b>, "
            f"кошельков: {len(crypto['wallets'])}"
        )
    if st["jobs"]:
        lines.append("")
        lines.append("<b>Фоновые задачи:</b>")
        for j in st["jobs"][:12]:
            nxt = ("—" if j["next_run_in_s"] is None
                   else f"через {j['next_run_in_s'] // 60} мин")
            lines.append(f"  • {escape_html(j['name'])} — {nxt}")
    else:
        lines.append("Фоновые задачи: не запущены")

    keys = st["config"]
    missing = [k for k, v in keys.items() if not v]
    if missing:
        lines.append("")
        lines.append("⚠️ Не заданы: " + escape_html(", ".join(missing)))
    return "\n".join(lines)


def _format_wallets(owner_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    wallets = bot_admin.wallets_ui(owner_id)
    if not wallets:
        return ("👛 Кошельков нет. Добавь командой /wallet_add.", None)
    lines = ["<b>👛 Кошельки</b>"]
    buttons: list[list[InlineKeyboardButton]] = []
    for w in wallets[:20]:
        status = "✅" if w["active"] else "⛔"
        lines.append(
            f"{status} <b>{escape_html(w['label'] or w['chain'])}</b> "
            f"({escape_html(w['chain'])}) — {w['tx_count']} операций\n"
            f"   <code>{escape_html(w['address'][:20])}…</code>"
        )
        toggle = "выкл" if w["active"] else "вкл"
        buttons.append([
            InlineKeyboardButton(
                f"{status} {escape_html((w['label'] or w['address'][:8]))} — {toggle}",
                callback_data=f"adm:wallet_toggle:{w['id']}",
            )
        ])
    buttons.append([InlineKeyboardButton("« Меню", callback_data="adm:menu")])
    return ("\n".join(lines), InlineKeyboardMarkup(buttons))


def _format_tx(owner_id: int) -> str:
    rows = bot_admin.transactions_ui(owner_id, limit=15)
    if not rows:
        return "📒 Операций нет. Добавь /addtx или /airdrop."
    lines = ["<b>📒 Последние операции</b>"]
    for r in rows:
        arrow = "🟢" if r["direction"] == "in" else "🔴"
        fiat = f" · {format_rub(float(r['fiat_rub']))}" if r["fiat_rub"] else ""
        lines.append(
            f"{arrow} {escape_html(r['date'])} {escape_html(r['type'])} "
            f"<b>{escape_html(r['asset'])}</b> {escape_html(r['qty'])}{fiat}"
        )
    return "\n".join(lines)


def _format_tax(owner_id: int) -> str:
    t = bot_admin.tax_settings_ui(owner_id)
    status_labels = {"resident": "резидент", "nonresident": "нерезидент",
                     "miner": "майнер"}
    method_labels = {"fifo": "FIFO", "wac": "средневзвешенная (WAC)"}
    return "\n".join([
        "<b>⚙️ Настройки ФНС</b>",
        f"ИНН: {escape_html(t.get('taxpayer_inn') or '— не задан —')}",
        f"ФИО: {escape_html(t.get('taxpayer_name') or '— не задано —')}",
        f"Статус: {status_labels.get(t.get('tax_status', ''), t.get('tax_status'))}",
        f"Метод себестоимости: "
        f"{method_labels.get(t.get('cost_basis', ''), t.get('cost_basis'))}",
        "",
        "<i>Изменить: /tax_setup</i>",
    ])


def _format_logs() -> str:
    lines = recent_logs(limit=30)
    if not lines:
        return "📜 Лог-буфер пуст."
    # Telegram не любит сырые newlines внутри entity — отдаём как есть,
    # escape не нужен: текст уходит без parse_mode.
    body = "\n".join(l["text"] for l in lines)
    return f"📜 Последние {len(lines)} строк лога:\n\n{body}"[:3900]


def _format_reconcile(data: dict) -> str:
    year = data.get("year", datetime.now().year)
    ok = data.get("ok")
    issues = data.get("issues") or []
    status = ("✅ Сверка пройдена" if ok
              else f"⚠️ Расхождений: {len(issues)}")
    lines = [
        f"<b>⚖️ Сверка дебет-кредит за {year}</b>",
        status,
        "",
        f"Σ Дт: <b>{format_rub(float(data.get('total_dr_rub') or 0))}</b> · "
        f"Σ Кт: <b>{format_rub(float(data.get('total_cr_rub') or 0))}</b>",
    ]
    for issue in issues[:8]:
        lines.append(f"• [{escape_html(issue.get('kind', ''))}] "
                     f"{escape_html(str(issue.get('message', ''))[:160])}")
    if len(issues) > 8:
        lines.append(f"…и ещё {len(issues) - 8}. Детали — /reconcile_xlsx")
    return "\n".join(lines)


# ------------------------------------------------------------- хендлеры


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/admin` — главное меню."""
    if not await _is_owner_chat(update, context):
        return
    if not _owner_id(context):
        await update.message.reply_text(
            "Владелец не настроен. Добавь CRYPTO_OWNER_ID в .env."
        )
        return
    await update.message.reply_text(
        "⚙️ <b>Сервис управления</b>\n"
        "Мониторинг и ручные запуски. Веб-панель: /admin на порту бота.",
        parse_mode="HTML", reply_markup=_main_menu(),
    )


async def admin_menu_callback(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """Callback-кнопки меню (prefix `adm:`)."""
    query = update.callback_query
    if not query or not await _is_owner_chat(update, context):
        return
    await query.answer()

    data = query.data or ""
    owner_id = _owner_id(context)
    config = context.bot_data.get("BOT_CONFIG")
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"

    async def show(text: str, markup: InlineKeyboardMarkup | None = BACK_BUTTON):
        # edit_message_text падает «message is not modified», если нажали
        # дважды — глушим.
        try:
            await query.edit_message_text(text, parse_mode="HTML",
                                          reply_markup=markup)
        except Exception as e:
            logger.debug(f"admin menu edit: {e}")

    if action == "menu":
        await show("⚙️ <b>Сервис управления</b>", _main_menu())
        return

    if action == "status":
        await show(admin_status_text(context.application, config,
                                     context.bot_data.get("BOT_START_TIME")
                                     or 0.0))
        return

    if action == "wallets":
        text, markup = _format_wallets(owner_id)
        await show(text, markup)
        return

    if action == "wallet_toggle" and len(parts) > 2:
        wallet_id = int(parts[2])
        current = {w["id"]: w for w in bot_admin.wallets_ui(owner_id)}.get(wallet_id)
        if current is not None:
            bot_admin.set_wallet_active(owner_id, wallet_id,
                                        not current["active"])
        text, markup = _format_wallets(owner_id)
        await show(text, markup)
        return

    if action == "tx":
        await show(_format_tx(owner_id))
        return

    if action == "tax":
        await show(_format_tax(owner_id))
        return

    if action == "logs":
        # Без parse_mode: в логах сырые символы ломают разметку.
        try:
            await query.edit_message_text(_format_logs(), reply_markup=BACK_BUTTON)
        except Exception as e:
            logger.debug(f"admin logs edit: {e}")
        return

    if action == "sync_cex":
        if not config or not (config.bybit_api_key or config.okx_api_key):
            await show("🔄 CEX-ключи не заданы (BYBIT_API_KEY / OKX_API_KEY).")
            return
        await query.edit_message_text("🔄 Синхронизирую биржи…")
        try:
            result = await bot_admin.manual_cex_sync(config, owner_id)
            parts_out = []
            for exch, r in result.items():
                if r is None:
                    parts_out.append(f"{exch}: ключей нет")
                else:
                    errs = f", ошибок {len(r['errors'])}" if r["errors"] else ""
                    parts_out.append(f"{exch}: +{r['saved']} операций{errs}")
            await show("✅ <b>Синк бирж завершён</b>\n" +
                       "\n".join(escape_html(p) for p in parts_out))
        except Exception as e:
            logger.error(f"manual cex sync: {e}", exc_info=True)
            await show(f"❌ Ошибка синка: {escape_html(str(e))}")
        return

    if action == "sync_jetton":
        await query.edit_message_text("📱 Сканирую TON-кошельки…")
        try:
            result = await bot_admin.manual_jetton_sync(config, owner_id)
            wallets = result.get("wallets") or []
            if not wallets:
                await show("📱 Нет активных TON-кошельков.")
                return
            lines = []
            for w in wallets:
                if "error" in w:
                    lines.append(f"❌ {escape_html(w['wallet'])}: "
                                 f"{escape_html(w['error'][:100])}")
                else:
                    lines.append(f"✅ {escape_html(w['wallet'])}: "
                                 f"+{w['saved']} операций")
            await show("📱 <b>Скан TON завершён</b>\n" + "\n".join(lines))
        except Exception as e:
            logger.error(f"manual jetton sync: {e}", exc_info=True)
            await show(f"❌ Ошибка скана: {escape_html(str(e))}")
        return

    if action == "reconcile":
        year = datetime.now().year
        await query.edit_message_text(f"⚖️ Сверяю дебет-кредит за {year}…")
        try:
            data = await bot_admin.manual_reconcile(config, owner_id, year)
            await show(_format_reconcile(data))
        except Exception as e:
            logger.error(f"manual reconcile: {e}", exc_info=True)
            await show(f"❌ Ошибка сверки: {escape_html(str(e))}")
        return

    await show("⚙️ <b>Сервис управления</b>", _main_menu())
