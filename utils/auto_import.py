"""Автоимпорт: адреса кошельков + выписки бирж → операции для налоговой базы.

Идея. Пользователю не нужно вбивать каждую операцию руками. Он отдаёт
адреса своих кошельков и (по возможности) выписки бирж о покупке и продаже,
а бот сам:

  1. сканирует историю переводов по каждому адресу (utils/chain_scan.py);
  2. сопоставляет переводы с записями из выписок (вывод с биржи ↔ зачисление
     на кошелёк);
  3. классифицирует каждое движение и записывает в бухгалтерию только то,
     что действительно влияет на налоговую базу.

ГЛАВНОЕ ПРАВИЛО УЧЁТА. Актив должен быть учтён РОВНО ОДИН раз — там, где он
появился. Перемещение актива (биржа → кошелёк, кошелёк → биржа, свой кошелёк →
свой кошелёк) не создаёт ни дохода, ни расхода, ни нового лота: актив уже
учтён лотом в месте покупки. Поэтому такие проводки помечаются is_internal
и не трогают ни FIFO-лоты, ни расчётный остаток.

Что из этого следует для налоговой базы:

  * входящий перевод с неизвестного адреса — это ДОХОД (аирдроп) по ст. 214
    НК, если он не сопоставлен с выводом с биржи и не пришёл со своего
    кошелька. Пользователь увидит такую строку в списке на проверку;
  * исходящий перевод на неизвестный адрес — РЕАЛИЗАЦИЯ (расход по НК), если
    это не стейкинг и не перевод своему кошельку;
  * обмен (в одном событии и уходит актив, и приходит другой) — реализация
    уходящего актива и ПРИОБРЕТЕНИЕ пришедшего, с себестоимостью равной
    отданной сумме. Раньше приход в обмене считался аирдропом, то есть один
    обмен облагался дважды: и как реализация, и как доход;
  * перевод «свой кошелёк ↔ свой кошелёк» и «вывод с биржи на свой кошелёк»
    не трогают базу вовсе.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

from utils.chain_scan import ChainMove, scan_wallet
from utils.crypto_db import (Chain, TxType, add_transaction, ensure_asset,
                              find_duplicate, list_wallets)
from utils.database import get_db
from utils.price_oracle import get_asset_price_rub
from utils.statement_import import parse_statement

logger = logging.getLogger(__name__)

# Источник ончейн-операций в crypto_transactions.
CHAIN_SOURCE = "chain-sync"

# Окно сопоставления перевода с записью из выписки: сеть может задержать
# зачисление на сутки, а выписка биржи иногда ставит дату операции, а не
# дату фактического вывода.
MATCH_WINDOW_S = 3 * 86400

# Допуск по сумме при сопоставлении: комиссия биржи идёт из суммы вывода.
AMOUNT_TOLERANCE = Decimal("0.01")


# ------------------------------------------------------------- результаты


@dataclass
class Review:
    """Строка, которую стоит посмотреть пользователю."""

    kind: str            # airdrop / outflow / no_quote / no_cost / exchange_hint
    message: str
    asset: str = ""
    ts: int = 0
    amount: str = ""
    tx_id: int = 0


@dataclass
class Classified:
    """Движение после классификации."""

    move: ChainMove
    kind: str            # internal / income / disposal / swap_out / swap_in
    tx_type: str = ""
    rub: Decimal = Decimal(0)
    matched_tx: int = 0
    note: str = ""


@dataclass
class StatementResult:
    rows: int = 0
    saved: int = 0
    duplicates: int = 0
    rematched: int = 0
    errors: list[str] = field(default_factory=list)
    review: list[Review] = field(default_factory=list)


@dataclass
class AutoImportResult:
    moves: int = 0
    saved: int = 0
    duplicates: int = 0
    internal: int = 0
    review: list[Review] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ------------------------------------------------------------- выписки


async def import_statement(
    owner_id: int,
    content: bytes,
    filename: str = "",
    exchange: str = "",
) -> StatementResult:
    """Импортирует выписку биржи (CSV/XLSX) в бухгалтерию.

    Покупки и продажи идут тем же путём, что и API-синк: quote-валюта
    переводится в рубли по курсу на дату сделки, парная проводка создаётся
    автоматически. Вводы и выводы — перемещения: они не меняют налоговую
    базу, но нужны, чтобы сопоставить их с ончейн-переводами и не посчитать
    зачисление как доход.
    """
    from tasks.cex_sync import persist_cex_rows

    result = StatementResult()
    rows, errors = parse_statement(content, filename, exchange)
    result.errors.extend(errors)
    result.rows = len(rows)
    if not rows:
        return result

    # Себестоимость внесённого актива: у ввода извне (с карты, с банка) она
    # равна рыночной стоимости на дату ввода. Без неё актив числился бы с
    # нулевой себестоимостью, и вся последующая прибыль попала бы в базу.
    for row in rows:
        if row.tx_type != TxType.TRANSFER_IN:
            continue
        try:
            price = await get_asset_price_rub(row.asset, row.ts)
        except Exception:
            price = None
        if price:
            try:
                rub = Decimal(row.amount) * Decimal(str(price))
                row.cost_basis_rub = f"{rub:.2f}"
                row.note += " (себестоимость — по рынку на дату ввода)"
            except Exception:
                pass
        else:
            result.review.append(Review(
                kind="no_cost",
                message=(f"ввод {row.amount} {row.asset}: нет котировки на дату "
                         f"ввода — себестоимость 0 ₽, укажи её вручную"),
                asset=row.asset, ts=row.ts, amount=row.amount,
            ))

    saved, dups = await persist_cex_rows(owner_id, rows, exchange or "csv")
    result.saved = saved
    result.duplicates = dups

    # Появились записи о вводах и выводах — пересопоставляем с ними ранее
    # записанные ончейн-переводы: вывод с биржи перестаёт быть «доходом».
    try:
        result.rematched = rematch_transfers(owner_id)
    except Exception as e:
        logger.warning(f"auto_import: пересопоставление не удалось: {e}")
    return result


# ------------------------------------------------------------- ончейн


def _load_transfer_rows(owner_id: int) -> list[dict]:
    """Перемещения из бухгалтерии — по ним сопоставляем ончейн-переводы."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT t.id, t.ts, t.tx_type, t.amount_raw, t.is_internal, "
            "       a.symbol, a.decimals "
            "FROM crypto_transactions t "
            "JOIN crypto_assets a ON a.id = t.asset_id "
            "WHERE t.owner_id = ? AND t.tx_type IN (?, ?)",
            (owner_id, TxType.TRANSFER_IN, TxType.TRANSFER_OUT),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        decimals = int(r["decimals"] or 9)
        raw = Decimal(r["amount_raw"] or 0)
        out.append({
            "id": int(r["id"]),
            "ts": int(r["ts"]),
            "tx_type": r["tx_type"],
            "symbol": r["symbol"],
            "amount": raw / (Decimal(10) ** decimals),
            "is_internal": bool(r["is_internal"]) if "is_internal" in r.keys()
            else False,
        })
    return out


