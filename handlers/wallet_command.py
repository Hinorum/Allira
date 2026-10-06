import asyncio
import io
import logging
from collections import OrderedDict
from datetime import datetime
from telegram import InputFile, Update
from telegram.ext import ContextTypes

from tasks.marketapp_reports import (
    collect_rent_events,
    sync_rent_from_blockchain,
    _format_ton,
    _nano_to_ton,
    _ts_days_ago,
    userfriendly_to_raw,
    MSK,
    MARKETAPP_API_URL,
)
from utils.common import escape_html
from utils.database import get_all_rent_events, get_db
from utils.http_client import get_client

logger = logging.getLogger(__name__)

MAX_SYNC_PAGES = 1000

# Чаты, у которых уже идёт тяжёлая операция и её описание. Без этого повторный
# /marketapprent (или /marketappgifts) во время долгого сканирования запускал бы
# второй параллельный прогон: двойная нагрузка на API и дубли в ответах.
_sync_in_progress: dict[int, str] = {}


async def _resolve_wallet(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command: str,
    args: list[str] | None = None,
    usage: str | None = None,
) -> tuple[str, bool] | None:
    """Разбор адреса кошелька из аргументов/конфига и проверка владельца.

    Было продублировано в marketapprent_command и marketappgifts_command.
    Возвращает (wallet, is_owner) либо None — тогда юзеру уже ответили.

    args — уже разобранные аргументы (по умолчанию context.args): у /marketapptop
    первый аргумент — число дней, и адрес кошелька остаётся вторым.
    usage — подсказка использования, если у команды есть ещё и другие аргументы.
    """
    if args is None:
        args = context.args
    if usage is None:
        usage = f"Использование: /{command} <адрес кошелька>"
    wallet = ""
    if args:
        candidate = args[0].strip()
        if userfriendly_to_raw(candidate).startswith("0:"):
            wallet = candidate
        else:
            await update.message.reply_text(
                "Неверный адрес кошелька.\n" + usage
            )
            return None
    if not wallet:
        wallet = context.bot_data.get("MARKETAPP_WALLET", "")

    if not wallet:
        await update.message.reply_text(
            "Кошелёк не передан и MARKETAPP_WALLET не настроен.\n" + usage
        )
        return None

    owner_wallet = context.bot_data.get("MARKETAPP_WALLET", "")
    is_owner = bool(owner_wallet and userfriendly_to_raw(owner_wallet) == userfriendly_to_raw(wallet))
    return wallet, is_owner


