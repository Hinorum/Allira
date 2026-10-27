"""Источники отчёта и готовность к расчёту.

Идея: человек не должен помнить порядок команд. Он отдаёт адреса
кошельков одним сообщением (сеть определится сама), видит на одном
экране, что уже есть и чего не хватает, а «Обновить всё» прогоняет
сканирование сетей и синк бирж одной кнопкой.

Модуль не пишет в Telegram — только данные: разбор списка адресов,
пакетное добавление и снимок состояния источников. Так его можно
тестировать без бота.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from utils.crypto_db import (
    Chain,
    CryptoError,
    add_wallet,
    detect_chain,
    list_wallets,
    normalize_address,
)
from utils.database import get_db

# Флаг в context.user_data: бот ждёт сообщение со списком адресов.
AWAIT_WALLETS_KEY = "await_wallets"

# Маркеры списка: «- адрес», «• адрес», «1. адрес», «1) адрес».
_BULLET_RE = re.compile(r"^\s*(?:[-–—•*]|\d+[.)])\s+")
# Разделители адресов в одном сообщении.
_SPLIT_RE = re.compile(r"[\n,;]+")


@dataclass
class WalletLine:
    """Одна строка ввода: адрес и, если был, ярлык."""

    address: str
    label: str = ""


@dataclass
class BulkWalletResult:
    """Итог пакетного добавления — для короткой сводки в чате."""

    added: list = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    invalid: list[tuple[str, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.duplicates or self.invalid)


@dataclass
class SourcesSnapshot:
    """Что уже отдано боту и чего не хватает до отчёта."""

    year: int
    wallets: list
    cex: list[tuple[str, bool]]
    pending: int
    ops_year: int

    @property
    def has_sources(self) -> bool:
        return bool(self.wallets) or any(ok for _, ok in self.cex)

    @property
    def cex_line(self) -> str:
        if not self.cex:
            return "биржи не настроены"
        return ", ".join(
            f"{name} {'✅' if ok else '⚪'}" for name, ok in self.cex
        )

    @property
    def summary_line(self) -> str:
        """Одна строка для /tax: что уже есть и сколько спорит."""
        return (f"Источники: кошельков — {len(self.wallets)}; "
                f"{self.cex_line}; спорных — {self.pending}")

    @property
    def missing(self) -> list[str]:
        out: list[str] = []
        if not self.has_sources:
            out.append(
                "Нет источников: пришли адрес кошелька или выписку биржи."
            )
        if self.pending:
            out.append(
                f"{self.pending} операций ждут проверки — от них зависит база."
            )
        if self.has_sources and not self.ops_year:
            out.append(
                f"За {self.year} операций нет: проверь год или догрузи историю."
            )
        return out


def _looks_like_address(token: str) -> bool:
    return detect_chain(token) != Chain.OTHER


def _split_label(line: str) -> tuple[str, str]:
    """Отделить явный ярлык: «адрес | подпись» или «адрес<TAB>подпись»."""
    for sep in ("|", "\t"):
        if sep in line:
            left, right = line.split(sep, 1)
            return left.strip(), right.strip()
    return line.strip(), ""


def _find_address(line: str) -> tuple[str, str]:
    """Найти адрес в строке, даже если подпись стоит перед ним.

    Адрес — первый токен, похожий на адрес по формату. Всё остальное
    считаем подписью: «основной 0x…» тоже поймём.
    """
    parts = line.split()
    for i, token in enumerate(parts):
        if _looks_like_address(token):
            rest = " ".join(parts[:i] + parts[i + 1:]).strip(" -–—|")
            return token, rest
    return line.strip(), ""


def parse_wallet_lines(text: str) -> list[WalletLine]:
    """Разобрать сообщение со списком адресов.

    Принимает адреса по одному в строке, через запятую или точку с
    запятой, с маркерами списка и необязательными подписями.
    """
    out: list[WalletLine] = []
    for raw in _SPLIT_RE.split(text or ""):
        line = _BULLET_RE.sub("", raw).strip().strip("<>\"'")
        if not line:
            continue
        body, explicit_label = _split_label(line)
        address, rest = _find_address(body)
        if not address:
            continue
        out.append(WalletLine(address=address,
                              label=explicit_label or rest))
    return out


def _existing_addresses(owner_id: int) -> set[tuple[str, str]]:
    return {
        (w.chain, normalize_address(w.chain, w.address))
        for w in list_wallets(owner_id)
    }


def add_wallets_from_text(owner_id: int, text: str) -> BulkWalletResult:
    """Добавить все адреса из сообщения, не падая на плохих строках.

    Сеть определяется по виду адреса. Дубли и опечатки не прерывают
    пакет: они попадают в отчёт, остальные адреса добавляются.
    """
    result = BulkWalletResult()
    seen = _existing_addresses(owner_id)

    for item in parse_wallet_lines(text):
        chain = detect_chain(item.address)
        if chain == Chain.OTHER:
            result.invalid.append(
                (item.address, "не похож на адрес — нужна сеть")
            )
            continue

        normalized = normalize_address(chain, item.address)
        if (chain, normalized) in seen:
            result.duplicates.append(item.address)
            continue

        try:
            wallet = add_wallet(owner_id, chain, item.address, item.label)
        except CryptoError as e:
            result.invalid.append((item.address, str(e)))
            continue

        result.added.append(wallet)
        seen.add((wallet.chain, normalize_address(wallet.chain, wallet.address)))

    return result


def cex_connections(cfg) -> list[tuple[str, bool]]:
    """Какие биржи настроены в конфиге (read-only ключи)."""
    return [
        ("Bybit", bool(getattr(cfg, "bybit_api_key", "")
                       and getattr(cfg, "bybit_api_secret", ""))),
        ("OKX", bool(getattr(cfg, "okx_api_key", "")
                     and getattr(cfg, "okx_api_secret", "")
                     and getattr(cfg, "okx_passphrase", ""))),
    ]


def sources_snapshot(owner_id: int, cfg, year: int | None = None) -> SourcesSnapshot:
    """Собрать состояние источников за год — без сетевых вызовов."""
    year = int(year or datetime.now().year)
    start = int(datetime(year, 1, 1).timestamp())
    end = int(datetime(year + 1, 1, 1).timestamp())

    with get_db() as conn:
        ops = conn.execute(
            "SELECT COUNT(*) AS n FROM crypto_transactions "
            "WHERE owner_id = ? AND ts >= ? AND ts < ?",
            (owner_id, start, end),
        ).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM crypto_transactions "
            "WHERE owner_id = ? AND source = 'chain' "
            "AND (is_internal IS NULL OR is_internal = 0) "
            "AND (is_confirmed IS NULL OR is_confirmed = 0)",
            (owner_id,),
        ).fetchone()

    return SourcesSnapshot(
        year=year,
        wallets=list_wallets(owner_id),
        cex=cex_connections(cfg),
        pending=int(pending["n"] or 0),
        ops_year=int(ops["n"] or 0),
    )
