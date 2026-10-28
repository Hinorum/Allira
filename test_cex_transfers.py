"""Тесты импорта депозитов и выводов CEX.

Проверяется:

  1. Маппинг Bybit/OKX: берутся только успешные ончейн-операции,
     получаются TRANSFER_IN/TRANSFER_OUT и стабильные external_id.
  2. Bybit отдаёт историю окнами меньше 30 дней и листает cursor.
  3. OKX листает историю через after/before.
  4. Сквозное сопоставление: депозит на биржу закрывает исходящий перевод
     кошелька, вывод — входящий; ложный доход/реализация исчезают.
  5. sync_cex пишет переводы, двигает отдельный водяной знак и запускает
     rematch_transfers.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace

TEST_DB = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = TEST_DB
sys.path.insert(0, ".")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

FAILURES: list[str] = []

TON_A = "0:" + "a" * 64


def check(cond: bool, name: str, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def make_cfg(**over):
    base = dict(
        crypto_owner_id=1,
        bybit_api_key="", bybit_api_secret="",
        okx_api_key="", okx_api_secret="", okx_passphrase="",
    )
    base.update(over)
    return SimpleNamespace(**base)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class BybitFakeClient:
    """По одному депозиту на страницу; вторая страница — по cursor."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params))
        if "deposit/query-record" in url:
            start = params["startTime"]
            cursor = params.get("cursor", "")
            row_id = f"d-{start}-{cursor or 'first'}"
            row = {
                "coin": "USDT", "chain": "TRX", "amount": "10",
                "status": 3, "successAt": str(int(start) + 1000),
                "id": row_id,
            }
            return FakeResponse({
                "retCode": 0,
                "result": {
                    "rows": [row],
                    "nextPageCursor": "" if cursor else "page-2",
                },
            })
        return FakeResponse({
            "retCode": 0,
            "result": {"rows": [], "nextPageCursor": ""},
        })


class OkxFakeClient:
    """Первая страница — 100 депозитов, вторая — один старый, плюс вывод."""

    def __init__(self, until_ms: int):
        self.until_ms = until_ms
        self.calls: list[tuple[str, dict]] = []
        self.deposit_pages = 0

    async def get(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params))
        if "deposit-history" in url:
            self.deposit_pages += 1
            if self.deposit_pages == 1:
                rows = [{
                    "ccy": "USDT", "chain": "USDT-TRC20", "amt": "1",
                    "state": "2", "ts": str(self.until_ms - i * 1000),
                    "depId": f"d{i}", "type": "4",
                } for i in range(100)]
            else:
                rows = [{
                    "ccy": "USDT", "chain": "USDT-TRC20", "amt": "1",
                    "state": "2", "ts": str(self.until_ms - 200_000),
                    "depId": "d-old", "type": "4",
                }]
            return FakeResponse({"code": "0", "msg": "", "data": rows})
        return FakeResponse({
            "code": "0", "msg": "",
            "data": [{
                "ccy": "BTC", "chain": "BTC-Bitcoin", "amt": "0.01",
                "state": "2", "ts": str(self.until_ms - 5000),
                "wdId": "w1", "type": "4", "fee": "0.0001",
                "feeCcy": "BTC",
            }],
        })


