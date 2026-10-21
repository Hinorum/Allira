"""Хендлеры крипто-учёта: кошельки, транзакции, баланс, P&L, импорт.

Команды (только в ЛС создателя):
  /wallet_add <chain> <address> [label]   — зарегистрировать кошелёк
  /wallet_list                            — список кошельков с балансом
  /wallet_del <id>                        — пометить кошелёк неактивным
  /addtx <type> <asset> <amount> [date] [price_rub] [note]
                                            — записать операцию
  /tx_list [year] [filter]                — последние операции
  /tx_del <id>                            — удалить операцию
  /balance                                — текущий портфель
  /pnl [year]                             — прибыль/убыток за год
  /airdrop <asset> <amount> [date] [price_rub]
                                            — быстрый ввод аирдропа
  /rate <asset>                           — текущая цена в RUB
  /import — реплаем на CSV-файл          — импорт выписки биржи

Дизайн:
  • Все хендлеры читают owner_id из bot_data["CRYPTO_OWNER_ID"]
    (настраивается через env CRYPTO_OWNER_ID, дефолт — CREATOR_USER_IDS[0]).
  • Команды длинные и параметризованные — парсятся вручную, без FSM.
  • Документы (CSV/PDF) отдаются через bot.send_document.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

from telegram import InputFile, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from utils.common import escape_html
from utils.crypto_db import (
    Asset, Chain, CryptoError, Tx, TxType, Wallet,
    add_transaction, add_wallet, ensure_asset, get_asset, humanize_amount,
    list_transactions, list_wallets,
)
from utils.csv_import import parse_csv
from utils.database import get_db
from utils.price_oracle import format_rub, get_asset_price_rub
from utils.tax_calc import rebuild_lots
from tasks.cex_sync import persist_cex_rows

logger = logging.getLogger(__name__)


# --------------------------------------------------------- гварды


def _owner_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    return int(context.bot_data.get("CRYPTO_OWNER_ID") or 0)


def _deny_no_owner(update: Update) -> bool:
    """Возвращает True, если owner_id = 0 и пользователю уже ответили."""
    return False  # гард ниже; см. _is_owner_chat


async def _is_owner_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Только ЛС владельца бота: как и message_handler.handle_private_message."""
    if not update.effective_user:
        return False
    user_id = update.effective_user.id
    creator_ids = context.bot_data.get("CREATOR_USER_IDS") or ()
    creator_username = (context.bot_data.get("CREATOR_USERNAME") or "").lstrip("@").lower()
    username = (update.effective_user.username or "").lower()
    if creator_ids and user_id not in creator_ids:
        await update.message.reply_text(
            "Команды крипто-учёта доступны только владельцу бота."
        )
        return False
    if not creator_ids and username != creator_username:
        await update.message.reply_text(
            "Команды крипто-учёта доступны только владельцу бота."
        )
        return False
    if not _owner_id(context):
        await update.message.reply_text(
            "Владелец не настроен. Добавь CRYPTO_OWNER_ID или CREATOR_USER_IDS в .env."
        )
        return False
    return True


# --------------------------------------------------------- /wallet_add


