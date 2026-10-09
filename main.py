import os
import time
import logging
import json
import uuid
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineQueryResultArticle,
    InputTextMessageContent,
    BotCommand,
    BotCommandScopeChat,
)
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    InlineQueryHandler,
)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from cachetools import TTLCache

from utils.common import setup_logging, escape_html
from utils.database import (
    init_db, increment_stat, get_stat, get_total_users, get_messages_today,
    close_all, prune_live_states, get_rent_events_stats, LAST_MATCH_PATHS,
    get_sync_state,
)
from utils.config import BotConfig
from utils.http_client import close_client
from prompts.loader import preload_all_prompts
from handlers.start_command import start_command, help_command, show_help_callback
from handlers.wallet_command import marketapprent_command, marketappgifts_command, marketapptop_command
from handlers.message_handler import (
    handle_message,
    handle_private_message,
    _get_history,
    HISTORY_KEY,
    LAST_SEEN_KEY,
)
from handlers.stats_command import stats_command, history_command, leaderboard_command
from handlers.moderation import ban_command, unban_command
from handlers.dice_tournament import (
    restore_tournaments,
    start_persist_task,
    stop_persist_task,
    start_dice_tournament_registration,
    register_for_tournament,
    end_registration_and_start_round,
    make_tournament_roll,
    stop_tournament_command,
    set_tournament_mode,
    start_tournament_from_menu,
)
from tasks.autoposting import setup_autoposting
from tasks.marketapp_reports import setup_marketapp_jobs, LAST_LINKAGE, LAST_HISTORY_META, LAST_SYNC, LAST_HTTP_ERROR

load_dotenv()
setup_logging()

logger = logging.getLogger(__name__)

config = BotConfig.from_env()
BOT_START_TIME = time.time()

_inline_rate_limit = TTLCache(maxsize=200, ttl=60)


def _build_health_payload() -> dict:
    uptime = int(time.time() - BOT_START_TIME)
    hours = uptime // 3600
    minutes = (uptime % 3600) // 60
    data = {
        "status": "ok",
        "uptime": f"{hours}h {minutes}m",
        "uptime_seconds": uptime,
    }
    try:
        data.update(asyncio.run(_collect_health_stats()))
    except Exception as e:
        # /health обязан всегда отдавать 200: на Render провал этой ручки
        # считается падением сервиса и вызывает рестарт.
        logging.getLogger(__name__).warning(f"/health: сбор статистики не удался: {e}")
        data.setdefault("total_messages", 0)
        data.setdefault("messages_today", 0)
        data.setdefault("total_users", 0)
    return data


class HealthHandler(BaseHTTPRequestHandler):
    """Ручка живости на stdlib.

    Раньше здесь был Flask и его dev-сервер (gunicorn стоял в requirements,
    но не использовался). Flask нужен был ровно для двух маршрутов, поэтому
    заменён на встроенный сервер — без лишней зависимости.
    """

    server_version = "Allira/1.0"

    def _respond(self, code: int, body: str, content_type: str = "text/plain; charset=utf-8"):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            try:
                body = json.dumps(_build_health_payload())
            except Exception as e:
                logging.getLogger(__name__).warning(f"/health: {e}")
                body = json.dumps({"status": "ok"})
            self._respond(200, body, "application/json")
        elif path == "/":
            self._respond(200, "Allira Bot is running!")
        else:
            self._respond(404, "not found")

    def log_message(self, fmt, *args):
        # Иначе каждый пинг UptimeRobot сыплет в лог отдельной строкой.
        logging.getLogger(__name__).debug("health: " + fmt, *args)


USER_DATA_TTL = 7 * 86400


async def purge_stale_user_data(context: ContextTypes.DEFAULT_TYPE):
    """Убирает user_data юзеров, которые не писали больше недели.

    Память диалога живёт в user_data и без этой задачи копилась бы вечно —
    процесс на бесплатном инстансе держится неделями и расползается.
    """
    now = time.time()
    removed = 0
    for user_id, data in list(context.application.user_data.items()):
        last_seen = data.get(LAST_SEEN_KEY, 0) if isinstance(data, dict) else 0
        if now - last_seen > USER_DATA_TTL:
            context.application.user_data.pop(user_id, None)
            removed += 1
    if removed:
        logger.info(f"Очищено неактивных профилей памяти: {removed}")