async def main() -> int:
    import utils.database as db

    db.DB_PATH = TEST_DB
    db.init_db()

    import utils.cex_api as cex_api
    from utils.auto_import import rematch_transfers
    from utils.crypto_db import TxType, add_transaction, add_wallet
    from utils.csv_import import ParsedRow

    print("=" * 60)
    print("Тест импорта депозитов и выводов CEX")
    print("=" * 60)

    # ------------------------------------------------ 1. маппинг Bybit
    print("1. Маппинг Bybit")
    dep = cex_api._bybit_deposit_to_parsed({
        "coin": "USDT", "chain": "TRX", "amount": "12.5",
        "status": 3, "successAt": "1742728163000", "id": "160",
    })
    check(dep is not None and dep.tx_type == TxType.TRANSFER_IN,
          "успешный депозит — TRANSFER_IN", str(dep))
    check(dep is not None and dep.external_id == "bybit:deposit:160",
          "external_id депозита", str(dep))
    check(dep is not None and dep.ts == 1742728163,
          "время депозита в секундах", str(dep))
    check(cex_api._bybit_deposit_to_parsed({
        "coin": "USDT", "amount": "1", "status": 2,
        "successAt": "1742728163000", "id": "161",
    }) is None, "промежуточный статус депозита пропущен")
    check(cex_api._bybit_deposit_to_parsed({
        "coin": "USDT", "amount": "1", "status": "10012",
        "successAt": "1742728163000", "id": "162",
    }) is not None, "10012 (credited) — успешный депозит")

    wd = cex_api._bybit_withdraw_to_parsed({
        "coin": "ETH", "chain": "ETH", "amount": "0.1",
        "status": "success", "withdrawFee": "0.001",
        "createTime": "1742738305000", "withdrawId": "131",
        "withdrawType": 0,
    })
    check(wd is not None and wd.tx_type == TxType.TRANSFER_OUT,
          "успешный вывод — TRANSFER_OUT", str(wd))
    check(wd is not None and wd.external_id == "bybit:withdraw:131",
          "external_id вывода", str(wd))
    check(wd is not None and "fee 0.001 ETH" in wd.note,
          "комиссия вывода в примечании", getattr(wd, "note", ""))
    check(cex_api._bybit_withdraw_to_parsed({
        "coin": "ETH", "amount": "0.1", "status": "Pending",
        "createTime": "1742738305000", "withdrawId": "132",
    }) is None, "Pending-вывод пропущен")
    check(cex_api._bybit_withdraw_to_parsed({
        "coin": "ETH", "amount": "0.1", "status": "BlockchainConfirmed",
        "createTime": "1742738305000", "withdrawId": "133",
        "withdrawType": 0,
    }) is not None, "BlockchainConfirmed — успешный вывод")
    check(cex_api._bybit_withdraw_to_parsed({
        "coin": "ETH", "amount": "0.1", "status": "success",
        "createTime": "1742738305000", "withdrawId": "134",
        "withdrawType": 1,
    }) is None, "внутренний off-chain вывод пропущен")

    # ------------------------------------------------- 2. маппинг OKX
    print("2. Маппинг OKX")
    odep = cex_api._okx_deposit_to_parsed({
        "ccy": "USDT", "chain": "USDT-TRC20", "amt": "10",
        "state": "2", "ts": "1742728163000", "depId": "88", "type": "4",
    })
    check(odep is not None and odep.tx_type == TxType.TRANSFER_IN
          and odep.external_id == "okx:deposit:88",
          "депозит OKX — TRANSFER_IN", str(odep))
    check(cex_api._okx_deposit_to_parsed({
        "ccy": "USDT", "amt": "10", "state": "1",
        "ts": "1742728163000", "depId": "89", "type": "4",
    }) is not None, "state 1 (credited) принимается")
    check(cex_api._okx_deposit_to_parsed({
        "ccy": "USDT", "amt": "10", "state": "0",
        "ts": "1742728163000", "depId": "90", "type": "4",
    }) is None, "ожидающий подтверждения депозит пропущен")
    check(cex_api._okx_deposit_to_parsed({
        "ccy": "USDT", "amt": "10", "state": "2",
        "ts": "1742728163000", "depId": "91", "type": "3",
    }) is None, "внутренний депозит OKX пропущен")

    owd = cex_api._okx_withdraw_to_parsed({
        "ccy": "BTC", "chain": "BTC-Bitcoin", "amt": "0.01",
        "state": "2", "ts": "1742728163000", "wdId": "154",
        "type": "4", "fee": "0.0001", "feeCcy": "BTC",
    })
    check(owd is not None and owd.tx_type == TxType.TRANSFER_OUT
          and owd.external_id == "okx:withdraw:154",
          "вывод OKX — TRANSFER_OUT", str(owd))
    check(cex_api._okx_withdraw_to_parsed({
        "ccy": "BTC", "amt": "0.01", "state": "1",
        "ts": "1742728163000", "wdId": "155", "type": "4",
    }) is None, "вывод в процессе пропущен")
    check(cex_api._okx_withdraw_to_parsed({
        "ccy": "BTC", "amt": "0.01", "state": "2",
        "ts": "1742728163000", "wdId": "156", "type": "3",
    }) is None, "внутренний вывод OKX пропущен")

    # ------------------------------- 3. окна и пагинация Bybit
    print("3. Окна и пагинация Bybit")
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - 60 * 86400 * 1000
    client = BybitFakeClient()
    original_get_client = cex_api.get_client
    original_pause = cex_api.TRANSFER_PAUSE_S

    async def fake_get_client():
        return client

    cex_api.get_client = fake_get_client
    cex_api.TRANSFER_PAUSE_S = 0
    try:
        result = await cex_api.sync_bybit_transfers(
            "key", "secret", since_ts=since_ms, until_ts=now_ms,
        )
    finally:
        cex_api.get_client = original_get_client
        cex_api.TRANSFER_PAUSE_S = original_pause

    deposit_calls = [p for u, p in client.calls if "deposit/query-record" in u]
    withdraw_calls = [p for u, p in client.calls if "withdraw/query-record" in u]
    windows = {p["startTime"] for p in deposit_calls}
    widths_ok = all(
        int(p["endTime"]) - int(p["startTime"])
        <= cex_api.BYBIT_TRANSFER_WINDOW_MS
        for p in deposit_calls
    )
    check(len(windows) == 3, "60 дней разбиты на три окна", str(len(windows)))
    check(widths_ok, "каждое окно не шире 29 дней")
    check(any(p.get("cursor") for p in deposit_calls),
          "cursor-пагинация внутри окна", str(deposit_calls[:2]))
    check(bool(withdraw_calls), "выводы запрошены отдельно")
    check(all(p.get("withdrawType") == "0" for p in withdraw_calls),
          "запрошены только ончейн-выводы")
    check(len(result.rows) == 6, "две страницы в каждом из трёх окон",
          str(len(result.rows)))
    check(all(r.tx_type == TxType.TRANSFER_IN for r in result.rows),
          "все строки — депозиты")
    check(not result.errors, "ошибок нет", str(result.errors))

    # ------------------------------------- 4. пагинация OKX
    print("4. Пагинация OKX")
    okx_client = OkxFakeClient(now_ms)
    cex_api.get_client = fake_get_client
    cex_api.TRANSFER_PAUSE_S = 0

    async def fake_okx_client():
        return okx_client

    cex_api.get_client = fake_okx_client
    try:
        okx_result = await cex_api.sync_okx_transfers(
            "key", "secret", "pass", since_ts=now_ms - 5 * 86400 * 1000,
            until_ts=now_ms,
        )
    finally:
        cex_api.get_client = original_get_client
        cex_api.TRANSFER_PAUSE_S = original_pause

    deposit_pages = [p for u, p in okx_client.calls
                     if "deposit-history" in u]
    check(okx_client.deposit_pages == 2,
          "депозиты OKX прочитаны двумя страницами",
          str(okx_client.deposit_pages))
    check(len(deposit_pages) == 2, "два запроса депозитов",
          str(len(deposit_pages)))
    check(deposit_pages[1].get("after") != deposit_pages[0].get("after"),
          "вторая страница сдвинута через after", str(deposit_pages))
    check(len(okx_result.rows) == 102,
          "101 депозит и один вывод", str(len(okx_result.rows)))
    check(sum(1 for r in okx_result.rows
              if r.tx_type == TxType.TRANSFER_OUT) == 1,
          "вывод OKX распознан")
    check(not okx_result.errors, "ошибок OKX нет", str(okx_result.errors))

    # --------------------- 5. сквозное сопоставление с кошельком
    print("5. Сквозное сопоставление с кошельком")
    owner = 9001
    wallet = add_wallet(owner, "ton", TON_A, "мой")
    base_ts = 1_742_000_000
    add_transaction(owner, base_ts, "USDT", TxType.DISPOSAL_OTHER, "10",
                    source="chain-sync", wallet_id=wallet.id)
    add_transaction(owner, base_ts + 3600, "BTC", TxType.AIRDROP, "0.01",
                    source="chain-sync", wallet_id=wallet.id)

    dep_row = cex_api._bybit_deposit_to_parsed({
        "coin": "USDT", "chain": "TRX", "amount": "10",
        "status": 3, "successAt": str((base_ts + 120) * 1000),
        "id": "e2e-dep",
    })
    wd_row = cex_api._bybit_withdraw_to_parsed({
        "coin": "BTC", "chain": "BTC", "amount": "0.01",
        "status": "success", "createTime": str((base_ts + 3720) * 1000),
        "withdrawId": "e2e-wd", "withdrawType": 0,
    })

    # persist живёт в tasks.cex_sync; импорт отдельно, чтобы не тянуть
    # Telegram-зависимости в модуль API.
    from tasks.cex_sync import persist_cex_rows
    saved, dups = await persist_cex_rows(owner, [dep_row, wd_row], "bybit")
    check(saved == 2 and dups == 0, "депозит и вывод записаны",
          f"saved={saved}, dups={dups}")

    changed = rematch_transfers(owner)
    check(changed == 2, "сопоставлены обе стороны", str(changed))

    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT t.tx_type, t.is_internal, t.source, t.note, a.symbol "
            "FROM crypto_transactions t "
            "JOIN crypto_assets a ON a.id = t.asset_id "
            "WHERE t.owner_id = ? ORDER BY t.id",
            (owner,),
        ).fetchall()
    by_key = {(r["source"], r["symbol"]): r for r in rows}
    usdt_chain = by_key.get(("chain-sync", "USDT"))
    btc_chain = by_key.get(("chain-sync", "BTC"))
    check(usdt_chain is not None
          and usdt_chain["tx_type"] == TxType.TRANSFER_OUT
          and usdt_chain["is_internal"] == 1,
          "исходящий перевод кошелька закрыт депозитом на биржу",
          str(dict(usdt_chain)) if usdt_chain else "нет")
    check(btc_chain is not None
          and btc_chain["tx_type"] == TxType.TRANSFER_IN
          and btc_chain["is_internal"] == 1,
          "входящий перевод кошелька закрыт выводом с биржи",
          str(dict(btc_chain)) if btc_chain else "нет")
    usdt_dep = by_key.get(("bybit", "USDT"))
    check(usdt_dep is not None and usdt_dep["is_internal"] == 1,
          "депозит на биржу помечен внутренним",
          str(dict(usdt_dep)) if usdt_dep else "нет")

    # ---------------------- 6. sync_cex: переводы и rematch
    print("6. sync_cex: переводы и rematch")
    import tasks.cex_sync as cex_sync
    import utils.auto_import as auto_import

    owner = 9002
    cfg = make_cfg(crypto_owner_id=owner,
                   bybit_api_key="key", bybit_api_secret="secret")
    trade_ts = int(time.time()) - 3600
    trade = ParsedRow(
        ts=trade_ts, asset="BTC", tx_type=TxType.BUY, amount="0.5",
        fiat_value_rub="", cost_basis_rub="",
        note="bybit buy BTCUSDT", external_id="bybit:trade-1",
        quote_asset="USDT", quote_amount="50", source="bybit",
    )
    transfer = cex_api._bybit_deposit_to_parsed({
        "coin": "USDT", "chain": "TRX", "amount": "10",
        "status": 3, "successAt": str(trade_ts * 1000),
        "id": "sync-dep",
    })
    captured: dict = {}

    async def fake_trades(key, secret, since_ts=None, until_ts=None):
        captured["trade_until"] = until_ts
        return cex_api.CexSyncResult(exchange="bybit", rows=[trade])

    async def fake_transfers(key, secret, since_ts=None, until_ts=None):
        return cex_api.CexSyncResult(exchange="bybit", rows=[transfer])

    original_trades = cex_sync.sync_bybit_trades
    original_transfers = cex_sync.sync_bybit_transfers
    original_trade_wm = cex_sync.get_cex_watermark
    original_transfer_wm = cex_sync.get_cex_transfer_watermark
    original_set_trade = cex_sync.set_cex_watermark
    original_set_transfer = cex_sync.set_cex_transfer_watermark
    original_price = cex_sync.get_asset_price_rub
    original_rematch = auto_import.rematch_transfers

    async def fake_price(asset, ts):
        return 80.0

    cex_sync.sync_bybit_trades = fake_trades
    cex_sync.sync_bybit_transfers = fake_transfers
    cex_sync.get_cex_watermark = lambda name: 0
    cex_sync.get_cex_transfer_watermark = lambda name: 0
    cex_sync.set_cex_watermark = lambda name, ts: captured.__setitem__(
        "trade_wm", ts)
    cex_sync.set_cex_transfer_watermark = lambda name, ts: captured.__setitem__(
        "transfer_wm", ts)
    cex_sync.get_asset_price_rub = fake_price
    auto_import.rematch_transfers = lambda owner_id: 4
    try:
        report = await cex_sync.sync_cex(owner, cfg)
    finally:
        cex_sync.sync_bybit_trades = original_trades
        cex_sync.sync_bybit_transfers = original_transfers
        cex_sync.get_cex_watermark = original_trade_wm
        cex_sync.get_cex_transfer_watermark = original_transfer_wm
        cex_sync.set_cex_watermark = original_set_trade
        cex_sync.set_cex_transfer_watermark = original_set_transfer
        cex_sync.get_asset_price_rub = original_price
        auto_import.rematch_transfers = original_rematch

    check(report.saved == 3, "две ноги сделки и депозит", str(report.saved))
    check(report.transfers_saved == 1, "перевод учтён отдельно",
          str(report.transfers_saved))
    check(report.rematched == 4, "rematch запущен внутри sync_cex",
          str(report.rematched))
    check((captured.get("trade_wm") or 0) == captured.get("trade_until"),
          "водяной знак сделок сдвинут к концу окна",
          str(captured.get("trade_wm")))
    check((captured.get("transfer_wm") or 0) > trade_ts * 1000,
          "водяной знак переводов сдвинут", str(captured.get("transfer_wm")))

    with db.get_db() as conn:
        dep_db = conn.execute(
            "SELECT t.cost_basis_rub, t.note, t.tx_type FROM crypto_transactions t "
            "JOIN crypto_assets a ON a.id = t.asset_id "
            "WHERE t.owner_id = ? AND t.external_id = ?",
            (owner, "bybit:deposit:sync-dep"),
        ).fetchone()
    check(dep_db is not None and dep_db["tx_type"] == TxType.TRANSFER_IN,
          "депозит записан", str(dict(dep_db)) if dep_db else "нет")
    check(dep_db is not None and dep_db["cost_basis_rub"] == "800.00",
          "внешний ввод оценён по рынку", str(dict(dep_db)) if dep_db else "нет")

    # Ошибка asset-эндпоинта не должна двигать водяной знак переводов.
    owner = 9003
    cfg = make_cfg(crypto_owner_id=owner,
                   bybit_api_key="key", bybit_api_secret="secret")
    captured.clear()

    async def empty_trades(key, secret, since_ts=None, until_ts=None):
        return cex_api.CexSyncResult(exchange="bybit", rows=[])

    async def failing_transfers(key, secret, since_ts=None, until_ts=None):
        return cex_api.CexSyncResult(exchange="bybit",
                                     errors=["assets down"])

    cex_sync.sync_bybit_trades = empty_trades
    cex_sync.sync_bybit_transfers = failing_transfers
    cex_sync.get_cex_watermark = lambda name: 0
    cex_sync.get_cex_transfer_watermark = lambda name: 0
    cex_sync.set_cex_watermark = lambda name, ts: captured.__setitem__(
        "trade_wm", ts)
    cex_sync.set_cex_transfer_watermark = lambda name, ts: captured.__setitem__(
        "transfer_wm", ts)
    auto_import.rematch_transfers = lambda owner_id: 0
    try:
        report = await cex_sync.sync_cex(owner, cfg)
    finally:
        cex_sync.sync_bybit_trades = original_trades
        cex_sync.sync_bybit_transfers = original_transfers
        cex_sync.get_cex_watermark = original_trade_wm
        cex_sync.get_cex_transfer_watermark = original_transfer_wm
        cex_sync.set_cex_watermark = original_set_trade
        cex_sync.set_cex_transfer_watermark = original_set_transfer
        auto_import.rematch_transfers = original_rematch

    check("assets down" in report.errors, "ошибка переводов в отчёте",
          str(report.errors))
    check("transfer_wm" not in captured,
          "при ошибке водяной знак переводов не сдвинут", str(captured))

    print()
    if FAILURES:
        print(f"❌ Провалено проверок: {len(FAILURES)}")
        for name in FAILURES:
            print(f"   • {name}")
        return 1
    print("✅ Все проверки пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
