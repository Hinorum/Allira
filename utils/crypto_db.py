"""CRUD по кошелькам, активам и транзакциям крипто-учёта.

Слой над SQLite: типизированный API для хендлеров и расчётных модулей.
Изолирует SQL от остального кода и держит единый справочник типов
операций.

TxType — закрытый справочник. Именно от него зависит налоговая
классификация в utils.tax_calc: «income» и «disposal» дают базу для
НДФЛ, остальные — справочно и в отчёте показываются отдельно.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

from utils.common import normalize_ton_address
from utils.database import get_db
from utils.price_oracle import DEFAULT_COINGECKO_IDS

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------- типы


class TxType:
    """Закрытый справочник типов операций.

    Категория «income» — то, что образует доход в смысле ст. 214 НК
    (аирдроп, майнинг, стейкинг-вознаграждение, возврат).
    Категория «disposal» — реализация, уменьшающая остаток актива
    (продажа, обмен, оплата картой, перевод в пользу другого лица).
    Нейтральные типы (transfer_in, transfer_out) — движение между
    «своими» кошельками; налоговой базы не дают, но участвуют в FIFO
    как «приход/списание» с переносом себестоимости.
    """

    # Доходы
    AIRDROP = "airdrop"
    MINING = "mining"
    STAKING = "staking"
    REWARD = "reward"
    INTEREST = "interest"
    GIFT_IN = "gift_in"           # входящий дар (не от родственника — ст. 217 НК)
    REFUND = "refund"
    INCOME_OTHER = "income_other"

    # Расходы (реализация)
    SELL = "sell"
    SWAP_OUT = "swap_out"
    SPEND = "spend"               # оплата товаров/услуг
    GIFT_OUT = "gift_out"
    SELL_NFT = "sell_nft"
    DISPOSAL_OTHER = "disposal_other"

    # Внутренние перемещения (не доход и не расход для НДФЛ)
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"

    @classmethod
    def income_types(cls) -> set[str]:
        return {
            cls.AIRDROP, cls.MINING, cls.STAKING, cls.REWARD, cls.INTEREST,
            cls.GIFT_IN, cls.REFUND, cls.INCOME_OTHER,
        }

    @classmethod
    def disposal_types(cls) -> set[str]:
        return {
            cls.SELL, cls.SWAP_OUT, cls.SPEND, cls.GIFT_OUT,
            cls.SELL_NFT, cls.DISPOSAL_OTHER,
        }

    @classmethod
    def transfer_types(cls) -> set[str]:
        return {cls.TRANSFER_IN, cls.TRANSFER_OUT}

    @classmethod
    def all(cls) -> list[str]:
        return sorted(cls.income_types() | cls.disposal_types() | cls.transfer_types())

    @classmethod
    def label(cls, t: str) -> str:
        labels = {
            cls.AIRDROP: "Аирдроп",
            cls.MINING: "Майнинг",
            cls.STAKING: "Стейкинг",
            cls.REWARD: "Награда",
            cls.INTEREST: "Процент",
            cls.GIFT_IN: "Подарок входящий",
            cls.REFUND: "Возврат",
            cls.INCOME_OTHER: "Прочий доход",
            cls.SELL: "Продажа",
            cls.SWAP_OUT: "Обмен (исход)",
            cls.SPEND: "Оплата",
            cls.GIFT_OUT: "Подарок исходящий",
            cls.SELL_NFT: "Продажа NFT",
            cls.DISPOSAL_OTHER: "Прочее списание",
            cls.TRANSFER_IN: "Перевод между своими",
            cls.TRANSFER_OUT: "Перевод между своими",
        }
        return labels.get(t, t)


class Chain:
    TON = "ton"
    EVM = "evm"
    BTC = "btc"
    TRON = "tron"
    SOLANA = "solana"
    OTHER = "other"

    @classmethod
    def all(cls) -> list[str]:
        return [cls.TON, cls.EVM, cls.BTC, cls.TRON, cls.SOLANA, cls.OTHER]


# ------------------------------------------------------------- ошибки


class CryptoError(ValueError):
    """Пользовательская ошибка ввода — показать в чате без трейсбека."""


# --------------------------------------------------------- дата-классы


@dataclass
class Asset:
    id: int
    symbol: str
    name: str
    chain: str
    decimals: int
    coingecko_id: str
    contract: str
    kind: str
    fiat_symbol: str

    @property
    def display_amount(self) -> str:
        return self.symbol  # удобный alias


@dataclass
class Wallet:
    id: int
    owner_id: int
    label: str
    chain: str
    address: str
    kind: str

    @property
    def short_address(self) -> str:
        a = self.address
        if len(a) <= 12:
            return a
        return f"{a[:6]}…{a[-4:]}"


@dataclass
class Tx:
    id: int
    owner_id: int
    ts: int
    wallet_id: Optional[int]
    asset_id: int
    tx_type: str
    direction: str
    amount_raw: str
    fiat_value_rub: Optional[str]
    cost_basis_rub: Optional[str]
    note: str
    source: str
    external_id: Optional[str]

    def amount_human(self, decimals: int) -> Decimal:
        return _to_decimal(self.amount_raw) / (Decimal(10) ** decimals)


# ----------------------------------------------------- каталог активов


def _to_decimal(value: str | int | float | Decimal | None) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def ensure_asset(
    symbol: str,
    name: Optional[str] = None,
    chain: str = Chain.OTHER,
    decimals: int = 9,
    coingecko_id: Optional[str] = None,
    contract: str = "",
    kind: str = "coin",
    fiat_symbol: str = "RUB",
) -> Asset:
    """Создать актив, если его ещё нет. Возвращает существующий/новый Asset."""
    symbol = symbol.strip().upper()
    if not symbol:
        raise CryptoError("Пустой символ актива")
    if coingecko_id is None:
        coingecko_id = DEFAULT_COINGECKO_IDS.get(symbol, "")
    if name is None:
        name = symbol
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM crypto_assets WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row:
            return _asset_from_row(row)
        cur = conn.execute(
            """
            INSERT INTO crypto_assets
                (symbol, name, chain, decimals, coingecko_id, contract, kind, fiat_symbol)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, name, chain, decimals, coingecko_id or "",
             contract or "", kind, fiat_symbol),
        )
        return Asset(
            id=cur.lastrowid,
            symbol=symbol,
            name=name,
            chain=chain,
            decimals=decimals,
            coingecko_id=coingecko_id or "",
            contract=contract or "",
            kind=kind,
            fiat_symbol=fiat_symbol,
        )