async def _collect_health_stats() -> dict:
    async def _sync_state():
        if not config.marketapp_wallet:
            return None
        return await get_sync_state(config.marketapp_wallet)

    total_messages, messages_today, total_users, rent, sync_state = await asyncio.gather(
        get_stat("total_messages"),
        get_messages_today(),
        get_total_users(),
        get_rent_events_stats(),
        _sync_state(),
    )
    return {
        "total_messages": total_messages,
        "messages_today": messages_today,
        "total_users": total_users,
        # Диагностика «нет данных» в отчётах: SQLite на Render стирается при
        # каждом деплое, и по этим счётчикам видно, наполнилась ли база аренды.
        "rent_events": rent["total"],
        "rent_events_distinct": rent["distinct_hash"],
        "rent_events_linked": rent["linked"],
        "rent_events_last_ts": rent["last_ts"],
        # Глубина истории: если скан блокчейна оборвался, oldest_ts встаёт
        # на последние дни и «доход за период» молча недосчитывает.
        "rent_events_oldest_ts": rent["oldest_ts"],
        "rent_events_span_days": rent["span_days"],
        "rent_events_by_source": rent["by_source"],
        # Какие комментарии транзакций составляют «доход»: аренда помечена в
        # блокчейне текстом, а посторонние переводы сюда попадают, если проходят
        # маркер. По «сколько привязано к NFT» видно, где фильтр пускает лишнее.
        "rent_comments": rent["comments"],
        "rent_duration": rent["duration"],
        # Последние прогоны скана (ключ — источник tonapi/toncenter):
        # страницы, завершённость, сохранено и ошибка. По ним видно, что
        # именно встало, если история перестала расти.
        "rent_sync": {
            path: {"age_s": int(time.time() - v["at"]), **v}
            for path, v in LAST_SYNC.items()
        },
        # Последняя ошибка сетевого слоя: какая именно страница не прошла
        # (429 / статус / сеть / обрезанный JSON) и когда. Скан-ошибки её
        # подмешивают в своё поле error, но здесь она видна целиком, даже
        # когда последний прогон прошёл чисто.
        "http_error": (
            None if not LAST_HTTP_ERROR["at"] else {
                "age_s": int(time.time() - LAST_HTTP_ERROR["at"]),
                "text": LAST_HTTP_ERROR["text"],
            }
        ),
        # Чекпоинт, от которого продолжается скан. hash_len=0 означает, что
        # границу записал tonapi (он пишет пустой hash) — пагинация TON Center
        # от такой границы невозможна, и скан уходит в полный перебор.
        "rent_sync_state": (
            None if not sync_state else {
                "lt": sync_state.get("last_synced_lt"),
                "hash_len": len(sync_state.get("last_synced_hash") or ""),
                "utime": sync_state.get("last_synced_utime"),
                "at": sync_state.get("last_sync_at"),
            }
        ),
        # Чем закончилась последняя попытка привязать платежи к подаркам:
        # collected — сколько записей вернул Marketapp, matched — сколько
        # совпало с блокчейном, error — причина, если упала.
        "linkage": {
            "age_s": int(time.time() - LAST_LINKAGE["at"]) if LAST_LINKAGE["at"] else None,
            "collected": LAST_LINKAGE["collected"],
            "matched": LAST_LINKAGE["matched"],
            "categories": LAST_LINKAGE["categories"],
            "api_keys": LAST_LINKAGE["api_keys"],
            "direction": LAST_LINKAGE["direction"],
            # Полнота выгрузки Marketapp: страниц прочитано, сколько записей
            # отброшено (чужие кошельки / без суммы) и на какой причине
            # остановилась пагинация по категориям — по этому видно, мало ли
            # отдаёт их API или мы сами чего-то не дочитываем.
            "pages": LAST_LINKAGE["pages"],
            "dropped": dict(LAST_LINKAGE["dropped"]),
            "stops": dict(LAST_LINKAGE["stops"]),
            # Глубина чтения ленты по категориям: top_ts — граница, ниже
            # которой прошлый прогон уже всё прочитал (по ней следующий
            # прогон читает только новое), depth_ts — как глубоко дошли.
            "feed": dict(LAST_LINKAGE["feed"]),
            # Чем привязались: hash — тот же tx (точно), exact — совпали
            # src/dst/ts, price_tight — цена в ±300 сек, price — в ±2 ч.
            "match_paths": dict(LAST_MATCH_PATHS),
            # Первый ответ rent/history: ключи конверта, записей на странице
            # и был ли курсор — видно, обрывается ли пагинация.
            "history_meta": dict(LAST_HISTORY_META),
            "error": LAST_LINKAGE["error"],
        },
        # Только признаки «задано/не задано», сами секреты не отдаются.
        "config": {
            "marketapp_wallet": bool(config.marketapp_wallet),
            "marketapp_api_key": bool(config.marketapp_api_key),
            "sync_jobs": bool(config.marketapp_api_key and config.marketapp_wallet),
            "tonapi_key": bool(config.tonapi_api_key),
            "toncenter_key": bool(config.toncenter_api_key),
            "admin_ids": len(config.admin_user_ids),
        },
    }


