"""Веб-панель управления ботом поверх stdlib health-сервера.

Маршруты (все, кроме /health, — на префиксе /admin):
  GET  /admin                      — одностраничный интерфейс (SPA, один файл)
  GET  /admin/api/status           — статус: задачи, БД, крипто, ключи
  GET  /admin/api/logs             — последние строки лога (?limit=&level=)
  GET  /admin/api/wallets          — кошельки
  POST /admin/api/wallets          — добавить кошелёк {chain,address,label}
  POST /admin/api/wallets/toggle   — вкл/выкл кошелёк {id,active}
  GET  /admin/api/transactions     — операции (?limit=&offset=&asset=&type=)
  POST /admin/api/transactions     — добавить {asset,tx_type,amount,...}
  POST /admin/api/transactions/delete — удалить {id}
  GET  /admin/api/tax              — настройки ФНС
  POST /admin/api/tax              — сохранить настройки
  POST /admin/api/sync/cex         — ручной синк бирж
  POST /admin/api/sync/jetton      — ручной скан TON
  POST /admin/api/reconcile        — сверка дебет-кредит {year}

Авторизация: заголовок X-Admin-Token с токеном из env ADMIN_PANEL_TOKEN.
Если токен не задан — панель отдаёт 404 (не афишируем панель в открытом
доступе на Render, где порт виден всем).

Бизнес-логика — в utils/bot_admin.py; здесь только HTTP и рендер.
"""

from __future__ import annotations

import json
import logging
import hmac
import threading
import time
import urllib.parse
from datetime import datetime

from utils import bot_admin
from utils.log_buffer import recent_logs

logger = logging.getLogger(__name__)

# Заполняется из main.py при старте.
_config = None
_application = None
_start_time = 0.0
_token = ""
_ready = threading.Event()

# ------------------------------------------------------------- настройка


def configure(config, application, start_time: float, token: str) -> None:
    global _config, _application, _start_time, _token
    _config = config
    _application = application
    _start_time = start_time
    _token = (token or "").strip()
    _ready.set()


def _authorized(handler) -> bool:
    """Проверка токена: строгое равенство, а не вхождение подстроки.

    Раньше было `_token in header` — «терпим Bearer-префикс» превратилось
    в «терпим любую строку, содержащую токен»: заголовок
    «xx<токен>yy» проходил проверку. Сравнение — через
    hmac.compare_digest, чтобы не светить секрет по времени ответа.
    """
    if not _token:
        return False
    header = (handler.headers.get("X-Admin-Token") or "").strip()
    if header:
        if header.startswith("Bearer "):
            header = header[len("Bearer "):].strip()
        return hmac.compare_digest(header, _token)
    # Запасной вариант: query-параметр для отладки.
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
    supplied = (query.get("token") or [""])[0].strip()
    return bool(supplied) and hmac.compare_digest(supplied, _token)


def _clamp(value: int, low: int, high: int) -> int:
    """Ограничить число диапазоном.

    В SQLite отрицательный LIMIT означает «без ограничения»: запрос
    ?limit=-1 отдавал всю таблицу транзакций одним куском — память и
    время запроса росли вместе с базой.
    """
    return max(low, min(value, high))


# ------------------------------------------------------------ диспетчер


