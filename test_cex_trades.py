"""Тесты истории сделок CEX: окна и пагинация.

Проверяется:

  1. Bybit execution/list не получает диапазон шире 7 дней; внутри окна
     работает cursor, окна идут подряд без разрывов.
  2. OKX читает fills-history, а не короткий fills; следующая страница
     запрашивается через after=billId.
  3. Если billId нет, OKX-страница сдвигается по времени и не зацикливается.
"""

import asyncio
import json
import os
import sys
import tempfile
import time

TEST_DB = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = TEST_DB
sys.path.insert(0, ".")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

FAILURES: list[str] = []


def check(cond: bool, name: str, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class BybitTradesClient:
    """Одна сделка на страницу; вторая страница — по cursor."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params))
        start = int(params["startTime"])
        cursor = params.get("cursor", "")
        row = {
            "execId": f"e-{start}-{cursor or 'first'}",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execQty": "0.01",
            "execPrice": "50000",
            "execTime": str(start + 1000),
        }
        return FakeResponse({
            "retCode": 0,
            "result": {
                "list": [row],
                "nextPageCursor": "" if cursor else "next-page",
            },
        })


class OkxAfterClient:
    """100 сделок на первой странице, одна старая — по after=billId."""

    def __init__(self, until_ms: int):
        self.until_ms = until_ms
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params))
        if params.get("after"):
            rows = [{
                "instId": "BTC-USDT", "side": "buy", "fillSz": "0.01",
                "fillPx": "50000", "ts": str(self.until_ms - 200_000),
                "tradeId": "t-old", "billId": "b-old",
            }]
        else:
            rows = [{
                "instId": "BTC-USDT", "side": "buy", "fillSz": "0.01",
                "fillPx": "50000", "ts": str(self.until_ms - i * 1000),
                "tradeId": f"t{i}", "billId": f"b{i}",
            } for i in range(100)]
        return FakeResponse({"code": "0", "msg": "", "data": rows})


class OkxTimeClient:
    """Страницы без billId: вторая должна сдвинуться по end."""

    def __init__(self, until_ms: int):
        self.until_ms = until_ms
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params))
        if len(self.calls) == 1:
            rows = [{
                "instId": "ETH-USDT", "side": "sell", "fillSz": "0.1",
                "fillPx": "3000", "ts": str(self.until_ms - i * 1000),
                "tradeId": f"e{i}",
            } for i in range(100)]
        else:
            rows = [{
                "instId": "ETH-USDT", "side": "sell", "fillSz": "0.1",
                "fillPx": "3000", "ts": str(self.until_ms - 300_000),
                "tradeId": "e-old",
            }]
        return FakeResponse({"code": "0", "msg": "", "data": rows})


async def main() -> int:
    import utils.database as db

    db.DB_PATH = TEST_DB
    db.init_db()

    import utils.cex_api as cex_api
    from utils.crypto_db import TxType

    print("=" * 60)
    print("Тест истории сделок CEX")
    print("=" * 60)

    now_ms = int(time.time() * 1000)
    original_get_client = cex_api.get_client
    original_pause = cex_api.TRADE_PAUSE_S

    # ------------------------------------------ 1. окна Bybit
    print("1. Bybit: окна не шире 7 дней и cursor")
    bybit = BybitTradesClient()

    async def fake_bybit_client():
        return bybit

    cex_api.get_client = fake_bybit_client
    cex_api.TRADE_PAUSE_S = 0
    since_ms = now_ms - 20 * 86400 * 1000
    try:
        result = await cex_api.sync_bybit_trades(
            "key", "secret", since_ts=since_ms, until_ts=now_ms,
        )
    finally:
        cex_api.get_client = original_get_client
        cex_api.TRADE_PAUSE_S = original_pause

    calls = [p for u, p in bybit.calls if "execution/list" in u]
    starts = sorted({int(p["startTime"]) for p in calls})
    widths_ok = all(
        int(p["endTime"]) - int(p["startTime"])
        <= cex_api.BYBIT_TRADE_WINDOW_MS
        for p in calls
    )
    contiguous = all(
        starts[i + 1] == int(next(
            p["endTime"] for p in calls
            if int(p["startTime"]) == starts[i]
        )) + 1
        for i in range(len(starts) - 1)
    )
    check(len(starts) == 3, "20 дней разбиты на три окна", str(starts))
    check(widths_ok, "каждое окно не шире 7 дней", str(calls[:2]))
    check(contiguous, "окна идут подряд без разрывов", str(starts))
    check(sum(1 for p in calls if p.get("cursor")) == 3,
          "cursor использован в каждом окне", str(calls[:2]))
    check(len(result.rows) == 6, "две страницы в каждом окне",
          str(len(result.rows)))
    check(all(r.tx_type == TxType.BUY for r in result.rows),
          "все строки — покупки")
    check(not result.errors, "ошибок Bybit нет", str(result.errors))

    # ------------------------------- 2. OKX: after=billId
    print("2. OKX: fills-history и after=billId")
    okx = OkxAfterClient(now_ms)

    async def fake_okx_client():
        return okx

    cex_api.get_client = fake_okx_client
    cex_api.TRADE_PAUSE_S = 0
    try:
        okx_result = await cex_api.sync_okx_trades(
            "key", "secret", "pass",
            since_ts=now_ms - 5 * 86400 * 1000, until_ts=now_ms,
        )
    finally:
        cex_api.get_client = original_get_client
        cex_api.TRADE_PAUSE_S = original_pause

    okx_urls = [u for u, _ in okx.calls]
    check(all("fills-history" in u for u in okx_urls),
          "используется fills-history, а не короткий fills", str(okx_urls))
    check(len(okx.calls) == 2, "две страницы OKX", str(len(okx.calls)))
    check(okx.calls[1][1].get("after") == "b99",
          "вторая страница через after=billId", str(okx.calls[1][1]))
    check(len(okx_result.rows) == 101, "100 + 1 сделка",
          str(len(okx_result.rows)))
    check(okx_result.rows[0].external_id == "okx:t0",
          "external_id из tradeId", str(okx_result.rows[0].external_id))
    check(not okx_result.errors, "ошибок OKX нет", str(okx_result.errors))

    # --------------------------- 3. OKX: резервный сдвиг по времени
    print("3. OKX: сдвиг по времени без billId")
    okx_time = OkxTimeClient(now_ms)

    async def fake_time_client():
        return okx_time

    cex_api.get_client = fake_time_client
    cex_api.TRADE_PAUSE_S = 0
    try:
        time_result = await cex_api.sync_okx_trades(
            "key", "secret", "pass",
            since_ts=now_ms - 5 * 86400 * 1000, until_ts=now_ms,
        )
    finally:
        cex_api.get_client = original_get_client
        cex_api.TRADE_PAUSE_S = original_pause

    check(len(okx_time.calls) == 2, "без billId всё равно две страницы",
          str(len(okx_time.calls)))
    check(int(okx_time.calls[1][1]["end"])
          < int(okx_time.calls[0][1]["end"]),
          "вторая страница сдвинута по end",
          str((okx_time.calls[0][1], okx_time.calls[1][1])))
    check("after" not in okx_time.calls[1][1],
          "after не подставляется без billId", str(okx_time.calls[1][1]))
    check(len(time_result.rows) == 101, "100 + 1 сделка по времени",
          str(len(time_result.rows)))
    check(not time_result.errors, "ошибок нет", str(time_result.errors))

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
