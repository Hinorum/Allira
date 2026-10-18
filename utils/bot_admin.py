"""Общая логика сервиса управления ботом.

Один слой для ДВУХ интерфейсов: веб-панели (/admin) и Telegram-админ-меню
(/admin в чате). Здесь — всё, что нужно «справа» управлять ботом:

  • статус: аптайм, фоновые задачи, здоровье БД, крипто-сводка;
  • ручные запуски синхронизаций (CEX, TON) и сверки;
  • CRUD кошельков и операций (дублирует crypto_db, но с нормальными
    ошибками для UI);
  • настройки налогоплательщика;
  • кольцевой буфер последних логов (см. utils/log_buffer).

Веб-панель и телеграм-меню НЕ содержат бизнес-логики — только рендер и
перевод параметров в вызовы этих функций.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Optional

from utils.crypto_db import (CryptoError, add_transaction, add_wallet,
                             delete_transaction, get_tax_settings,
                             list_wallets, set_tax_settings,
                             humanize_amount)
from utils.database import get_db
from utils.reconciliation import compute_balances, reconcile

logger = logging.getLogger(__name__)

# Главный event loop (захватывается в main при старте). Веб-сервер работает
# в отдельном потоке и запускает async-операции через run_coroutine_threadsafe
# — иначе глобальный httpx-клиент из get_client() упадёт «attached to a
# different loop».
MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


def run_async(coro, timeout: float = 300.0):
    """Выполнить coroutine в главном loop'е из любого потока."""
    if MAIN_LOOP is None or MAIN_LOOP.is_closed():
        raise RuntimeError("Главный event loop не инициализирован")
    return asyncio.run_coroutine_threadsafe(coro, MAIN_LOOP).result(timeout)


# ------------------------------------------------------------- статус


def database_stats() -> dict:
    """Размер БД и базовые счётчики — быстро, один SQL-файл на диске."""
    from utils.database import DB_PATH
    size = 0
    try:
        size = os.path.getsize(DB_PATH)
    except OSError:
        pass
    counts: dict[str, int] = {}
    try:
        with get_db() as conn:
            for table in ("users", "messages", "crypto_transactions",
                          "crypto_wallets", "tax_reports"):
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*) AS c FROM {table}").fetchone()
                    counts[table] = int(row["c"] if row else 0)
                except Exception:
                    counts[table] = -1
    except Exception as e:
        logger.warning(f"database_stats: {e}")
    return {"path": DB_PATH, "size_bytes": size, "counts": counts}


def jobs_status(application) -> list[dict]:
    """Состояние фоновых задач PTB: имя, интервал, время до следующего запуска."""
    out: list[dict] = []
    try:
        jq = application.job_queue
        for job in jq.jobs():
            next_t = getattr(job, "next_t", None)
            out.append({
                "name": job.name,
                "next_run_in_s": int(next_t) if next_t is not None else None,
            })
    except Exception as e:
        logger.warning(f"jobs_status: {e}")
    return sorted(out, key=lambda j: j["name"])


def crypto_stats(owner_id: int) -> dict:
    """Сводка крипто-учёта для панели: кошельки, операции, балансы, синки."""
    if not owner_id:
        return {"enabled": False}
    wallets = list_wallets(owner_id)
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c, MAX(ts) AS last_ts "
            "FROM crypto_transactions WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()
        tx_count = int(row["c"] if row else 0)
        last_tx_ts = int(row["last_ts"]) if row and row["last_ts"] else 0

        by_type = {}
        try:
            rows = conn.execute(
                "SELECT tx_type, COUNT(*) AS c FROM crypto_transactions "
                "WHERE owner_id = ? GROUP BY tx_type ORDER BY c DESC",
                (owner_id,),
            ).fetchall()
            by_type = {r["tx_type"]: int(r["c"]) for r in rows}
        except Exception:
            pass

    balances = []
    try:
        balances = [
            {"symbol": b.symbol, "qty": str(b.qty), "cost_rub": str(b.cost_rub)}
            for b in compute_balances(owner_id) if b.qty != 0
        ]
    except Exception as e:
        logger.warning(f"crypto_stats balances: {e}")

    # Watermark'и синков — когда последний раз что-то подтягивалось.
    watermarks: dict[str, int] = {}
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM bot_stats "
                "WHERE key LIKE 'cex_sync_ts:%' OR key LIKE 'jetton_sync_ts:%'"
            ).fetchall()
            watermarks = {r["key"]: int(r["value"] or 0) for r in rows}
    except Exception:
        pass

    return {
        "enabled": True,
        "owner_id": owner_id,
        "wallets": [{"id": w.id, "chain": w.chain, "label": w.label,
                     "address": w.short_address} for w in wallets],
        "tx_count": tx_count,
        "last_tx_ts": last_tx_ts,
        "by_type": by_type,
        "balances": balances,
        "sync_watermarks": watermarks,
    }


