import os
import time
import logging
import json
import uuid
from dotenv import load_dotenv
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    InlineQueryHandler,
)
from flask import Flask
from threading import Thread
from cachetools import TTLCache

from utils.common import setup_logging
from utils.database import init_db, increment_stat, get_stat, get_total_users, get_messages_today, close_all
from utils.config import BotConfig
from utils.http_client import close_client
from prompts.loader import preload_all_prompts
from handlers.start_command import start_command, help_command, show_help_callback
from handlers.message_handler import handle_message, handle_private_message
from handlers.stats_command import stats_command, history_command, leaderboard_command
from handlers.dice_tournament import (
    start_dice_tournament_registration,
    register_for_tournament,
    end_registration_and_start_round,
    make_tournament_roll,
    stop_tournament_command,
    set_tournament_mode,
    start_tournament_from_menu,
)
from tasks.autoposting import setup_autoposting
from tasks.marketapp_reports import setup_marketapp_jobs

load_dotenv()
setup_logging()

logger = logging.getLogger(__name__)

config = BotConfig.from_env()
BOT_START_TIME = time.time()

flask_app = Flask(__name__)

_inline_rate_limit = TTLCache(maxsize=200, ttl=60)


@flask_app.route('/')
def home():
    return "Allira Bot is running!", 200


@flask_app.route('/health')
def health():
    uptime = int(time.time() - BOT_START_TIME)
    hours = uptime // 3600
    minutes = (uptime % 3600) // 60
    data = {
        "status": "ok",
        "uptime": f"{hours}h {minutes}m",
        "uptime_seconds": uptime,
        "total_messages": get_stat("total_messages"),
        "messages_today": get_messages_today(),
        "total_users": get_total_users(),
    }
    return json.dumps(data), 200, {"Content-Type": "application/json"}


def run_flask():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    flask_app.run(host='0.0.0.0', port=config.port, debug=False, use_reloader=False)


async def post_init(application: Application):
    logger.info("Инициализация бота...")

    init_db()
    preload_all_prompts()

    try:
        bot_info = await application.bot.get_me()
        config.bot_username = bot_info.username
        config.bot_id = bot_info.id
        application.bot_data["bot_username"] = bot_info.username
        application.bot_data["bot_id"] = bot_info.id
        logger.info(f"Бот @{bot_info.username} запущен")
    except Exception as e:
        logger.error(f"Ошибка получения информации о боте: {e}")

    application.bot_data.update({
        "DEFAULT_MODEL": config.default_model,
        "LANE_MODEL": config.lane_model,
        "FALLBACK_MODEL": config.fallback_model,
        "OPENROUTER_API_KEY": config.openrouter_api_key,
        "NEWS_CHANNEL_ID": config.news_channel_id,
        "MARKETAPP_API_KEY": config.marketapp_api_key,
        "MARKETAPP_WALLET": config.marketapp_wallet,
    })

    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Старый вебхук и pending updates удалены")
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
    await close_client()
    close_all()
    logger.info("Бот остановлен")


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    if not query:
        return

    user_id = update.inline_query.from_user.id
    now = time.time()
    last_call = _inline_rate_limit.get(user_id, 0)
    if now - last_call < 3.0:
        return
    _inline_rate_limit[user_id] = now

    from utils.ai_responses import decide_speaker
    from prompts.loader import load_prompt
    from utils.ai_responses import get_llm_response

    speaker = decide_speaker(query)
    model = context.bot_data["LANE_MODEL"] if speaker == "lane" else context.bot_data["DEFAULT_MODEL"]

    try:
        response = await get_llm_response(
            user_prompt=query,
            system_prompt=load_prompt(speaker),
            model=model,
            api_key=context.bot_data["OPENROUTER_API_KEY"]
        )

        if response.startswith("Технические") or response.startswith("Слишком") or response.startswith("Сервис") or response.startswith("Все модели") or response.startswith("Модель вернула"):
            return

        if len(response) > 1000:
            response = response[:997] + "..."

        results = [
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title=f"⚡ {speaker.title()}",
                description=response[:100],
                input_message_content=InputTextMessageContent(
                    message_text=f"*{speaker.title()}:* {response}",
                    parse_mode="Markdown"
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=300, is_personal=True)
    except Exception as e:
        logger.error(f"Inline error: {e}")


async def clear_context_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("=> Контекст сброшен. Начинай с чистого листа.")


def main():
    if not config.bot_token:
        logger.critical("BOT_TOKEN не найден!")
        return

    Thread(target=run_flask, daemon=True).start()
    logger.info("Health-check сервер запущен")

    application = Application.builder() \
        .token(config.bot_token) \
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

    if config.news_channel_id:
        application.add_handler(MessageHandler(
            filters.TEXT & filters.Chat(chat_id=config.news_channel_id) & ~filters.COMMAND,
            handle_message
        ))

    application.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        handle_message
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_private_message
    ))

    logger.info("Запуск в режиме polling")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