def _load_chain_rows(owner_id: int) -> list[dict]:
    """Ончейн-проводки, записанные сканером."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT t.id, t.ts, t.tx_type, t.direction, t.amount_raw, "
            "       t.is_internal, a.symbol, a.decimals "
            "FROM crypto_transactions t "
            "JOIN crypto_assets a ON a.id = t.asset_id "
            "WHERE t.owner_id = ? AND t.source = ?",
            (owner_id, CHAIN_SOURCE),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        decimals = int(r["decimals"] or 9)
        raw = Decimal(r["amount_raw"] or 0)
        out.append({
            "id": int(r["id"]),
            "ts": int(r["ts"]),
            "tx_type": r["tx_type"],
            "direction": r["direction"],
            "symbol": r["symbol"],
            "amount": raw / (Decimal(10) ** decimals),
            "is_internal": bool(r["is_internal"]) if "is_internal" in r.keys()
            else False,
        })
    return out


def rematch_transfers(owner_id: int) -> int:
    """Пересопоставляет записанные ончейн-переводы с записями из выписок.

    Нужна после импорта выписки: перевод, записанный раньше как аирдроп
    (доход), после появления выписки оказывается выводом с биржи — то есть
    перемещением, которое в базу не попадает. Здесь такие проводки
    переклассифицируются, и уже записанный «ложный доход» исчезает.

    Возвращает количество переклассифицированных проводок.
    """
    chain_rows = _load_chain_rows(owner_id)
    transfer_rows = _load_transfer_rows(owner_id)
    changed = 0

    for row in chain_rows:
        if row["is_internal"]:
            continue
        want = (TxType.TRANSFER_OUT if row["direction"] == "in"
                else TxType.TRANSFER_IN)
        match = None
        for tr in transfer_rows:
            if tr["tx_type"] != want or tr["is_internal"]:
                continue
            if tr["symbol"] != row["symbol"]:
                continue
            if abs(tr["ts"] - row["ts"]) > MATCH_WINDOW_S:
                continue
            if not _same_amount(tr["amount"], row["amount"]):
                continue
            match = tr
            break
        if match is None:
            continue

        new_type = (TxType.TRANSFER_IN if row["direction"] == "in"
                    else TxType.TRANSFER_OUT)
        with get_db() as conn:
            conn.execute(
                "UPDATE crypto_transactions "
                "SET tx_type = ?, is_internal = 1, fiat_value_rub = NULL "
                "WHERE id = ? AND owner_id = ?",
                (new_type, row["id"], owner_id),
            )
            if match["tx_type"] == TxType.TRANSFER_IN:
                # Ввод на биржу открывает лот, а актив уже учтён в месте
                # покупки — помечаем и его внутренним.
                conn.execute(
                    "UPDATE crypto_transactions SET is_internal = 1 "
                    "WHERE id = ? AND owner_id = ?",
                    (match["id"], owner_id),
                )
        changed += 1

    if changed:
        logger.info(f"auto_import: пересопоставлено {changed} переводов")
    return changed


def _same_amount(a: Decimal, b: Decimal) -> bool:
    """Суммы совпадают с допуском 1% (комиссия биржи может уменьшать вывод)."""
    if a <= 0 or b <= 0:
        return False
    diff = abs(a - b)
    return diff <= max(a, b) * AMOUNT_TOLERANCE


def _match_transfer(move: ChainMove, rows: Iterable[dict]) -> Optional[dict]:
    """Ищет в бухгалтерии перемещение, соответствующее ончейн-переводу.

    Входящий перевод на кошелёк ↔ вывод с биржи (transfer_out).
    Исходящий перевод с кошелька ↔ ввод на биржу (transfer_in).
    """
    want = TxType.TRANSFER_OUT if move.direction == "in" else TxType.TRANSFER_IN
    best: Optional[dict] = None
    for row in rows:
        if row["tx_type"] != want or row["is_internal"]:
            continue
        if row["symbol"] != move.asset:
            continue
        if abs(row["ts"] - move.ts) > MATCH_WINDOW_S:
            continue
        try:
            amount = Decimal(move.amount)
        except Exception:
            continue
        if not _same_amount(row["amount"], amount):
            continue
        if best is None or abs(row["ts"] - move.ts) < abs(best["ts"] - move.ts):
            best = row
    return best


def _group_moves(moves: list[ChainMove]) -> dict[str, list[ChainMove]]:
    groups: dict[str, list[ChainMove]] = {}
    for m in moves:
        groups.setdefault(f"{m.chain}:{m.group}", []).append(m)
    return groups


def _is_roundtrip(group: list[ChainMove]) -> bool:
    """Один и тот же актив ушёл и вернулся — это не обмен (стейкинг, депозит)."""
    ins = [m for m in group if m.direction == "in"]
    outs = [m for m in group if m.direction == "out"]
    if not ins or not outs:
        return False
    if len(ins) == 1 and len(outs) == 1:
        try:
            return (ins[0].asset == outs[0].asset
                    and _same_amount(Decimal(ins[0].amount),
                                     Decimal(outs[0].amount)))
        except Exception:
            return False
    return False


async def classify_moves(owner_id: int, moves: list[ChainMove]) -> list[Classified]:
    """Классифицирует ончейн-движения: доход, реализация, обмен, перемещение."""
    my_addresses = {w.address.lower() for w in list_wallets(owner_id)}
    transfer_rows = _load_transfer_rows(owner_id)
    groups = _group_moves(moves)

    result: list[Classified] = []
    for move in moves:
        counterparty = (move.counterparty or "").lower()

        # 1. Перевод между своими кошельками — чистое перемещение.
        if counterparty and counterparty in my_addresses:
            result.append(Classified(
                move=move, kind="internal",
                tx_type=(TxType.TRANSFER_IN if move.direction == "in"
                         else TxType.TRANSFER_OUT),
                note="перевод между своими кошельками",
            ))
            continue

        # 2. Сопоставление с записями из выписок бирж.
        matched = _match_transfer(move, transfer_rows)
        if matched is not None:
            note = (f"перемещение: сопоставлено с выпиской "
                    f"(проводка #{matched['id']})")
            result.append(Classified(
                move=move, kind="internal",
                tx_type=(TxType.TRANSFER_IN if move.direction == "in"
                         else TxType.TRANSFER_OUT),
                matched_tx=matched["id"], note=note,
            ))
            continue

        # 3. Обмен: в одной группе и уход, и приход.
        group = groups.get(f"{move.chain}:{move.group}", [move])
        if _is_roundtrip(group):
            result.append(Classified(
                move=move, kind="internal",
                tx_type=(TxType.TRANSFER_IN if move.direction == "in"
                         else TxType.TRANSFER_OUT),
                note="актив ушёл и вернулся (стейкинг/депозит) — не обмен",
            ))
            continue

        has_in = any(m.direction == "in" for m in group)
        has_out = any(m.direction == "out" for m in group)
        if has_in and has_out:
            if move.direction == "out":
                result.append(Classified(
                    move=move, kind="swap_out", tx_type=TxType.SWAP_OUT,
                    note="обмен: актив отдан",
                ))
            else:
                result.append(Classified(
                    move=move, kind="swap_in", tx_type=TxType.TRADE_IN,
                    note="обмен: актив получен (себестоимость = отданная сумма)",
                ))
            continue

        # 4. Одиночное движение.
        if move.direction == "in":
            result.append(Classified(
                move=move, kind="income", tx_type=TxType.AIRDROP,
                note="входящий перевод с неизвестного адреса — доход (ст. 214 НК)",
            ))
        else:
            result.append(Classified(
                move=move, kind="disposal", tx_type=TxType.SWAP_OUT,
                note="исходящий перевод на неизвестный адрес — реализация",
            ))
    return result


async def persist_moves(owner_id: int, classified: list[Classified],
                        wallet_id: Optional[int] = None,
                        ) -> tuple[int, int, list[Review]]:
    """Записывает классифицированные движения в бухгалтерию.

    Возвращает (сохранено, дублей, список на проверку).
    """
    saved = 0
    dups = 0
    review: list[Review] = []

    # Оценка в рублях нужна и обмену (себестоимость полученного актива), и
    # доходу, и реализации. Считаем один раз на актив+дата.
    price_cache: dict[tuple[str, int], Optional[Decimal]] = {}

    async def rub_price(asset: str, ts: int) -> Optional[Decimal]:
        key = (asset, ts)
        if key not in price_cache:
            try:
                price = await get_asset_price_rub(asset, ts)
            except Exception:
                price = None
            price_cache[key] = (Decimal(str(price))
                                if price is not None else None)
        return price_cache[key]

    # Сначала оцениваем отданные в обмене активы: их стоимость становится
    # себестоимостью полученных, а порядок движений в списке произвольный.
    for item in classified:
        if item.kind != "swap_out":
            continue
        price = await rub_price(item.move.asset, item.move.ts)
        if price is None:
            continue
        try:
            item.rub = (Decimal(item.move.amount) * price).quantize(
                Decimal("0.01"))
        except Exception:
            item.rub = Decimal(0)

    for item in classified:
        move = item.move
        # Дубли: та же операция уже записана (повторный скан).
        ext_id = move.key
        if find_duplicate(owner_id, CHAIN_SOURCE, ext_id) is not None:
            dups += 1
            continue

        asset = ensure_asset(
            symbol=move.asset,
            chain=move.chain,
            decimals=move.decimals,
            contract=move.meta.get("jetton") or move.meta.get("contract") or "",
            kind="jetton" if move.chain == Chain.TON else "token",
        )
        # Decimals у уже заведённого актива могут отличаться (актив с тем же
        # тикером на другой сети). Пересчитываем по decimals каталога, иначе
        # количество уехало бы на порядки.
        amount = move.amount
        if move.amount_raw and asset.decimals != move.decimals:
            amount = f"{Decimal(move.amount_raw) / (Decimal(10) ** asset.decimals):f}"

        price = await rub_price(asset.symbol, move.ts)
        rub = Decimal(0)
        if price is not None:
            try:
                rub = (Decimal(amount) * price).quantize(Decimal("0.01"))
            except Exception:
                rub = Decimal(0)

        tx_type = item.tx_type
        fiat = None
        cost = None
        is_internal = item.kind == "internal"
        item_review: list[Review] = []

        if item.kind == "income":
            if price is None:
                item_review.append(Review(
                    kind="no_quote",
                    message=(f"{move.asset}: нет котировки на дату перевода — "
                             f"доход не посчитан"),
                    asset=move.asset, ts=move.ts, amount=amount,
                ))
            else:
                fiat = f"{rub:.2f}"
            item_review.append(Review(
                kind="airdrop",
                message=(f"{move.amount} {move.asset} — записано как доход "
                         f"(аирдроп). Если это вывод с биржи — пришли выписку, "
                         f"операция станет перемещением"),
                asset=move.asset, ts=move.ts, amount=amount,
            ))
        elif item.kind in ("disposal", "swap_out"):
            if price is None:
                item_review.append(Review(
                    kind="no_quote",
                    message=(f"{move.asset}: нет котировки на дату перевода — "
                             f"выручка не посчитана"),
                    asset=move.asset, ts=move.ts, amount=amount,
                ))
            else:
                fiat = f"{rub:.2f}"
            item_review.append(Review(
                kind="outflow",
                message=(f"{move.amount} {move.asset} — записано как реализация. "
                         f"Если это стейкинг или перевод — поправь тип"),
                asset=move.asset, ts=move.ts, amount=amount,
            ))
        elif item.kind == "swap_in":
            # Себестоимость пришедшего актива = тому, что отдано.
            cost = _swap_in_cost(classified, move)
            if not cost:
                item_review.append(Review(
                    kind="no_cost",
                    message=(f"{move.asset}: не удалось оценить полученный в "
                             f"обмене актив — себестоимость 0 ₽"),
                    asset=move.asset, ts=move.ts, amount=amount,
                ))

        note = item.note
        if move.counterparty:
            note += f"; контрагент {move.counterparty}"
        if move.tx_hash:
            note += f"; tx {move.tx_hash[:24]}"

        try:
            tx = add_transaction(
                owner_id=owner_id,
                ts=move.ts,
                wallet_id=wallet_id,
                asset_symbol=asset.symbol,
                tx_type=tx_type,
                amount=amount,
                fiat_value_rub=fiat,
                cost_basis_rub=cost,
                note=note,
                source=CHAIN_SOURCE,
                external_id=ext_id,
                is_internal=is_internal,
            )
        except Exception as e:
            logger.warning(f"auto_import: не записано {move.key}: {e}")
            continue

        if item.kind == "internal" and item.matched_tx:
            # Ввод на биржу, найденный по выписке: он открывает лот, а актив
            # уже учтён лотом в месте покупки. Помечаем проводку внутренней.
            _mark_internal(owner_id, item.matched_tx)

        saved += 1
        for r in item_review:
            r.tx_id = tx.id
        review.extend(item_review)

    return saved, dups, review


def _swap_in_cost(classified: list[Classified], move: ChainMove) -> Optional[str]:
    """Себестоимость актива, полученного в обмене.

    Равна рублёвой стоимости того, что было отдано в той же группе.
    """
    for item in classified:
        if item.kind != "swap_out":
            continue
        if item.move.group != move.group or item.move.chain != move.chain:
            continue
        price = getattr(item, "rub", None)
        if price:
            return f"{price:.2f}"
    return None


def _mark_internal(owner_id: int, tx_id: int) -> None:
    """Помечает проводку перемещением внутри периметра."""
    with get_db() as conn:
        conn.execute(
            "UPDATE crypto_transactions SET is_internal = 1 "
            "WHERE id = ? AND owner_id = ?",
            (tx_id, owner_id),
        )


def _delete_superseded_legacy(owner_id: int, moves: list[ChainMove]) -> int:
    """Удаляет записи прежнего Jetton-сканера, заменённые новым.

    Старый сканер писал source='jetton-sync' с ключом
    «jetton-sync:{event_id}:{symbol}:{direction}». Новый пишет те же события
    с другой классификацией (например, перевод с биржи больше не доход), и
    без удаления старых строк актив учитывался бы дважды.
    """
    removed = 0
    with get_db() as conn:
        for move in moves:
            if move.chain != Chain.TON:
                continue
            legacy = f"jetton-sync:{move.tx_hash}:{move.asset}:{move.direction}"
            cur = conn.execute(
                "DELETE FROM crypto_transactions "
                "WHERE owner_id = ? AND source = 'jetton-sync' "
                "AND external_id = ?",
                (owner_id, legacy),
            )
            removed += cur.rowcount or 0
    return removed


# ------------------------------------------------------------- общий вход


async def run(owner_id: int, config=None,
              wallets=None) -> AutoImportResult:
    """Сканирует кошельки и записывает операции в бухгалтерию.

    Для каждого кошелька ведётся водяной знак (bot_stats): фоновый прогон
    берёт только новые переводы, а ручной запуск — тоже, чтобы не грузить
    сеть всей историей. Полную историю бот забирает при первом прогоне.
    """
    from utils.config import BotConfig

    cfg = config if config is not None else BotConfig()
    result = AutoImportResult()

    owner_wallets = wallets if wallets is not None else list_wallets(owner_id)
    if not owner_wallets:
        result.errors.append(
            "не задано ни одного кошелька — /wallet_add <сеть> <адрес>")
        return result

    tonapi_key = getattr(cfg, "tonapi_api_key", "") or ""
    etherscan_key = getattr(cfg, "etherscan_api_key", "") or ""
    chain_ids = _evm_chain_ids(cfg)

    for wallet in owner_wallets:
        since_ts = _get_watermark(wallet.id)
        try:
            moves = await scan_wallet(wallet, tonapi_key, etherscan_key,
                                      chain_ids, since_ts=since_ts)
        except Exception as e:
            logger.warning(f"auto_import: скан {wallet.address} не удался: {e}")
            result.errors.append(
                f"скан {wallet.address[:10]}… не удался: {e}")
            continue

        result.moves += len(moves)
        if not moves:
            continue

        classified = await classify_moves(owner_id, moves)
        saved, dups, review = await persist_moves(owner_id, classified,
                                                  wallet_id=wallet.id)
        result.saved += saved
        result.duplicates += dups
        result.internal += sum(1 for c in classified if c.kind == "internal")
        result.review.extend(review)
        result.errors.extend(_delete_superseded_legacy(owner_id, moves))

        newest = max(m.ts for m in moves)
        if newest:
            _set_watermark(wallet.id, newest)
        await asyncio.sleep(0.4)

    return result


def _get_watermark(wallet_id: int) -> int:
    """Водяной знак сканирования: с какого момента брать новые переводы."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM bot_stats WHERE key = ?",
            (f"chain_sync_ts:{wallet_id}",),
        ).fetchone()
    return int(row["value"]) if row else 0