def full_status(application, config, start_time: float) -> dict:
    """Полный статус для панели/админ-меню (без сетевых вызовов)."""
    uptime = int(time.time() - start_time)
    owner_id = int(getattr(config, "crypto_owner_id", 0) or 0)
    try:
        from tasks.marketapp_reports import LAST_SYNC, LAST_HTTP_ERROR
        marketapp = {
            "last_sync": {k: int(v["at"]) for k, v in LAST_SYNC.items()},
            "last_error": (LAST_HTTP_ERROR["text"] if LAST_HTTP_ERROR["at"] else None),
        }
    except Exception:
        marketapp = {}

    return {
        "time": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        "uptime_seconds": uptime,
        "uptime": f"{uptime // 3600}h {(uptime % 3600) // 60}m",
        "jobs": jobs_status(application),
        "database": database_stats(),
        "crypto": crypto_stats(owner_id),
        "marketapp": marketapp,
        "config": {
            # Только признаки «задано/не задано» — секреты не отдаются.
            "bot_token": bool(config.bot_token),
            "openrouter": bool(config.openrouter_api_key),
            "tonapi": bool(config.tonapi_api_key),
            "toncenter": bool(config.toncenter_api_key),
            "marketapp": bool(config.marketapp_api_key and config.marketapp_wallet),
            "bybit": bool(config.bybit_api_key and config.bybit_api_secret),
            "okx": bool(config.okx_api_key and config.okx_api_secret
                        and config.okx_passphrase),
            "admin_chat": bool(config.admin_chat_id),
        },
    }


# --------------------------------------------------------- ручные синки


async def manual_cex_sync(config, owner_id: int) -> dict:
    """Ручной запуск синка бирж (тот же код, что у фоновой джобы)."""
    from utils.cex_api import sync_bybit_trades, sync_okx_trades
    from tasks.cex_sync import persist_cex_rows

    result: dict = {"bybit": None, "okx": None}
    if config.bybit_api_key and config.bybit_api_secret:
        try:
            sync = await sync_bybit_trades(config.bybit_api_key,
                                           config.bybit_api_secret)
            saved, dups = await persist_cex_rows(owner_id, sync.rows, "bybit")
            result["bybit"] = {"saved": saved, "dups": dups,
                               "errors": sync.errors}
        except Exception as e:
            result["bybit"] = {"saved": 0, "dups": 0, "errors": [str(e)]}
    if config.okx_api_key and config.okx_api_secret and config.okx_passphrase:
        try:
            sync = await sync_okx_trades(config.okx_api_key, config.okx_api_secret,
                                         config.okx_passphrase)
            saved, dups = await persist_cex_rows(owner_id, sync.rows, "okx")
            result["okx"] = {"saved": saved, "dups": dups, "errors": sync.errors}
        except Exception as e:
            result["okx"] = {"saved": 0, "dups": 0, "errors": [str(e)]}
    return result


async def manual_jetton_sync(config, owner_id: int) -> dict:
    """Ручной запуск скана TON-кошельков (тот же код, что у фоновой джобы)."""
    from tasks.crypto_sync import sync_jetton_transfers
    from utils.crypto_db import Chain, list_wallets

    api_key = getattr(config, "tonapi_api_key", "")
    out: list[dict] = []
    for w in list_wallets(owner_id, chain=Chain.TON):
        try:
            saved = await sync_jetton_transfers(w, api_key)
            out.append({"wallet": w.short_address, "saved": saved})
        except Exception as e:
            out.append({"wallet": w.short_address, "error": str(e)})
    return {"wallets": out}


async def manual_reconcile(config, owner_id: int, year: int,
                           with_actual: bool = True) -> dict:
    """Ручной запуск сверки — возвращает отчёт в JSON-подобном виде."""
    from utils.reconciliation import report_to_dict
    report = await reconcile(owner_id, year, config=config,
                             with_actual=with_actual)
    data = report_to_dict(report)
    data["ok"] = report.ok
    return data


# ------------------------------------------------------------- CRUD UI


