# main.py
import os
import time
import logging
import json
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

from utils.common import setup_logging
from utils.database import init_db, increment_stat, get_stat, get_total_users, get_messages_today
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

load_dotenv()
setup_logging()

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
LANE_MODEL = os.getenv("LANE_MODEL", "google/gemma-4-31b-it:free")
NEWS_CHANNEL_ID = os.getenv("NEWS_CHANNEL_ID")
PORT = int(os.getenv("PORT", "10000"))

BOT_START_TIME = time.time()

flask_app = Flask(__name__)


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
    flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)


async def post_init(application: Application):
    logger.info("Инициализация бота...")

    init_db()
    preload_all_prompts()

    try:
        bot_info = await application.bot.get_me()
        application.bot_data["bot_username"] = bot_info.username
        application.bot_data["bot_id"] = bot_info.id
        logger.info(f"Бот @{bot_info.username} запущен")
    except Exception as e:
        logger.error(f"Ошибка получения информации о боте: {e}")

    application.bot_data.update({
        "DEFAULT_MODEL": DEFAULT_MODEL,
        "LANE_MODEL": LANE_MODEL,
        "FALLBACK_MODEL": FALLBACK_MODEL,
        "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
        "NEWS_CHANNEL_ID": NEWS_CHANNEL_ID,
    })

    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Старый вебхук и pending updates удалены")
    except Exception:
        pass

    if NEWS_CHANNEL_ID:
        setup_autoposting(application)
        logger.info(f"Автопостинг настроен для {NEWS_CHANNEL_ID}")


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    if not query:
        return

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
        if len(response) > 1000:
            response = response[:997] + "..."

        results = [
            InlineQueryResultArticle(
                id=f"allira_{hash(query)}",
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
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN не найден!")
        return

    Thread(target=run_flask, daemon=True).start()
    logger.info("Health-check сервер запущен")

    application = Application.builder() \
        .token(BOT_TOKEN) \
        .post_init(post_init) \
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

    if NEWS_CHANNEL_ID:
        application.add_handler(MessageHandler(
            filters.TEXT & filters.Chat(chat_id=NEWS_CHANNEL_ID) & ~filters.COMMAND,
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