def handle_get(handler) -> bool:
    """GET-маршруты панели. True — запрос обработан (ответ уже отправлен)."""
    if not _ready.is_set():
        return False
    parsed = urllib.parse.urlparse(handler.path)
    path = parsed.path.rstrip("/") or "/"
    if path == "/admin":
        # Токен не задан — панель выключена (404, как и обещает докстринг):
        # порт на Render виден всему интернету, и афишировать панель
        # ровно тогда, когда её собирались скрыть, незачем.
        # Страница отдаётся без токена: это пустая SPA-оболочка, всё
        # содержимое — за /admin/api/*, а токен вводится в её форме.
        if not _token:
            handler._respond(404, "not found", "text/plain; charset=utf-8")
            return True
        handler._respond(200, _page_html(), "text/html; charset=utf-8")
        return True
    if not path.startswith("/admin/api/"):
        return False
    if not _authorized(handler):
        handler._respond(401, json.dumps({"error": "unauthorized"}),
                         "application/json")
        return True
    query = urllib.parse.parse_qs(parsed.query)
    api = path[len("/admin/api/"):]
    try:
        if api == "status":
            data = bot_admin.full_status(_application, _config, _start_time)
        elif api == "logs":
            limit = _clamp(int((query.get("limit") or ["150"])[0]), 1, 400)
            level = (query.get("level") or [""])[0]
            data = {"lines": recent_logs(limit=limit, level=level)}
        elif api == "wallets":
            data = {"wallets": bot_admin.wallets_ui(_owner_id())}
        elif api == "transactions":
            data = {"rows": bot_admin.transactions_ui(
                _owner_id(),
                limit=_clamp(int((query.get("limit") or ["50"])[0]), 1, 200),
                offset=_clamp(int((query.get("offset") or ["0"])[0]), 0, 10_000_000),
                asset=(query.get("asset") or [""])[0],
                tx_type=(query.get("type") or [""])[0],
            )}
        elif api == "tax":
            data = bot_admin.tax_settings_ui(_owner_id())
        else:
            handler._respond(404, json.dumps({"error": "not found"}),
                             "application/json")
            return True
        handler._respond(200, json.dumps(data, ensure_ascii=False, default=str),
                         "application/json")
    except Exception as e:
        logger.error(f"admin api GET {api}: {e}", exc_info=True)
        handler._respond(500, json.dumps({"error": str(e)}),
                         "application/json")
    return True


def handle_post(handler) -> bool:
    """POST-маршруты панели. True — запрос обработан."""
    if not _ready.is_set():
        return False
    path = urllib.parse.urlparse(handler.path).path.rstrip("/") or "/"
    if not path.startswith("/admin/api/"):
        return False
    if not _authorized(handler):
        handler._respond(401, json.dumps({"error": "unauthorized"}),
                         "application/json")
        return True

    body = _read_body(handler)
    api = path[len("/admin/api/"):]
    try:
        if api == "wallets":
            data = bot_admin.add_wallet_ui(
                _owner_id(),
                str(body.get("chain") or "ton"),
                str(body.get("address") or ""),
                str(body.get("label") or ""),
            )
        elif api == "wallets/toggle":
            # bool("false") — это True: клиент, присылающий булево
            # строкой (curl, form-encoded), получал обратное тому, что
            # просил. Разбираем значение явно.
            active_raw = body.get("active", True)
            if isinstance(active_raw, str):
                active = active_raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                active = bool(active_raw)
            ok = bot_admin.set_wallet_active(
                _owner_id(), int(body.get("id") or 0), active)
            data = {"ok": ok}
        elif api == "transactions":
            data = bot_admin.add_transaction_ui(
                _owner_id(),
                asset=str(body.get("asset") or ""),
                tx_type=str(body.get("tx_type") or ""),
                amount=str(body.get("amount") or ""),
                date_str=str(body.get("date") or ""),
                fiat_rub=str(body.get("fiat_rub") or ""),
                cost_rub=str(body.get("cost_rub") or ""),
                note=str(body.get("note") or ""),
            )
        elif api == "transactions/delete":
            data = {"ok": bot_admin.delete_transaction_ui(
                _owner_id(), int(body.get("id") or 0))}
        elif api == "tax":
            bot_admin.save_tax_settings(_owner_id(), body)
            data = bot_admin.tax_settings_ui(_owner_id())
        elif api == "sync/cex":
            data = bot_admin.run_async(
                bot_admin.manual_cex_sync(_config, _owner_id()), timeout=240)
        elif api == "sync/jetton":
            data = bot_admin.run_async(
                bot_admin.manual_jetton_sync(_config, _owner_id()), timeout=240)
        elif api == "reconcile":
            year = int(body.get("year") or datetime.now().year)
            data = bot_admin.run_async(
                bot_admin.manual_reconcile(_config, _owner_id(), year),
                timeout=300)
        else:
            handler._respond(404, json.dumps({"error": "not found"}),
                             "application/json")
            return True
        handler._respond(200, json.dumps(data, ensure_ascii=False, default=str),
                         "application/json")
    except Exception as e:
        logger.error(f"admin api POST {api}: {e}", exc_info=True)
        handler._respond(500, json.dumps({"error": str(e)}),
                         "application/json")
    return True


def _owner_id() -> int:
    return int(getattr(_config, "crypto_owner_id", 0) or 0)


