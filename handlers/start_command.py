import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Начать турнир", callback_data="start_tournament_from_menu")]
    ])
    
    text = (
        "Привет! Я твой помощник в мире криптовалют и NFT.\n\n"
        "Организую турниры в кости\n"
        "Автоматически публикую новости\n"
        "Отвечаю в стиле персонажей: Аллира и Лэйн\n\n"
        "Используй команды:\n"
        "/start_tournament - запустить турнир\n"
        "/stop_tournament - остановить текущий турнир"
    )
    
    await update.message.reply_text(text, reply_markup=keyboard)