def run_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", config.port), HealthHandler)
    server.daemon_threads = True
    server.serve_forever()


# Команды для выпадающего меню "/" — их же Telegram подсказывает при наборе.
# Ограничения API: до 100 команд, имя до 32 символов (без "/"), подпись до 256.
BOT_COMMANDS: list[tuple[str, str]] = [
    ("start", "Меню и приветствие"),
    ("help", "Как мной пользоваться"),
    ("marketapptop", "ТОП подарков: сумма и доходность (TON/сут)"),
    ("marketappgifts", "Доход по каждому подарку отдельно"),
    ("marketapprent", "Отчёт по аренде кошелька"),
    ("stats", "Статистика бота"),
    ("history", "Последние турниры"),
    ("leaderboard", "Таблица лидеров"),
    ("clear", "Забыть контекст разговора"),
    ("start_tournament", "Запустить турнир на кубиках"),
    ("stop_tournament", "Остановить турнир"),
]

ADMIN_COMMANDS: list[tuple[str, str]] = [
    ("ban", "Забанить юзера (реплаем или с его id)"),
    ("unban", "Разбанить юзера"),
]


def _as_bot_commands(pairs: list[tuple[str, str]]) -> list[BotCommand]:
    return [BotCommand(name, description) for name, description in pairs]


