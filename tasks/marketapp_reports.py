import asyncio
import json
import logging
import time
import httpx
from datetime import datetime, timedelta, timezone, time as dt_time
from telegram.ext import ContextTypes

from utils.database import (
    save_marketapp_profit, get_previous_profit, get_profit_for_period,
    save_blockchain_rent_events, get_sync_state, set_sync_state,
    enrich_blockchain_events, get_all_rent_events,
    get_linkage_feed, set_linkage_feed, get_rent_events_stats
)
from utils.config import BotConfig
from utils.common import normalize_ton_address
from utils.http_client import get_client, with_retry

logger = logging.getLogger(__name__)

MARKETAPP_API_URL = "https://api.marketapp.org"
TONCENTER_API_URL = "https://toncenter.com/api/v2"
TONAPI_API_URL = "https://tonapi.io/v2"
RENT_CATEGORIES = ["gifts", "usernames", "numbers"]
RENT_COMMENT_MARKERS = ("marketapp", "rent")

# Служебные пометки Marketapp: входящий перевод с таким комментарием — не
# платёж за аренду (13 штук на 0.2497 TON, по ~0.019 каждая, раздували бы
# «доход»). Список пополняется, когда в rent_comments (/health) всплывает
# новая подпись, у которой нет привязанных к NFT платежей.
NON_RENT_COMMENTS = ("rent settings has been updated",)


def is_rent_comment(comment: str | None) -> bool:
    """Комментарий транзакции означает платёж за аренду.

    Подстроки RENT_COMMENT_MARKERS ловят все виды аренды (чтобы не потерять
    платёж), NON_RENT_COMMENTS вычитает известные служебные сообщения
    (чтобы не раздувать доход).
    """
    text = (comment or "").lower()
    if not any(marker in text for marker in RENT_COMMENT_MARKERS):
        return False
    return not any(marker in text for marker in NON_RENT_COMMENTS)
MAX_PAGE_RETRIES = 3
# Потолок страниц на одну категорию за прогон: лента Marketapp общая,
# платформенная — чтобы дойти до старта окна блокчейна, нужно ~450 страниц.
MAX_HISTORY_PAGES = 1000
# Общий бюджет страниц на весь прогон привязки (≈25 мин при паузе 1.5 с —
# проверка в test_budget): держит джобу внутри её интервала (30 мин).
# Прогон, остановленный по бюджету, границу «непрочитанного» не двигает —
# глубина догоняется дальше.
LINKAGE_RUN_BUDGET = 1000
# Идёт ли прогон привязки: и джоба, и отчёт пишут состояние чтения ленты
# (курсор/границы) — параллельные читатели затёрли бы прогресс друг друга
# и удвоили бы нагрузку на API. Отчёт при занятости привязку пропускает.
LINKAGE_BUSY = {"running": False}
# Сколько страниц разрешено читать отчёту, когда привязки ещё нет вовсе:
# раньше отчёт упирался в полное чтение ленты (~20 мин ожидания).
LINKAGE_REPORT_BUDGET = 60
# Страховка инкрементального чтения: заново перечитываем сутки сверху —
# вдруг новые записи в их ленте встают не строго сверху.
LINKAGE_REREAD_S = 86400
# Потолок страниц для полного скана истории блокчейна (свежая база).
MAX_SYNC_PAGES = 1000
# «Лишнее» событие: глубже максимума своей страницы настолько, что это не
# может быть обычным порядком ленты. У tonapi попадались старые события с
# чужим lt/timestamp на свежей позиции: их lt уводил пагинацию (before_lt)
# в начало истории, скан закрывался пустой страницей как «конец», а
# чекпоинт писался по ним же — история обрезалась навсегда. Событие с таким
# разрывом не участвует в управлении сканом (пагинация/граница/чекпоинт);
# если оно действительно старое — страница его настоящего возраста вернёт.
OUTLIER_GAP_S = 90 * 86400
# Чекпоинт «глубже данных» только при разрыве сильнее этого: хвост истории
# без единого платежа — норма (у кошелька первые ~10 месяцев — вовсе без
# аренды), поэтому ложная тревога здесь дешевле, чем вечный инкремент по
# 3 страницы с обрезанной историей.
COHERENCY_GAP_S = 400 * 86400
# Лимит одной страницы в сканерах блокчейна: каждая лишняя страница — это
# пауза и запрос. Крупные страницы иногда приходят обрезанными (тело JSON
# рвётся) — их дробит защита внутри fetch_*.
# У tonapi потолок жёсткий и проверен напрямую: limit=500 → 400
# «value 500 greater than 100». Попытка читать больше сотни вырубала
# источник целиком (0 страниц, история не росла) — ставить только 100.
TONAPI_PAGE_LIMIT = 100
# TON Center 500 принял — страниц впятеро меньше, проверено на проде.
TONCENTER_PAGE_LIMIT = 500
# Пэйсинг чтения ленты Marketapp: их API ни разу не вернул 429, поэтому
# стартуем с 1.5с (было 3с), а на ошибке пауза удваивается до 8с — темп
# падает только тогда, когда есть с чем осторожничать.
LINKAGE_PAGE_DELAY = 1.5
LINKAGE_PAGE_DELAY_MAX = 8.0
# Лимит одной страницы их ленты: их потолок — 100 (500 отклоняется 422
# и уходил в http_error при каждом старте). Автооткат в fetch_rent_history
# остаётся страховкой, если старший лимит когда-нибудь станет валидным.
MARKETAPP_PAGE_LIMIT = 100
_history_limit = {"value": MARKETAPP_PAGE_LIMIT}
# Проба пустой категории: сколько записей истории читать вглубь, прежде чем
# признать, что платёжей этого кошелька тут нет, и не жечь дальше бюджет
# догона впустую (у категории с большим потоком чужих записей иное дно —
# окно блокчейна, но кап добирает и его). Кап считается в страницах под
# текущий лимит ленты, хранится в состоянии (probe_left) и тратится от
# прогона к прогону; первая же запись категории снимает его — категория
# с платежами дальше читается по общим правилам.
HISTORY_PROBE_ITEMS = 120_000
HISTORY_PROBE_STOP = "проба пустой категории исчерпана"

MSK = timezone(timedelta(hours=3))

# Последняя попытка привязать платежи к подаркам (collect/обогащение) —
# отдаётся в /health. Без неё «топ не построить» приходилось расшифровывать
# по логам: здесь сразу видно, была ли попытка, сколько записей собрано,
# сколько совпало и с какой ошибкой упала.
LAST_LINKAGE: dict = {
    "at": 0, "collected": 0, "matched": 0, "error": "",
    "categories": {}, "api_keys": [], "direction": {"in": 0, "out": 0},
    # Почему записей меньше, чем платежей в блокчейне: no_wallet — API вернул
    # чужие записи, no_price — записи без суммы, pages/stops — сколько страниц
    # прочитано и на какой причине остановились. Без этого «66 из 759»
    # нельзя отличить «у Marketapp просто мало истории» от «мы молча
    # отбрасываем».
    "pages": 0, "dropped": {"no_wallet": 0, "no_price": 0}, "stops": {},
    # До какой глубины ленты прочитано по категориям (top_ts — граница
    # «непрочитанного», depth_ts — самая старая прочитанная ts).
    "feed": {},
}

# Первый ответ rent/history за жизнь процесса (см. fetch_rent_history).
LAST_HISTORY_META: dict = {"envelope_keys": [], "items": 0, "has_cursor": False,
                           "sample_tx_hash": None, "page_limit": 0,
                           "sample_cursor": {"len": 0, "numeric": False}}

