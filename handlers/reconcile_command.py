"""Хендлер сверки «дебет-кредит» и балансов.

Команды (только в ЛС владельца):
  /reconcile [год]        — полная сверка: журнал проводок, контроль
                            парности, расчётные остатки против фактических
                            (TON-кошельки + биржи Bybit/OKX).
  /reconcile_xlsx [год]   — XLSX-выгрузка журнала проводок и расхождений.

Сверка — ключевая проверка для налоговой отчётности: если Дт ≠ Кт или
расчётный остаток разошёлся с сетью, в 3-НДФЛ уйдут недостоверные цифры.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime

from telegram import InputFile, Update
from telegram.ext import ContextTypes

from utils.common import escape_html
from utils.crypto_db import get_tax_settings
from utils.permissions import owner_id as _owner_id
from utils.permissions import require_owner as _is_owner_chat
from utils.price_oracle import format_rub
from utils.reconciliation import ReconciliationReport, reconcile
from utils.report_export import render_reconciliation_xlsx

logger = logging.getLogger(__name__)


# Проверки владельца — единые для всех команд, см. utils/permissions.py.


def _parse_year(args) -> int:
    if args and args[0].isdigit():
        return int(args[0])
    return datetime.now().year


def _format_report(report: ReconciliationReport) -> str:
    """Текстовое сообщение со сводкой сверки."""
    status = "✅ Сверка пройдена" if report.ok else f"⚠️ Расхождений: {len(report.issues)}"
    lines = [
        f"<b>Сверка дебет-кредит за {report.year}</b>",
        f"{status}",
        "",
        f"Проводок в журнале: <b>{len(report.entries)}</b>",
        f"Σ Дт: <b>{format_rub(float(report.total_dr_rub))}</b> · "
        f"Σ Кт: <b>{format_rub(float(report.total_cr_rub))}</b>",
        "",
        "<b>Расчётные остатки:</b>",
    ]
    active = [b for b in report.balances if b.qty != 0]
    if not active:
        lines.append("  — нет активов с ненулевым остатком")
    for b in active[:15]:
        market = (f" (~{format_rub(float(b.market_value_rub))})"
                  if b.market_value_rub is not None else "")
        lines.append(f"  {escape_html(b.symbol)}: <b>{b.qty}</b>{market}")

    if report.actual:
        lines.append("")
        lines.append("<b>Фактические остатки (сеть/биржи):</b>")
        for a in report.actual[:15]:
            lines.append(f"  {escape_html(a.symbol)}: <b>{a.qty}</b> "
                         f"<i>({escape_html(a.where)})</i>")

    if report.issues:
        lines.append("")
        lines.append("<b>Расхождения:</b>")
        for issue in report.issues[:10]:
            lines.append(f"• [{escape_html(issue.kind)}] {escape_html(issue.message[:180])}")
        if len(report.issues) > 10:
            lines.append(f"…и ещё {len(report.issues) - 10}. Детали — в /reconcile_xlsx")

    lines.append("")
    lines.append(
        "<i>Сверка не заменяет консультацию налогового консультанта, но "
        "гарантирует, что журнал сходится с сетью и биржами.</i>"
    )
    return "\n".join(lines)


async def reconcile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/reconcile [год]`."""
    if not await _is_owner_chat(update, context):
        return
    year = _parse_year(context.args)
    owner = _owner_id(context)

    await update.message.reply_text(
        f"Сверяю дебет-кредит за {year}… Загружаю балансы сети и бирж."
    )
    config = context.bot_data.get("BOT_CONFIG")
    report = await reconcile(owner, year, config=config, with_actual=True)
    await update.message.reply_text(_format_report(report), parse_mode="HTML")


async def reconcile_xlsx_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/reconcile_xlsx [год]` — журнал проводок + расхождения в XLSX."""
    if not await _is_owner_chat(update, context):
        return
    year = _parse_year(context.args)
    owner = _owner_id(context)

    await update.message.reply_text(f"Готовлю XLSX-сверку за {year}…")
    config = context.bot_data.get("BOT_CONFIG")
    report = await reconcile(owner, year, config=config, with_actual=True)
    settings = get_tax_settings(owner)
    payload = await asyncio.to_thread(render_reconciliation_xlsx, report, settings)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(payload),
                           filename=f"reconciliation_{year}.xlsx"),
        caption=f"Сверка дебет-кредит за {year}. "
                f"Расхождений: {len(report.issues)}.",
    )
