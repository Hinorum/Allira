# main.py
import os
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)
from telegram.constants import ChatAction
from flask import Flask
from threading import Thread

# Инициализация модулей
from utils.common import setup_logging
from prompts.loader import preload_all_prompts
from handlers.start_command import start_command
from handlers.message_handler import handle_message, handle_private_message
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

# Загрузка конфигурации
load_dotenv()
setup_logging()

logger = logging.getLogger(__name__)

# Конфигурация бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "deepseek/deepseek-chat:free")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "meta-llama/llama-4-maverick:free")
LANE_MODEL = os.getenv("LANE_MODEL", "google/gemini-2.5-flash-preview:free")
NEWS_CHANNEL_ID = os.getenv("NEWS_CHANNEL_ID")
PORT = int(os.getenv("PORT", "10000"))

# Настройка вебхука для Railway
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"https://{RAILWAY_PUBLIC_DOMAIN}{WEBHOOK_PATH}" if RAILWAY_PUBLIC_DOMAIN else None
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN")

# Flask для health-check
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Allira Bot is running!", 200

@flask_app.route('/health')
def health():
    return "OK", 200

@flask_app.route('/models')
def list_models():
    import httpx
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return "No API key", 500
    try:
        r = httpx.get(
            'https://openrouter.ai/api/v1/models',
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=30
        )
        if r.status_code != 200:
            return f"API error: {r.status_code} {r.text[:200]}", 500
        data = r.json()
        free = [m['id'] for m in data.get('data', []) if ':free' in m['id']]
        free.sort()
        lines = [f"<h2>Free models ({len(free)}):</h2><ul>"]
        for m in free:
            lines.append(f"<li><code>{m}</code></li>")
        lines.append("</ul>")
        return "\n".join(lines), 200
    except Exception as e:
        return f"Error: {e}", 500

def run_flask():
    """Запуск Flask сервера для health-check"""
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

async def post_init(application: Application):
    """Инициализация после запуска"""
    logger.info("Инициализация бота...")
    
    try:
        bot_info = await application.bot.get_me()
        application.bot_data["bot_username"] = bot_info.username
        application.bot_data["bot_id"] = bot_info.id
        logger.info(f"Бот @{bot_info.username} запущен")
    except Exception as e:
        logger.error(f"Ошибка получения информации о боте: {e}")

    # Сохраняем конфигурацию
    application.bot_data.update({
        "DEFAULT_MODEL": DEFAULT_MODEL,
        "LANE_MODEL": LANE_MODEL,
        "FALLBACK_MODEL": FALLBACK_MODEL,
        "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
        "PEXELS_API_KEY": PEXELS_API_KEY,
        "NEWS_CHANNEL_ID": NEWS_CHANNEL_ID,
    })

    # Предзагрузка промптов
    preload_all_prompts()

    # Настройка вебхука
    if WEBHOOK_URL and WEBHOOK_SECRET_TOKEN:
        try:
            await application.bot.set_webhook(
                url=WEBHOOK_URL,
                secret_token=WEBHOOK_SECRET_TOKEN,
                allowed_updates=Update.ALL_TYPES
            )
            logger.info(f"Вебхук установлен: {WEBHOOK_URL}")
        except Exception as e:
            logger.error(f"Ошибка установки вебхука: {e}")

    # Настройка автопостинга
    if NEWS_CHANNEL_ID:
        setup_autoposting(application)
        logger.info(f"Автопостинг настроен для {NEWS_CHANNEL_ID}")

async def post_shutdown(application: Application):
    """Действия при завершении"""
    logger.info("Завершение работы бота...")
    if WEBHOOK_URL:
        try:
            await application.bot.delete_webhook()
            logger.info("Вебхук удален")
        except Exception as e:
            logger.error(f"Ошибка удаления вебхука: {e}")

def main():
    """Главная функция запуска"""
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN не найден!")
        return

    # Запуск Flask для health-check
    Thread(target=run_flask, daemon=True).start()
    logger.info("Health-check сервер запущен")

    # Создание приложения
    application = Application.builder() \
        .token(BOT_TOKEN) \
        .post_init(post_init) \
        .post_shutdown(post_shutdown) \
        .build()

    # Регистрация обработчиков команд
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("start_tournament", start_dice_tournament_registration))
    application.add_handler(CommandHandler("stop_tournament", stop_tournament_command))

    # Callback обработчики для турнира
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

    # Обработчик сообщений в канале (только для новостного канала)
    if NEWS_CHANNEL_ID:
        application.add_handler(MessageHandler(
            filters.TEXT & filters.Chat(chat_id=NEWS_CHANNEL_ID) & ~filters.COMMAND,
            handle_message
        ))

    # Обработчик личных сообщений
    application.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_private_message
    ))

    # Запуск бота
    if WEBHOOK_URL:
        logger.info(f"Запуск в режиме вебхука: {WEBHOOK_URL}")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET_TOKEN,
            url_path=WEBHOOK_PATH
        )
    else:
        logger.info("Запуск в режиме polling")
        application.run_polling()

if __name__ == "__main__":
    main()
