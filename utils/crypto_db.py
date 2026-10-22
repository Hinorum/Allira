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

    # Торговые/конвертационные операции — двойная запись «дебет-кредит».
    # BUY — покупка актива (приход с себестоимостью, НЕ доход для НДФЛ).
    # TRADE_IN — приход quote-валюты от продажи/обмена (выручка, но не
    #            самостоятельный доход: налог уже по парной стороне сделки).
    # TRADE_OUT — списание quote-валюты при покупке/обмене (конвертация,
    #            не расход в смысле ст. 220 НК, но участвует в балансе).
    BUY = "buy"
    TRADE_IN = "trade_in"
    TRADE_OUT = "trade_out"

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
    def trade_types(cls) -> set[str]:
        return {cls.BUY, cls.TRADE_IN, cls.TRADE_OUT}

    @classmethod
    def all(cls) -> list[str]:
        return sorted(cls.income_types() | cls.disposal_types()
                      | cls.transfer_types() | cls.trade_types())

    @classmethod
    def lot_in_types(cls) -> set[str]:
        """Типы, открывающие FIFO-лот (много/списание с баланса)."""
        return cls.income_types() | {cls.TRANSFER_IN, cls.BUY, cls.TRADE_IN}

    @classmethod
    def lot_out_types(cls) -> set[str]:
        """Типы, списывающие FIFO-лот (списание с баланса).

        TRANSFER_OUT сюда не входит намеренно: запись вводится вручную у
        обеих сторон, net-effect нулевой (см. tax_calc._walk_fifo).
        """
        return cls.disposal_types() | {cls.TRADE_OUT}

    @classmethod
    def taxable_income_types(cls) -> set[str]:
        """Доходы, образующие базу НДФЛ (ст. 214 НК РФ).

        Конвертации (TRADE_IN) и внутренние переводы сюда НЕ входят:
        выручка от продажи облагается по парной стороне сделки (SELL и т.п.).
        """
        return cls.income_types()

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
            cls.BUY: "Покупка",
            cls.TRADE_IN: "Приход по сделке",
            cls.TRADE_OUT: "Списание по сделке",
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
    # Перемещение внутри отслеживаемого периметра (свой кошелёк ↔ биржа).
    # Не открывает и не гасит лот, не влияет на остаток.
    is_internal: bool = False

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


def detect_chain(address: str) -> str:
    """Определить сеть по виду адреса.

    Нужно, чтобы пользователь отдавал только адреса: `/wallet_add 0x…`
    без указания сети. Разбор по формату:
      * «0:hex…» (raw) и «EQ…»/«UQ…» (friendly) — TON;
      * «0x» + 40 hex — EVM (Ethereum, BSC, Polygon и прочие);
      * «T» + 33 base58 — TRON;
      * «1…»/«3…»/«bc1…» — Bitcoin;
      * base58 32-44 символа — Solana.
    """
    addr = (address or "").strip()
    if not addr:
        return Chain.OTHER

    if addr.startswith("0:"):
        return Chain.TON
    if addr.startswith("0x") and len(addr) == 42:
        return Chain.EVM
    if addr[:2].upper() in ("EQ", "UQ") and len(addr) >= 46:
        return Chain.TON
    if addr.startswith("T") and len(addr) == 34:
        return Chain.TRON
    if addr.lower().startswith("bc1"):
        return Chain.BTC
    if addr[0] in "13" and 25 <= len(addr) <= 35:
        return Chain.BTC
    if 32 <= len(addr) <= 44 and all(
        c.isalnum() for c in addr
    ) and not addr.isdigit():
        return Chain.SOLANA
    return Chain.OTHER


def add_wallet(
    owner_id: int,
    chain: str,
    address: str,
    label: str = "",
    kind: str = "own",
) -> Wallet:
    chain = (chain or "").strip().lower()
    if chain in ("", "auto", "?"):
        # Сеть не указана — определяем по виду адреса.
        chain = detect_chain(address)
    elif chain == Chain.OTHER:
        # «other» — тоже автоопределение, но с откатом на other.
        chain = detect_chain(address)
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