def _read_body(handler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return {}
    raw = handler.rfile.read(min(length, 1_000_000))
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


# ------------------------------------------------------------- страница

PAGE_TITLE = "Allira · Панель управления"


def _page_html() -> str:
    return _HTML.replace("__TOKEN_PLACEHOLDER__", "") \
                .replace("__TITLE__", PAGE_TITLE)


# Одностраничник: ванильный JS, без внешних запросов (в Render-контейнере
# нет интернет-доступа к CDN из браузера пользователя — и не нужен).
_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#0f1420;--card:#171e2e;--line:#26304a;--txt:#e6ebf5;--muted:#8b96ad;
--acc:#4c8dff;--ok:#34c07a;--warn:#f0a73c;--bad:#e5534b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 18px;
background:var(--card);border-bottom:1px solid var(--line);
position:sticky;top:0;z-index:5}
header h1{font-size:17px;margin:0;font-weight:600}
header .st{margin-left:auto;font-size:13px;color:var(--muted)}
nav{display:flex;gap:6px;padding:10px 14px;overflow-x:auto;
border-bottom:1px solid var(--line);background:#121828}
nav button{background:none;border:1px solid transparent;color:var(--muted);
padding:7px 14px;border-radius:8px;cursor:pointer;font-size:14px;white-space:nowrap}
nav button.on{background:var(--card);color:var(--txt);border-color:var(--line)}
main{padding:16px;max-width:1100px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px;margin-bottom:14px}
.card h2{margin:0 0 12px;font-size:15px;font-weight:600}
.grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}
.kpi{background:#121828;border:1px solid var(--line);border-radius:10px;padding:12px}
.kpi .v{font-size:20px;font-weight:600}
.kpi .l{font-size:12px;color:var(--muted);margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);
vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12.5px}
tr:last-child td{border-bottom:none}
input,select{background:#121828;border:1px solid var(--line);color:var(--txt);
padding:8px 10px;border-radius:8px;font-size:14px;width:100%}
input:focus,select:focus{outline:none;border-color:var(--acc)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:end}
.row>div{flex:1;min-width:130px}
label{font-size:12.5px;color:var(--muted);display:block;margin-bottom:4px}
button.btn{background:var(--acc);border:none;color:#fff;padding:9px 16px;
border-radius:8px;cursor:pointer;font-size:14px}
button.btn.ghost{background:none;border:1px solid var(--line);color:var(--muted)}
button.btn:disabled{opacity:.5;cursor:default}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;
border:1px solid var(--line);color:var(--muted)}
.pill.ok{color:var(--ok);border-color:#2a5c45}
.pill.bad{color:var(--bad);border-color:#5c2a2a}
.pill.warn{color:var(--warn);border-color:#5c4a2a}
.pill.in{color:var(--ok)} .pill.out{color:var(--bad)}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.muted{color:var(--muted)} .small{font-size:12.5px}
#login{max-width:360px;margin:60px auto}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);
background:#1d2740;border:1px solid var(--line);padding:10px 18px;
border-radius:10px;opacity:0;transition:.3s;pointer-events:none;z-index:9}
#toast.show{opacity:1}
pre.logs{background:#0b0f18;border:1px solid var(--line);border-radius:10px;
padding:12px;font-size:12px;max-height:60vh;overflow:auto;white-space:pre-wrap}
.L-WARNING{color:var(--warn)} .L-ERROR{color:var(--bad)}
.L-CRITICAL{color:#ff7b72} .L-DEBUG{color:var(--muted)}
.hidden{display:none}
</style>
</head>
<body>
<header>
  <h1>⚙ Allira · Панель управления</h1>
  <span class="st" id="uptime">…</span>
</header>

<div id="login" class="card">
  <h2>Вход</h2>
  <p class="small muted">Токен из <code>ADMIN_PANEL_TOKEN</code> в .env сервера.</p>
  <div class="row"><div><label>Токен</label>
    <input type="password" id="token" autocomplete="current-password"></div></div>
  <p style="margin-top:14px"><button class="btn" onclick="saveToken()">Войти</button></p>
</div>

<div id="app" class="hidden">
<nav id="tabs">
  <button data-t="status" class="on">Статус</button>
  <button data-t="crypto">Криптоучёт</button>
  <button data-t="tx">Операции</button>
  <button data-t="recon">Сверка Дт/Кт</button>
  <button data-t="tax">Настройки ФНС</button>
  <button data-t="logs">Логи</button>
</nav>
<main>

<section id="t-status">
  <div class="card"><h2>Состояние</h2><div class="grid" id="kpis"></div></div>
  <div class="card"><h2>Фоновые задачи</h2><table id="jobs">
    <thead><tr><th>Задача</th><th>Следующий запуск</th></tr></thead><tbody></tbody>
  </table></div>
  <div class="card"><h2>Ключи</h2><div class="grid" id="cfgkeys"></div></div>
  <div class="card"><h2>Действия</h2>
    <div class="row">
      <div><button class="btn" onclick="action('sync/cex',this)">Синк бирж</button></div>
      <div><button class="btn" onclick="action('sync/jetton',this)">Скан TON</button></div>
      <div><button class="btn ghost" onclick="reloadStatus()">Обновить</button></div>
    </div>
    <p class="small muted" id="action-out"></p>
  </div>
</section>

<section id="t-crypto" class="hidden">
  <div class="card"><h2>Портфель (расчётные остатки)</h2>
    <table id="bal"><thead><tr><th>Актив</th><th>Количество</th>
    <th>Себестоимость, ₽</th></tr></thead><tbody></tbody></table></div>
  <div class="card"><h2>Кошельки</h2>
    <table id="wallets"><thead><tr><th>Сеть</th><th>Метка</th><th>Адрес</th>
    <th>Операций</th><th></th></tr></thead><tbody></tbody></table>
    <h2 style="margin-top:18px">Добавить кошелёк</h2>
    <div class="row">
      <div><label>Сеть</label><select id="w-chain">
        <option value="ton">ton</option><option value="evm">evm</option>
        <option value="btc">btc</option><option value="tron">tron</option>
        <option value="solana">solana</option><option value="other">other</option>
      </select></div>
      <div style="flex:3"><label>Адрес</label><input id="w-addr"></div>
      <div><label>Метка</label><input id="w-label"></div>
      <div><button class="btn" onclick="addWallet()">Добавить</button></div>
    </div>
  </div>
</section>

<section id="t-tx" class="hidden">
  <div class="card"><h2>Операции</h2>
    <div class="row">
      <div><label>Актив</label><input id="f-asset" placeholder="BTC"></div>
      <div><label>Тип</label><select id="f-type"><option value="">все</option></select></div>
      <div style="flex:0"><button class="btn ghost" onclick="loadTx(true)">Показать</button></div>
    </div>
    <table id="tx" style="margin-top:12px">
      <thead><tr><th>Дата</th><th>Тип</th><th>Актив</th><th>Кол-во</th>
      <th>₽</th><th>Источник</th><th>Комментарий</th><th></th></tr></thead>
      <tbody></tbody></table>
    <p><button class="btn ghost" onclick="moreTx()">Ещё 50</button></p>
  </div>
  <div class="card"><h2>Добавить операцию</h2>
    <div class="row">
      <div><label>Актив</label><input id="a-asset" placeholder="USDT"></div>
      <div><label>Тип</label><select id="a-type"></select></div>
      <div><label>Количество</label><input id="a-amount" placeholder="100"></div>
      <div><label>Дата (ГГГГ-ММ-ДД)</label><input id="a-date"></div>
    </div>
    <div class="row" style="margin-top:8px">
      <div><label>Сумма / себестоимость, ₽</label><input id="a-fiat"></div>
      <div style="flex:3"><label>Комментарий</label><input id="a-note"></div>
      <div><button class="btn" onclick="addTx()">Записать</button></div>
    </div>
  </div>
</section>

<section id="t-recon" class="hidden">
  <div class="card"><h2>Сверка дебет-кредит</h2>
    <div class="row">
      <div><label>Год</label><input id="r-year" placeholder="2026"></div>
      <div><button class="btn" onclick="runReconcile(this)">Сверить</button></div>
    </div>
    <p class="small muted">Парность проводок, отрицательные остатки и сверка
    расчётных балансов с TonAPI/Bybit/OKX. Длится до минуты.</p>
    <div id="recon-out"></div>
  </div>
</section>

<section id="t-tax" class="hidden">
  <div class="card"><h2>Настройки для 3-НДФЛ</h2>
    <div class="row">
      <div><label>ИНН</label><input id="t-inn"></div>
      <div style="flex:2"><label>ФИО</label><input id="t-name"></div>
    </div>
    <div class="row" style="margin-top:8px">
      <div><label>Статус</label><select id="t-status">
        <option value="resident">резидент</option>
        <option value="miner">майнер</option>
        <option value="nonresident">нерезидент</option></select></div>
      <div><label>Метод себестоимости</label><select id="t-method">
        <option value="fifo">FIFO</option><option value="wac">WAC</option></select></div>
      <div><button class="btn" onclick="saveTax()">Сохранить</button></div>
    </div>
  </div>
</section>

<section id="t-logs" class="hidden">
  <div class="card"><h2>Последние логи</h2>
    <div class="row">
      <div><label>Уровень</label><select id="l-level">
        <option value="">все</option><option>INFO</option>
        <option>WARNING,ERROR,CRITICAL</option></select></div>
      <div style="flex:0"><button class="btn ghost" onclick="loadLogs()">Обновить</button></div>
    </div>
    <pre class="logs" id="logbox" style="margin-top:12px"></pre>
  </div>
</section>

</main>
</div>

<div id="toast"></div>

<script>
const TYPES=["airdrop","mining","staking","reward","interest","gift_in","refund",
"income_other","sell","swap_out","spend","gift_out","sell_nft","disposal_other",
"transfer_in","transfer_out","buy","trade_in","trade_out"];
let TOKEN=localStorage.getItem("allira_token")||"";
let txOffset=0;

function toast(msg,err){const t=document.getElementById("toast");
t.textContent=msg;t.style.borderColor=err?"#5c2a2a":"#2a5c45";
t.classList.add("show");setTimeout(()=>t.classList.remove("show"),2600);}

async function api(path,opt={}){
opt.headers=Object.assign({"X-Admin-Token":TOKEN,"Content-Type":"application/json"},
opt.headers||{});
const r=await fetch(path,opt);
if(r.status===401){showLogin();throw new Error("unauthorized");}
const data=await r.json().catch(()=>({}));
if(!r.ok)throw new Error(data.error||r.statusText);
return data;
}
function saveToken(){TOKEN=document.getElementById("token").value.trim();
localStorage.setItem("allira_token",TOKEN);boot();}
function showLogin(){document.getElementById("login").classList.remove("hidden");
document.getElementById("app").classList.add("hidden");}
function showApp(){document.getElementById("login").classList.add("hidden");
document.getElementById("app").classList.remove("hidden");}

// ---- табы
document.getElementById("tabs").addEventListener("click",e=>{
const b=e.target.closest("button");if(!b)return;
document.querySelectorAll("#tabs button").forEach(x=>x.classList.remove("on"));
b.classList.add("on");
document.querySelectorAll("main>section").forEach(s=>s.classList.add("hidden"));
document.getElementById("t-"+b.dataset.t).classList.remove("hidden");
if(b.dataset.t==="crypto")loadCrypto();
if(b.dataset.t==="tx")loadTx(true);
if(b.dataset.t==="tax")loadTax();
if(b.dataset.t==="logs")loadLogs();
});

// ---- статус
async function reloadStatus(){
try{
const s=await api("/admin/api/status");
document.getElementById("uptime").textContent="аптайм "+s.uptime;
const c=s.crypto||{};
const k=[
 ["Аптайм",s.uptime],["Задач",(s.jobs||[]).length],
 ["БД",fmtSize((s.database||{}).size_bytes)],
 ["Операций",((s.database||{}).counts||{}).crypto_transactions],
 ["Кошельков",c.enabled?(c.wallets||[]).length:0],
 ["Сообщений",((s.database||{}).counts||{}).messages],
];
document.getElementById("kpis").innerHTML=k.map(x=>
`<div class="kpi"><div class="v">${esc(x[1]??"—")}</div><div class="l">${esc(x[0])}</div></div>`).join("");
document.getElementById("jobs").querySelector("tbody").innerHTML=
(s.jobs||[]).map(j=>`<tr><td class="mono">${esc(j.name)}</td><td>${
j.next_run_in_s==null?"—":sec(j.next_run_in_s)}</td></tr>`).join("")||
`<tr><td colspan=2 class=muted>нет задач</td></tr>`;
document.getElementById("cfgkeys").innerHTML=Object.entries(s.config||{}).map(([k,v])=>
`<div class="kpi"><div class="v">${v?'<span class="pill ok">да</span>':
'<span class="pill bad">нет</span>'}</div><div class="l">${k}</div></div>`).join("");
}catch(e){if(e.message!=="unauthorized")toast("Статус: "+e.message,true);}
}
async function action(name,btn){
btn.disabled=true;document.getElementById("action-out").textContent="Выполняю…";
try{const d=await api("/admin/api/"+name,{method:"POST"});
document.getElementById("action-out").textContent="Готово: "+JSON.stringify(d);
toast("Готово");reloadStatus();}
catch(e){document.getElementById("action-out").textContent="Ошибка: "+e.message;
toast(e.message,true);}
btn.disabled=false;
}

// ---- крипто
async function loadCrypto(){
try{
const s=await api("/admin/api/status"), c=s.crypto||{};
const bal=(c.balances||[]).map(b=>`<tr><td class=mono>${esc(b.symbol)}</td>
<td>${esc(b.qty)}</td><td>${(+b.cost_rub).toLocaleString("ru-RU")}</td></tr>`).join("");
document.querySelector("#bal tbody").innerHTML=bal||
'<tr><td colspan=3 class=muted>нет остатков</td></tr>';
const w=await api("/admin/api/wallets");
document.querySelector("#wallets tbody").innerHTML=(w.wallets||[]).map(x=>
`<tr><td>${esc(x.chain)}</td><td>${esc(x.label)}</td>
<td class="mono">${esc(x.address.slice(0,14))}…</td><td>${esc(x.tx_count)}</td>
<td><button class="btn ghost" onclick="toggleWallet(${parseInt(x.id,10)||0},${!x.active})">
${x.active?"выключить":"включить"}</button></td></tr>`).join("")||
'<tr><td colspan=5 class=muted>кошельков нет</td></tr>';
}catch(e){toast(e.message,true);}
}
async function addWallet(){
try{await api("/admin/api/wallets",{method:"POST",body:JSON.stringify({
chain:document.getElementById("w-chain").value,
address:document.getElementById("w-addr").value,
label:document.getElementById("w-label").value})});
toast("Кошелёк добавлен");loadCrypto();}
catch(e){toast(e.message,true);}
}
async function toggleWallet(id,active){
try{await api("/admin/api/wallets/toggle",{method:"POST",
body:JSON.stringify({id,active})});loadCrypto();}
catch(e){toast(e.message,true);}
}

// ---- операции
function fillTypes(){
const opts=TYPES.map(t=>`<option value="${t}">${t}</option>`).join("");
document.getElementById("a-type").innerHTML=opts;
document.getElementById("f-type").innerHTML='<option value="">все</option>'+opts;
}
async function loadTx(reset){
try{
if(reset){txOffset=0;document.querySelector("#tx tbody").innerHTML="";}
const f=document.getElementById("f-asset").value.trim();
const t=document.getElementById("f-type").value;
const d=await api(`/admin/api/transactions?limit=50&offset=${txOffset}`+
`&asset=${encodeURIComponent(f)}&type=${encodeURIComponent(t)}`);
const rows=(d.rows||[]).map(r=>`<tr><td class=mono>${esc(r.date)}</td>
<td><span class="pill ${esc(r.direction)}">${esc(r.type)}</span></td>
<td class=mono>${esc(r.asset)}</td><td>${esc(r.qty)}</td>
<td>${esc(r.fiat_rub||r.cost_rub||"—")}</td><td class=muted>${esc(r.source)}</td>
<td class="small muted">${esc(r.note)}</td>
<td><button class="btn ghost" onclick="delTx(${parseInt(r.id,10)||0})">✕</button></td></tr>`).join("");
document.querySelector("#tx tbody").insertAdjacentHTML("beforeend",rows||
(reset?'<tr><td colspan=8 class=muted>нет операций</td></tr>':""));
txOffset+=(d.rows||[]).length;
}catch(e){toast("Операции: "+e.message,true);}
}
function moreTx(){loadTx(false);}
async function addTx(){
try{await api("/admin/api/transactions",{method:"POST",body:JSON.stringify({
asset:document.getElementById("a-asset").value,
tx_type:document.getElementById("a-type").value,
amount:document.getElementById("a-amount").value,
date:document.getElementById("a-date").value,
fiat_rub:document.getElementById("a-fiat").value,
note:document.getElementById("a-note").value})});
toast("Операция записана");loadTx(true);}
catch(e){toast(e.message,true);}
}
async function delTx(id){
if(!confirm("Удалить операцию #"+id+"?"))return;
try{await api("/admin/api/transactions/delete",{method:"POST",
body:JSON.stringify({id})});loadTx(true);}
catch(e){toast(e.message,true);}
}

// ---- сверка
async function runReconcile(btn){
btn.disabled=true;const out=document.getElementById("recon-out");
out.innerHTML='<p class="muted">Сверяю… это занимает до минуты.</p>';
try{
const y=document.getElementById("r-year").value;
const d=await api("/admin/api/reconcile",{method:"POST",
body:JSON.stringify({year:y||new Date().getFullYear()})});
const issues=(d.issues||[]).map(i=>
`<tr><td><span class="pill bad">${esc(i.kind)}</span></td>
<td class="small">${esc(i.message)}</td></tr>`).join("");
const bal=(d.balances||[]).filter(b=>+b.qty!==0).map(b=>
`<tr><td class=mono>${esc(b.symbol)}</td><td>${b.qty}</td>
<td>${(+b.cost_rub).toLocaleString("ru-RU")}</td></tr>`).join("");
out.innerHTML=`<div class="grid" style="margin-top:12px">
<div class="kpi"><div class="v">${d.ok?'<span class="pill ok">чисто':
'<span class="pill bad">'+d.issues.length+' расхождений'}</span>'}</div>
<div class="l">Итог за ${d.year}</div></div>
<div class="kpi"><div class="v">${(+d.total_dr_rub).toLocaleString("ru-RU")}</div>
<div class="l">Σ Дт, ₽</div></div>
<div class="kpi"><div class="v">${(+d.total_cr_rub).toLocaleString("ru-RU")}</div>
<div class="l">Σ Кт, ₽</div></div>
<div class="kpi"><div class="v">${(d.entries||[]).length||d.entries||0}</div>
<div class="l">Проводок</div></div></div>
${issues?`<h2 style="margin-top:14px">Расхождения</h2>
<table>${issues}</table>`:""}
<h2 style="margin-top:14px">Остатки</h2>
<table><thead><tr><th>Актив</th><th>Кол-во</th><th>Себестоимость, ₽</th></tr></thead>
<tbody>${bal||'<tr><td colspan=3 class=muted>нет данных</td></tr>'}</tbody></table>`;
toast(d.ok?"Сверка чистая":"Расхождения найдены",!d.ok);
}catch(e){out.innerHTML='<p style="color:#e5534b">Ошибка: '+esc(e.message)+'</p>';}
btn.disabled=false;
}

// ---- налоги
async function loadTax(){
try{const t=await api("/admin/api/tax");
document.getElementById("t-inn").value=t.taxpayer_inn||"";
document.getElementById("t-name").value=t.taxpayer_name||"";
document.getElementById("t-status").value=t.tax_status||"resident";
document.getElementById("t-method").value=t.cost_basis||"fifo";}
catch(e){toast(e.message,true);}
}
async function saveTax(){
try{await api("/admin/api/tax",{method:"POST",body:JSON.stringify({
taxpayer_inn:document.getElementById("t-inn").value,
taxpayer_name:document.getElementById("t-name").value,
tax_status:document.getElementById("t-status").value,
cost_basis:document.getElementById("t-method").value})});
toast("Настройки сохранены");}
catch(e){toast(e.message,true);}
}

// ---- логи
async function loadLogs(){
try{const lvl=document.getElementById("l-level").value;
const d=await api(`/admin/api/logs?limit=250&level=${encodeURIComponent(lvl)}`);
document.getElementById("logbox").innerHTML=(d.lines||[]).map(l=>
`<span class="L-${l.level}">${esc(l.text)}</span>`).join("\n")||
"лог пуст";}
catch(e){toast(e.message,true);}
}

// ---- utils
function esc(s){return String(s??"").replace(/[&<>"]/g,
c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function sec(s){if(s<90)return s+" сек";if(s<5400)return Math.round(s/60)+" мин";
return Math.round(s/3600)+" ч";}
function fmtSize(b){if(!b)return "0 КБ";if(b<1048576)return (b/1024).toFixed(0)+" КБ";
return (b/1048576).toFixed(1)+" МБ";}

async function boot(){
if(!TOKEN){showLogin();return;}
try{await api("/admin/api/status");showApp();fillTypes();reloadStatus();
setInterval(reloadStatus,30000);}
catch(e){showLogin();}
}
boot();
</script>
</body>
</html>
"""