# Последние прогоны скана блокчейна (ключ — путь tonapi/toncenter) — для
# /health: сколько страниц пройдено, дошёл ли до конца истории, сколько
# сохранено и ошибка, если была. Без этого «в базе 698 событий и они не
# растут» приходилось объяснять вслепую: видно и обрыв, и ошибку, и то,
# что история дописана не до конца.
LAST_SYNC: dict = {}


def _record_sync(path: str, pages: int, complete: bool, saved: int, error: str = "",
                 via: str = "", page_ts: list | None = None, saved_ts_min: int = 0):
    prev = LAST_SYNC.get(path)
    LAST_SYNC[path] = {
        "at": time.time(),
        "pages": pages,
        "complete": complete,
        "saved": saved,
        "error": error,
        # Чем закончился именно этот прогон: «конец истории», «граница»,
        # «хвост» или «лимит страниц». Без этого complete=true не отличает
        # настоящий конец от ложного — инцидент с обрезанной историей так
        # и выглядел: complete=true, а страниц прочитано 13.
        "via": via,
        # ts-диапазон [старый, новый] последней прочитанной страницы: где
        # чтение физически остановилось, даже если чекпоинт врёт о глубине.
        "page_ts": page_ts or [],
        # Самое старое событие, сохранённое этим прогоном: разрыв с page_ts
        # показывает, что из прочитанного не дошло до базы.
        "saved_ts_min": saved_ts_min,
        # Прогон до этого: без него непонятно, что дал ПОЛНЫЙ скан (первый
        # после деплоя), — его запись затирается следующим, инкрементальным.
        "prev": None if not prev else {
            k: prev[k] for k in ("at", "pages", "complete", "saved")
        },
    }


# Последняя ошибка сетевого слоя (запрос + причина) — подмешивается в
# ошибку скана для /health: без неё «TON Center недоступен» не отличает
# rate limit от 500-го и не говорит, на какой странице скан встал.
LAST_HTTP_ERROR: dict = {"at": 0, "text": ""}


def _note_http_error(text: str):
    LAST_HTTP_ERROR.update({"at": time.time(), "text": text[:300]})


def record_linkage(collected: int | None = None, matched: int | None = None,
                   error: str = "", categories: dict | None = None,
                   api_keys: list | None = None,
                   direction: dict | None = None,
                   pages: int | None = None,
                   dropped: dict | None = None,
                   stops: dict | None = None,
                   feed: dict | None = None) -> None:
    if collected is not None:
        LAST_LINKAGE["collected"] = int(collected)
    if matched is not None:
        LAST_LINKAGE["matched"] = int(matched)
    if categories is not None:
        LAST_LINKAGE["categories"] = dict(categories)
    if api_keys is not None:
        LAST_LINKAGE["api_keys"] = list(api_keys)
    if direction is not None:
        LAST_LINKAGE["direction"] = dict(direction)
    if pages is not None:
        LAST_LINKAGE["pages"] = int(pages)
    if dropped is not None:
        LAST_LINKAGE["dropped"] = dict(dropped)
    if stops is not None:
        LAST_LINKAGE["stops"] = dict(stops)
    if feed is not None:
        LAST_LINKAGE["feed"] = dict(feed)
    LAST_LINKAGE["error"] = (error or "")[:300]
    LAST_LINKAGE["at"] = int(time.time())


def record_price_nano(item: dict) -> str:
    """Сумма платежа из записи Marketapp в нано — с фолбэком на price.

    API отдаёт и price_nano, и price (в TON); если нано-поле пустое или ноль,
    запись раньше молча выбрасывалась, и платеж оставался «не привязанным»,
    хотя данные для привязки были.
    """
    raw = str(item.get("price_nano") or "").strip()
    try:
        nano = int(float(raw)) if raw else 0
    except (TypeError, ValueError):
        nano = 0
    if nano > 0:
        return str(nano)
    try:
        price = float(item.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    return str(int(price * 1_000_000_000)) if price > 0 else ""


def _format_ton(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    return f"{value:.2f}"


def _nano_to_ton(nano: str) -> float:
    return int(nano) / 1_000_000_000


def _ts_now() -> int:
    return int(datetime.now(MSK).timestamp())


def _ts_days_ago(days: int) -> int:
    return int((datetime.now(MSK) - timedelta(days=days)).timestamp())


def userfriendly_to_raw(addr: str) -> str:
    """EQ/UQ -> 0:hex.

    Делегирует единому нормализатору из utils.common: раньше эта же логика
    была продублирована ещё и в database._normalize_addr.
    """
    return normalize_ton_address(addr)


async def _request_page(
    label: str,
    do_request,
    *,
    base_delay: float = 2.0,
    grow_backoff: bool = False,
):
    """Общий ретрай одной страницы внешнего API.

    Возвращает распарсенный JSON либо None, если попытки исчерпаны.
    Раньше эта же конструкция (сеть / 429 / не-200 / битый JSON) была
    продублирована четырежды: fetch_toncenter_txns, fetch_tonapi_events,
    страница транзакций и история Marketapp — каждая со своими правками.
    """
    client = await get_client()
    backoff = base_delay

    def _bump():
        nonlocal backoff
        if grow_backoff:
            backoff = min(backoff * 2, 30.0)

    for attempt in range(MAX_PAGE_RETRIES):
        try:
            response = await do_request(client)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            logger.warning(f"{label} сеть (попытка {attempt + 1}/{MAX_PAGE_RETRIES}): {e}")
            _note_http_error(f"{label}: сеть — {e}")
            await asyncio.sleep(backoff)
            _bump()
            continue

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "5"))
            wait = max(retry_after, backoff) if grow_backoff else retry_after
            logger.warning(f"{label} 429 (попытка {attempt + 1}/{MAX_PAGE_RETRIES}), ожидание {wait}с...")
            _note_http_error(f"{label}: 429, ждём {wait}с")
            await asyncio.sleep(wait)
            _bump()
            continue

        if response.status_code != 200:
            logger.error(f"{label} статус {response.status_code}: {response.text[:200]}")
            _note_http_error(f"{label}: статус {response.status_code}")
            await asyncio.sleep(backoff)
            continue

        try:
            return response.json()
        except Exception:
            # TON Center отдаёт строки с сырыми управляющими символами
            # (переносы внутри комментариев/метаданных): тело при этом
            # полностью валидно и закрыто, но строгий json их не терпит —
            # из-за одного такого символа скан падал на конкретной странице
            # и история больше никогда не уходила глубже. Разбираем мягко.
            try:
                return json.loads(response.text, strict=False)
            except Exception:
                pass
            # Начало И конец тела: по хвосту видно, что ответ обрезан на
            # середине (тонкий gateway), а не отдан битым намеренно.
            text = response.text
            logger.error(f"{label}: невалидный JSON len={len(text)}: "
                         f"{text[:60]!r} … {text[-60:]!r}")
            _note_http_error(f"{label}: невалидный JSON len={len(text)}: "
                             f"{text[:60]!r} … {text[-40:]!r}")
            await asyncio.sleep(backoff)
            continue
    # Конкретную причину (429 / статус / сеть / JSON) не затираем — только
    # помечаем, что попытки кончились: по ней в /health видно, что именно
    # остановило скан.
    if LAST_HTTP_ERROR["text"]:
        LAST_HTTP_ERROR["text"] = f"{LAST_HTTP_ERROR['text']} (попытки исчерпаны)"
    else:
        _note_http_error(f"{label}: попытки исчерпаны")
    return None


