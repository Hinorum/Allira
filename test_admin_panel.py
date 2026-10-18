"""Функциональный тест сервиса управления.

Проверяет:
  1. utils/log_buffer — кольцевой буфер логов (последние строки, фильтр).
  2. utils/bot_admin — database_stats, wallets_ui/CRUD, transactions_ui,
     tax_settings_ui/save_tax_settings (валидация).
  3. utils/web_panel — авторизация (без токена 401, с токеном 200),
     отдача HTML-страницы /admin, JSON API /admin/api/* (мок-хендлер).
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import urllib.parse

TEST_DB = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = TEST_DB
sys.path.insert(0, ".")

FAILURES: list[str] = []


def check(cond: bool, name: str, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class MockHandler:
    """Заглушка BaseHTTPRequestHandler: собирает статус, заголовки и тело."""

    def __init__(self, path: str, headers: dict | None = None, body: bytes = b""):
        self.path = path
        self.headers = dict(headers or {})
        if body:
            self.headers.setdefault("Content-Length", str(len(body)))
        self.rfile = __import__("io").BytesIO(body)
        self.status = 0
        self.response = b""
        self.content_type = ""

    def _respond(self, status: int, body: str, content_type: str = "text/plain"):
        self.status = status
        self.response = body.encode("utf-8")
        self.content_type = content_type


def main() -> int:
    # ---------------------------------------------------------- лог-буфер
    print("1. Лог-буфер")
    from utils.log_buffer import RecentLogHandler, recent_logs, setup_log_buffer

    handler = RecentLogHandler(capacity=5)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    log = logging.getLogger("test.buffer")
    for i in range(8):
        log.info("строка %s", i)
    lines = handler.tail(limit=10)
    check(len(lines) == 5, "буфер держит только capacity строк", str(len(lines)))
    check("строка 7" in lines[-1]["text"], "последняя строка — самая свежая",
          lines[-1]["text"])
    log.error("ошибка-тест")
    check(handler.tail(limit=5, level="ERROR")[0]["level"] == "ERROR",
          "фильтр по уровню работает")
    root.removeHandler(handler)
    setup_log_buffer()
    logging.getLogger("test.buffer2").info("в общий буфер")
    check(any("в общий буфер" in l["text"] for l in recent_logs()),
          "setup_log_buffer ловит записи корневого logger")

    # ------------------------------------------------------- bot_admin: БД
    print("2. bot_admin: статус и CRUD")
    import utils.database as db
    db.DB_PATH = TEST_DB
    db.init_db()

    from utils import bot_admin
    from utils.crypto_db import CryptoError

    stats = bot_admin.database_stats()
    check("size_bytes" in stats and "counts" in stats,
          "database_stats возвращает размер и счётчики", str(stats)[:120])

    owner = 999003
    w = bot_admin.add_wallet_ui(owner, "ton", "0:abc123def4567890abc123def4567890abc123def4567890abc123def4567890",
                                "тестовый")
    check(bool(w.get("id")), "add_wallet_ui создаёт кошелёк", str(w))
    wallets = bot_admin.wallets_ui(owner)
    check(len(wallets) == 1 and wallets[0]["label"] == "тестовый",
          "wallets_ui отдаёт добавленный кошелёк", str(wallets))
    check(bot_admin.set_wallet_active(owner, w["id"], False),
          "set_wallet_active выключает кошелёк")
    check(bot_admin.wallets_ui(owner)[0]["active"] is False,
          "кошелёк действительно выключен")
    check(not bot_admin.set_wallet_active(owner, 999999, True),
          "set_wallet_active чужой id не трогает")

    tx = bot_admin.add_transaction_ui(
        owner, asset="USDT", tx_type="airdrop", amount="100",
        date_str="2025-06-01", fiat_rub="8500", note="тест")
    check(bool(tx.get("id")), "add_transaction_ui создаёт операцию", str(tx))
    rows = bot_admin.transactions_ui(owner, asset="usdt")
    check(len(rows) == 1 and rows[0]["qty"] == "100",
          "transactions_ui с фильтром по активу", str(rows))
    check(bot_admin.delete_transaction_ui(owner, tx["id"]),
          "delete_transaction_ui удаляет")
    check(bot_admin.transactions_ui(owner) == [], "список пуст после удаления")

    bad = None
    try:
        bot_admin.add_transaction_ui(
            owner, asset="USDT", tx_type="airdrop", amount="100",
            date_str="01.06.2025")
    except (CryptoError, ValueError) as e:
        bad = e
    check(bad is not None, "некорректная дата отклоняется", str(bad))

    # --------------------------------------------------- bot_admin: налоги
    print("3. bot_admin: настройки ФНС")
    bot_admin.save_tax_settings(owner, {"taxpayer_inn": "7700000000",
                                        "tax_status": "resident",
                                        "cost_basis": "fifo"})
    ts = bot_admin.tax_settings_ui(owner)
    check(ts.get("taxpayer_inn") == "7700000000",
          "настройки ФНС сохраняются", str(ts))
    bad = None
    try:
        bot_admin.save_tax_settings(owner, {"tax_status": "космонавт"})
    except CryptoError as e:
        bad = e
    check(bad is not None, "недопустимый статус отклонён", str(bad))
    bad = None
    try:
        bot_admin.save_tax_settings(owner, {"cost_basis": "lifo"})
    except CryptoError as e:
        bad = e
    check(bad is not None, "недопустимый метод отклонён", str(bad))

    # -------------------------------------------------------- web_panel
    print("4. web_panel: авторизация и API")
    from utils import web_panel

    class StubConfig:
        bot_token = "x"
        openrouter_api_key = ""
        tonapi_api_key = ""
        toncenter_api_key = ""
        marketapp_api_key = ""
        marketapp_wallet = ""
        bybit_api_key = ""
        bybit_api_secret = ""
        okx_api_key = ""
        okx_api_secret = ""
        okx_passphrase = ""
        admin_chat_id = ""
        crypto_owner_id = owner

    web_panel.configure(StubConfig(), None, time.time(), "secret-token")

    h = MockHandler("/admin")
    check(web_panel.handle_get(h) and h.status == 200
          and b"<html" in h.response.lower(),
          "/admin отдаёт HTML-страницу", str(h.status))
    check(b"__TOKEN_PLACEHOLDER__" not in h.response,
          "плейсхолдеры в HTML заменены")

    h = MockHandler("/admin/api/status")
    check(web_panel.handle_get(h) and h.status == 401,
          "API без токена → 401", str(h.status))

    h = MockHandler("/admin/api/status", {"X-Admin-Token": "Bearer secret-token"})
    check(web_panel.handle_get(h) and h.status == 200,
          "API с токеном → 200", str(h.status))
    data = json.loads(h.response)
    check(data.get("crypto", {}).get("enabled") is True
          and "jobs" in data and "database" in data,
          "status содержит статус бота", str(data)[:150])

    h = MockHandler("/admin/api/status?token=secret-token")
    check(web_panel.handle_get(h) and h.status == 200,
          "token через query-параметр принимается", str(h.status))

    h = MockHandler("/admin/api/wallets", {"X-Admin-Token": "secret-token"})
    check(web_panel.handle_get(h) and h.status == 200
          and json.loads(h.response)["wallets"],
          "GET /admin/api/wallets отдаёт кошельки", str(h.status))

    body = json.dumps({"asset": "BTC", "tx_type": "airdrop",
                       "amount": "5", "date": "2025-07-01"}).encode()
    h = MockHandler("/admin/api/transactions", {"X-Admin-Token": "secret-token"},
                    body)
    check(web_panel.handle_post(h) and h.status == 200,
          "POST /admin/api/transactions записывает", h.response[:120].decode(
              "utf-8", "replace"))

    h = MockHandler("/admin/api/nope", {"X-Admin-Token": "secret-token"})
    check(web_panel.handle_get(h) and h.status == 404, "неизвестный API → 404")

    # Не-/admin путь панель не трогает.
    h = MockHandler("/health")
    check(web_panel.handle_get(h) is False, "/health не перехватывается")

    # Токен не задан → панель закрыта вообще.
    web_panel.configure(StubConfig(), None, time.time(), "")
    h = MockHandler("/admin/api/status", {"X-Admin-Token": ""})
    check(web_panel.handle_get(h) and h.status == 401,
          "без ADMIN_PANEL_TOKEN панель закрыта")

    # ----------------------------------------------------- admin_menu
    print("5. Telegram-админ-меню: рендер")
    from handlers import admin_menu

    text = admin_menu.admin_status_text(None, StubConfig(), time.time() - 3600)
    check("Аптайм" in text and "БД" in text,
          "статус содержит аптайм и размер БД", text[:120])
    check("CRYPTO_OWNER_ID" in text or "Не заданы" in text
          or "Операций" in text, "статус содержит крипто-сводку", text[:200])

    wtext, markup = admin_menu._format_wallets(owner)
    check("Кошельки" in wtext and markup is not None
          and any("wallet_toggle" in b.callback_data
                  for row in markup.inline_keyboard for b in row),
          "список кошельков с кнопками toggle", wtext[:150])

    txtext = admin_menu._format_tx(owner)
    check("Последние операции" in txtext and "BTC" in txtext,
          "последние операции отображаются", txtext[:150])

    taxtext = admin_menu._format_tax(owner)
    check("7700000000" in taxtext and "FIFO" in taxtext,
          "настройки ФНС с сохранённым ИНН", taxtext[:150])

    logs_text = admin_menu._format_logs()
    check("лог" in logs_text.lower(), "логи форматируются", logs_text[:80])

    recon = admin_menu._format_reconcile({
        "year": 2025, "ok": False, "total_dr_rub": "1000",
        "total_cr_rub": "900",
        "issues": [{"kind": "pairing", "message": "непарная нога"}],
    })
    check("Расхождений: 1" in recon and "pairing" in recon,
          "сводка сверки с расхождениями", recon[:150])

    menu = admin_menu._main_menu()
    datas = [b.callback_data for row in menu.inline_keyboard for b in row]
    check(all(d.startswith("adm:") for d in datas) and len(datas) == 8,
          "главное меню: 8 кнопок с префиксом adm:", str(datas))

    # ------------------------------------------------------------- итог
    if FAILURES:
        print(f"\n❌ ПРОВАЛЕНО: {len(FAILURES)} — {FAILURES}")
        return 1
    print("\n✅ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