def _set_watermark(wallet_id: int, ts: int) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO bot_stats (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = MAX(CAST(value AS INTEGER), ?),
                updated_at = CURRENT_TIMESTAMP
            """,
            (f"chain_sync_ts:{wallet_id}", str(ts), ts),
        )


def _evm_chain_ids(cfg) -> list[int]:
    """Сети EVM, которые сканируем (из конфига)."""
    raw = getattr(cfg, "evm_chain_ids", "") or ""
    ids: list[int] = []
    for part in str(raw).replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in ids:
            ids.append(int(part))
    return ids or [1]


def pending_reviews(owner_id: int) -> list[Review]:
    """Операции, записанные сканером и требующие решения пользователя.

    Это те переводы, которые бот не смог однозначно назвать: неизвестный
    контрагент (может быть аирдропом, а может — выводом с биржи) и
    исходящие переводы (может быть реализацией, а может — стейкингом).
    Пока они не помечены как перемещения, они влияют на базу.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT t.id, t.ts, t.tx_type, t.amount_raw, t.note, "
            "       t.fiat_value_rub, a.symbol, a.decimals "
            "FROM crypto_transactions t "
            "JOIN crypto_assets a ON a.id = t.asset_id "
            "WHERE t.owner_id = ? AND t.source = ? "
            "AND (t.is_internal IS NULL OR t.is_internal = 0) "
            "ORDER BY t.ts DESC LIMIT 50",
            (owner_id, CHAIN_SOURCE),
        ).fetchall()

    out: list[Review] = []
    for r in rows:
        decimals = int(r["decimals"] or 9)
        amount = (Decimal(r["amount_raw"] or 0)
                  / (Decimal(10) ** decimals)).normalize()
        symbol = r["symbol"]
        if r["tx_type"] == TxType.AIRDROP:
            kind = "airdrop"
            message = (f"{amount} {symbol} — записано как доход (аирдроп). "
                       f"Если это вывод с биржи — пришли выписку")
        elif r["tx_type"] in TxType.disposal_types():
            kind = "outflow"
            message = (f"{amount} {symbol} — записано как реализация. "
                       f"Если это стейкинг или перевод — поправь тип")
        else:
            kind = "other"
            message = f"{amount} {symbol} — проверь тип операции"
        out.append(Review(
            kind=kind, message=message, asset=symbol, ts=int(r["ts"]),
            amount=f"{amount:f}", tx_id=int(r["id"]),
        ))
    return out