@with_retry(max_retries=2, base_delay=5.0)
async def fetch_toncenter_txns(address: str, limit: int = 100, lt: str = None, hash_val: str = None,
                               api_key: str = None) -> list | None:
    params = {"address": address, "limit": limit}
    if lt and hash_val:
        params["lt"] = lt
        params["hash"] = hash_val
    headers = {"User-Agent": "AlliraBot/1.0"}
    if api_key:
        headers["X-API-Key"] = api_key

    async def do(client):
        return await client.get(
            f"{TONCENTER_API_URL}/getTransactions",
            params=params,
            headers=headers,
            timeout=20.0
        )

    # Страницу глубокой истории TON Center отдаёт обрезанной: тело обрывается
    # на середине, JSON не парсится, и скан вставал намертво. Дробим: сначала
    # просятся страницы меньшего размера, пока не влезут. Ошибка не про размер
    # (сеть/429/статус) — дробить бессмысленно, выходим сразу.
    for split_limit in (limit, max(25, limit // 4), 10):
        if params["limit"] != split_limit:
            params["limit"] = split_limit
            logger.warning(f"TON Center: страница не влезла — пробую {split_limit} транзакций")
        data = await _request_page("TON Center", do)
        if data is None:
            if "невалидный JSON" not in LAST_HTTP_ERROR.get("text", ""):
                return None
            continue
        break
    else:
        return None

    if not data.get("ok"):
        logger.error(f"TON Center API: {data}")
        # Причина уходит в /health: ok=false у TON Center — это обычно и есть
        # rate limit либо отклонённый запрос, а не сетевая недоступность.
        _note_http_error(f"TON Center: ok=false — {data.get('reason') or data}")
        return None
    return data.get("result", [])


@with_retry(max_retries=2, base_delay=3.0)
async def fetch_tonapi_events(address: str, before_lt: str = None, limit: int = None,
                              api_key: str = None) -> list | None:
    params = {"limit": limit or TONAPI_PAGE_LIMIT}
    if before_lt:
        params["before_lt"] = before_lt
    headers = {"User-Agent": "AlliraBot/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async def do(client):
        return await client.get(
            f"{TONAPI_API_URL}/accounts/{address}/events",
            params=params,
            headers=headers,
            timeout=20.0
        )

    # Как у TON Center: крупная страница может прийти обрезанной (тело JSON
    # рвётся) — тогда пробуем меньшую. Ошибка не про размер (сеть/429/
    # статус) — дробить бессмысленно, выходим сразу.
    for split_limit in (params["limit"], max(25, params["limit"] // 4), 10):
        if params["limit"] != split_limit:
            params["limit"] = split_limit
            logger.warning(f"tonapi: страница не влезла — пробую {split_limit} событий")
        data = await _request_page("tonapi", do)
        if data is None:
            if "невалидный JSON" not in LAST_HTTP_ERROR.get("text", ""):
                return None
            continue
        break
    else:
        return None

    events = data.get("events") or []
    logger.info(f"tonapi: страница перед lt={before_lt or 'begin'} — служебных событий {len(events)}")
    return events


def _tonapi_extract_rent(event: dict, raw_wallet: str) -> dict | None:
    for action in event.get("actions", []):
        if action.get("type") != "TonTransfer":
            continue
        tt = action.get("TonTransfer", {})
        comment = tt.get("comment", "") or ""
        if not is_rent_comment(comment):
            continue
        recipient = tt.get("recipient", {}) or {}
        dst = recipient.get("address", "")
        if not dst or userfriendly_to_raw(dst) != raw_wallet:
            continue
        amount = int(tt.get("amount", 0) or 0)
        if amount <= 0:
            continue
        sender = tt.get("sender", {}) or {}
        if comment:
            logger.debug(f"[rent comment] {comment!r}")
        return {
            "tx_hash": event.get("event_id", ""),
            "ts": int(event.get("timestamp", 0) or 0),
            "src": sender.get("address", ""),
            "dst": dst,
            "value_nano": str(amount),
            "comment": comment[:200],
        }
    return None


@with_retry(max_retries=2, base_delay=3.0)
async def _sync_from_tonapi(wallet: str, max_pages: int = 50, from_scratch: bool = False) -> tuple[int, bool]:
    raw_wallet = userfriendly_to_raw(wallet)
    api_key = BotConfig.from_env().tonapi_api_key
    logger.info(f"[tonapi sync] wallet={wallet} raw={raw_wallet} api_key={bool(api_key)} from_scratch={from_scratch} max_pages={max_pages}")

    # Граница нужна и для решения «писать ли чекпоинт»: tonapi пишет свой
    # event-lt, TON Center — tx-lt, пространства разные. Поэтому чекпоинт
    # ставит только тот, у кого его не было (TON Center гонится последним
    # и перекрывает), иначе каждый прогон TON Center заново прочёсывает
    # всю историю.
    sync_state = await get_sync_state(wallet)
    had_state = bool(sync_state and sync_state.get("last_synced_lt"))

    boundary = None
    if not from_scratch and had_state:
        try:
            boundary = int(sync_state["last_synced_lt"])
        except (TypeError, ValueError):
            boundary = None

    before_lt = None
    new_events = []
    pages = 0
    scan_complete = False
    deep_lt = None
    deep_utime = 0
    via = ""           # чем закончился прогон — для /health
    last_page_ts: list = []  # ts-диапазон последней прочитанной страницы

    while pages < max_pages and not scan_complete:
        if pages > 0:
            await asyncio.sleep(4.2 if not api_key else 0.25)

        events = await fetch_tonapi_events(wallet, before_lt=before_lt, api_key=api_key)
        if not events:
            if events is None:
                saved = await save_blockchain_rent_events(new_events, wallet) if new_events else 0
                reason = ("страница не вернулась; " + LAST_HTTP_ERROR["text"]
                          if LAST_HTTP_ERROR["text"] else "страница не вернулась")
                _record_sync("tonapi", pages, False, saved, reason,
                             via="ошибка", page_ts=last_page_ts)
                return 0, False
            via = "конец истории (пустая страница)"
            scan_complete = True
            break

        page_ts_vals = [
            t for t in (int(ev.get("timestamp", 0) or 0) for ev in events) if t
        ]
        page_max_ts = max(page_ts_vals, default=0)
        if page_ts_vals:
            last_page_ts = [min(page_ts_vals), page_max_ts]
        for ev in events:
            try:
                lt = int(ev.get("lt", 0) or 0)
            except (TypeError, ValueError):
                lt = 0
            ts = int(ev.get("timestamp", 0) or 0)
            # «Лишнее» старое событие на свежей странице: его lt/timestamp
            # не должны двигать ни пагинацию, ни границу, ни чекпоинт —
            # иначе before_lt прыгает в начало истории, скан «завершается»
            # пустой страницей, а глубина чекпоинта отражает выдумку, а не
            # прочитанные данные (см. OUTLIER_GAP_S).
            is_outlier = bool(page_max_ts and ts and page_max_ts - ts > OUTLIER_GAP_S)
            if boundary is not None and lt <= boundary and not is_outlier:
                deep_lt = boundary
                deep_utime = ts
                via = "граница чекпоинта"
                scan_complete = True
                break
            rent = _tonapi_extract_rent(ev, raw_wallet)
            if rent:
                new_events.append(rent)
            if not is_outlier:
                deep_lt = lt
                deep_utime = ts

        if scan_complete:
            break
        if deep_lt is None:
            via = "страница без событий с lt"
            break
        before_lt = str(deep_lt)
        pages += 1

    if new_events:
        saved = await save_blockchain_rent_events(new_events, wallet)
        logger.info(f"Блокчейн (tonapi): сохранено {saved} событий аренды")
    else:
        saved = 0
        logger.warning(f"[tonapi sync] найденных событий аренды: 0 (страниц перебрано: {pages}, scan_complete={scan_complete})")

    # Чекпоинт ставим по фактически пройденной границе — но только если его
    # не было вовсе: иначе event-lt tonapi затирает tx-lt TON Center, и тот
    # на каждом прогоне заново прочёсывает всю историю. Раньше прогон с
    # max_pages=3 границу не писал, и следующий начинал с той же точки.
    if deep_lt is not None and deep_utime and not had_state:
        await set_sync_state(wallet, str(deep_lt), "", deep_utime)

    if not via:
        via = "лимит страниц"
    saved_ts_min = min((int(e.get("ts", 0) or 0) for e in new_events), default=0)
    _record_sync("tonapi", pages, scan_complete, saved, via=via,
                 page_ts=last_page_ts, saved_ts_min=saved_ts_min)
    return saved, True


async def sync_rent_from_blockchain(wallet: str, max_pages: int = 50, from_scratch: bool = False) -> int:
    """Скан истории аренды: гоним ОБА источника и сливаем результат.

    TON Center отвечает транзакциями с полным текстом комментария и даёт
    заметно более полную базу (1847 событий против 698 у tonapi, у которого
    часть комментариев не доходит и глубина истории ограничена). tonapi
    идёт первым — TON Center пишет чекпоинт последним (tx-lt), и следующие
    прогоны обходятся без полного прочёсывания. Дублей нет: дедуп по
    tx_hash и по ts±300 + src/dst. Упавший источник не отменяет второй.
    """
    logger.info(f"[sync] старт wallet={wallet} max_pages={max_pages} from_scratch={from_scratch}")
    saved = 0
    try:
        saved += (await _sync_from_tonapi(wallet, max_pages, from_scratch))[0]
    except Exception as e:
        logger.warning(f"[sync] tonapi упал: {e}", exc_info=True)
        _record_sync("tonapi", 0, False, 0, f"{type(e).__name__}: {e}"[:200])
    try:
        saved += await _sync_from_toncenter(wallet, max_pages, from_scratch)
    except Exception as e:
        logger.warning(f"[sync] TON Center упал: {e}", exc_info=True)
        _record_sync("toncenter", 0, False, 0, f"{type(e).__name__}: {e}"[:200])
    logger.info(f"[sync] завершён: saved={saved}")
    return saved


async def _sync_from_toncenter(wallet: str, max_pages: int = 50, from_scratch: bool = False) -> int:
    raw_wallet = userfriendly_to_raw(wallet)
    api_key = BotConfig.from_env().toncenter_api_key

    boundary_lt = None
    boundary_hash = None
    start_lt = None
    start_hash = None
    if not from_scratch:
        sync_state = await get_sync_state(wallet)
        if sync_state:
            prev = LAST_SYNC.get("toncenter") or {}
            if prev and not prev.get("complete"):
                # Прошлый прогон оборвался (rate limit и т.п.): граница — не
                # потолок, а точка старта. Идём от неё ГЛУБЖЕ: иначе каждая
                # попытка начинала бы сначала, упиралась в ту же ошибку и
                # история оставалась бы оборванной навсегда.
                start_lt = sync_state.get("last_synced_lt")
                start_hash = sync_state.get("last_synced_hash")
                if not start_hash:
                    # Границу записал tonapi (он пишет пустой hash) — TON
                    # Center от неё пагинировать не может: без hash запрос
                    # уходит битым. Идём с начала истории, дублей не будет
                    # (дедуп по tx_hash и по ts±300 + src/dst).
                    start_lt = start_hash = None
            else:
                # История дописана до конца: граница — точка останова, всё
                # что новее, уже просканировано.
                boundary_lt = sync_state.get("last_synced_lt")
                boundary_hash = sync_state.get("last_synced_hash")

    cur_lt = start_lt
    cur_hash = start_hash
    new_events = []
    pages = 0
    scan_complete = False
    scan_error = ""
    deep_lt = None
    deep_hash = None
    deep_utime = 0
    via = ""           # чем закончился прогон — для /health
    last_page_ts: list = []  # ts-диапазон последней прочитанной страницы

    while pages < max_pages and not scan_complete:
        if pages > 0:
            # TON Center без ключа ограничен 1 запросом в секунду; даже с
            # ключом гонка 0.3с/страница рвалась на ~19-й странице. Полный
            # скан при 1с — это ~20 секунд, переплата несущественна.
            await asyncio.sleep(1.0)

        # fetch_toncenter_txns уже ретраит страницу внутри себя — раньше здесь
        # стоял второй такой же цикл, и одна страница могла породить до 9 запросов.
        items = await fetch_toncenter_txns(wallet, limit=TONCENTER_PAGE_LIMIT, lt=cur_lt, hash_val=cur_hash, api_key=api_key)

        if items is None:
            logger.error("TON Center недоступен — история синхронизирована не полностью")
            scan_error = "TON Center недоступен"
            via = "ошибка"
            break

        if not items:
            via = "конец истории (пустая страница)"
            scan_complete = True
            break

        item_ts_vals = [it.get("utime", 0) for it in items if it.get("utime")]
        if item_ts_vals:
            last_page_ts = [min(item_ts_vals), max(item_ts_vals)]

        found_boundary = False
        page_cursor_lt = None
        page_cursor_hash = None
        page_last_utime = 0
        for item in items:
            tx_id = item.get("transaction_id", {})
            tx_hash = tx_id.get("hash", "")
            tx_lt = tx_id.get("lt", "")
            page_cursor_lt = tx_lt
            page_cursor_hash = tx_hash
            page_last_utime = item.get("utime", 0)

            # TON Center v2 включает в ответ транзакцию-курсор (пересечение страниц)
            if cur_lt is not None and tx_lt == cur_lt and tx_hash == cur_hash:
                continue

            if boundary_lt is not None and tx_lt == boundary_lt and tx_hash == boundary_hash:
                deep_lt = tx_lt
                deep_hash = tx_hash
                deep_utime = page_last_utime
                found_boundary = True
                break

            in_msg = item.get("in_msg", {})
            dest = in_msg.get("destination", "")
            if userfriendly_to_raw(dest) != raw_wallet:
                continue

            message = in_msg.get("message", "")
            if not is_rent_comment(message):
                continue

            utime = item.get("utime", 0)
            value = int(in_msg.get("value", 0))
            if value <= 0:
                continue

            new_events.append({
                "tx_hash": tx_hash,
                "ts": utime,
                "src": in_msg.get("source", ""),
                "dst": dest,
                "value_nano": str(value),
                "comment": message[:200],
            })

        if found_boundary:
            via = "граница чекпоинта"
            scan_complete = True
            break

        # TON Center v2 на «хвосте» истории возвращает курсорную транзакцию самой
        # (lt/hash совпадают с запрошенным) — значит, старше ничего нет
        if cur_lt is not None and page_cursor_lt == cur_lt and page_cursor_hash == cur_hash:
            via = "хвост истории (курсор не сдвинулся)"
            scan_complete = True
            break

        if not page_cursor_lt or not page_cursor_hash:
            scan_error = "история кончилась без явного конца (нет курсора)"
            via = "ошибка"
            break

        cur_lt, cur_hash = page_cursor_lt, page_cursor_hash
        deep_lt = cur_lt
        deep_hash = cur_hash
        deep_utime = page_last_utime
        pages += 1

    # Чекпоинт ставим по фактически пройденной границе — даже если скан
    # оборвался (иначе каждая попытка начинала бы с начала и вечно упиралась
    # в ту же ошибку). Но граница может ТОЛЬКО углубляться: инкрементальный
    # прогон, не дошедший до старой границы, выдавал бы мелкую точку (3
    # страницы от новейшей транзакции) — глубокая история при этом терялась.
    if deep_lt and deep_hash:
        state = await get_sync_state(wallet)
        stored_utime = int((state or {}).get("last_synced_utime") or 0)
        if not state or not stored_utime or int(deep_utime or 0) < stored_utime:
            await set_sync_state(wallet, deep_lt, deep_hash, deep_utime)

    saved = 0
    if new_events:
        saved = await save_blockchain_rent_events(new_events, wallet)
        logger.info(f"Блокчейн: сохранено {saved} событий аренды")
    if scan_error and LAST_HTTP_ERROR["text"]:
        scan_error = f"{scan_error} ({LAST_HTTP_ERROR['text']})"
    if not via:
        via = "лимит страниц"
    saved_ts_min = min((int(e.get("ts", 0) or 0) for e in new_events), default=0)
    _record_sync("toncenter", pages, scan_complete, saved, scan_error,
                 via=via, page_ts=last_page_ts, saved_ts_min=saved_ts_min)
    return saved


async def sync_blockchain_rent_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        wallet = context.bot_data.get("MARKETAPP_WALLET", "")
        if not wallet:
            return
        # Три режима:
        #  * свежая база (деплой на бесплатном Render стирает диск) — полный
        #    синк с нуля;
        #  * история дописана не до конца (прошлый прогон оборвался по rate
        #    limit) — продолжаем с границы, но с большим лимитом страниц:
        #    иначе оборванный скан оставался бы оборванным навсегда;
        #  * всё синхронизировано — только новые транзакции (3 страницы).
        sync_state = await get_sync_state(wallet)
        prev = LAST_SYNC.get("toncenter") or {}
        if not sync_state:
            await sync_rent_from_blockchain(wallet, max_pages=MAX_SYNC_PAGES, from_scratch=True)
        elif prev.get("complete") is False:
            await sync_rent_from_blockchain(wallet, max_pages=MAX_SYNC_PAGES, from_scratch=False)
        else:
            # Чекпоинт может врать о глубине: страница с «лишним» старым
            # событием уводила пагинацию в начало истории, скан закрывался
            # пустой страницей как «конец», а чекпоинт писался по нему же.
            # Тогда история висела обрезанной навсегда: инкремент читал
            # 3 страницы у верха и никогда не возвращался вглубь. Если база
            # отстаёт от чекпоинта сильнее, чем на COHERENCY_GAP_S, — прогон
            # идёт с границы и дочитывает недостающее, вместо «тихих» 3
            # страниц.
            oldest = (await get_rent_events_stats()).get("oldest_ts") or 0
            state_utime = int(sync_state.get("last_synced_utime") or 0)
            if oldest and state_utime and oldest > state_utime + COHERENCY_GAP_S:
                logger.info(
                    f"[sync] история не сходится: база от {oldest}, чекпоинт от "
                    f"{state_utime} — догоняю с границы"
                )
                await sync_rent_from_blockchain(wallet, max_pages=MAX_SYNC_PAGES, from_scratch=False)
            else:
                await sync_rent_from_blockchain(wallet, max_pages=3, from_scratch=False)
    except Exception as e:
        logger.error(f"Ошибка синхронизации блокчейна: {e}", exc_info=True)


@with_retry(max_retries=2, base_delay=5.0)
async def fetch_rent_history(api_token: str, category: str, limit: int = None,
                             cursor: str = None) -> tuple[list | None, str | None]:
    limit = _history_limit["value"] if limit is None else limit
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    headers = {
        "Authorization": api_token,
        "User-Agent": "AlliraBot/1.0"
    }

    async def do(client):
        return await client.get(
            f"{MARKETAPP_API_URL}/v1/rent/{category}/history/",
            params=params,
            headers=headers,
            timeout=15.0
        )

    data = await _request_page(
        f"Marketapp ({category}/history)", do, base_delay=3.0, grow_backoff=True
    )
    if data is None and limit > 100 and _history_limit["value"] == limit:
        # Крупную страницу отклонили (обрыв тела, 4xx на limit) — откатываемся
        # на сотню и запоминаем, чтобы дальше не долбить. Ошибка не про
        # размер (сеть/429) — лимит не трогаем, его лечит ретрай.
        text = LAST_HTTP_ERROR.get("text", "")
        if any(s in text for s in ("невалидный JSON", " 400", " 413", " 422")):
            logger.warning(f"Marketapp: страница из {limit} записей не принята — откат на 100")
            _history_limit["value"] = limit = 100
            params["limit"] = 100
            data = await _request_page(
                f"Marketapp ({category}/history)", do, base_delay=3.0, grow_backoff=True
            )
    if data is None:
        return None, None

    LAST_HISTORY_META["page_limit"] = limit
    items = data.get("items", [])
    next_cursor = data.get("cursor") or data.get("next_cursor") or data.get("next_cursor_url") or None
    # Метаданные ПЕРВОГО ответа за жизнь процесса — чтобы по /health видно
    # было, не обрываем ли мы пагинацию из-за незнакомого ключа курсора:
    # 68 записей при 1899 платежах могли значить и «API больше не отдаёт»,
    # и «мы читаем не тот ключ». По ключам конверта это отличается.
    if not LAST_HISTORY_META["envelope_keys"]:
        LAST_HISTORY_META.update({
            "envelope_keys": sorted(data.keys()),
            "items": len(items),
            "has_cursor": bool(next_cursor),
            # Образец их tx_hash: по нему видно, отдаёт ли API настоящий
            # хеш транзакции (hex/base64) или внутренний идентификатор —
            # от этого зависит, сработает ли привязка «по хешу».
            "sample_tx_hash": (items[0].get("tx_hash") if items else None),
            # Формат курсора (без самого значения — он может быть подписью):
            # по числовому/длинному виду видно, возобновится ли чтение с
            # сохранённого места в следующем прогоне.
            "sample_cursor": {
                "len": len(str(next_cursor)) if next_cursor else 0,
                "numeric": str(next_cursor).isdigit() if next_cursor else False,
            },
        })
    return items, next_cursor


async def fetch_income_for_period(api_token: str, since_ts: int, wallet: str = "") -> float:
    total_ton = 0.0
    raw_wallet = userfriendly_to_raw(wallet)

    for i, category in enumerate(RENT_CATEGORIES):
        if i > 0:
            await asyncio.sleep(2)
        cursor = None
        for page in range(MAX_HISTORY_PAGES):
            if page > 0:
                await asyncio.sleep(LINKAGE_PAGE_DELAY)
            items, next_cursor = await fetch_rent_history(api_token, category, cursor=cursor)
            if not items:
                break
            stop = False
            for item in items:
                ts = item.get("ts", 0)
                if ts < since_ts:
                    stop = True
                    break
                if raw_wallet and userfriendly_to_raw(item.get("src", "")) != raw_wallet and userfriendly_to_raw(item.get("dst", "")) != raw_wallet:
                    continue
                total_ton += _nano_to_ton(record_price_nano(item) or "0")
            if stop or not next_cursor:
                break
            cursor = next_cursor

    return total_ton


async def fetch_my_rented(api_token: str) -> list | None:
    try:
        client = await get_client()
        response = await client.get(
            f"{MARKETAPP_API_URL}/v1/rent/my-rented/",
            headers={
                "Authorization": api_token,
                "User-Agent": "AlliraBot/1.0"
            },
            timeout=15.0
        )
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "10"))
            logger.warning(f"Marketapp API 429 (my-rented), ожидание {retry_after}с...")
            await asyncio.sleep(retry_after)
            return None
        response.raise_for_status()
        data = response.json()
        return data.get("items", [])
    except Exception as e:
        logger.error(f"Marketapp API ошибка (my-rented): {e}")
        return None


async def collect_rent_events(api_token: str, wallet: str,
                              page_budget: int | None = None) -> int:
    """Читает ленту истории Marketapp и привязывает записи к платежам.

    Их лента общая, платформенная (~100 записей на страницу и ~38 страниц
    в сутки), а окно блокчейна теперь в год — порядка 14000 страниц.
    Состояние каждого хранится в linkage_feed_state, каждая категория
    читается двумя фазами, сначала верх всех категорий, потом догон:
      * верх ленты — до границы прошлого прогона, чтобы свежие платежи
        привязывались сразу, не дожидаясь догона глубины;
      * догон глубины — возобновление с сохранённого курсора deep_cursor:
        без него каждый прогон стартовал бы с верха, упирался в бюджет
        и не продвигался вглубь — ровно так привязка зависла на 500
        страниц за прогон;
      * флаг done (окно достигнуто) — остаётся только верх, это инкремент;
      * отчёт передаёт свой маленький бюджет: верх прочитается целиком,
        глубина — по остатку.
    """
    raw_wallet = userfriendly_to_raw(wallet)
    collected = []
    by_cat: dict[str, int] = {}  # сколько записей дал каждая категория — в /health
    api_keys: list[str] | None = None  # какие поля реально отдаёт API — в /health
    direction = {"in": 0, "out": 0}  # записи, где кошелёк получает / платит
    # Диагностика полноты выгрузки (см. LAST_LINKAGE): страницы прочитаны,
    # записи отброшены и причина остановки пагинации по каждой категории.
    pages_read = 0
    dropped = {"no_wallet": 0, "no_price": 0}
    stops: dict[str, str] = {}
    budget = LINKAGE_RUN_BUDGET if page_budget is None else max(int(page_budget), 1)

    known = await get_all_rent_events(wallet)
    min_ts = min((int(e.get("ts", 0) or 0) for e in known), default=0)
    logger.info(
        f"marketapp: блокчейн-событий по кошельку={len(known)}, min_ts={min_ts}, "
        f"бюджет страниц={budget}"
    )
    if not known:
        # Обогащение сопоставляет записи API с блокчейн-событиями: без
        # событий глубокое чтение ленты — чистая трата запросов. После
        # каждого деплоя база пуста, а джоба привязки стартует раньше синка.
        stops = {c: "нет блокчейн-событий" for c in RENT_CATEGORIES}
        record_linkage(collected=0, matched=0,
                       categories={c: 0 for c in RENT_CATEGORIES},
                       api_keys=[], direction=direction,
                       pages=0, dropped=dropped, stops=stops, feed={})
        return 0

    # Границы чтения по категориям: top_ts — ts самой новой записи прошлого
    # прогона (глубже уже прочитано), depth_ts — как глубоко дошли вообще,
    # deep_cursor — курсор возобновления догона, done — окно достигнуто.
    feed = await get_linkage_feed(wallet)
    window_start = min_ts - 604800  # за 7 дней до самого старого платежа

    # Каждая категория читается двумя фазами: (1) свежий верх — до границы
    # прошлого прогона, чтобы новые платежи привязывались сразу; (2) догон
    # глубины — с сохранённого курсора, без которого каждый прогон
    # стартовал бы с верха и не продвигался вглубь (ровно так привязка
    # зависла на 500 страниц за прогон). Фаза 2 добавляется, только если
    # окно ещё не добрано и курсор есть. Сначала верх всех категорий,
    # потом догон: свежие платежи важнее глубины, а пустые категории не
    # должны съедать бюджет догона.
    tops: list[tuple[str, str | None]] = []
    deeps: list[tuple[str, str | None]] = []
    for category in RENT_CATEGORIES:
        st = feed.get(category) or {}
        resume = None if st.get("done") else (st.get("deep_cursor") or None)
        # Граница «уже прочитано» безопасна, лишь когда глубину продолжит
        # фаза с курсором (или окно уже добыто): пока возобновления нет,
        # чтение сверху — это и есть весь догон, останавливаться рано.
        if not resume or int(st.get("top_ts", 0) or 0):
            tops.append((category, None))
        if resume:
            deeps.append((category, resume))
    tasks = tops + deeps

    for i, (category, start_cursor) in enumerate(tasks):
        if i > 0:
            await asyncio.sleep(5)
        state = feed.get(category) or {}
        if start_cursor is not None and state.get("done"):
            # Окно добрано предыдущей фазой этого же прогона.
            by_cat.setdefault(category, 0)
            continue
        if budget <= 0:
            stops[category] = "бюджет страниц исчерпан"
            by_cat.setdefault(category, 0)
            continue
        prev_top = int(state.get("top_ts", 0) or 0)
        incremental = bool(state.get("done"))
        saved_deep = None if incremental else (state.get("deep_cursor") or None)
        has_deep = bool(saved_deep)
        boundary_ok = incremental or has_deep
        cursor = start_cursor
        start_from_top = cursor is None
        page_delay = LINKAGE_PAGE_DELAY
        collected_before = len(collected)
        pages_cat = 0
        dirty = False  # состояние изменилось, даже если страниц не прочли
        newest = 0     # самая новая ts — новая граница «непрочитанного»
        deepest = 0    # самая старая прочитанная ts — глубина покрытия
        done = incremental  # True, когда упёрлись в конец ленты или в окно
        stop_reason = ""
        # Проба пустой категории: только для догона глубины и только пока
        # у категории ни одной записи не было (ни раньше, ни в верхней
        # фазе этого же прогона — она уже посчитана в hits).
        probe_left = None
        if start_cursor is not None and not incremental \
                and int(state.get("hits", 0) or 0) == 0:
            if "probe_left" in state:
                probe_left = int(state["probe_left"])
            else:
                probe_left = max(
                    1, HISTORY_PROBE_ITEMS // max(1, _history_limit["value"]))
        for page in range(MAX_HISTORY_PAGES):
            if budget <= 0:
                stop_reason = "бюджет страниц исчерпан"
                break
            if probe_left is not None and probe_left <= 0:
                stop_reason = HISTORY_PROBE_STOP
                break
            if page > 0:
                await asyncio.sleep(page_delay)
            items, next_cursor = await fetch_rent_history(api_token, category, cursor=cursor)
            if items:
                page_delay = max(LINKAGE_PAGE_DELAY, page_delay * 0.75)
            else:
                page_delay = min(page_delay * 2, LINKAGE_PAGE_DELAY_MAX)
            if not items:
                if cursor is None or incremental:
                    # Пусто с верха либо дошли до самого конца ленты.
                    done = True
                    stop_reason = "нет записей на странице"
                else:
                    # Сохранённый курсор не подхватился — читаем с верха,
                    # но сначала сбрасываем его в состоянии, иначе следующий
                    # прогон уйдёт в тот же тупик.
                    cursor = None
                    dirty = True
                    stop_reason = "курсор не подхватился, начнём с верха"
                break
            budget -= 1
            pages_read += 1
            pages_cat += 1
            if probe_left is not None:
                probe_left -= 1
            if api_keys is None:
                api_keys = sorted(items[0].keys())
            page_ts = [int(ev.get("ts", 0) or 0) for ev in items]
            oldest = min(page_ts, default=0)
            newest = max(newest, max(page_ts, default=0))
            if oldest:
                deepest = min(deepest, oldest) if deepest else oldest
            # 1) Ниже границы прошлого прогона уже всё прочитано (сутки
            #    перечитываем заново — страховка от нестрогой сортировки их
            #    ленты). Работает только для фазы, стартующей сверху, и
            #    только когда глубину продолжит фаза с курсором или окно
            #    уже добыто: иначе это единственный способ дойти вглубь.
            if (start_from_top and boundary_ok and prev_top
                    and oldest and oldest <= prev_top - LINKAGE_REREAD_S):
                logger.info(
                    f"marketapp: {category}/history — ниже границы прошлого прогона "
                    f"(oldest={oldest} <= {prev_top - LINKAGE_REREAD_S}), "
                    f"прочитано страниц: {page + 1}"
                )
                stop_reason = "уже прочитано в прошлых прогонах"
                break
            # 2) Старше окна блокчейна — глубже листать незачем, окно достигнуто.
            if window_start and oldest and oldest < window_start:
                logger.info(
                    f"marketapp: {category}/history — история ушла старее блокчейн-границы "
                    f"(oldest={oldest} < window={window_start}), останавливаюсь на странице {page + 1}"
                )
                stop_reason = "история старее блокчейна"
                done = True
                break
            if page == 0:
                sample = items[0] if items else {}
                logger.info(
                    f"marketapp: {category}/history страница 1 — {len(items)} записей, "
                    f"next_cursor={next_cursor!r} keys={sorted(sample.keys())} "
                    f"sample src={sample.get('src')!r} dst={sample.get('dst')!r} ts={sample.get('ts')!r} price_nano={sample.get('price_nano')!r}"
                )
            elif page < 5:
                logger.info(f"marketapp: {category}/history страница {page + 1} — {len(items)} записей")
            for item in items:
                src_raw = userfriendly_to_raw(item.get("src", ""))
                dst_raw = userfriendly_to_raw(item.get("dst", ""))
                if src_raw != raw_wallet and dst_raw != raw_wallet:
                    dropped["no_wallet"] += 1
                    continue
                if dst_raw == raw_wallet:
                    direction["in"] += 1
                elif src_raw == raw_wallet:
                    direction["out"] += 1
                price_nano = record_price_nano(item)
                if not price_nano:
                    dropped["no_price"] += 1
                    continue
                record = {
                    "tx_hash": item.get("tx_hash") or "",
                    "category": category,
                    "address": item.get("address", ""),
                    "name": item.get("name", ""),
                    "collection_address": item.get("collection_address", ""),
                    "is_extend": item.get("is_extend") or False,
                    "duration": int(item.get("duration", 0) or 0),
                    "ts": int(item.get("ts", 0) or 0),
                    "src": item.get("src", ""),
                    "dst": item.get("dst", ""),
                    "price_nano": price_nano,
                }
                collected.append(record)

            if not next_cursor:
                stop_reason = "курсор кончился"
                done = True
                break
            cursor = next_cursor
        else:
            stop_reason = "лимит страниц"

        stops[category] = stop_reason
        by_cat[category] = by_cat.get(category, 0) + (len(collected) - collected_before)

        # Состояние пишем и при обрыве по бюджету: прочитанный отрезок
        # непрерывен и покрыт целиком, а без сохранённого курсора глубина
        # с прогона на прогон не росла бы. Верх (top_ts) двигаем только когда
        # читали с самой новой записи — иначе граница объявит непрочитанные
        # участки прочитанными.
        if pages_cat or dirty:
            new_state = dict(state)
            delta = len(collected) - collected_before
            hits = int(new_state.get("hits", 0) or 0)
            if hits or delta:
                # Категория давала записи (в этом прогоне или раньше) —
                # кап пробы снимается навсегда, вглубь читаем по общим
                # правилам до окна блокчейна или конца ленты.
                new_state["hits"] = hits + delta
                new_state.pop("probe_left", None)
            elif probe_left is not None:
                new_state["probe_left"] = probe_left
            if start_from_top and newest:
                new_state["top_ts"] = newest
            if not incremental:
                if deepest:
                    old_depth = int(new_state.get("depth_ts", 0) or 0)
                    new_state["depth_ts"] = min(old_depth, deepest) if old_depth else deepest
                # Курсор двигает только фаза, читающая вглубь: фаза сверху
                # при живом возобновлении не должна затирать его точкой у
                # границы — иначе догон начнётся заново с верха.
                if not start_from_top or not has_deep:
                    new_state["deep_cursor"] = cursor
            if done:
                new_state["done"] = True
            feed[category] = new_state

    await set_linkage_feed(wallet, feed)
    logger.info(f"marketapp: собрано записей по кошельку: {len(collected)} по категориям={by_cat}")
    record_linkage(collected=len(collected), matched=0, categories=by_cat,
                   api_keys=api_keys, direction=direction,
                   pages=pages_read, dropped=dropped, stops=stops, feed=feed)
    if not collected:
        return 0

    enriched = await enrich_blockchain_events(collected, wallet)
    record_linkage(collected=len(collected), matched=enriched, categories=by_cat,
                   api_keys=api_keys, direction=direction,
                   pages=pages_read, dropped=dropped, stops=stops, feed=feed)
    logger.info(f"marketapp: enrich совпадений с блокчейн-событиями: {enriched} из {len(collected)}")
    if enriched:
        logger.info(f"Дополнено метаданными из Marketapp: {enriched}")
    return enriched


async def sync_rent_events_job(context: ContextTypes.DEFAULT_TYPE):
    if LINKAGE_BUSY["running"]:
        # Прошлый прогон ещё идёт (1000 страниц — до ~25 мин): второй
        # параллельный только удвоит нагрузку и затрёт курсор первого.
        # Пропускаем — глубина догоняется следующим прогоном.
        logger.info("marketapp: прогон привязки ещё идёт — пропускаю")
        return
    LINKAGE_BUSY["running"] = True
    try:
        bot_data = context.bot_data
        api_token = bot_data.get("MARKETAPP_API_KEY")
        wallet = bot_data.get("MARKETAPP_WALLET", "")

        if not api_token or not wallet:
            record_linkage(error="нет MARKETAPP_API_KEY или MARKETAPP_WALLET")
            return

        await collect_rent_events(api_token, wallet)
    except Exception as e:
        logger.error(f"Ошибка сбора событий аренды: {e}", exc_info=True)
        record_linkage(error=f"{type(e).__name__}: {e}")
    finally:
        LINKAGE_BUSY["running"] = False


def format_daily_report(current_profit: float, previous_profit: float | None) -> str:
    lines = [
        "<b>📊 ОТЧЁТ ЗА СУТКИ</b>\n",
        f"Прибыль с аренды: <b>{_format_ton(current_profit)} TON</b>",
    ]

    if previous_profit is not None and previous_profit > 0:
        change = ((current_profit - previous_profit) / previous_profit) * 100
        emoji = "\U0001f7e2" if change > 0 else "\U0001f534" if change < 0 else "\u26aa"
        lines.append(f"Изменение: {emoji} {change:+.1f}% к предыдущему отчёту")
    elif previous_profit is not None and previous_profit == 0:
        lines.append("Изменение: \U0001f7e2 новая прибыль!")

    lines.append(f"\n<i>{datetime.now().strftime('%d.%m.%Y %H:%M')}</i>")
    return "\n".join(lines)


def format_weekly_report(profits: list) -> str:
    if not profits:
        return "<b>📊 ОТЧЁТ ЗА НЕДЕЛЮ</b>\n\nНет данных за неделю."

    total = sum(p["profit_ton"] for p in profits)
    avg = total / 7 if total else 0
    values = [p["profit_ton"] for p in profits]
    min_p = min(values) if values else 0
    max_p = max(values) if values else 0

    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).strftime("%d.%m")
    week_end = now.strftime("%d.%m")

    lines = [
        f"<b>📊 ОТЧЁТ ЗА НЕДЕЛЮ ({week_start} - {week_end})</b>\n",
        f"Прибыль: <b>{_format_ton(total)} TON</b>",
        f"Средняя в день: {_format_ton(avg)} TON",
        f"Мин/Макс: {_format_ton(min_p)} / {_format_ton(max_p)} TON",
        f"Дней с данными: {len(profits)} из 7",
    ]

    if len(profits) >= 2:
        first = profits[-1]["profit_ton"]
        last = profits[0]["profit_ton"]
        if first > 0:
            change = ((last - first) / first) * 100
            emoji = "\U0001f7e2" if change > 0 else "\U0001f534" if change < 0 else "\u26aa"
            lines.append(f"\nДинамика недели: {emoji} {change:+.1f}%")

    lines.append(f"\n<i>{now.strftime('%d.%m.%Y %H:%M')}</i>")
    return "\n".join(lines)


def format_monthly_report(profits: list) -> str:
    if not profits:
        return "<b>📊 ОТЧЁТ ЗА МЕСЯЦ</b>\n\nНет данных за месяц."

    total = sum(p["profit_ton"] for p in profits)
    avg = total / 30 if total else 0
    values = [p["profit_ton"] for p in profits]
    min_p = min(values) if values else 0
    max_p = max(values) if values else 0

    now = datetime.now()
    month_name = now.strftime("%B %Y")

    lines = [
        f"<b>📊 ОТЧЁТ ЗА {month_name.upper()}</b>\n",
        f"Прибыль: <b>{_format_ton(total)} TON</b>",
        f"Средняя в день: {_format_ton(avg)} TON",
        f"Мин/Макс: {_format_ton(min_p)} / {_format_ton(max_p)} TON",
        f"Дней с данными: {len(profits)} из 30",
    ]

    lines.append(f"\n<i>{now.strftime('%d.%m.%Y %H:%M')}</i>")
    return "\n".join(lines)


async def _send_report(context: ContextTypes.DEFAULT_TYPE, report_text: str, save_db: bool = False, db_period: str = None, profit: float = None):
    bot_data = context.bot_data
    channel_id = bot_data.get("NEWS_CHANNEL_ID")

    if not channel_id:
        return

    if save_db and profit is not None and db_period:
        await save_marketapp_profit(db_period, profit)

    await context.bot.send_message(
        chat_id=channel_id,
        text=report_text,
        parse_mode="HTML"
    )


async def daily_profit_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        bot_data = context.bot_data
        api_token = bot_data.get("MARKETAPP_API_KEY")
        wallet = bot_data.get("MARKETAPP_WALLET", "")

        if not api_token:
            logger.warning("MARKETAPP_API_KEY не задан")
            return

        logger.info("Получаю данные за сутки...")
        since_ts = _ts_days_ago(1)
        profit = await fetch_income_for_period(api_token, since_ts, wallet)

        if profit == 0:
            logger.info("Прибыли за сутки нет, сохраняю 0 для статистики")
            await save_marketapp_profit("day", 0.0)
            return

        previous = await get_previous_profit("day")
        previous_profit = previous["profit_ton"] if previous else None

        report = format_daily_report(profit, previous_profit)
        await _send_report(context, report, save_db=True, db_period="day", profit=profit)
        logger.info(f"Ежедневный отчет отправлен: {profit} TON")

    except Exception as e:
        logger.error(f"Ошибка ежедневного отчета: {e}", exc_info=True)


async def weekly_profit_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        bot_data = context.bot_data
        api_token = bot_data.get("MARKETAPP_API_KEY")
        wallet = bot_data.get("MARKETAPP_WALLET", "")

        if not api_token:
            return

        logger.info("Получаю данные за неделю...")
        since_ts = _ts_days_ago(7)
        profit = await fetch_income_for_period(api_token, since_ts, wallet)

        if profit > 0:
            await save_marketapp_profit("week", profit)

        profits = await get_profit_for_period("day", 7)
        report = format_weekly_report(profits)
        await _send_report(context, report)
        logger.info("Еженедельный отчет отправлен")

    except Exception as e:
        logger.error(f"Ошибка еженедельного отчета: {e}", exc_info=True)


async def monthly_profit_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        if datetime.now().day != 1:
            return

        bot_data = context.bot_data
        api_token = bot_data.get("MARKETAPP_API_KEY")
        wallet = bot_data.get("MARKETAPP_WALLET", "")

        if not api_token:
            return

        logger.info("Получаю данные за месяц...")
        since_ts = _ts_days_ago(30)
        profit = await fetch_income_for_period(api_token, since_ts, wallet)

        if profit > 0:
            await save_marketapp_profit("month", profit)

        profits = await get_profit_for_period("day", 30)
        report = format_monthly_report(profits)
        await _send_report(context, report)
        logger.info("Ежемесячный отчет отправлен")

    except Exception as e:
        logger.error(f"Ошибка ежемесячного отчета: {e}", exc_info=True)


def setup_marketapp_jobs(application):
    for job_name in ["marketapp_daily", "marketapp_weekly", "marketapp_monthly", "marketapp_rent_sync", "blockchain_rent_sync"]:
        jobs = application.job_queue.get_jobs_by_name(job_name)
        for job in jobs:
            job.schedule_removal()

    report_time = dt_time(hour=9, minute=0, tzinfo=MSK)

    application.job_queue.run_daily(
        daily_profit_report,
        time=report_time,
        name="marketapp_daily"
    )

    application.job_queue.run_daily(
        weekly_profit_report,
        time=report_time,
        days=(0,),
        name="marketapp_weekly"
    )

    application.job_queue.run_daily(
        monthly_profit_report,
        time=report_time,
        name="marketapp_monthly"
    )

    application.job_queue.run_repeating(
        sync_blockchain_rent_job,
        interval=timedelta(minutes=10),
        first=30,
        name="blockchain_rent_sync"
    )

    application.job_queue.run_repeating(
        sync_rent_events_job,
        interval=timedelta(minutes=30),
        first=45,
        name="marketapp_rent_sync"
    )

    logger.info("Marketapp отчеты настроены (09:00 MSK, блокчейн-синхронизация 10 мин, история 30 мин)")