def get_asset(symbol: str) -> Optional[Asset]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM crypto_assets WHERE symbol = ?",
            (symbol.strip().upper(),),
        ).fetchone()
    return _asset_from_row(row) if row else None


def list_assets(active_only: bool = True) -> list[Asset]:
    sql = "SELECT * FROM crypto_assets"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY symbol"
    with get_db() as conn:
        rows = conn.execute(sql).fetchall()
    return [_asset_from_row(r) for r in rows]


def _asset_from_row(row) -> Asset:
    return Asset(
        id=row["id"],
        symbol=row["symbol"],
        name=row["name"],
        chain=row["chain"],
        decimals=int(row["decimals"] or 0),
        coingecko_id=row["coingecko_id"] or "",
        contract=row["contract"] or "",
        kind=row["kind"] or "coin",
        fiat_symbol=row["fiat_symbol"] or "RUB",
    )


# ------------------------------------------------------------ кошельки


def normalize_address(chain: str, address: str) -> str:
    addr = (address or "").strip()
    if chain == Chain.TON:
        return normalize_ton_address(addr)
    return addr


def add_wallet(
    owner_id: int,
    chain: str,
    address: str,
    label: str = "",
    kind: str = "own",
) -> Wallet:
    chain = (chain or Chain.OTHER).lower()
    if chain not in Chain.all():
        raise CryptoError(f"Неизвестный блокчейн: {chain!r}. Допустимо: {Chain.all()}")
    address = normalize_address(chain, address)
    if not address:
        raise CryptoError("Пустой адрес кошелька")
    if kind not in ("own", "exchange", "custodial"):
        raise CryptoError(f"Неизвестный тип кошелька: {kind!r}")
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM crypto_wallets WHERE chain = ? AND address = ?",
            (chain, address),
        ).fetchone()
        if existing:
            return _wallet_from_row(existing)
        cur = conn.execute(
            """
            INSERT INTO crypto_wallets (owner_id, label, chain, address, kind)
            VALUES (?, ?, ?, ?, ?)
            """,
            (owner_id, label.strip(), chain, address, kind),
        )
        return Wallet(
            id=cur.lastrowid, owner_id=owner_id, label=label.strip(),
            chain=chain, address=address, kind=kind,
        )