def _dedupe_events(events: list) -> list:
    seen = set()
    unique = []
    for ev in events:
        key = (
            int(ev.get("ts", 0) or 0),
            (ev.get("src") or "").strip().lower(),
            (ev.get("dst") or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(ev)
    return unique


def _total_nano(events: list, since_ts: int = 0) -> int:
    return sum(int(ev["price_nano"] or 0) for ev in events if ev["ts"] >= since_ts)


async def marketapprent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/marketapprent вызван, args={context.args}")
    resolved = await _resolve_wallet(update, context, "marketapprent")
    if resolved is None:
        return
    wallet, is_owner = resolved
    api_token = context.bot_data.get("MARKETAPP_API_KEY")

    chat_id = update.message.chat_id
    if chat_id in _sync_in_progress:
        await update.message.reply_text(
            f"Уже выполняется: {_sync_in_progress[chat_id]}. Подожди готовый отчёт."
        )
        return
    _sync_in_progress[chat_id] = "синхронизация аренды"

    await update.message.reply_text(
        "Синхронизация запущена в фоне — чат не блокирую, отвечу с готовым отчётом.\n"
        f"Кошелёк: <code>{escape_html(wallet)}</code>",
        parse_mode="HTML"
    )
    asyncio.create_task(
        _rent_sync_task(context.bot, chat_id, wallet, api_token, is_owner)
    )


async def _rent_sync_task(bot, chat_id: int, wallet: str, api_token: str, is_owner: bool):
    """Долгий синк кошелька в фоне.

    Раньше он выполнялся прямо внутри хендлера: сканирование блокчейна
    (до 1000 страниц) вставало между апдейтами и подвешивало весь бот.
    """
    try:
        await _run_rent_sync(bot, chat_id, wallet, api_token, is_owner)
    except Exception as e:
        logger.error(f"/marketapprent: фоновая синхронизация упала: {e}", exc_info=True)
        try:
            await bot.send_message(chat_id, "Синхронизация прервалась из-за ошибки. Попробуй позже.")
        except Exception:
            pass
    finally:
        _sync_in_progress.pop(chat_id, None)


async def _run_rent_sync(bot, chat_id: int, wallet: str, api_token: str, is_owner: bool):
    api_saved = 0
    blockchain_saved = 0

    await bot.send_message(chat_id, "Сканирую полную историю из блокчейна...")
    blockchain_saved = await sync_rent_from_blockchain(wallet, max_pages=MAX_SYNC_PAGES, from_scratch=True)
    logger.info(f"/marketapprent: блокчейн-синк завершён, сохранено={blockchain_saved}")

    if api_token and is_owner:
        await bot.send_message(chat_id, "Дополняю метаданными из Marketapp...")
        try:
            api_saved = await collect_rent_events(api_token, wallet)
        except Exception as e:
            logger.error(f"Marketapp API дополнение: {e}", exc_info=True)

    events = _dedupe_events(await get_all_rent_events(wallet))
    sorted_events = sorted(events, key=lambda e: e["ts"], reverse=True)

    now_ts = int(datetime.now(MSK).timestamp())
    day_ts = now_ts - 86400
    week_ts = now_ts - 604800
    month_ts = now_ts - 2592000

    total_ops = len(sorted_events)
    day_income = _total_nano(sorted_events, day_ts) / 1_000_000_000
    week_income = _total_nano(sorted_events, week_ts) / 1_000_000_000
    month_income = _total_nano(sorted_events, month_ts) / 1_000_000_000
    total_income = _total_nano(sorted_events, 0) / 1_000_000_000

    if sorted_events:
        first_ts = sorted_events[-1]["ts"]
        last_ts = sorted_events[0]["ts"]
        period_text = (
            f"{datetime.fromtimestamp(first_ts, MSK).strftime('%d.%m.%Y')} — "
            f"{datetime.fromtimestamp(last_ts, MSK).strftime('%d.%m.%Y')}"
        )
    else:
        period_text = "нет данных"

    lines = [
        "<b>Прибыль с аренды NFT:</b>\n",
        f"Сутки (24ч): <b>{_format_ton(day_income)} TON</b>",
        f"Неделя: <b>{_format_ton(week_income)} TON</b>",
        f"Месяц (30д): <b>{_format_ton(month_income)} TON</b>",
        f"За всё время: <b>{_format_ton(total_income)} TON</b>",
        f"\nПериод сбора: {period_text}",
        f"Операций в базе: <b>{total_ops}</b>",
    ]

    if api_saved or blockchain_saved:
        parts = []
        if blockchain_saved:
            parts.append(f"блокчейн: +{blockchain_saved} платежей")
        if api_saved:
            parts.append(f"метаданные Marketapp: {api_saved}")
        lines.append("Сохранено: " + ", ".join(parts))
    else:
        lines.append("Сохранено: ничего (оба источника не вернули данные)")

    if sorted_events:
        lines.append("\n<b>Разбивка по месяцам:</b>")
        monthly = OrderedDict()
        for ev in sorted_events:
            key = datetime.fromtimestamp(ev["ts"], MSK).strftime("%m.%Y")
            monthly[key] = monthly.get(key, 0) + int(ev["price_nano"] or 0)

        for key in sorted(monthly.keys()):
            lines.append(f"{key}: <b>{_format_ton(monthly[key] / 1_000_000_000)} TON</b>")

    try:
        if api_token and is_owner:
            client = await get_client()
            response = await client.get(
                f"{MARKETAPP_API_URL}/v1/rent/my-rented/",
                headers={"Authorization": api_token, "User-Agent": "AlliraBot/1.0"},
                timeout=15.0
            )
            if response.status_code == 200:
                rented = response.json().get("items", [])
                if rented:
                    lines.append(f"\nАктивных аренд: {len(rented)}")
                    for item in rented[:5]:
                        name = escape_html(item.get("nft_name", "?"))
                        price = _nano_to_ton(item.get("price_per_day", "0"))
                        lines.append(f"  - {name}: {_format_ton(price)} TON/день")
    except Exception:
        pass

    lines.append(f"\n<i>{datetime.now(MSK).strftime('%d.%m.%Y %H:%M')}</i>")
    await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")

    if sorted_events:
        await send_rent_history(bot, chat_id, sorted_events)


async def send_rent_history(bot, chat_id: int, events: list):
    lines = [f"Сдачи в аренду — всего {len(events)} событий:\n"]
    for ev in events:
        amount = _format_ton(_nano_to_ton(ev["price_nano"]))
        line = (
            f"{datetime.fromtimestamp(ev['ts'], MSK).strftime('%d.%m.%Y %H:%M')} — "
            f"+{amount} TON"
        )
        name = (ev.get("nft_name") or "").strip()
        nft_addr = (ev.get("nft_address") or "").strip()
        if name:
            line += f" — {name}"
        lines.append(line)
        if nft_addr:
            lines.append(f"  https://getgems.io/nft/{nft_addr}")

    payload = "\n".join(lines).encode("utf-8")
    await bot.send_document(
        chat_id=chat_id,
        document=InputFile(io.BytesIO(payload), filename="rent_history.txt"),
        caption=f"Все сдачи в аренду: <b>{len(events)}</b> событий, "
                f"+{_format_ton(_nano_to_ton(sum(int(ev['price_nano'] or 0) for ev in events)))} TON",
        parse_mode="HTML",
    )


async def _enrich_by_price(api_token: str, wallet: str) -> int:
    from tasks.marketapp_reports import fetch_my_rented

    rented = await fetch_my_rented(api_token)
    if not rented:
        logger.info("_enrich_by_price: нет данных от my-rented")
        return 0

    nfts = []
    for item in rented:
        price = int(item.get("price_per_day", "0") or 0)
        if price > 0:
            nfts.append({
                "address": item.get("nft_address", ""),
                "name": item.get("nft_name", ""),
                "price": price,
            })

    logger.info(f"_enrich_by_price: {len(nfts)} NFT с ценами")
    if not nfts:
        return 0

    events = await get_all_rent_events(wallet)
    unmatched = [e for e in events if not (e.get("nft_address") or "").strip()]
    logger.info(f"_enrich_by_price: {len(unmatched)} событий без привязки к NFT")

    if not unmatched:
        return 0

    def _do_enrich():
        enriched = 0
        with get_db() as conn:
            for ev in unmatched:
                value = int(ev.get("price_nano", "0") or 0)
                if value <= 0:
                    continue

                best_nft = None
                best_diff = float("inf")
                for nft in nfts:
                    for days in range(1, 31):
                        expected = nft["price"] * days
                        diff = abs(value - expected)
                        if diff < best_diff:
                            best_diff = diff
                            best_nft = nft

                if best_nft and best_diff / max(value, 1) < 0.05:
                    conn.execute(
                        "UPDATE marketapp_rent_events "
                        "SET nft_address=?, nft_name=?, source='price_match' WHERE id=?",
                        (best_nft["address"], best_nft["name"], ev["id"]),
                    )
                    enriched += 1
        return enriched

    return await asyncio.to_thread(_do_enrich)


def _count_linked(events: list) -> int:
    """Сколько платежей привязано к конкретному подарку (есть nft_address)."""
    return sum(1 for e in events if (e.get("nft_address") or "").strip())


async def _ensure_rent_data(bot, chat_id: int, wallet: str, api_token: str, is_owner: bool) -> list:
    """События аренды с гарантией, что база заполнена и платежи привязаны к NFT.

    Free-план Render не даёт постоянного диска: SQLite стирается при каждом
    деплое. Фоновые джобы добывают только новые платежи «сверху» истории, а
    полную историю делает только from_scratch-синк — раньше его запускал
    исключительно /marketapprent, поэтому /marketapptop и /marketappgifts
    после деплоя отвечали «нет данных». Теперь отчёты сами запускают тот же
    синк и привязку, что и ручная команда.
    """
    events = _dedupe_events(await get_all_rent_events(wallet))

    if not events:
        await bot.send_message(
            chat_id,
            "База пуста (Render стирает её при каждом деплое) — запускаю полную "
            "синхронизацию истории, это займёт до нескольких минут.\n"
            "Отвечу с готовым отчётом, команду не перезапускай.",
        )
        try:
            saved = await sync_rent_from_blockchain(
                wallet, max_pages=MAX_SYNC_PAGES, from_scratch=True
            )
            logger.info(f"_ensure_rent_data: полный синк завершён, сохранено={saved}")
        except Exception as e:
            logger.error(f"_ensure_rent_data: синк упал: {e}", exc_info=True)
            await bot.send_message(
                chat_id,
                "Синхронизация из блокчейна не удалась: "
                f"<code>{escape_html(str(e)[:200])}</code>",
                parse_mode="HTML",
            )
        events = _dedupe_events(await get_all_rent_events(wallet))
        if not events:
            return events

    if events and _count_linked(events) == 0 and api_token and is_owner:
        await bot.send_message(chat_id, "Привязываю платежи к подаркам через Marketapp...")
        try:
            matched = await collect_rent_events(api_token, wallet)
            logger.info(f"_ensure_rent_data: marketapp-привязка, совпало={matched}")
        except Exception as e:
            logger.error(f"_ensure_rent_data: marketapp-привязка упала: {e}", exc_info=True)
            await bot.send_message(
                chat_id,
                "Привязка через Marketapp не удалась: "
                f"<code>{escape_html(str(e)[:200])}</code>",
                parse_mode="HTML",
            )
        events = _dedupe_events(await get_all_rent_events(wallet))

        if events and _count_linked(events) == 0:
            # Точный матч не сработал — пробуем примерять по цене аренды
            # (медленный перебор events × nfts × 30 дней, но зато без истории).
            try:
                enriched = await _enrich_by_price(api_token, wallet)
                if enriched:
                    events = _dedupe_events(await get_all_rent_events(wallet))
                    logger.info(f"_ensure_rent_data: enrich по цене привязал {enriched}")
            except Exception as e:
                logger.error(f"_ensure_rent_data: enrich по цене упал: {e}", exc_info=True)
                await bot.send_message(
                    chat_id,
                    "Привязка по цене аренды не удалась: "
                    f"<code>{escape_html(str(e)[:200])}</code>",
                    parse_mode="HTML",
                )

    return events


def _no_linkage_reason(api_token: str, is_owner: bool) -> str:
    """Почему платежи нельзя разложить по подаркам — для сообщения юзеру."""
    if not api_token:
        return "не задан MARKETAPP_API_KEY"
    if not is_owner:
        return "кошелёк не совпадает с MARKETAPP_WALLET — привязка доступна только для своего"
    return "Marketapp не вернул совпадений (подробности в логах Render)"


async def _send_no_linkage(bot, chat_id: int, events: list, api_token: str, is_owner: bool):
    total = _format_ton(_nano_to_ton(str(sum(int(e.get("price_nano", 0) or 0) for e in events))))
    await bot.send_message(
        chat_id,
        f"В базе {len(events)} платежей на {total} TON, но ни один не привязан к подарку:\n"
        f"{_no_linkage_reason(api_token, is_owner)}.\n\n"
        "Без привязки к NFT разложить доход по подаркам нельзя — суммы по кошельку "
        "смотри в /marketapprent.",
        parse_mode="HTML",
    )


async def marketappgifts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/marketappgifts вызван, args={context.args}")
    resolved = await _resolve_wallet(update, context, "marketappgifts")
    if resolved is None:
        return
    wallet, is_owner = resolved

    api_token = context.bot_data.get("MARKETAPP_API_KEY")
    logger.info(
        f"/marketappgifts: api_token={'есть' if api_token else 'НЕТ'} is_owner={is_owner}"
    )

    chat_id = update.message.chat_id
    if chat_id in _sync_in_progress:
        await update.message.reply_text(
            f"Уже выполняется: {_sync_in_progress[chat_id]}. Подожди готовый отчёт."
        )
        return
    _sync_in_progress[chat_id] = "отчёт по подаркам"

    await update.message.reply_text(
        "Собираю отчёт по подаркам в фоне — чат не блокирую, отвечу с готовым."
    )
    asyncio.create_task(_gifts_report_task(context.bot, chat_id, wallet, api_token, is_owner))


async def _gifts_report_task(bot, chat_id: int, wallet: str, api_token: str, is_owner: bool):
    """Обогащение платежей по ценам NFT — самый тяжёлый шаг отчёта.

    Цикл events × nfts × 30 дней считается в потоке, но всё равно держал
    апдейт: сейчас идёт в фоне, как и синк аренды.
    """
    try:
        await _run_gifts_report(bot, chat_id, wallet, api_token, is_owner)
    except Exception as e:
        logger.error(f"/marketappgifts: фоновый отчёт упал: {e}", exc_info=True)
        try:
            await bot.send_message(chat_id, "Отчёт прервался из-за ошибки. Попробуй позже.")
        except Exception:
            pass
    finally:
        _sync_in_progress.pop(chat_id, None)


async def _run_gifts_report(bot, chat_id: int, wallet: str, api_token: str, is_owner: bool):
    # Сами наполняем базу при деплое-обнулении и привязываем платежи к NFT
    events = await _ensure_rent_data(bot, chat_id, wallet, api_token, is_owner)
    logger.info(f"/marketappgifts: событий в БД={len(events)}, с-подарком={_count_linked(events)}")
    if not events:
        await bot.send_message(
            chat_id,
            f"Платежей по кошельку <code>{escape_html(wallet)}</code> нет даже после "
            "полной синхронизации.\n"
            "Проверь MARKETAPP_WALLET и запусти /marketapprent — он покажет период сбора.",
            parse_mode="HTML",
        )
        return

    per_gift = OrderedDict()
    for ev in events:
        addr = (ev.get("nft_address") or "").strip()
        if not addr:
            continue
        name = (ev.get("nft_name") or "").strip()
        entry = per_gift.setdefault(addr, {"name": name, "nano": 0, "count": 0})
        entry["nano"] += int(ev["price_nano"] or 0)
        entry["count"] += 1

    total_nano = sum(int(e.get("price_nano", 0) or 0) for e in events)
    gifts_nano = sum(g["nano"] for g in per_gift.values())
    linked_count = sum(g["count"] for g in per_gift.values())
    unlinked_nano = total_nano - gifts_nano
    unlinked_count = len(events) - linked_count

    total_ton = _format_ton(_nano_to_ton(str(total_nano)))

    if not per_gift:
        await bot.send_message(
            chat_id,
            f"Общий доход: <b>{total_ton} TON</b> ({len(events)} событий)\n\n"
            f"Причина: {_no_linkage_reason(api_token, is_owner)}.\n"
            "Платежи не привязаны к конкретным подаркам — разложить доход по ним нельзя, "
            "суммы по кошельку смотри в /marketapprent.",
            parse_mode="HTML",
        )
        return

    # Сумма по списку и общий доход — разные числа (непривязанные платежи в
    # список не входят). Показываем оба явно, иначе заголовок «46 NFT / 41.53»
    # не сходился с суммой строк списка.
    summary_line = f"Всего платежей: {total_ton} TON · {len(events)} плат."
    if unlinked_count:
        summary_line += (
            f" — не привязано {unlinked_count} плат. "
            f"на {_format_ton(_nano_to_ton(str(unlinked_nano)))} TON"
        )
    header = (
        f"<b>Доход по подаркам</b> ({wallet[:10]}...): "
        f"{len(per_gift)} NFT / {_format_ton(_nano_to_ton(str(gifts_nano)))} TON\n"
        f"{summary_line}\n"
    )
    lines = [header]
    current_len = len(header)

    for addr, gift in sorted(per_gift.items(), key=lambda kv: kv[1]["nano"], reverse=True):
        ton = _format_ton(_nano_to_ton(str(gift["nano"])))
        name = escape_html(gift["name"]) if gift["name"] else "без названия"
        line = (
            f"<a href=\"https://getgems.io/nft/{addr}\">"
            f"{name}</a> — <b>{ton} TON</b> ({gift['count']} сд.)"
        )
        if current_len and current_len + len(name) + 60 > 3800:
            await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
            lines = [header]
            current_len = len(header)
        lines.append(line)
        current_len += len(line) + 1

    await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


# --- /marketapptop: топ подарков по сумме и доходности ---

TOP_LIMIT = 10


def _parse_top_days(args: list[str]) -> tuple[int | None, list[str]]:
    """Разбирает /marketapptop [дней] [адрес].

    Возвращает (days, оставшиеся аргументы). days=None — за всё время.
    Первый аргумент считается периодом, только если это чисто число (или
    «всё»): адреса TON начинаются с EQ/UQ/0:, поэтому не пересекаются.
    """
    if not args:
        return None, []
    raw = args[0].strip().lower()
    if raw in ("всё", "все", "all", "*"):
        return None, args[1:]
    if raw.isdigit():
        days = int(raw)
        if not 1 <= days <= 3650:
            raise ValueError("период должен быть от 1 до 3650 дней")
        return days, args[1:]
    return None, args


def _format_rate(value: float) -> str:
    """TON/сут: два знака дают «0.00» у мелких подарков — показываем точнее."""
    if value >= 10:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.3f}"
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _top_rows(events: list, since_ts: int) -> tuple[list, dict]:
    """Группировка платежей за период по NFT: строки топа и сводка.

    Платежи без привязки к NFT в топ не попадают (нельзя понять, какой
    подарок заработал), но считаются в сводке — чтобы суммы сходились.
    """
    per_gift = OrderedDict()
    summary = {
        "total_nano": 0,
        "count": 0,
        "unlinked_nano": 0,
        "unlinked_count": 0,
        "first_ts": 0,
        "last_ts": 0,
    }

    for ev in events:
        ts = int(ev.get("ts", 0) or 0)
        if ts < since_ts:
            continue
        nano = int(ev.get("price_nano", 0) or 0)
        summary["total_nano"] += nano
        summary["count"] += 1
        if not summary["first_ts"] or ts < summary["first_ts"]:
            summary["first_ts"] = ts
        if ts > summary["last_ts"]:
            summary["last_ts"] = ts

        addr = (ev.get("nft_address") or "").strip()
        if not addr:
            summary["unlinked_count"] += 1
            summary["unlinked_nano"] += nano
            continue
        name = (ev.get("nft_name") or "").strip()
        entry = per_gift.setdefault(addr, {"name": name, "nano": 0, "count": 0})
        entry["nano"] += nano
        entry["count"] += 1

    rows = sorted(per_gift.items(), key=lambda kv: kv[1]["nano"], reverse=True)
    return rows, summary


def _build_top_report(wallet: str, rows: list, summary: dict, days: int | None) -> str:
    """HTML-сообщение с топом: сумма за период + доходность TON/сут.

    Знаменатель один для всех строк (дни периода), чтобы рейтинг по сумме и
    по доходности были сравнимы между собой, а не завышали разовые платежи.
    """
    now_ts = int(datetime.now(MSK).timestamp())
    if days:
        period_days = float(days)
    else:
        period_days = max(1.0, (now_ts - summary["first_ts"]) / 86400)

    title = f"за {days} дн." if days else "за всё время"
    total = _format_ton(_nano_to_ton(str(summary["total_nano"])))
    # Доход за период и доход, разложенный по подаркам, — разные числа:
    # непривязанные платежи считаются в общем итоге, но в топ не попадают.
    linked_nano = summary["total_nano"] - summary["unlinked_nano"]
    linked_count = summary["count"] - summary["unlinked_count"]
    lines = [
        f"<b>ТОП подарков {title}</b>",
        f"Кошелёк: <code>{escape_html(wallet)}</code>",
        f"Период: {datetime.fromtimestamp(summary['first_ts'], MSK).strftime('%d.%m.%Y')} — "
        f"{datetime.fromtimestamp(summary['last_ts'], MSK).strftime('%d.%m.%Y')} "
        f"({period_days:.0f} дн.)",
        f"Доход за период: <b>{total} TON</b> · {summary['count']} плат.",
        f"Привязано к подаркам: <b>{_format_ton(_nano_to_ton(str(linked_nano)))} TON</b> · "
        f"{linked_count} плат. · {len(rows)} подарков",
        f"Топ-{min(TOP_LIMIT, len(rows))} по сумме, доходность = сумма ÷ дни периода:\n",
    ]

    for i, (addr, gift) in enumerate(rows[:TOP_LIMIT], 1):
        ton_value = _nano_to_ton(str(gift["nano"]))
        name = escape_html(gift["name"]) if gift["name"] else "без названия"
        lines.append(
            f"{i}. <a href=\"https://getgems.io/nft/{addr}\">{name}</a> — "
            f"<b>{_format_ton(ton_value)} TON</b> ({gift['count']} плат.) · "
            f"{_format_rate(ton_value / period_days)} TON/сут"
        )

    if summary["unlinked_count"]:
        unlinked = _format_ton(_nano_to_ton(str(summary["unlinked_nano"])))
        lines.append(
            f"\nНе привязано к NFT: {summary['unlinked_count']} плат. на {unlinked} TON — "
            "в топ не попали."
        )

    lines.append(f"\n<i>{datetime.now(MSK).strftime('%d.%m.%Y %H:%M')}</i>")
    return "\n".join(lines)


async def marketapptop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/marketapptop вызван, args={context.args}")
    try:
        days, rest = _parse_top_days(context.args)
    except ValueError as e:
        await update.message.reply_text(
            f"Неверный период: {e}.\n"
            "Использование: /marketapptop [дней] [адрес кошелька]\n"
            "Примеры: /marketapptop — за всё время, /marketapptop 7 — за 7 дней."
        )
        return

    resolved = await _resolve_wallet(
        update,
        context,
        "marketapptop",
        rest,
        usage=(
            "Использование: /marketapptop [дней] [адрес кошелька]\n"
            "Примеры: /marketapptop — за всё время, /marketapptop 7 — за 7 дней."
        ),
    )
    if resolved is None:
        return
    wallet, is_owner = resolved

    api_token = context.bot_data.get("MARKETAPP_API_KEY")
    chat_id = update.message.chat_id
    if chat_id in _sync_in_progress:
        await update.message.reply_text(
            f"Уже выполняется: {_sync_in_progress[chat_id]}. Подожди готовый отчёт."
        )
        return
    _sync_in_progress[chat_id] = "топ подарков"

    await update.message.reply_text(
        "Собираю топ подарков в фоне — чат не блокирую, отвечу с готовым."
    )
    asyncio.create_task(
        _top_report_task(context.bot, chat_id, wallet, api_token, is_owner, days)
    )


async def _top_report_task(bot, chat_id: int, wallet: str, api_token: str,
                           is_owner: bool, days: int | None):
    """Фоновый топ: обогащение NFT по ценам — тот же тяжёлый шаг, что и в отчёте."""
    try:
        await _run_top_report(bot, chat_id, wallet, api_token, is_owner, days)
    except Exception as e:
        logger.error(f"/marketapptop: фоновый топ упал: {e}", exc_info=True)
        try:
            await bot.send_message(chat_id, "Топ прервался из-за ошибки. Попробуй позже.")
        except Exception:
            pass
    finally:
        _sync_in_progress.pop(chat_id, None)


async def _run_top_report(bot, chat_id: int, wallet: str, api_token: str,
                          is_owner: bool, days: int | None):
    # Сами наполняем базу при деплое-обнулении и привязываем платежи к NFT
    events = await _ensure_rent_data(bot, chat_id, wallet, api_token, is_owner)
    logger.info(f"/marketapptop: событий в БД={len(events)}, "
                f"с-подарком={_count_linked(events)}, days={days}")
    if not events:
        await bot.send_message(
            chat_id,
            f"Платежей по кошельку <code>{escape_html(wallet)}</code> нет даже после "
            "полной синхронизации.\n"
            "Проверь MARKETAPP_WALLET и запусти /marketapprent — он покажет период сбора.",
            parse_mode="HTML",
        )
        return

    since_ts = _ts_days_ago(days) if days else 0
    rows, summary = _top_rows(events, since_ts)

    if not summary["count"]:
        await bot.send_message(chat_id, "За этот период платежей не было.")
        return

    if not rows:
        period_events = [e for e in events if int(e.get("ts", 0) or 0) >= since_ts]
        await _send_no_linkage(bot, chat_id, period_events, api_token, is_owner)
        return

    await bot.send_message(
        chat_id, _build_top_report(wallet, rows, summary, days), parse_mode="HTML"
    )