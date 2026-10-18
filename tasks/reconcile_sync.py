"""Фоновая задача: ежедневная сверка «дебет-кредит» с уведомлением.

Раз в сутки (в 03:30 локального времени — минимум активности) сверяет
журнал проводок за текущий год с фактическими балансами сети и бирж.
Если найдены расхождения — шлёт владельцу сообщение со списком, чтобы
ошибки в учёте обнаруживались ДО подачи 3-НДФЛ, а не по звонку из ФНС.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dt_time

from telegram.ext import Application

from utils.reconciliation import reconcile

logger = logging.getLogger(__name__)

# Ключ в bot_data: статус последней сверки + день последнего уведомления,
# чтобы не спамить одинаковыми расхождениями каждый день.
LAST_RECONCILE_KEY = "LAST_RECONCILE_STATUS"
LAST_NOTIFY_DAY_KEY = "LAST_REconcile_NOTIFY_DAY"


def setup_reconcile_jobs(application: Application) -> None:
    """Подключает ежедневную сверку, если настроен владелец крипто-учёта."""
    config = application.bot_data.get("BOT_CONFIG")
    if not config:
        return
    owner_id = int(config.crypto_owner_id or 0)
    if not owner_id:
        logger.info("setup_reconcile_jobs: CRYPTO_OWNER_ID не задан — пропуск")
        return
    if not (config.tonapi_api_key or config.bybit_api_key or config.okx_api_key):
        logger.info("setup_reconcile_jobs: нет ключей сети/бирж — пропуск")
        return

    application.job_queue.run_daily(
        _reconcile_job,
        time=dt_time(hour=3, minute=30),  # минимум активности сети
        name="daily_reconcile",
        data={"owner_id": owner_id},
    )
    logger.info(f"Ежедневная сверка: owner={owner_id}, 03:30 локального времени")


async def _reconcile_job(context) -> None:
    owner_id = int(context.job.data.get("owner_id") or 0)
    if not owner_id:
        return
    year = datetime.now().year
    config = context.bot_data.get("BOT_CONFIG")
    try:
        report = await reconcile(owner_id, year, config=config,
                                 with_actual=True)
    except Exception as e:
        logger.error(f"daily_reconcile: {e}", exc_info=True)
        return

    status = "ok" if report.ok else f"drift:{len(report.issues)}"
    last = context.bot_data.get(LAST_RECONCILE_KEY)
    today = datetime.now().day
    last_notify_day = context.bot_data.get(LAST_NOTIFY_DAY_KEY, 0)
    context.bot_data[LAST_RECONCILE_KEY] = status

    if report.ok:
        if last and last != "ok":
            context.bot_data.pop(LAST_NOTIFY_DAY_KEY, None)
            await _notify(owner_id, context,
                          f"✅ Сверка за {year} снова чистая: "
                          f"расхождений нет.")
        return

    # Расхождения есть: уведомляем при смене статуса или раз в 7 дней,
    # пока проблема не ушла.
    if last == status and (today - int(last_notify_day)) < 7:
        return
    context.bot_data[LAST_NOTIFY_DAY_KEY] = today

    lines = [f"⚠️ <b>Сверка дебет-кредит за {year}: "
             f"{len(report.issues)} расхождений</b>", ""]
    for issue in report.issues[:8]:
        lines.append(f"• {issue.message[:200]}")
    if len(report.issues) > 8:
        lines.append(f"…и ещё {len(report.issues) - 8}")
    lines.append("")
    lines.append("Детали: <code>/reconcile</code> или <code>/reconcile_xlsx</code>")
    await _notify(owner_id, context, "\n".join(lines))


async def _notify(owner_id: int, context, text: str) -> None:
    chat_ids = set()
    config = context.bot_data.get("BOT_CONFIG")
    if config and config.admin_chat_id:
        chat_ids.add(config.admin_chat_id)
    chat_ids.add(str(owner_id))
    for chat_id in chat_ids:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text,
                                           parse_mode="HTML")
        except Exception as e:
            logger.warning(f"reconcile notify {chat_id}: {e}")
        await asyncio.sleep(0.5)