def list_wallets(owner_id: int, chain: Optional[str] = None,
                 active_only: bool = True) -> list[Wallet]:
    sql = "SELECT * FROM crypto_wallets WHERE owner_id = ?"
    args: list = [owner_id]
    if active_only:
        sql += " AND is_active = 1"
    if chain:
        sql += " AND chain = ?"
        args.append(chain.lower())
    sql += " ORDER BY chain, label"
    with get_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_wallet_from_row(r) for r in rows]


def get_wallet(owner_id: int, wallet_id: int) -> Optional[Wallet]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM crypto_wallets WHERE id = ? AND owner_id = ?",
            (wallet_id, owner_id),
        ).fetchone()
    return _wallet_from_row(row) if row else None


def _wallet_from_row(row) -> Wallet:
    return Wallet(
        id=row["id"],
        owner_id=row["owner_id"],
        label=row["label"] or "",
        chain=row["chain"],
        address=row["address"],
        kind=row["kind"] or "own",
    )


# --------------------------------------------------------- транзакции


def _normalize_amount(decimals: int, amount: str | float | Decimal) -> str:
    """Любое удобное число → строка в минимальных единицах (нано)."""
    if isinstance(amount, str) and not amount:
        raise CryptoError("Пустое количество")
    d = _to_decimal(amount)
    if d <= 0:
        raise CryptoError("Количество должно быть > 0")
    raw = int((d * (Decimal(10) ** decimals)).to_integral_value())
    if raw <= 0:
        raise CryptoError("Слишком малое количество (после decimals)")
    return str(raw)


def add_transaction(
    owner_id: int,
    ts: int,
    asset_symbol: str,
    tx_type: str,
    amount,
    *,
    wallet_id: Optional[int] = None,
    fiat_value_rub: Optional[float | str] = None,
    cost_basis_rub: Optional[float | str] = None,
    note: str = "",
    source: str = "manual",
    external_id: str = "",
) -> Tx:
    """Создать запись об операции.

    Валидация:
      * тип — из справочника TxType;
      * amount > 0 в абсолютной величине; знак выводится из tx_type;
      * кошелёк (если указан) принадлежит owner_id;
      * ts в пределах разумного (после 2009 — год появления BTC).
    """
    if tx_type not in TxType.all():
        raise CryptoError(f"Неизвестный тип операции: {tx_type!r}")
    asset = ensure_asset(asset_symbol.strip().upper())
    if ts < 1_000_000_000 or ts > 4_102_444_800:
        raise CryptoError("Дата операции вне разумного диапазона (2009-2100)")

    if wallet_id:
        w = get_wallet(owner_id, wallet_id)
        if not w:
            raise CryptoError(f"Кошелёк #{wallet_id} не найден или чужой")

    amount_raw = _normalize_amount(asset.decimals, amount)

    if tx_type in TxType.income_types() or tx_type == TxType.TRANSFER_IN:
        direction = "in"
    else:
        direction = "out"

    fv = str(fiat_value_rub) if fiat_value_rub not in (None, "") else None
    cb = str(cost_basis_rub) if cost_basis_rub not in (None, "") else None

    with get_db() as conn:
        # Защита от дублей: внешний источник + external_id.
        if source != "manual" and external_id:
            dup = conn.execute(
                "SELECT id FROM crypto_transactions WHERE source=? AND external_id=?",
                (source, external_id),
            ).fetchone()
            if dup:
                logger.info(f"add_transaction: дубль {source}/{external_id} — пропуск")
                return get_transaction(dup["id"])  # type: ignore[return-value]
        cur = conn.execute(
            """
            INSERT INTO crypto_transactions
                (owner_id, ts, wallet_id, asset_id, tx_type, direction,
                 amount_raw, fiat_value_rub, cost_basis_rub, note, source, external_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (owner_id, ts, wallet_id, asset.id, tx_type, direction,
             amount_raw, fv, cb, note.strip()[:500], source, external_id[:200]),
        )
        return get_transaction(cur.lastrowid)  # type: ignore[arg-type]


def get_transaction(tx_id: int) -> Optional[Tx]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM crypto_transactions WHERE id = ?", (tx_id,),
        ).fetchone()
    return _tx_from_row(row) if row else None


def list_transactions(
    owner_id: int,
    *,
    year: Optional[int] = None,
    asset_symbol: Optional[str] = None,
    tx_type: Optional[str] = None,
    wallet_id: Optional[int] = None,
    limit: int = 200,
) -> list[Tx]:
    sql = "SELECT * FROM crypto_transactions WHERE owner_id = ?"
    args: list = [owner_id]
    if year:
        sql += " AND ts >= ? AND ts < ?"
        args += [_year_start(year), _year_end(year)]
    if asset_symbol:
        sql += " AND asset_id = (SELECT id FROM crypto_assets WHERE symbol = ?)"
        args.append(asset_symbol.strip().upper())
    if tx_type:
        sql += " AND tx_type = ?"
        args.append(tx_type)
    if wallet_id:
        sql += " AND wallet_id = ?"
        args.append(wallet_id)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_tx_from_row(r) for r in rows]


def delete_transaction(tx_id: int, owner_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM crypto_transactions WHERE id = ? AND owner_id = ?",
            (tx_id, owner_id),
        )
        return cur.rowcount > 0


def update_transaction_fiat(tx_id: int, owner_id: int,
                            fiat_value_rub: Optional[str],
                            cost_basis_rub: Optional[str] = None) -> bool:
    with get_db() as conn:
        if cost_basis_rub is None:
            cur = conn.execute(
                """
                UPDATE crypto_transactions
                SET fiat_value_rub = ?
                WHERE id = ? AND owner_id = ?
                """,
                (fiat_value_rub, tx_id, owner_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE crypto_transactions
                SET fiat_value_rub = ?, cost_basis_rub = ?
                WHERE id = ? AND owner_id = ?
                """,
                (fiat_value_rub, cost_basis_rub, tx_id, owner_id),
            )
        return cur.rowcount > 0


