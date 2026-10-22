"""Скан ончейн-движений по адресам: TON и EVM.

Зачем. Чтобы пользователь отдавал только адреса своих кошельков, а бот сам
доставал историю переводов и превращал её в операции для налоговой базы.

Что умеет:
  * TON — TonAPI `/v2/accounts/{addr}/events`: нативные переводы TON и
    переводы Jetton'ов в одном потоке, с контрагентом и хэшем.
  * EVM — Etherscan V2 (`api.etherscan.io/v2/api`): один ключ на много
    сетей, `tokentx` для ERC-20 и `txlist` для нативной монеты.

Что НЕ делает:
  * не решает, доход это или перевод, и не пишет в бухгалтерию — это делает
    utils/auto_import.py. Здесь только «что произошло на блокчейне»;
  * не трогает NFT — у них нет котировки, и в базу они бы попали мусором.

Каждый счётчик денег — ChainMove. Ключ дедупликации: хэш транзакции + актив
+ направление (+ log_index для EVM), поэтому повторный скан не создаёт дублей.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from utils.http_client import get_client

logger = logging.getLogger(__name__)

TONAPI_API_URL = "https://tonapi.io/v2"
ETHERSCAN_API_URL = "https://api.etherscan.io/v2/api"

# Сети EVM, которые умеет читать Etherscan V2: chain_id → (символ, decimals).
EVM_CHAINS: dict[int, tuple[str, int]] = {
    1: ("ETH", 18),
    10: ("ETH", 18),
    56: ("BNB", 18),
    137: ("POL", 18),
    8453: ("ETH", 18),
    42161: ("ETH", 18),
    43114: ("AVAX", 18),
}

TON_DECIMALS = 9


@dataclass
class ChainMove:
    """Одно движение активов по сети."""

    ts: int
    chain: str                 # ton / evm
    address: str               # мой адрес
    asset: str                 # символ
    amount: str                # человеческие единицы
    decimals: int
    direction: str             # "in" | "out"
    counterparty: str = ""     # адрес отправителя/получателя
    tx_hash: str = ""          # хэш транзакции (TON: event_id)
    log_index: int = 0         # для EVM — индекс лога в транзакции
    group: str = ""            # событие/транзакция: вход и выход в одной группе
    kind: str = "transfer"     # transfer | swap
    amount_raw: int = 0        # минимальные единицы (для пересчёта decimals)
    meta: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Ключ дедупликации: одна и та же операция — один ключ."""
        return (f"{self.chain}:{self.tx_hash}:{self.asset}:"
                f"{self.direction}:{self.log_index}")


# --------------------------------------------------------------- TON