def _normalize_money(value, label: str) -> Optional[str]:
    """Рублевая сумма → нормализованная строка (или None).

    Проверка нужна, потому что оба денежных поля приходили свободным
    текстом (панель, ручной ввод): «8 500» или «8500₽» сохранялись как
    есть, и потом float() на таком значении ронял /admin, а отрицательная
    себестоимость давала отрицательный расход в отчёте для ФНС.

    Пустая строка — это «сумма не задана», а не ноль.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace(" ", "").replace("\u00a0", "").replace("₽", "")
        if not text:
            return None
    else:
        text = str(value).strip()
    try:
        amount = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        raise CryptoError(f"{label.capitalize()} должна быть числом: {value!r}")
    if amount < 0:
        raise CryptoError(f"{label.capitalize()} не может быть отрицательной: {value!r}")
    return str(amount)


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
    is_internal: bool = False,
) -> Tx:
    """Создать запись об операции.

    Валидация:
      * тип — из справочника TxType;
      * amount > 0 в абсолютной величине; знак выводится из tx_type;
      * кошелёк (если указан) принадлежит owner_id;
      * ts в пределах разумного (после 2009 — год появления BTC).

    is_internal=True — перемещение внутри отслеживаемого периметра (свой
    кошелёк ↔ биржа). Такая проводка не открывает и не гасит FIFO-лот и не
    влияет на расчётный остаток: актив учтён там, где он acquired.
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

    if tx_type in TxType.lot_in_types():
        direction = "in"
    else:
        direction = "out"

    fv = _normalize_money(fiat_value_rub, "сумма")
    cb = _normalize_money(cost_basis_rub, "себестоимость")

    with get_db() as conn:
        # Защита от дублей: владелец + внешний источник + external_id.
        # owner_id в условии обязателен: без него второй владелец учёта
        # получал ЧУЖУЮ транзакцию вместо своей — импорт для него молча
        # не создавал запись, а отчёт показывал чужую операцию.
        if source != "manual" and external_id:
            dup = conn.execute(
                "SELECT id FROM crypto_transactions "
                "WHERE owner_id=? AND source=? AND external_id=?",
                (owner_id, source, external_id),
            ).fetchone()
            if dup:
                logger.info(f"add_transaction: дубль {source}/{external_id} — пропуск")
                return get_transaction(dup["id"])  # type: ignore[return-value]
        cur = conn.execute(
            """
            INSERT INTO crypto_transactions
                (owner_id, ts, wallet_id, asset_id, tx_type, direction,
                 amount_raw, fiat_value_rub, cost_basis_rub, note, source,
                 external_id, is_internal)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (owner_id, ts, wallet_id, asset.id, tx_type, direction,
             amount_raw, fv, cb, note.strip()[:500], source, external_id[:200],
             1 if is_internal else 0),
        )
        return get_transaction(cur.lastrowid)  # type: ignore[arg-type]


def get_transaction(tx_id: int) -> Optional[Tx]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM crypto_transactions WHERE id = ?", (tx_id,),
        ).fetchone()
    return _tx_from_row(row) if row else None


def find_duplicate(owner_id: int, source: str, external_id: str) -> Optional[int]:
    """id уже существующей транзакции с тем же external_id или None.

    Нужен там, где важно отличить «сохранено» от «дубль»: add_transaction
    на дубле возвращает существующую запись, не бросая исключения, и
    вызывающий код считал её новой — импорт рапортовал «сохранено 100»,
    хотя в базе ничего не изменилось.
    """
    if not external_id or source == "manual":
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM crypto_transactions "
            "WHERE owner_id=? AND source=? AND external_id=?",
            (owner_id, source, external_id),
        ).fetchone()
    return int(row["id"]) if row else None


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
        is_internal=bool(row["is_internal"]) if "is_internal" in row.keys()
        else False,
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
    cols = ", ".join(safe.keys())
    placeholders = ", ".join("?" for _ in safe)
    # Значения должны быть и в INSERT, и в DO UPDATE: при первом INSERT
    # конфликта нет, и SQLite берёт только VALUES — «спрятанные» в
    # DO UPDATE значения при создании строки молча терялись бы.
    updates = ", ".join(f"{k}=excluded.{k}" for k in safe)
    with get_db() as conn:
        conn.execute(
            f"""
            INSERT INTO tax_settings (owner_id, {cols})
            VALUES (?, {placeholders})
            ON CONFLICT(owner_id) DO UPDATE SET {updates}, updated_at=CURRENT_TIMESTAMP
            """,
            [owner_id, *safe.values()],
        )


def _year_start(year: int) -> int:
    return int(time.mktime(time.strptime(f"{year}-01-01 00:00:00", "%Y-%m-%d %H:%M:%S")))


# ------------------------------------------------- расходы, уменьшающие базу


def add_tax_expense(owner_id: int, year: int, amount_rub,
                    category: str = "", note: str = "") -> int:
    """Записать прочий расход за год (электричество, комиссии, амортизация).

    Расходы уменьшают налоговую базу: ст. 221 НК РФ разрешает уменьшать
    доходы на расходы. Без них база майнера считалась бы завышенной на всю
    сумму добычи, а комиссии бирж и стоимость обслуживания кошельков не
    попадали бы в расчёт вообще.
    """
    amount = _to_decimal(amount_rub)
    if amount <= 0:
        raise CryptoError("Сумма расхода должна быть больше нуля")
    if not (2009 <= year <= 2100):
        raise CryptoError("Год вне диапазона 2009-2100")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO tax_expenses "
            "(owner_id, year, amount_rub, category, note) VALUES (?, ?, ?, ?, ?)",
            (owner_id, year, f"{amount:.2f}", category.strip()[:60],
             note.strip()[:200]),
        )
        return int(cur.lastrowid)


def list_tax_expenses(owner_id: int, year: Optional[int] = None) -> list[dict]:
    """Расходы по годам (или за один год)."""
    sql = ("SELECT id, year, amount_rub, category, note, created_at "
           "FROM tax_expenses WHERE owner_id = ?")
    args: list = [owner_id]
    if year is not None:
        sql += " AND year = ?"
        args.append(year)
    sql += " ORDER BY year DESC, id DESC"
    with get_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [
        {
            "id": int(r["id"]),
            "year": int(r["year"]),
            "amount_rub": r["amount_rub"] or "0",
            "category": r["category"] or "",
            "note": r["note"] or "",
        }
        for r in rows
    ]


def delete_tax_expense(expense_id: int, owner_id: int) -> bool:
    """Удалить расход. True — удалён."""
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM tax_expenses WHERE id = ? AND owner_id = ?",
            (expense_id, owner_id),
        )
        return bool(cur.rowcount)


def total_tax_expenses(owner_id: int, year: int) -> Decimal:
    """Сумма расходов за год — вычитается из налоговой базы."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT amount_rub FROM tax_expenses WHERE owner_id = ? AND year = ?",
            (owner_id, year),
        ).fetchall()
    total = Decimal(0)
    for r in row:
        total += _to_decimal(r["amount_rub"])
    return total


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