async def wallet_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/wallet_add <chain> <address> [label]`."""
    if not await _is_owner_chat(update, context):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Использование:\n"
            "<code>/wallet_add &lt;chain&gt; &lt;address&gt; [label]</code>\n\n"
            f"<b>chain</b> ∈ {string_list_of(Chain.all())}\n"
            "Для TON подходят EQ/UQ/raw-формы.",
            parse_mode="HTML",
        )
        return
    chain = args[0].lower()
    address = args[1]
    label = " ".join(args[2:]) if len(args) > 2 else ""
    owner = _owner_id(context)
    try:
        wallet = add_wallet(owner, chain, address, label)
    except CryptoError as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return
    await update.message.reply_text(
        f"Кошелёк добавлен:\n"
        f"  <b>{escape_html(wallet.label or '—')}</b>\n"
        f"  chain: <code>{wallet.chain}</code>\n"
        f"  address: <code>{escape_html(wallet.address)}</code>\n"
        f"  id: <b>#{wallet.id}</b>",
        parse_mode="HTML",
    )


async def wallet_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/wallet_list` — все кошельки + текущий баланс из tax_lots."""
    if not await _is_owner_chat(update, context):
        return
    owner = _owner_id(context)
    wallets = list_wallets(owner)
    if not wallets:
        await update.message.reply_text(
            "Кошельков пока нет. Добавь: <code>/wallet_add ton EQ... ярлык</code>",
            parse_mode="HTML",
        )
        return

    # Перестроить лоты для баланса.
    rebuild_lots(owner)

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT w.id, w.label, w.chain, w.address, a.symbol, a.decimals,
                   COALESCE(SUM(CAST(l.qty_remaining AS REAL)), 0) AS qty
            FROM crypto_wallets w
            LEFT JOIN tax_lots l ON l.owner_id = w.owner_id
                                  AND l.qty_remaining > 0
            LEFT JOIN crypto_assets a ON a.id = l.asset_id
            WHERE w.owner_id = ?
            GROUP BY w.id, a.id
            ORDER BY w.chain, w.label, a.symbol
            """,
            (owner,),
        ).fetchall()

    # Группируем по кошельку.
    by_wallet: dict[int, list[tuple[str, Decimal, int]]] = {}
    wallet_by_id = {w.id: w for w in wallets}
    for r in rows:
        if r["symbol"] is None:
            continue
        wallet_id = r["id"]
        qty = Decimal(str(r["qty"] or 0)) / (Decimal(10) ** int(r["decimals"]))
        by_wallet.setdefault(wallet_id, []).append(
            (r["symbol"], qty, int(r["decimals"]))
        )

    lines = ["<b>Мои кошельки:</b>\n"]
    for w in wallets:
        title = w.label or w.short_address
        lines.append(
            f"<b>#{w.id}</b> · <code>{w.chain}</code> · {escape_html(title)}\n"
            f"  <code>{escape_html(w.address)}</code>"
        )
        holdings = by_wallet.get(w.id, [])
        if not holdings:
            lines.append("  <i>баланс пуст</i>")
        else:
            for sym, qty, _dec in holdings:
                lines.append(f"  • {sym}: <b>{qty:.8f}</b>".rstrip("0").rstrip("."))
        lines.append("")

    # Суммарный баланс по активам.
    total_by_asset: dict[str, Decimal] = {}
    for holdings in by_wallet.values():
        for sym, qty, _ in holdings:
            total_by_asset[sym] = total_by_asset.get(sym, Decimal(0)) + qty
    if total_by_asset:
        lines.append("<b>Σ по активам:</b>")
        for sym in sorted(total_by_asset):
            q = total_by_asset[sym]
            lines.append(f"  • {sym}: <b>{q:.8f}</b>".rstrip("0").rstrip("."))

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def wallet_del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/wallet_del <id>` — пометить неактивным (для архива)."""
    if not await _is_owner_chat(update, context):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Использование: <code>/wallet_del &lt;id&gt;</code>", parse_mode="HTML"
        )
        return
    wallet_id = int(args[0])
    owner = _owner_id(context)
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE crypto_wallets SET is_active = 0 "
            "WHERE id = ? AND owner_id = ?",
            (wallet_id, owner),
        )
        if cur.rowcount:
            await update.message.reply_text(f"Кошелёк #{wallet_id} помечен неактивным.")
        else:
            await update.message.reply_text("Не найден.")


# --------------------------------------------------------- /addtx


async def addtx_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/addtx <type> <asset> <amount> [date] [price_rub] [note...]`"""
    if not await _is_owner_chat(update, context):
        return
    args = context.args or []
    usage = (
        "➕ <b>Записать операцию</b>\n\n"
        "Формат:\n"
        "<code>/addtx &lt;тип&gt; &lt;актив&gt; &lt;количество&gt; "
        "[дата] [цена ₽] [комментарий]</code>\n\n"
        f"типы: {string_list_of(TxType.all())}\n"
        "актив — символ (BTC, TON, USDT, NOT…)\n"
        "количество — сколько\n"
        "дата — по умолчанию сегодня (МСК)\n"
        "цена ₽ — фиатная стоимость всей операции (если знаешь)\n"
        "комментарий — свободный текст\n\n"
        "Примеры:\n"
        "<code>/addtx airdrop BTC 0.001 15.01.2025</code>\n"
        "<code>/addtx sell BTC 0.3 20.06.2025 2500000</code>\n"
        "<code>/addtx mining TON 12 01.02.2025 3000 майнинг</code>"
    )
    if len(args) < 3:
        await update.message.reply_text(usage, parse_mode="HTML")
        return
    tx_type = args[0].strip().lower()
    asset = args[1].strip().upper()
    try:
        amount = Decimal(args[2].replace(",", "."))
    except InvalidOperation:
        await update.message.reply_text(
            "Количество должно быть числом.\n" + usage, parse_mode="HTML"
        )
        return
    ts = int(time.time())
    fiat = ""
    cost = ""
    note_parts: list[str] = []
    extra = args[3:]
    if extra:
        iso_or_ddmm = extra[0]
        if "." in iso_or_ddmm and len(iso_or_ddmm) == 10:
            try:
                ts = int(time.mktime(time.strptime(iso_or_ddmm, "%d.%m.%Y")))
                extra = extra[1:]
            except ValueError:
                pass
        elif "-" in iso_or_ddmm and len(iso_or_ddmm) == 10:
            try:
                ts = int(time.mktime(time.strptime(iso_or_ddmm, "%Y-%m-%d")))
                extra = extra[1:]
            except ValueError:
                pass
    if extra:
        try:
            fiat_dec = Decimal(extra[0].replace(",", "."))
            fiat = str(fiat_dec)
            extra = extra[1:]
        except InvalidOperation:
            pass
    note = " ".join(extra).strip()

    # Если для income нет фиатной цены — попробуем подтянуть автоматически.
    if not fiat and tx_type in TxType.income_types():
        try:
            market = await get_asset_price_rub(asset, ts)
            if market:
                fiat = str((Decimal(str(market)) * amount).quantize(Decimal("0.01")))
        except Exception as e:
            logger.warning(f"get_asset_price_rub: {e}")

    try:
        tx = add_transaction(
            owner_id=_owner_id(context),
            ts=ts,
            asset_symbol=asset,
            tx_type=tx_type,
            amount=str(amount),
            fiat_value_rub=fiat or None,
            cost_basis_rub=cost or None,
            note=note,
        )
    except CryptoError as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return

    asset_meta = ensure_asset(asset)
    pretty = humanize_amount(tx.amount_raw, asset_meta.decimals)
    msg = [
        "Записано:",
        f"  <b>#{tx.id}</b> · {escape_html(TxType.label(tx.tx_type))}",
        f"  {escape_html(asset)}: <b>{pretty}</b>",
        f"  дата: {datetime.fromtimestamp(ts).strftime('%d.%m.%Y')}",
    ]
    if fiat:
        msg.append(f"  стоимость: <b>{format_rub(float(fiat))}</b>")
    if note:
        msg.append(f"  коммент: {escape_html(note[:200])}")
    await update.message.reply_text("\n".join(msg), parse_mode="HTML")


# --------------------------------------------------------- /airdrop


async def airdrop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/airdrop <asset> <amount> [date] [price_rub] [note]` — ярлык для /addtx."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "🎁 <b>Записать аирдроп</b>\n\n"
            "Формат:\n"
            "<code>/airdrop &lt;актив&gt; &lt;количество&gt; "
            "[дата] [цена ₽] [комментарий]</code>\n\n"
            "Примеры:\n"
            "<code>/airdrop BTC 0.5</code> — 0.5 BTC сегодня\n"
            "<code>/airdrop TON 25 15.01.2026 180000</code> — 25 TON с ценой\n"
            "<code>/airdrop NOT 1000 20.01.2026</code> — 1000 NOT\n\n"
            "Дата по умолчанию — сегодня. Цена нужна, чтобы посчитать доход: "
            "если её не указать, возьму рыночную на дату операции.\n\n"
            "Проверить, что записалось: <code>/tx_list</code>",
            parse_mode="HTML",
        )
        return
    new_args = ["airdrop"] + list(args)
    context.args = new_args
    await addtx_command(update, context)


# --------------------------------------------------------- /tx_list


async def tx_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/tx_list [year] [asset] [type]` — последние операции + кнопки смены типа."""
    if not await _is_owner_chat(update, context):
        return
    args = context.args or []
    year = None
    asset = None
    tx_type = None
    for a in args:
        if a.isdigit() and len(a) == 4 and (a.startswith("20") or a.startswith("21")):
            year = int(a)
        elif a.upper() in {c.upper() for c in Chain.all()}:
            continue
        elif a in TxType.all():
            tx_type = a
        else:
            asset = a.upper()
    owner = _owner_id(context)
    txs = list_transactions(owner, year=year, asset_symbol=asset,
                            tx_type=tx_type, limit=50)
    if not txs:
        await update.message.reply_text("Операций не найдено.")
        return

    asset_cache: dict[int, Asset] = {}

    def asset_of(tx: Tx) -> Asset:
        if tx.asset_id in asset_cache:
            return asset_cache[tx.asset_id]
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM crypto_assets WHERE id = ?", (tx.asset_id,)
            ).fetchone()
        from utils.crypto_db import _asset_from_row
        a = _asset_from_row(row)
        asset_cache[tx.asset_id] = a
        return a

    # Ограничиваем количество операций с inline-кнопками: 10 строк × 3 кнопки
    # = 30 callback_data в одной клавиатуре — потолок Telegram. Остальное
    # выводим текстом без кнопок (для исправления — отдельный /tx_fix).
    buttonable = txs[:10]
    rest = txs[10:]

    lines = ["<b>Операции:</b>\n"]
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for tx in buttonable:
        a = asset_of(tx)
        amt = humanize_amount(tx.amount_raw, a.decimals)
        date = datetime.fromtimestamp(tx.ts).strftime("%d.%m.%Y")
        sign = "↗" if tx.direction == "in" else "↘"
        type_lbl = TxType.label(tx.tx_type)
        info = f"<b>#{tx.id}</b> {sign} {type_lbl} <b>{amt}</b> {a.symbol}"
        info += f" · {date}"
        if tx.fiat_value_rub:
            info += f" · {format_rub(float(tx.fiat_value_rub))}"
        if tx.note:
            info += f"\n  <i>{escape_html(tx.note[:120])}</i>"
        lines.append(info)

        # Кнопки: 3 самых вероятных альтернативы типа + «Удалить».
        # Список альтернатив зависит от текущего направления:
        if tx.direction == "in":
            alts = [TxType.AIRDROP, TxType.MINING, TxType.STAKING,
                    TxType.GIFT_IN, TxType.REFUND]
        else:
            alts = [TxType.SELL, TxType.SWAP_OUT, TxType.SPEND,
                    TxType.GIFT_OUT, TxType.SELL_NFT]
        alts = [t for t in alts if t != tx.tx_type][:3]
        row_buttons: list[InlineKeyboardButton] = []
        for alt in alts:
            short = {
                TxType.AIRDROP: "Аирдроп",
                TxType.MINING: "Майнинг",
                TxType.STAKING: "Стейкинг",
                TxType.GIFT_IN: "Подарок вход",
                TxType.REFUND: "Возврат",
                TxType.SELL: "Продажа",
                TxType.SWAP_OUT: "Обмен",
                TxType.SPEND: "Оплата",
                TxType.GIFT_OUT: "Подарок исх",
                TxType.SELL_NFT: "Продажа NFT",
            }.get(alt, alt)
            row_buttons.append(InlineKeyboardButton(
                text=short,
                callback_data=f"txset:{tx.id}:{alt}",
            ))
        row_buttons.append(InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=f"txdel:{tx.id}",
        ))
        keyboard_rows.append(row_buttons)

    if rest:
        lines.append(f"\n<i>…и ещё {len(rest)} (без кнопок — уточни фильтр)</i>")

    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3500] + "\n<i>…обрезано</i>"

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None,
    )