async def scan_ton(address: str, api_key: str = "",
                   limit: int = 1000, since_ts: int = 0) -> list[ChainMove]:
    """История переводов TON-кошелька: нативный TON + Jetton'ы.

    TonAPI отдаёт события (транзакции) с действиями. Внутри одного события
    могут быть и входящий, и исходящий перевод — это обмен на DEX. Такие
    помечаются kind='swap' и получают общий group, чтобыauto_import понял:
    актив ушёл и актив пришёл, а это не «приход из воздуха».

    since_ts — с какого момента нужны события (0 — вся история). События
    отдаются от новых к старым, поэтому пагинация останавливается, как
    только дошли до нужной даты.
    """
    client = await get_client()
    headers = {"User-Agent": "AlliraBot/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    moves: list[ChainMove] = []
    before_lt: Optional[int] = None
    pages = 0
    while len(moves) < limit:
        params = {"limit": 100}
        if before_lt:
            params["before_lt"] = before_lt
        try:
            resp = await client.get(
                f"{TONAPI_API_URL}/accounts/{address}/events",
                params=params, headers=headers, timeout=25.0,
            )
        except Exception as e:
            logger.warning(f"ton events сеть: {e}")
            break
        if resp.status_code == 429:
            logger.warning("ton events: 429 — пауза")
            await asyncio.sleep(5.0)
            break
        if resp.status_code != 200:
            logger.warning(f"ton events {resp.status_code}")
            break
        try:
            data = resp.json() or {}
        except Exception:
            break

        events = data.get("events") or []
        if not events:
            break
        pages += 1

        oldest = 0
        for ev in events:
            ts = int(ev.get("timestamp") or 0)
            if ts:
                oldest = min(oldest, ts) if oldest else ts
            moves.extend(_ton_event_to_moves(address, ev))
        if since_ts and oldest and oldest < since_ts:
            # Дошли до нужной даты — дальше идти незачем.
            break
        if len(events) < 100:
            break
        before_lt = events[-1].get("lt")
        if not before_lt:
            break
        await asyncio.sleep(0.5)

    logger.info(f"scan_ton {address[:10]}…: {len(moves)} движений, страниц {pages}")
    return moves


def _ton_event_to_moves(address: str, ev: dict) -> list[ChainMove]:
    """Разбор одного события TonAPI в список движений."""
    ts = int(ev.get("timestamp") or 0)
    event_id = str(ev.get("event_id") or "")
    my = (address or "").lower()
    out: list[ChainMove] = []

    ins: list[ChainMove] = []
    outs: list[ChainMove] = []

    for action in ev.get("actions") or []:
        atype = action.get("type") or ""
        if atype == "TonTransfer":
            data = action.get("TonTransfer") or {}
            sender = ((data.get("sender") or {}).get("address") or "").lower()
            recipient = ((data.get("recipient") or {}).get("address") or "").lower()
            amount_raw = data.get("amount")
            if not amount_raw or sender == recipient:
                continue
            move = _ton_move(
                ts=ts, event_id=event_id, address=my,
                asset="TON", decimals=TON_DECIMALS, amount_raw=amount_raw,
                sender=sender, recipient=recipient, my=my,
            )
            if move is None:
                continue
            (ins if recipient == my else outs).append(move)
        elif atype == "JettonTransfer":
            data = action.get("JettonTransfer") or {}
            sender = ((data.get("sender") or {}).get("address") or "").lower()
            recipient = ((data.get("recipient") or {}).get("address") or "").lower()
            amount_raw = data.get("amount")
            jetton = data.get("jetton") or {}
            symbol = (jetton.get("symbol") or "").strip()
            if not amount_raw or not symbol:
                continue
            decimals = int(jetton.get("decimals") or 9)
            move = _ton_move(
                ts=ts, event_id=event_id, address=my,
                asset=symbol.upper(), decimals=decimals, amount_raw=amount_raw,
                sender=sender, recipient=recipient, my=my,
                meta={"jetton": jetton.get("address") or ""},
            )
            if move is None:
                continue
            (ins if recipient == my else outs).append(move)

    is_swap = bool(ins) and bool(outs)
    for move in ins + outs:
        move.group = event_id
        move.kind = "swap" if is_swap else "transfer"
        out.append(move)
    return out


def _ton_move(ts: int, event_id: str, address: str, asset: str, decimals: int,
              amount_raw, sender: str, recipient: str, my: str,
              meta: Optional[dict] = None) -> Optional[ChainMove]:
    """Одна сторона перевода в человеческих единицах."""
    try:
        raw = int(amount_raw)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    if sender == my and recipient == my:
        return None
    human = Decimal(raw) / (Decimal(10) ** decimals)
    direction = "in" if recipient == my else "out"
    counterparty = sender if direction == "in" else recipient
    return ChainMove(
        ts=ts, chain="ton", address=address, asset=asset,
        amount=f"{human:f}", decimals=decimals, direction=direction,
        counterparty=counterparty, tx_hash=event_id, group=event_id,
        amount_raw=raw, meta=meta or {},
    )


# --------------------------------------------------------------- EVM


async def scan_evm(chain_id: int, address: str, api_key: str = "",
                   limit: int = 1000, since_ts: int = 0) -> list[ChainMove]:
    """История ERC-20 и нативной монеты по адресу (Etherscan V2).

    chain_id: 1 — Ethereum, 56 — BSC, 137 — Polygon и т.д.
    """
    if not api_key:
        logger.info("scan_evm: нет ключа Etherscan — пропуск")
        return []
    native_symbol, native_decimals = EVM_CHAINS.get(chain_id, ("ETH", 18))
    client = await get_client()
    addr = (address or "").lower()

    moves: list[ChainMove] = []
    moves.extend(await _etherscan_call(
        client, chain_id, "tokentx", addr, api_key,
        limit=limit, kind="token", since_ts=since_ts,
    ))
    moves.extend(await _etherscan_call(
        client, chain_id, "txlist", addr, api_key,
        limit=limit, kind="native", since_ts=since_ts,
    ))

    out: list[ChainMove] = []
    for m in moves:
        if m.kind == "native":
            m.asset = native_symbol
            m.decimals = native_decimals
            m.meta["chain_id"] = chain_id
        else:
            m.meta["chain_id"] = chain_id
        out.append(m)
    logger.info(f"scan_evm chain={chain_id} {addr[:10]}…: {len(out)} движений")
    return out


async def _etherscan_call(client, chain_id: int, action: str, address: str,
                           api_key: str, limit: int,
                           kind: str, since_ts: int = 0) -> list[ChainMove]:
    """Один запрос к Etherscan V2 с пагинацией.

    Сортировка — от новых к старым: так можно остановиться, дойдя до
    нужной даты, и не тянуть всю историю адреса при каждом фоновом прогоне.
    """
    out: list[ChainMove] = []
    page = 1
    while len(out) < limit:
        params = {
            "chainid": str(chain_id),
            "module": "account",
            "action": action,
            "address": address,
            "page": str(page),
            "offset": "1000",
            "sort": "desc",
            "apikey": api_key,
        }
        try:
            resp = await client.get(ETHERSCAN_API_URL, params=params,
                                   timeout=25.0)
        except Exception as e:
            logger.warning(f"etherscan {action} сеть: {e}")
            break
        if resp.status_code != 200:
            logger.warning(f"etherscan {action} {resp.status_code}")
            break
        try:
            payload = resp.json() or {}
        except Exception:
            break
        result = payload.get("result") or []
        if not isinstance(result, list) or not result:
            break
        oldest = 0
        for item in result:
            move = _evm_item_to_move(item, address, action)
            if move is None:
                continue
            oldest = min(oldest, move.ts) if oldest else move.ts
            out.append(move)
        if since_ts and oldest and oldest < since_ts:
            break
        if len(result) < 1000:
            break
        page += 1
        await asyncio.sleep(0.4)
    return out


def _evm_item_to_move(item: dict, my_address: str, action: str) -> Optional[ChainMove]:
    """Строка ответа Etherscan → ChainMove."""
    try:
        ts = int(item.get("timeStamp") or 0)
    except (TypeError, ValueError):
        return None
    if not ts:
        return None
    sender = (item.get("from") or "").lower()
    recipient = (item.get("to") or "").lower()
    my = (my_address or "").lower()
    if not sender or not recipient or sender == recipient:
        return None
    if sender != my and recipient != my:
        return None

    if action == "txlist":
        raw_value = item.get("value") or "0"
        decimals = 18
        symbol = ""
        log_index = 0
    else:
        raw_value = item.get("value") or "0"
        decimals = int(item.get("tokenDecimal") or 18)
        symbol = (item.get("tokenSymbol") or "").strip().upper()
        if not symbol:
            # У токена без тикера опознаём по контракту: символ не нужен,
            # но актив должен быть заведён в каталог.
            symbol = (item.get("contractAddress") or "")[:10].upper()
        log_index = int(item.get("logIndex") or 0)

    try:
        raw = int(raw_value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None

    direction = "in" if recipient == my else "out"
    human = Decimal(raw) / (Decimal(10) ** decimals)
    tx_hash = str(item.get("hash") or "")
    return ChainMove(
        ts=ts, chain="evm", address=my, asset=symbol,
        amount=f"{human:f}", decimals=decimals, direction=direction,
        counterparty=sender if direction == "in" else recipient,
        tx_hash=tx_hash, log_index=log_index, group=tx_hash,
        kind="token" if action == "tokentx" else "native",
        amount_raw=raw, meta={"contract": item.get("contractAddress") or ""},
    )


# --------------------------------------------------------------- общий вход


async def scan_wallet(wallet, tonapi_key: str = "",
                       etherscan_key: str = "",
                       evm_chain_ids: Optional[list[int]] = None,
                       since_ts: int = 0) -> list[ChainMove]:
    """Сканирует один кошелёк по всем поддерживаемым сетям."""
    from utils.crypto_db import Chain

    chain = (wallet.chain or "").lower()
    if chain == Chain.TON:
        return await scan_ton(wallet.address, tonapi_key, since_ts=since_ts)
    if chain == Chain.EVM:
        ids = evm_chain_ids or [1]
        out: list[ChainMove] = []
        for cid in ids:
            out.extend(await scan_evm(cid, wallet.address, etherscan_key,
                                      since_ts=since_ts))
            await asyncio.sleep(0.3)
        return out
    logger.info(f"scan_wallet: сеть {chain!r} не поддерживается")
    return []