async def setup_bot_commands(bot) -> None:
    """Команды в меню "/" для всех чатов и отдельный список — для админов.

    Scope у Telegram подменяет список целиком, а не дополняет: у админов в
    scope должен лежать общий список плюс /ban и /unban, иначе в личке с ботом
    они бы увидели только модерацию.
    """
    try:
        await bot.set_my_commands(_as_bot_commands(BOT_COMMANDS))
    except Exception as e:
        logger.warning(f"Не удалось задать команды меню: {e}")
        return

    admins = list(config.admin_user_ids)
    if not admins:
        return
    # У BotCommandScopeChat один chat_id (мульти-чат у Telegram в этом scope нет),
    # поэтому каждому админу отправляем свой список отдельным вызовом.
    for admin_id in admins:
        try:
            await bot.set_my_commands(
                _as_bot_commands(BOT_COMMANDS + ADMIN_COMMANDS),
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as e:
            logger.warning(f"Не удалось задать админ-команды для {admin_id}: {e}")


async def post_init(application: Application):
    logger.info("Инициализация бота...")

    init_db()
    preload_all_prompts()

    await restore_tournaments()
    start_persist_task()
    try:
        removed = await prune_live_states(max_age_hours=24)
        if removed:
            logger.info(f"Убрано зависших записей турниров: {removed}")
    except Exception as e:
        logger.warning(f"Прунинг турниров не удался: {e}")

    application.job_queue.run_repeating(
        purge_stale_user_data, interval=3600, first=1800, name="purge_user_data"
    )

    try:
        bot_info = await application.bot.get_me()
        config.bot_username = bot_info.username
        config.bot_id = bot_info.id
        application.bot_data["bot_username"] = bot_info.username
        application.bot_data["bot_id"] = bot_info.id
        logger.info(f"Бот @{bot_info.username} запущен")
    except Exception as e:
        logger.error(f"Ошибка получения информации о боте: {e}")
        bot_info = None

    # Команды в меню "/" — ставим сразу после get_me, чтобы подсказки появились
    # у всех пользователей, а не после остальной инициализации.
    await setup_bot_commands(application.bot)

    if config.admin_chat_id and bot_info is not None:
        try:
            await application.bot.send_message(
                config.admin_chat_id,
                f"Бот @{bot_info.username} запущен (uptime сброслен)"
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить стартовое уведомление: {e}")
    else:
        logger.warning("ADMIN_CHAT_ID не задан — алерты об ошибках не придут в Telegram")

    if config.admin_chat_id and not config.admin_chat_id.lstrip("-").isdigit():
        # @username Телеграм принимает только для публичных супергрупп. Для
        # личного чата нужен numeric id, иначе алерты молча не приходят.
        logger.error(
            f"ADMIN_CHAT_ID={config.admin_chat_id!r} не похож на числовой ID. "
            f"Алерты придут, только если это публичный @username супергруппы. "
            f"Для личного чата возьми ID через @userinfobot."
        )

    application.bot_data.update({
        "DEFAULT_MODEL": config.default_model,
        "LANE_MODEL": config.lane_model,
        "VISION_MODEL": config.vision_model,
        "FALLBACK_MODEL": config.fallback_model,
        "OPENROUTER_API_KEY": config.openrouter_api_key,
        "NEWS_CHANNEL_ID": config.news_channel_id,
        "MARKETAPP_API_KEY": config.marketapp_api_key,
        "MARKETAPP_WALLET": config.marketapp_wallet,
        "ADMIN_USER_IDS": config.admin_user_ids,
        "CREATOR_USER_IDS": config.creator_user_ids,
        "CREATOR_USERNAME": config.creator_username,
    })
    if not config.admin_user_ids:
        logger.warning(
            "ADMIN_USER_IDS не задан — команды /ban и /unban никому не доступны"
        )
    if not config.creator_user_ids and not config.creator_username:
        logger.warning(
            "CREATOR_USER_IDS и CREATOR_USERNAME не заданы — в ЛС отвечает всем"
        )

    # Сразу видно в логах Render, откуда берутся данные отчётов: базу аренды
    # free-план стирает при каждом деплое, и без этой строки непонятно,
    # «нет данных» — это пустой кошелёк или деплой обнулил SQLite.
    rent_stats = await get_rent_events_stats()
    last_event = (
        datetime.fromtimestamp(rent_stats["last_ts"]).strftime("%d.%m.%Y %H:%M")
        if rent_stats["last_ts"] else "-"
    )
    logger.info(
        "Состояние данных: событий аренды %s (привязано к NFT %s), последнее от %s | "
        "MARKETAPP_WALLET=%s MARKETAPP_API_KEY=%s синк-джобы=%s | "
        "TONAPI=%s TONCENTER=%s админов=%s",
        rent_stats["total"], rent_stats["linked"], last_event,
        "да" if config.marketapp_wallet else "НЕТ",
        "да" if config.marketapp_api_key else "НЕТ",
        "да" if (config.marketapp_api_key and config.marketapp_wallet) else "НЕТ",
        "да" if config.tonapi_api_key else "НЕТ",
        "да" if config.toncenter_api_key else "НЕТ",
        len(config.admin_user_ids),
    )

    try:
        await application.bot.delete_webhook(drop_pending_updates=False)
        logger.info("Вебхук снят (pending updates сохранены)")
    except Exception as e:
        logger.warning(f"Ошибка удаления вебхука: {e}")

    if config.news_channel_id:
        setup_autoposting(application)
        logger.info(f"Автопостинг настроен для {config.news_channel_id}")

    if config.marketapp_api_key and config.marketapp_wallet:
        setup_marketapp_jobs(application)
        logger.info("Marketapp отчеты настроены")


async def post_shutdown(application: Application):
    logger.info("Завершение работы бота...")
    await stop_persist_task()
    await close_client()
    close_all()
    logger.info("Бот остановлен")


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inline = update.inline_query
    query = inline.query.strip()

    # Любой ранний выход обязан закрывать запрос, иначе у пользователя
    # остаётся вечный спиннер "загрузка..." до таймаута клиента.
    if not query:
        await inline.answer([], cache_time=0, is_personal=True)
        return

    user_id = inline.from_user.id
    now = time.time()
    last_call = _inline_rate_limit.get(user_id, 0)
    if now - last_call < 3.0:
        await inline.answer([], cache_time=0, is_personal=True)
        return
    _inline_rate_limit[user_id] = now

    from utils.ai_responses import decide_speaker, get_llm_response, is_service_error
    from prompts.loader import load_prompt

    speaker = decide_speaker(query)
    model = context.bot_data["LANE_MODEL"] if speaker == "lane" else context.bot_data["DEFAULT_MODEL"]

    try:
        response = await get_llm_response(
            user_prompt=query,
            system_prompt=load_prompt(speaker),
            model=model,
            api_key=context.bot_data["OPENROUTER_API_KEY"]
        )
    except Exception as e:
        logger.error(f"Inline error: {e}")
        await inline.answer([], cache_time=0, is_personal=True)
        return

    # Заглушки ("Все модели недоступны", "Сервис недоступен"...) в чат не идут.
    if is_service_error(response):
        await inline.answer([], cache_time=0, is_personal=True)
        return

    if len(response) > 1000:
        response = response[:997] + "..."

    results = [
        InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title=f"⚡ {speaker.title()}",
            description=response[:100],
            input_message_content=InputTextMessageContent(
                # HTML вместо Markdown: текст модели в Markdown-разметке
                # падал с BadRequest на первом же невинном символе.
                message_text=f"<b>{speaker.title()}:</b> {escape_html(response)}",
                parse_mode="HTML"
            )
        )
    ]
    try:
        await inline.answer(results, cache_time=300, is_personal=True)
    except Exception as e:
        logger.error(f"Inline answer error: {e}")


# Дедупликация алертов: в момент деплоя одна и та же ошибка прилетает
# пачкой (PTB ретраит поллинг), и админ получал четыре одинаковых сообщения.
_alert_cache: TTLCache = TTLCache(maxsize=64, ttl=600)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Ловит необработанные исключения: пишет в лог и сообщает админу.

    Раньше исключение уходило только в лог, и пользователь видел тишину —
    поэтому поломки приходилось диагностировать вслепую.
    """
    error = context.error

    # Conflict = второй поллер на том же токене. При деплое на Render это
    # штатно: старый инстанс ещё держит getUpdates, пока новый поднимается.
    # Алертить им админа бессмысленно — он самоизлечивается за секунды.
    if isinstance(error, Conflict):
        logger.warning(f"Конфликт поллинга (другой инстанс): {error}")
        return

    logger.error("Необработанная ошибка при обработке апдейта", exc_info=error)

    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    where = "неизвестный чат"
    if chat is not None:
        where = f"чат {getattr(chat, 'id', '?')}"
    who = f"от {user.id}" if user else ""
    alert_key = f"{type(error).__name__}:{str(error)[:200]}"
    try:
        if config.admin_chat_id and alert_key not in _alert_cache:
            _alert_cache[alert_key] = True
            await context.bot.send_message(
                config.admin_chat_id,
                f"Ошибка бота ({where}, {who}):\n{type(error).__name__}: {error}"[:1000]
            )
    except Exception as e:
        logger.error(f"Не удалось отправить алерт админу: {e}")

    if chat is not None:
        try:
            await chat.send_message("Что-то сломалось на моей стороне, попробуй ещё раз.")
        except Exception:
            pass


async def clear_context_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    turns = len(_get_history(context)) // 2
    # Чистим только память диалога: context.user_data.clear() затирал бы
    # и остальные ключи пользователя (включая метку активности).
    context.user_data.pop(HISTORY_KEY, None)
    if turns:
        await update.message.reply_text(
            f"=> Контекст сброшен, забыто {turns} реплик. Начинай с чистого листа."
        )
    else:
        await update.message.reply_text("=> Контекст и так пустой. С чего начнём?")


def build_application() -> Application:
    """Собирает приложение: билдер, все хендлеры, error handler.

    Вынесено из main(), чтобы сборку можно было проверить тестом: ошибки в
    сигнатурах PTB (например, лишний аргумент run_polling) иначе всплывают
    только на проде при старте и роняют бота в бесконечный рестарт.
    """
    # Без этого PTB обрабатывает апдейты строго последовательно: один долгий
    # LLM-запрос или синк кошелька останавливал бы бота для всех юзеров.
    # Ограниченный семафор — чтобы всплеск не выжег free-лимиты OpenRouter.
    # В PTB 21.1.1 этот флаг есть только у билдера, у run_polling его нет.
    application = Application.builder() \
        .token(config.bot_token) \
        .concurrent_updates(4) \
        .post_init(post_init) \
        .post_shutdown(post_shutdown) \
        .build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("start_tournament", start_dice_tournament_registration))
    application.add_handler(CommandHandler("stop_tournament", stop_tournament_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("clear", clear_context_command))
    application.add_handler(CommandHandler("marketapprent", marketapprent_command))
    application.add_handler(CommandHandler("marketappgifts", marketappgifts_command))
    application.add_handler(CommandHandler("marketapptop", marketapptop_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))

    application.add_handler(CallbackQueryHandler(
        show_help_callback, pattern="^show_help$"
    ))
    application.add_handler(CallbackQueryHandler(
        start_tournament_from_menu, pattern="^start_tournament_from_menu$"
    ))
    application.add_handler(CallbackQueryHandler(
        register_for_tournament, pattern="^register_for_tournament$"
    ))
    application.add_handler(CallbackQueryHandler(
        make_tournament_roll, pattern="^make_tournament_roll$"
    ))
    application.add_handler(CallbackQueryHandler(
        end_registration_and_start_round, pattern="^end_registration_and_start_round$"
    ))
    application.add_handler(CallbackQueryHandler(
        set_tournament_mode, pattern="^set_tournament_mode:"
    ))

    application.add_handler(InlineQueryHandler(handle_inline_query))

    application.add_error_handler(error_handler)

    # Фильтр контента: текст, подписи к медиа и сами вложения (фото, гифки,
    # стикеры, файлы). COMMAND отсекает только текстовые команды — у подписей
    # свои caption_entities, но команды в подписи никто не шлёт.
    content = (filters.TEXT | filters.CAPTION | filters.ATTACHMENT) & ~filters.COMMAND

    if config.news_channel_id:
        application.add_handler(MessageHandler(
            content & filters.Chat(chat_id=config.news_channel_id),
            handle_message
        ))

    application.add_handler(MessageHandler(
        content & filters.ChatType.GROUPS,
        handle_message
    ))

    application.add_handler(MessageHandler(
        content & filters.ChatType.PRIVATE,
        handle_private_message
    ))

    return application


def main():
    if not config.bot_token:
        logger.critical("BOT_TOKEN не найден!")
        return

    Thread(target=run_health_server, daemon=True).start()
    logger.info(f"Health-check сервер запущен на порту {config.port}")

    application = build_application()
    logger.info("Запуск в режиме polling")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