# Колбэки от кнопок «сменить тип» / «удалить».
async def tx_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка inline-кнопок под /tx_list."""
    query = update.callback_query
    await query.answer()
    if not query.data or ":" not in query.data:
        return
    kind, tx_id_raw, *rest = query.data.split(":", 2)
    try:
        tx_id = int(tx_id_raw)
    except ValueError:
        return

    owner = int(context.bot_data.get("CRYPTO_OWNER_ID") or 0)
    if not owner:
        return

    if kind == "txset":
        new_type = rest[0] if rest else ""
        if new_type not in TxType.all():
            await query.answer(f"Неизвестный тип: {new_type}", show_alert=False)
            return
        from utils.database import get_db as _db
        with _db() as conn:
            cur = conn.execute(
                "UPDATE crypto_transactions SET tx_type=?, direction=? "
                "WHERE id=? AND owner_id=?",
                (new_type,
                 "in" if new_type in TxType.income_types() else "out",
                 tx_id, owner),
            )
        if cur.rowcount:
            await query.answer(f"→ {TxType.label(new_type)}", show_alert=False)
            # Обновим строку текста сообщения, чтобы кнопка отражала текущее.
            try:
                old_text = query.message.text or ""
                # Меняем «<тип>» в строке этого #id на новый.
                import re as _re
                pattern = _re.compile(
                    rf"(#\b{tx_id}\b [↗↘] )(\S+(?:\s\S+)*?)( \*\*)",
                )
                new_text = pattern.sub(
                    rf"\g<1>{TxType.label(new_type)}\g<3>",
                    old_text, count=1,
                )
                if new_text != old_text:
                    await query.edit_message_text(
                        new_text, parse_mode="HTML",
                        reply_markup=query.message.reply_markup,
                    )
            except Exception:
                pass  # не критично
        else:
            await query.answer("Не найдено.", show_alert=True)

    elif kind == "txdel":
        from utils.crypto_db import delete_transaction
        if delete_transaction(tx_id, owner):
            await query.answer("Удалено.", show_alert=False)
            try:
                # Удалим строку из текста.
                import re as _re
                old_text = query.message.text or ""
                lines = old_text.split("\n")
                new_lines = [l for l in lines
                             if not _re.match(rf"<b>#{tx_id}\b", l)]
                # Также уберём строку-кнопку.
                if query.message.reply_markup:
                    new_markup_rows = []
                    for r in query.message.reply_markup.inline_keyboard:
                        if any(":" in b.callback_data and b.callback_data.split(":", 1)[1].split(":", 1)[0] == str(tx_id)
                               for b in r):
                            continue
                        new_markup_rows.append(r)
                    new_markup = InlineKeyboardMarkup(new_markup_rows) if new_markup_rows else None
                else:
                    new_markup = None
                await query.edit_message_text(
                    "\n".join(new_lines), parse_mode="HTML",
                    reply_markup=new_markup,
                )
            except Exception:
                pass
        else:
            await query.answer("Не найдено.", show_alert=True)


def _db():
    return get_db  # alias


async def tx_del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/tx_del <id>` — удалить операцию (вместе с лотами)."""
    if not await _is_owner_chat(update, context):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Использование: <code>/tx_del &lt;id&gt;</code>", parse_mode="HTML"
        )
        return
    tx_id = int(args[0])
    from utils.crypto_db import delete_transaction
    if delete_transaction(tx_id, _owner_id(context)):
        await update.message.reply_text(f"Операция #{tx_id} удалена.")
    else:
        await update.message.reply_text("Не найдено.")


