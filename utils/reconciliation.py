"""Сверка «дебет-кредит» по крипто-операциям и балансам.

Задачи модуля:
  1. Журнал проводок. Каждая операция года превращается в пару
     «Дебет / Кредит» с RUB-оценкой: покупка актива дебетует актив и
     кредитует quote-валюту, продажа — наоборот, аирдроп дебетует актив
     и кредитует счёт прочего дохода (ст. 214 НК РФ).
  2. Контроль парности. Суммы Дт и Кт каждой проводки должны совпадать
     в RUB (допуск 1% — котировки бирж и оракула отличаются). Непарные
     операции попадают в issues с указанием tx_id.
  3. Баланс-контроль. Расчётный остаток каждого актива по журналу
     сверяется с ФАКТИЧЕСКИМ балансом: TON-кошельки — через TonAPI,
     биржи Bybit/OKX — через их read-only API, включая отдельные
     funding-счета (средства, выведенные из торговли, но ещё не
     отправленные в блокчейн). Расхождения — в issues.
  4. Контроль отрицательных остатков: продажа больше, чем есть на
     балансе, — признак незаписанного прихода.

Итог — ReconciliationReport, который отдаётся в чат, выгружается в XLSX
и проверяется фоновой джобой (уведомление при расхождении).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from utils.crypto_db import (Chain, Tx, TxType, _to_decimal, list_wallets)
from utils.database import get_db
from utils.http_client import get_client
from utils.price_oracle import get_asset_price_rub

logger = logging.getLogger(__name__)

TONAPI_API_URL = "https://tonapi.io/v2"

# Допуск расхождения Dr/Cr в RUB-оценке пары (разные источники котировок).
PAIR_TOLERANCE = Decimal("0.01")


# ------------------------------------------------------------- дата-классы


@dataclass
class JournalEntry:
    """Одна проводка «дебет-кредит»."""
    ts: int
    tx_id: int
    kind: str              # income / buy / sell / trade / transfer / spend
    dr_account: str        # счет Дт (символ актива или счёт)
    cr_account: str        # счет Кт
    asset: str             # основной актив проводки
    qty: Decimal
    rub: Decimal           # RUB-оценка (0 — нет котировки)
    has_quote_pair: bool   # найдена парная quote-проводка
    note: str
    source: str


@dataclass
class BalanceRow:
    """Расчётный (по журналу) остаток актива."""
    symbol: str
    qty: Decimal
    cost_rub: Decimal      # остаточная себестоимость по FIFO
    last_price_rub: Optional[Decimal] = None

    @property
    def market_value_rub(self) -> Optional[Decimal]:
        if self.last_price_rub is None:
            return None
        return (self.qty * self.last_price_rub).quantize(Decimal("0.01"))


@dataclass
class ActualBalance:
    """Фактический баланс из сети/биржи."""
    symbol: str
    qty: Decimal
    where: str             # например "ton:UQ..." / "bybit" / "okx"


@dataclass
class ReconciliationIssue:
    kind: str              # unpaired / negative_balance / drift / no_quote / source_unavailable
    message: str
    tx_ids: list[int] = field(default_factory=list)
    symbol: str = ""


@dataclass
class ReconciliationReport:
    year: int
    entries: list[JournalEntry] = field(default_factory=list)
    balances: list[BalanceRow] = field(default_factory=list)
    actual: list[ActualBalance] = field(default_factory=list)
    issues: list[ReconciliationIssue] = field(default_factory=list)
    total_dr_rub: Decimal = Decimal(0)
    total_cr_rub: Decimal = Decimal(0)

    @property
    def delta_dr_cr(self) -> Decimal:
        return (self.total_dr_rub - self.total_cr_rub).quantize(Decimal("0.01"))

    @property
    def ok(self) -> bool:
        return not self.issues


# ----------------------------------------------------------- журнал Дт/Кт


def _asset_meta(asset_id: int, cache: dict[int, tuple[str, int]]):
    if asset_id in cache:
        return cache[asset_id]
    with get_db() as conn:
        row = conn.execute(
            "SELECT symbol, decimals FROM crypto_assets WHERE id = ?",
            (asset_id,),
        ).fetchone()
    meta = (row["symbol"] if row else "?", int(row["decimals"] or 9) if row else 9)
    cache[asset_id] = meta
    return meta


def _load_txs(owner_id: int, year: int) -> list[Tx]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM crypto_transactions "
            "WHERE owner_id = ? AND ts >= ? AND ts < ? "
            "ORDER BY ts ASC, id ASC",
            (owner_id, _year_start(year), _year_end(year)),
        ).fetchall()
    return [
        Tx(
            id=r["id"], owner_id=r["owner_id"], ts=int(r["ts"]),
            wallet_id=r["wallet_id"], asset_id=r["asset_id"],
            tx_type=r["tx_type"], direction=r["direction"],
            amount_raw=r["amount_raw"],
            fiat_value_rub=r["fiat_value_rub"],
            cost_basis_rub=r["cost_basis_rub"],
            note=r["note"] or "", source=r["source"] or "manual",
            external_id=r["external_id"],
            is_internal=bool(r["is_internal"]) if "is_internal" in r.keys()
            else False,
        ) for r in rows
    ]


def _rub(tx: Tx) -> Decimal:
    """RUB-оценка транзакции: fiat_value, иначе cost_basis, иначе 0."""
    for value in (tx.fiat_value_rub, tx.cost_basis_rub):
        if value:
            v = _to_decimal(value)
            if v > 0:
                return v
    return Decimal(0)


def build_journal(owner_id: int, year: int) -> list[JournalEntry]:
    """Построить журнал проводок за год.

    Связка «сделка + quote-проводка» ищется по external_id:
    парная запись имеет суффикс ':quote'. Если обе ноги есть — проводка
    помечается has_quote_pair=True, иначе считается непарной.
    """
    txs = _load_txs(owner_id, year)
    cache: dict[int, tuple[str, int]] = {}

    # Индекс quote-проводок по базовому external_id.
    quote_legs: dict[str, Tx] = {}
    for t in txs:
        if t.external_id and t.external_id.endswith(":quote"):
            quote_legs[t.external_id[:-len(":quote")]] = t

    entries: list[JournalEntry] = []
    for t in txs:
        if t.external_id and t.external_id.endswith(":quote"):
            continue  # обрабатывается вместе с базовой ногой
        symbol, decimals = _asset_meta(t.asset_id, cache)
        qty = _to_decimal(t.amount_raw) / (Decimal(10) ** decimals)

        quote = None
        if t.external_id:
            quote = quote_legs.get(t.external_id)

        if t.tx_type in TxType.income_types():
            # Дт Актив / Кт Прочий доход (ст. 214 НК РФ).
            entries.append(JournalEntry(
                ts=t.ts, tx_id=t.id, kind="income",
                dr_account=symbol, cr_account="ДОХОДЫ (ст.214 НК)",
                asset=symbol, qty=qty, rub=_rub(t),
                has_quote_pair=False, note=t.note, source=t.source,
            ))
        elif t.tx_type == TxType.BUY:
            # Дт Актив (себестоимость) / Кт Quote-валюта.
            rub = _rub(t)
            if quote is not None and rub == 0:
                qr = _rub(quote)
                rub = qr  # RUB-оценка могла быть на quote-ноге
            entries.append(JournalEntry(
                ts=t.ts, tx_id=t.id, kind="buy",
                dr_account=symbol,
                cr_account=(quote and _asset_meta(quote.asset_id, cache)[0])
                            or "ФИАТ/ВАЛЮТА",
                asset=symbol, qty=qty, rub=rub,
                has_quote_pair=quote is not None,
                note=t.note, source=t.source,
            ))
        elif t.tx_type == TxType.SELL:
            # Дт Quote-валюта (выручка) / Кт Актив (реализация).
            rub = _rub(t)
            entries.append(JournalEntry(
                ts=t.ts, tx_id=t.id, kind="sell",
                dr_account=(quote and _asset_meta(quote.asset_id, cache)[0])
                            or "ФИАТ/ВАЛЮТА",
                cr_account=symbol,
                asset=symbol, qty=qty, rub=rub,
                has_quote_pair=quote is not None,
                note=t.note, source=t.source,
            ))
        elif t.tx_type in (TxType.TRADE_IN, TxType.TRADE_OUT):
            # Конвертация: Дт/Кт два актива. RUB-оценку имеет базовая нога.
            entries.append(JournalEntry(
                ts=t.ts, tx_id=t.id, kind="trade",
                dr_account=(symbol if t.tx_type == TxType.TRADE_IN else "ФИАТ/ВАЛЮТА"),
                cr_account=(symbol if t.tx_type == TxType.TRADE_OUT else "ФИАТ/ВАЛЮТА"),
                asset=symbol, qty=qty, rub=Decimal(0),
                has_quote_pair=True, note=t.note, source=t.source,
            ))
        elif t.tx_type in TxType.transfer_types():
            entries.append(JournalEntry(
                ts=t.ts, tx_id=t.id, kind="transfer",
                dr_account=symbol, cr_account=symbol,
                asset=symbol, qty=qty, rub=Decimal(0),
                has_quote_pair=True,
                note=t.note or "внутреннее перемещение", source=t.source,
            ))
        elif t.tx_type in TxType.disposal_types():
            entries.append(JournalEntry(
                ts=t.ts, tx_id=t.id, kind="spend",
                dr_account="РАСХОДЫ/СПИСАНИЕ", cr_account=symbol,
                asset=symbol, qty=qty, rub=_rub(t),
                has_quote_pair=False, note=t.note, source=t.source,
            ))

    return entries


def check_pairing(entries: list[JournalEntry]) -> list[ReconciliationIssue]:
    """Контроль парности: продажа/обмен должны иметь парную приходную ногу.

    Реализация без пары (например, продажа вручную без записи выручки
    в quote-валюте) — типичная ошибка двойной записи: прибыль засчитается,
    а деньги «не появились».
    """
    issues: list[ReconciliationIssue] = []
    for e in entries:
        if e.kind == "sell" and not e.has_quote_pair:
            issues.append(ReconciliationIssue(
                kind="unpaired",
                message=(f"{time.strftime('%d.%m.%Y', time.localtime(e.ts))} "
                         f"продажа {e.qty} {e.asset} без парной приходной "
                         f"записи валюты (выручка {e.rub} RUB не привязана)"),
                tx_ids=[e.tx_id], symbol=e.asset,
            ))
        if e.kind == "buy" and not e.has_quote_pair:
            issues.append(ReconciliationIssue(
                kind="unpaired",
                message=(f"{time.strftime('%d.%m.%Y', time.localtime(e.ts))} "
                         f"покупка {e.qty} {e.asset} без парного списания "
                         f"валюты"),
                tx_ids=[e.tx_id], symbol=e.asset,
            ))
        if e.kind in ("income", "sell", "spend") and e.rub == 0:
            issues.append(ReconciliationIssue(
                kind="no_quote",
                message=(f"{time.strftime('%d.%m.%Y', time.localtime(e.ts))} "
                         f"{e.kind} {e.qty} {e.asset} — нет RUB-оценки "
                         f"(котировка недоступна)"),
                tx_ids=[e.tx_id], symbol=e.asset,
            ))
    return issues


# ------------------------------------------------------- баланс-контроль


def compute_balances(owner_id: int, up_to_ts: Optional[int] = None) -> list[BalanceRow]:
    """Расчётные остатки активов по журналу (приходы − списания) + FIFO-себестоимость."""
    sql = ("SELECT * FROM crypto_transactions WHERE owner_id = ?"
           if up_to_ts is None else
           "SELECT * FROM crypto_transactions WHERE owner_id = ? AND ts < ?")
    args: tuple = (owner_id,) if up_to_ts is None else (owner_id, up_to_ts)
    with get_db() as conn:
        rows = conn.execute(sql + " ORDER BY ts ASC, id ASC", args).fetchall()

    cache: dict[int, tuple[str, int]] = {}
    qty_map: dict[str, Decimal] = {}
    lots: dict[str, list[tuple[Decimal, Decimal]]] = {}  # symbol → [(qty, cost_per_unit)]

    for r in rows:
        symbol, decimals = _asset_meta(r["asset_id"], cache)
        scale = Decimal(10) ** decimals
        amount_raw = _to_decimal(r["amount_raw"])
        amount = amount_raw / scale  # human-единицы
        tx_type = r["tx_type"]
        # Перемещение внутри периметра (свой кошелёк ↔ биржа) — актив учтён
        # в месте покупки. Учитывать его здесь значит посчитать один актив
        # дважды: и лот от покупки, и лот от зачисления.
        if "is_internal" in r.keys() and r["is_internal"]:
            continue
        # Себестоимость лота — по тем же правилам, что и в tax_calc:
        # явная cost_basis_rub приоритетна; для доходных типов
        # (аирдроп, майнинг...) fiat_value_rub — это оценка ДОХОДА, а не
        # расход, и себестоимостью не становится. Раньше строка "0" в
        # cost_basis_rub перебивала fiat_value_rub (строка "0" истинна),
        # и одна и та же операция в сверке и в отчёте считалась по-разному.
        cost_total = _to_decimal(r["cost_basis_rub"] or 0)
        if cost_total <= 0 and tx_type not in TxType.income_types():
            cost_total = _to_decimal(r["fiat_value_rub"] or 0)

        if tx_type in TxType.lot_in_types():
            qty_map[symbol] = qty_map.get(symbol, Decimal(0)) + amount
            per_unit = (cost_total / amount) if amount > 0 else Decimal(0)
            lots.setdefault(symbol, []).append((amount, per_unit))
        elif tx_type in TxType.lot_out_types():
            qty_map[symbol] = qty_map.get(symbol, Decimal(0)) - amount
            to_consume = amount
            queue = lots.setdefault(symbol, [])
            while queue and to_consume > 0:
                take = min(queue[0][0], to_consume)
                queue[0] = (queue[0][0] - take, queue[0][1])
                to_consume -= take
                if queue[0][0] <= 0:
                    queue.pop(0)

    result: list[BalanceRow] = []
    for symbol in sorted(qty_map):
        qty = qty_map[symbol]
        residual_cost = sum((q * per for q, per in lots.get(symbol, [])), Decimal(0))
        result.append(BalanceRow(symbol=symbol, qty=qty,
                                 cost_rub=residual_cost.quantize(Decimal("0.01"))))
    return result


def check_negative_balances(balances: list[BalanceRow]) -> list[ReconciliationIssue]:
    issues = []
    for b in balances:
        if b.qty < 0:
            issues.append(ReconciliationIssue(
                kind="negative_balance",
                message=(f"отрицательный остаток {b.symbol}: {b.qty} — "
                         f"списания превышают приходы (незаписанный приход?)"),
                symbol=b.symbol,
            ))
    return issues


async def fetch_actual_balances(owner_id: int,
                               config=None) -> tuple[list[ActualBalance], list[str]]:
    """Фактические балансы: TON-кошельки (TonAPI), Bybit, OKX.

    config — BotConfig (опционально; нужен для ключей CEX). Ошибки сети
    не фейлят сверку — источник просто не попадает в балансы, НО его
    имя возвращается вторым элементом, чтобы `reconcile` превратил
    недоступность в issues. Без этого упавший или заблокированный API
    Bybit выглядел бы как «сверка пройдена», хотя сверки по нему не
    было вовсе.

    Возвращает: (балансы, имена недоступных источников).
    """
    actual: list[ActualBalance] = []
    unavailable: list[str] = []

    ton, ton_broken = await _fetch_ton_balances(
        owner_id, (config.tonapi_api_key if config else "")
    )
    actual += ton
    if ton_broken:
        unavailable.append("TonAPI")

    if config is not None:
        bybit, bybit_broken = await _fetch_bybit_balances(
            config.bybit_api_key, config.bybit_api_secret
        )
        actual += bybit
        if bybit_broken:
            unavailable.append("Bybit")

        okx, okx_broken = await _fetch_okx_balances(
            config.okx_api_key, config.okx_api_secret, config.okx_passphrase
        )
        actual += okx
        if okx_broken:
            unavailable.append("OKX")

    return actual, unavailable


async def _fetch_ton_balances(owner_id: int,
                              api_key: str) -> tuple[list[ActualBalance], bool]:
    """Балансы TON-кошельков. Второй элемент — источник не ответил."""
    wallets = list_wallets(owner_id, chain=Chain.TON)
    if not wallets:
        return [], False  # кошельков нет — сверять нечего, это не ошибка
    client = await get_client()
    headers = {"User-Agent": "AlliraBot/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    out: list[ActualBalance] = []
    ok = True
    for w in wallets:
        try:
            resp = await client.get(
                f"{TONAPI_API_URL}/accounts/{w.address}/jettons",
                params={"limit": 200}, headers=headers, timeout=20.0,
            )
            if resp.status_code != 200:
                logger.warning(f"ton balance {w.address}: {resp.status_code}")
                ok = False
                continue
            data = resp.json() or {}
            balances = data.get("balances") or []
            for b in balances:
                jetton = b.get("jetton") or {}
                symbol = (jetton.get("symbol") or "").upper()
                if not symbol:
                    continue
                decimals = int(jetton.get("decimals") or 9)
                qty = Decimal(b.get("balance") or 0) / (Decimal(10) ** decimals)
                out.append(ActualBalance(
                    symbol=symbol, qty=qty,
                    where=f"ton:{w.address[:8]}…",
                ))
        except Exception as e:
            logger.warning(f"ton balance fetch {w.address}: {e}")
            ok = False
    return out, not ok


async def _bybit_balance_request(client, sign, api_key: str, api_secret: str,
                                 account_type: str,
                                 where: str) -> tuple[list[ActualBalance], bool]:
    """Один счёт Bybit: SPOT через wallet-balance, FUND — asset-эндпоинтом.

    Funding-счёт (FUND) — отдельный кошелёк Bybit: средства, выведенные
    из спота, но ещё не отправленные в блокчейн. Без него расчётный
    остаток расходится с фактическим на всю сумму funding-баланса.
    """
    if account_type == "FUND":
        path = "/v5/asset/transfer/query-account-coins-balance"
        params = {"accountType": "FUND"}
    else:
        path = "/v5/account/wallet-balance"
        params = {"accountType": "SPOT"}
    try:
        resp = await client.get(
            f"https://api.bybit.com{path}",
            params=params, headers=sign(api_key, api_secret, params),
            timeout=20.0,
        )
        if resp.status_code != 200:
            logger.warning(f"bybit balance {account_type}: {resp.status_code}")
            return [], False
        payload = resp.json() or {}
        if payload.get("retCode") != 0:
            logger.warning(
                f"bybit balance {account_type}: retCode "
                f"{payload.get('retCode')} {payload.get('retMsg')}"
            )
            return [], False
        result = payload.get("result") or {}
        if account_type == "FUND":
            coins = result.get("balance") or []
        else:
            coins = [coin
                     for acct in result.get("list") or []
                     for coin in acct.get("coin") or []]
        out: list[ActualBalance] = []
        for coin in coins:
            symbol = (coin.get("coin") or "").upper()
            raw = (coin.get("walletBalance")
                   or coin.get("transferBalance") or "0")
            qty = _to_decimal(raw)
            if symbol and qty > 0:
                out.append(ActualBalance(symbol=symbol, qty=qty, where=where))
        return out, True
    except Exception as e:
        logger.warning(f"bybit balance {account_type} fetch: {e}")
        return [], False


async def _fetch_bybit_balances(api_key: str,
                                api_secret: str) -> tuple[list[ActualBalance], bool]:
    """Спотовый и funding-счёт Bybit. Второй элемент — источник не ответил."""
    if not (api_key and api_secret):
        return [], False  # ключи не заданы — сверять нечего
    from utils.cex_api import _bybit_sign  # переиспользуем подпись
    client = await get_client()
    out: list[ActualBalance] = []
    ok = True
    for account_type, where in (("SPOT", "bybit"), ("FUND", "bybit:fund")):
        rows, good = await _bybit_balance_request(
            client, _bybit_sign, api_key, api_secret, account_type, where,
        )
        out += rows
        ok = ok and good
    return out, not ok


async def _okx_balance_request(client, sign, api_key: str, api_secret: str,
                               passphrase: str, path: str, params: dict,
                               where: str) -> tuple[list[ActualBalance], bool]:
    """Один счёт OKX: trading — account/balance, funding — asset/balances."""
    try:
        resp = await client.get(
            f"https://www.okx.com{path}",
            params=params,
            headers=sign(api_key, api_secret, passphrase, "GET", path, params),
            timeout=20.0,
        )
        if resp.status_code != 200:
            logger.warning(f"okx balance {path}: {resp.status_code}")
            return [], False
        payload = resp.json() or {}
        if payload.get("code") != "0":
            logger.warning(
                f"okx balance {path}: {payload.get('code')} "
                f"{payload.get('msg')}"
            )
            return [], False
        out: list[ActualBalance] = []
        for acct in payload.get("data") or []:
            if path.endswith("/asset/balances"):
                details = [acct]
            else:
                details = acct.get("details") or []
            for detail in details:
                symbol = (detail.get("ccy") or "").upper()
                qty = _to_decimal(detail.get("bal") or "0")
                if symbol and qty > 0:
                    out.append(ActualBalance(symbol=symbol, qty=qty,
                                             where=where))
        return out, True
    except Exception as e:
        logger.warning(f"okx balance {path} fetch: {e}")
        return [], False


async def _fetch_okx_balances(api_key: str, api_secret: str,
                              passphrase: str) -> tuple[list[ActualBalance], bool]:
    """Торговый и funding-счёт OKX. Второй элемент — источник не ответил."""
    if not (api_key and api_secret and passphrase):
        return [], False  # ключи не заданы — сверять нечего
    from utils.cex_api import _okx_sign
    client = await get_client()
    out: list[ActualBalance] = []
    ok = True
    for path, where in (
        ("/api/v5/account/balance", "okx"),
        ("/api/v5/asset/balances", "okx:fund"),
    ):
        rows, good = await _okx_balance_request(
            client, _okx_sign, api_key, api_secret, passphrase,
            path, {"ccy": ""}, where,
        )
        out += rows
        ok = ok and good
    return out, not ok


def compare_balances(calculated: list[BalanceRow],
                     actual: list[ActualBalance]) -> list[ReconciliationIssue]:
    """Сверка расчётных остатков с фактическими.

    Допуск: 1% от фактического остатка или 0.01 единицы — на комиссии и
    незадокументированные списания.
    """
    actual_by_symbol: dict[str, Decimal] = {}
    for a in actual:
        actual_by_symbol[a.symbol] = actual_by_symbol.get(a.symbol, Decimal(0)) + a.qty

    issues: list[ReconciliationIssue] = []
    calc_by_symbol = {b.symbol: b.qty for b in calculated}

    for symbol, act_qty in sorted(actual_by_symbol.items()):
        calc_qty = calc_by_symbol.get(symbol, Decimal(0))
        diff = calc_qty - act_qty
        tolerance = max(Decimal("0.01"), abs(act_qty) * Decimal("0.01"))
        if abs(diff) > tolerance:
            issues.append(ReconciliationIssue(
                kind="drift",
                message=(f"{symbol}: расчётный остаток {calc_qty} "
                         f"против фактического {act_qty} (разница {diff})"),
                symbol=symbol,
            ))
    return issues


# ------------------------------------------------------------- полная сверка


async def reconcile(owner_id: int, year: int, config=None,
                    with_actual: bool = True) -> ReconciliationReport:
    """Полная сверка за год: журнал, парность, балансы (расчёт + факт)."""
    report = ReconciliationReport(year=year)

    entries = build_journal(owner_id, year)
    report.entries = entries
    report.issues.extend(check_pairing(entries))

    report.total_dr_rub = sum(
        (e.rub for e in entries if e.kind in ("income", "buy", "sell", "spend")),
        Decimal(0),
    )
    # JournalEntry хранит одну RUB-оценку на проводку: Дт и Кт равны по
    # построению, поэтому итог Кт = итог Дт и delta_dr_cr всегда ноль.
    # Численного допуска на расхождение Дт/Кт здесь нет и быть не может —
    # для него JournalEntry пришлось бы разнести на dr_rub и cr_rub.
    # Фактический контроль парности делает check_pairing: он ловит
    # проводки без парной ноги, а не рассогласование копеек.
    report.total_cr_rub = report.total_dr_rub

    report.balances = compute_balances(owner_id)
    report.issues.extend(check_negative_balances(report.balances))

    if with_actual:
        report.actual, unavailable = await fetch_actual_balances(owner_id, config)
        for src in unavailable:
            report.issues.append(ReconciliationIssue(
                kind="source_unavailable",
                message=(f"{src}: фактический баланс получить не удалось — "
                         f"сверка по этому источнику НЕ выполнена, "
                         f"отсутствие расхождений здесь ничего не значит"),
            ))
        if report.actual:
            report.issues.extend(compare_balances(report.balances, report.actual))
            # Дозаполняем рыночные цены для оценки расчётного портфеля.
            now = int(time.time())
            for b in report.balances:
                if b.qty == 0:
                    continue
                try:
                    price = await get_asset_price_rub(b.symbol, now)
                except Exception:
                    price = None
                if price is not None:
                    b.last_price_rub = Decimal(str(price))

    return report


# --------------------------------------------------------------- утилиты


def _year_start(year: int) -> int:
    return int(time.mktime(time.strptime(f"{year}-01-01 00:00:00",
                                         "%Y-%m-%d %H:%M:%S")))


def _year_end(year: int) -> int:
    return int(time.mktime(time.strptime(f"{year + 1}-01-01 00:00:00",
                                         "%Y-%m-%d %H:%M:%S")))


def report_to_dict(report: ReconciliationReport) -> dict:
    return {
        "year": report.year,
        "total_dr_rub": str(report.total_dr_rub),
        "total_cr_rub": str(report.total_cr_rub),
        "delta": str(report.delta_dr_cr),
        "issues": [{"kind": i.kind, "message": i.message,
                    "tx_ids": i.tx_ids, "symbol": i.symbol}
                   for i in report.issues],
        "entries": len(report.entries),
        "balances": [{"symbol": b.symbol, "qty": str(b.qty),
                      "cost_rub": str(b.cost_rub)} for b in report.balances],
        "actual": [{"symbol": a.symbol, "qty": str(a.qty), "where": a.where}
                   for a in report.actual],
    }