def _tx_from_row(row) -> Tx:
    return Tx(
        id=row["id"],
        owner_id=row["owner_id"],
        ts=int(row["ts"]),
        wallet_id=row["wallet_id"],
        asset_id=row["asset_id"],
        tx_type=row["tx_type"],
        direction=row["direction"],
        amount_raw=row["amount_raw"],
        fiat_value_rub=row["fiat_value_rub"],
        cost_basis_rub=row["cost_basis_rub"],
        note=row["note"] or "",
        source=row["source"] or "manual",
        external_id=row["external_id"],
    )


# ------------------------------------------------------- настройки / utils


def get_tax_settings(owner_id: int) -> dict:
    """Настройки плательщика. Дефолты — резидент, FIFO, RUB."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tax_settings WHERE owner_id = ?", (owner_id,)
        ).fetchone()
    if not row:
        return {
            "owner_id": owner_id,
            "taxpayer_inn": "",
            "taxpayer_name": "",
            "tax_status": "resident",
            "cost_basis": "fifo",
            "reporting_currency": "RUB",
        }
    return {
        "owner_id": row["owner_id"],
        "taxpayer_inn": row["taxpayer_inn"] or "",
        "taxpayer_name": row["taxpayer_name"] or "",
        "tax_status": row["tax_status"] or "resident",
        "cost_basis": row["cost_basis"] or "fifo",
        "reporting_currency": row["reporting_currency"] or "RUB",
    }


def set_tax_settings(owner_id: int, **fields) -> None:
    allowed = {"taxpayer_inn", "taxpayer_name", "tax_status",
               "cost_basis", "reporting_currency"}
    safe = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not safe:
        return
    placeholders = ", ".join(f"{k}=?" for k in safe)
    args = list(safe.values()) + [owner_id]
    with get_db() as conn:
        conn.execute(
            f"""
            INSERT INTO tax_settings (owner_id) VALUES (?)
            ON CONFLICT(owner_id) DO UPDATE SET {placeholders}, updated_at=CURRENT_TIMESTAMP
            """,
            [owner_id, *args],
        )


def _year_start(year: int) -> int:
    return int(time.mktime(time.strptime(f"{year}-01-01 00:00:00", "%Y-%m-%d %H:%M:%S")))


def _year_end(year: int) -> int:
    return int(time.mktime(time.strptime(f"{year + 1}-01-01 00:00:00", "%Y-%m-%d %H:%M:%S")))


def humanize_amount(amount_raw: str, decimals: int) -> str:
    """Минимальные единицы → строка вида '1.234567'."""
    d = _to_decimal(amount_raw)
    human = d / (Decimal(10) ** decimals)
    s = format(human, "f")
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = frac.rstrip("0") or "0"
        s = f"{whole}.{frac}"
    return s


def current_owner_id(context_or_default) -> int:
    """Удобный хелпер: вытащить owner_id из bot_data с дефолтом из CREATOR."""
    bot_data = getattr(context_or_default, "bot_data", {}) or {}
    return int(bot_data.get("CRYPTO_OWNER_ID") or 0)