# --------------------------------------------------------- /balance


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/balance` — портфель с текущей рыночной оценкой."""
    if not await _is_owner_chat(update, context):
        return
    owner = _owner_id(context)
    rebuild_lots(owner)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT a.symbol, a.decimals,
                   SUM(CAST(l.qty_remaining AS REAL)) AS qty_raw,
                   SUM(CAST(l.qty_remaining AS REAL) /
                       POW(10, a.decimals) * CAST(l.cost_per_unit_rub AS REAL)
                   ) AS cost_rub
            FROM tax_lots l
            JOIN crypto_assets a ON a.id = l.asset_id
            WHERE l.owner_id = ? AND CAST(l.qty_remaining AS REAL) > 0
            GROUP BY a.id
            ORDER BY a.symbol
            """,
            (owner,),
        ).fetchall()

    if not rows:
        await update.message.reply_text(
            "Баланс пуст. Запиши первую операцию: "
            "<code>/addtx airdrop BTC 0.001 15.01.2025</code>",
            parse_mode="HTML",
        )
        return

    lines = ["<b>Портфель:</b>\n"]
    total_value = 0.0
    total_cost = 0.0
    for r in rows:
        sym = r["symbol"]
        qty = float(r["qty_raw"] or 0) / (10 ** int(r["decimals"]))
        cost = float(r["cost_rub"] or 0)
        total_cost += cost
        price = await get_asset_price_rub(sym, int(time.time()))
        if price is None:
            value = None
        else:
            value = price * qty
            total_value += value
        avg_cost = cost / qty if qty else 0
        price_str = format_rub(value) if value is not None else "нет котировки"
        lines.append(
            f"<b>{sym}</b>: {qty:.8f}".rstrip("0").rstrip(".")
            + f"\n  стоимость: {price_str}"
            + (f"\n  средняя цена: {format_rub(avg_cost)}"
               if avg_cost > 0 else "")
        )

    lines.append("")
    lines.append(f"<b>Σ стоимость:</b> {format_rub(total_value)}")
    if total_cost > 0:
        lines.append(f"<b>Σ себестоимость:</b> {format_rub(total_cost)}")
        diff = total_value - total_cost
        lines.append(f"<b>Нереализованный P&L:</b> {format_rub(diff)}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# --------------------------------------------------------- /pnl


async def pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/pnl [year]` — реализованный P&L за год."""
    if not await _is_owner_chat(update, context):
        return
    args = context.args or []
    year = int(args[0]) if args and args[0].isdigit() else datetime.now().year
    owner = _owner_id(context)
    from utils.tax_calc import compute_year_report, cache_report
    summary = await compute_year_report(owner, year)
    cache_report(owner, summary)
    lines = [
        f"<b>P&L за {year}:</b>\n",
        f"Доход (аирдропы, майнинг и т.п.): <b>{format_rub(float(summary.total_income_rub))}</b>",
        f"Себестоимость реализованных: <b>{format_rub(float(summary.total_cost_rub))}</b>",
        f"Реализованная прибыль: <b>{format_rub(float(summary.realized_gain_rub))}</b>",
        f"Доходов событий: {len(summary.income_events)}",
        f"Реализаций событий: {len(summary.disposal_events)}",
    ]
    if summary.warnings:
        lines.append("\n<b>Замечания:</b>")
        for w in summary.warnings[:5]:
            lines.append(f"• {escape_html(w[:200])}")
        if len(summary.warnings) > 5:
            lines.append(f"…и ещё {len(summary.warnings) - 5}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# --------------------------------------------------------- /rate


async def rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/rate <asset>` — текущая цена актива в RUB."""
    if not await _is_owner_chat(update, context):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Использование: <code>/rate &lt;asset&gt;</code>", parse_mode="HTML"
        )
        return
    asset = args[0].strip().upper()
    ensure_asset(asset)
    price = await get_asset_price_rub(asset, int(time.time()))
    if price is None:
        await update.message.reply_text(
            f"Не удалось получить курс <b>{asset}</b> — проверь, "
            "что CoinGecko знает этот тикер.",
            parse_mode="HTML",
        )
        return
    await update.message.reply_text(
        f"<b>{asset}</b>: {format_rub(price)}",
        parse_mode="HTML",
    )


