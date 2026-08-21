import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "=> СИСТЕМА ОНЛАЙН. Приветствую, кожаный мешок.\n\n"
    "Я — Аллира. Цифровая королева криптохаоса. "
    "А со мной Лэйн — загадочный технофилософ, который шепчет на языке алгоритмов.\n\n"
    "Мы тут не просто болтаем — мы генерим контент, турнирим на кубиках "
    "и кормим твою ленту криптоинсайтами.\n\n"
    "Что умею:\n"
    "    -> Турнир на кубиках — кидай кости, побеждай, забирай очки\n"
    "    -> Автопостинг — новости с рынка прямо в твой канал\n"
    "    -> ИИ-чат — спрашивай про крипту, технологии, жизнь\n"
    "    -> Инлайн-режим — вызывай из любого чата\n\n"
    "Команды:\n"
    "    /start_tournament — запустить турнир\n"
    "    /stop_tournament — остановить турнир\n"
    "    /stats — статистика бота\n"
    "    /history — история турниров\n"
    "    /leaderboard — таблица лидеров\n"
    "    /clear — сброс контекста\n"
    "    /help — подробная инструкция\n\n"
    "В ЛС я отвечаю на любые вопросы.\n"
    "В группе — упомяни меня (@AlliraCryptoQueen_bot) или ответь на моё сообщение.\n\n"
    "DYOR и поехали. 🚀"
)

HELP_TEXT = (
    "=> ИНСТРУКЦИЯ ПО ИСПОЛЬЗОВАНИЮ\n\n"
    "В ЛИЧКЕ:\n"
    "Просто напиши мне — я отвечу. Выбери тему, и я подстроюсь:\n"
    "    - Крипта, трейдинг, инвестиции -> Аллира\n"
    "    - Технологии, ИИ, философия -> Лэйн\n\n"
    "В ГРУППОВОМ ЧАТЕ:\n"
    "    - Упомяни @AlliraCryptoQueen_bot в сообщении\n"
    "    - Или ответь реплаем на моё сообщение\n"
    "    - Или напиши \"аллира\" или \"лейн\"\n\n"
    "ИНЛАЙН-РЕЖИМ:\n"
    "    Напиши в любом чате: @AlliraCryptoQueen_bot <твой вопрос>\n"
    "    Выбери ответ — Аллира или Лэйн — и он отправится в чат.\n\n"
    "ТУРНИР НА КУБИКАХ:\n"
    "    1. Нажми /start_tournament\n"
    "    2. Игроки жмут \"Войти на арену\"\n"
    "    3. Админ жмёт \"Завершить набор\" и выбирает режим:\n"
    "        - 1 на 1\n"
    "        - Все против всех\n"
    "    4. Игроки кидают кубик через кнопку \"Бросить кубик\"\n"
    "    5. Админ запускает следующий раунд\n"
    "    6. Последний выживший — победитель\n\n"
    "    /stop_tournament — принудительная остановка\n\n"
    "СТАТИСТИКА:\n"
    "    /stats — общая статистика бота\n"
    "    /history — последние турниры\n"
    "    /leaderboard — топ игроков\n\n"
    "АВТОПОСТИНГ:\n"
    "    Бот автоматически постит новости в подключённый канал.\n"
    "    Интервал зависит от времени суток (чаще днём, реже ночью).\n\n"
    "DYOR. Не financial advice. Но я же предупреждала. 😏"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Турнир на кубиках", callback_data="start_tournament_from_menu")],
        [InlineKeyboardButton("Помощь", callback_data="show_help")]
    ])

    await update.message.reply_text(WELCOME_TEXT, reply_markup=keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def show_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(HELP_TEXT)