def wallets_ui(owner_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT w.*, "
            "(SELECT COUNT(*) FROM crypto_transactions t "
            " WHERE t.wallet_id = w.id) AS tx_count "
            "FROM crypto_wallets w WHERE w.owner_id = ? "
            "ORDER BY w.is_active DESC, w.chain, w.label",
            (owner_id,),
        ).fetchall()
    return [{
        "id": r["id"], "chain": r["chain"], "label": r["label"] or "",
        "address": r["address"], "kind": r["kind"] or "own",
        "active": bool(r["is_active"]), "tx_count": int(r["tx_count"] or 0),
    } for r in rows]


def add_wallet_ui(owner_id: int, chain: str, address: str,
                  label: str = "") -> dict:
    w = add_wallet(owner_id, chain, address, label)
    return {"id": w.id, "chain": w.chain, "address": w.address}


def set_wallet_active(owner_id: int, wallet_id: int, active: bool) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE crypto_wallets SET is_active = ? "
            "WHERE id = ? AND owner_id = ?",
            (1 if active else 0, wallet_id, owner_id),
        )
        return cur.rowcount > 0


def transactions_ui(owner_id: int, limit: int = 50, offset: int = 0,
                    asset: str = "", tx_type: str = "") -> list[dict]:
    sql = ("SELECT t.*, a.symbol, a.decimals, w.label AS wallet_label "
           "FROM crypto_transactions t "
           "JOIN crypto_assets a ON a.id = t.asset_id "
           "LEFT JOIN crypto_wallets w ON w.id = t.wallet_id "
           "WHERE t.owner_id = ?")
    args: list = [owner_id]
    if asset:
        sql += " AND a.symbol = ?"
        args.append(asset.strip().upper())
    if tx_type:
        sql += " AND t.tx_type = ?"
        args.append(tx_type)
    sql += " ORDER BY t.ts DESC, t.id DESC LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    with get_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{
        "id": r["id"],
        "ts": int(r["ts"]),
        "date": datetime.fromtimestamp(int(r["ts"])).strftime("%d.%m.%Y %H:%M"),
        "asset": r["symbol"],
        "qty": humanize_amount(r["amount_raw"], int(r["decimals"] or 9)),
        "type": r["tx_type"],
        "direction": r["direction"],
        "fiat_rub": r["fiat_value_rub"] or "",
        "cost_rub": r["cost_basis_rub"] or "",
        "note": r["note"] or "",
        "source": r["source"] or "manual",
        "wallet": r["wallet_label"] or "",
    } for r in rows]


def add_transaction_ui(owner_id: int, *, asset: str, tx_type: str, amount: str,
                       date_str: str = "", fiat_rub: str = "",
                       cost_rub: str = "", note: str = "") -> dict:
    """Добавить операцию из UI. date_str: YYYY-MM-DD (пусто = сейчас)."""
    if date_str:
        ts = int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())
    else:
        ts = int(time.time())
    tx = add_transaction(
        owner_id=owner_id, ts=ts, asset_symbol=asset, tx_type=tx_type,
        amount=amount, fiat_value_rub=fiat_rub or None,
        cost_basis_rub=cost_rub or None, note=note, source="admin-panel",
    )
    return {"id": tx.id}


def delete_transaction_ui(owner_id: int, tx_id: int) -> bool:
    return delete_transaction(tx_id, owner_id)


# ------------------------------------------------------- настройки ФНС


def tax_settings_ui(owner_id: int) -> dict:
    return get_tax_settings(owner_id)


def save_tax_settings(owner_id: int, payload: dict) -> None:
    allowed_status = {"resident", "miner", "nonresident"}
    allowed_method = {"fifo", "wac"}
    safe: dict = {}
    if payload.get("taxpayer_inn"):
        safe["taxpayer_inn"] = str(payload["taxpayer_inn"]).strip()
    if payload.get("taxpayer_name"):
        safe["taxpayer_name"] = str(payload["taxpayer_name"]).strip()
    status = str(payload.get("tax_status") or "").strip().lower()
    if status:
        if status not in allowed_status:
            raise CryptoError(f"Статус должен быть одним из: {sorted(allowed_status)}")
        safe["tax_status"] = status
    method = str(payload.get("cost_basis") or "").strip().lower()
    if method:
        if method not in allowed_method:
            raise CryptoError(f"Метод должен быть одним из: {sorted(allowed_method)}")
        safe["cost_basis"] = method
    if safe:
        set_tax_settings(owner_id, **safe)