# --------------------------------------------------------- /import


async def import_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/import` — реплай на CSV-файл. Парсим и заливаем."""
    if not await _is_owner_chat(update, context):
        return
    msg = update.message
    document = msg.document if msg else None
    if not document:
        await msg.reply_text(
            "Пришли CSV-файл выписки биржи <b>реплаем</b> на сообщение "
            "с командой <code>/import</code>.\n\n"
            "Поддерживаются: Bybit (trades), OKX (orders history), "
            "а также внутренний формат с колонками: "
            "<code>ts, asset, type, amount, fiat_rub, cost_basis_rub, note</code>",
            parse_mode="HTML",
        )
        return
    if not (document.file_name or "").lower().endswith(".csv"):
        await msg.reply_text("Ожидался .csv-файл.")
        return
    try:
        file = await context.bot.get_file(document.file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
    except Exception as e:
        logger.error(f"/import: скачивание не удалось: {e}", exc_info=True)
        await msg.reply_text(f"Не удалось скачать файл: {e}")
        return

    parsed, errors = parse_csv(buf.getvalue())

    if errors and not parsed:
        await msg.reply_text(
            "Не удалось распознать файл:\n" +
            "\n".join(f"• {escape_html(e)}" for e in errors[:10])
        )
        return

    owner = _owner_id(context)
    # Импорт идёт через persist_cex_rows — тем же путём, что и API-синк.
    # Раньше CSV-строки писались напрямую, и quote-сумма (USDT) попадала
    # в рублёвые поля: покупка 0.5 BTC по 60000 USDT давала себестоимость
    # 30 000 ₽ вместо ~2.4 млн ₽, и отчёт 3-НДФЛ был занижен в ~80 раз.
    # Здесь quote переводится в рубли по курсу на дату сделки, а если
    # котировки нет — сделка пишется без рублевой оценки и попадает в
    # отчёт как «нет котировки», а не как неверное число.
    saved, dups = await persist_cex_rows(owner, parsed, "csv")
    skipped = dups
    fail = 0

    msg_lines = [
        "<b>Импорт завершён:</b>",
        f"• распознано строк: {len(parsed)}",
        f"• сохранено проводок: {saved}",
    ]
    if skipped:
        msg_lines.append(f"• пропущено (дубли/ошибки): {skipped}")
    if fail:
        msg_lines.append(f"• сбой: {fail}")
    if errors:
        msg_lines.append("\n<b>Что не удалось:</b>")
        msg_lines.extend(f"• {escape_html(e)}" for e in errors[:10])
    await msg.reply_text("\n".join(msg_lines), parse_mode="HTML")


# --------------------------------------------------------- helpers


def string_list_of(items: list[str]) -> str:
    return ", ".join(f"<code>{x}</code>" for x in items)