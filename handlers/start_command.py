"""Главное меню бота: разделы, подменю, быстрые действия.

Раньше `/start` показывал две кнопки — «Турнир на кубиках» и «Помощь» —
при трёх десятках команд. Новичок видел пустое меню и уходил в
`/help`, где лежала простыня текста без навигации.

Теперь `/start` — это витрина: шесть разделов крупными кнопками, а
внутри каждого — конкретные команды с примерами. Кнопки-«быстрые
действия» выполняют команду сразу, без набора текста.
"""

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import CallbackQueryHandler, ContextTypes

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "👋 <b>Привет!</b> Я Аллира — крипто-шлюз, а рядом со мной Лэйн, "
    "тихий технофилософ.\n\n"
    "Что я умею:\n"
    "    🎲 <b>Турнир на кубиках</b> — сыграй с друзьями прямо в чате\n"
    "    💼 <b>Портфель</b> — что у тебя на балансе и в плюс\n"
    "    🧾 <b>Операции</b> — записывай сделки, аирдропы, обмены\n"
    "    📊 <b>Отчётность</b> — сводка 3-НДФЛ и выгрузки для ФНС\n"
    "    🎁 <b>Аренда подарков</b> — доход с аренды NFT на TON\n"
    "    💬 <b>Спросить</b> — просто напиши мне в личку\n\n"
    "Выбери раздел ниже — подскажу, с чего начать.\n\n"
    "DYOR и поехали. 🚀"
)

HELP_TEXT = (
    "=> ИНСТРУКЦИЯ ПО ИСПОЛЬЗОВАНИЮ\n\n"
    "В ЛИЧКЕ:\n"
    "Просто напиши мне — я отвечу. Выбери тему, и я подстроюсь:\n"
    "    - Крипта, трейдинг, инвестиции -> Аллира\n"
    "    - Технологии, ИИ, философия -> Лэйн\n\n"
    "ЖИВОЙ ЧАТ:\n"
    "    - Пока думаю — висит «печатает…», так что не ищи меня в списке\n"
    "    - На твоё сообщение ставлю реакцию: значит, я тебя услышал\n"
    "    - Под каждым ответом — кнопки быстрых ответов (рынок, портфель,\n"
    "      операции, сбросить память)\n"
    "    - Про крипту говорю с живыми цифрами рынка, а не по памяти\n"
    "    - /market — срез рынка: топ, лидеры роста и падения, TON\n\n"
    "ПАМЯТЬ:\n"
    "Я помню последние 8 реплик нашего разговора, так что можно не объяснять\n"
    "всё заново — просто продолжай. Лишние детали могу и забыть, извини.\n"
    "Сбросить память — /clear\n\n"
    "В ГРУППОВОМ ЧАТЕ:\n"
    "    - Упомяни @AlliraCryptoQueen_bot в сообщении\n"
    "    - Или ответь реплаем на моё сообщение\n"
    "    - Или напиши \"аллира\" или \"лейн\"\n\n"
    "ИНЛАЙН-РЕЖИМ:\n"
    "Напиши в любом чате: @AlliraCryptoQueen_bot <твой вопрос>\n"
    "Выбери ответ — Аллира или Лэйн — и он отправится в чат.\n\n"
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
    "ДОХОД ОТ АРЕНДЫ ПОДАРКОВ:\n"
    "    /marketapptop — топ-10 подарков по сумме и доходности (TON/сут)\n"
    "    /marketapptop 7 — то же по последним 7 дням (можно 30, 90 и т.д.)\n"
    "    /marketapptop 7 <адрес кошелька> — то же по чужому кошельку\n"
    "    Без адреса берётся кошелёк из конфига — он же и «свой» для отчётов.\n"
    "    /marketappgifts — доход по каждому подарку отдельно\n"
    "    /marketapprent — отчёт по аренде кошелька\n\n"
    "АВТОПОСТИНГ:\n"
    "    Бот автоматически постит новости в подключённый канал.\n"
    "    Интервал зависит от времени суток (чаще днём, реже ночью).\n\n"
    "КРИПТО-БУХГАЛТЕРИЯ (3-НДФЛ по НК РФ, ред.418-ФЗ):\n"
    "    /wallet_add <chain> <address> [label] — зарегистрировать кошелёк\n"
    "        chain ∈ ton, evm, btc, tron, solana, other\n"
    "    /wallet_list — список кошельков с текущим балансом по FIFO-лотам\n"
    "    /addtx <type> <asset> <amount> [ДД.ММ.ГГГГ] [price_rub] [комментарий]\n"
    "        type ∈ airdrop, mining, staking, sell, swap_out, spend,\n"
    "        transfer_in, transfer_out, sell_nft, refund, gift_in/out и др.\n"
    "    /airdrop <asset> <amount> [дата] [price_rub] — ярлык для addtx\n"
    "    /tx_list [год] [asset] [type] — история операций с фильтрами\n"
    "    /tx_del <id> — удалить операцию (например, ошибочную)\n"
    "    /balance — портфель по рыночной цене + нереализованный P&L\n"
    "    /pnl [год] — реализованный P&L за год\n"
    "    /rate <asset> — текущий курс актива в RUB\n"
    "    /import — реплай на CSV-файл выписки биржи (Bybit/OKX/внутр.)\n"
    "    /tax_setup ИНН=<инн> ИМЯ=<фио> СТАТУС=<resident|miner|nonresident>\n"
    "    /tax [год] — сводка + кнопки PDF/XLSX/CSV\n"
    "    /tax_pdf [год] — PDF-отчёт 3-НДФЛ\n"
    "    /tax_xlsx [год] — детальная XLSX-книга\n"
    "    /tax_csv income [год] — CSV доходов\n"
    "    /tax_csv disposal [год] — CSV реализаций\n\n"
    "Что считаем по нормам РФ:\n"
    "    * цифровая валюта = имущество (ст. 38 НК РФ), НДС не облагается\n"
    "    * доход = сумма продаж + стоимость аирдропов/майнинга/стейкинга на\n"
    "      дату получения по рыночной котировке\n"
    "    * расход = документально подтверждённые затраты на приобретение\n"
    "    * FIFO по умолчанию; можно переключить на WAC через /tax_setup МЕТОД=wac\n"
    "    * 3-НДФЛ резидента — прогрессивная шкала 13/15/18/20/22%\n"
    "      (нерезидент — 30%); майнер — отдельная шкала по доходу от добычи\n"
    "    * 3-НДФЛ до 30 апреля следующего года, уплата до 15 июля\n\n"
    "Перед подачей в ФНС — сверяй с выписками бирж и блокчейн-обозревателями.\n\n"
    "DYOR. Not financial advice. Но я же предупреждала. 😏"
)

# --- Разделы главного меню -------------------------------------------------
# callback_data: menu:<id> — открыть раздел, quick:<команда> — выполнить
# команду сразу.

MENU_SECTIONS: dict[str, dict] = {
    "portfolio": {
        "title": "💼 <b>Портфель</b>",
        "text": (
            "💼 <b>Портфель</b>\n\n"
            "Что у тебя на балансе, во сколько обошлось и сколько заработано.\n\n"
            "<code>/balance</code> — портфель по рыночной цене и нереализованный P&L\n"
            "<code>/pnl [год]</code> — реализованная прибыль/убыток за год\n"
            "<code>/rate BTC</code> — текущий курс актива в рублях\n"
            "<code>/wallet_list</code> — кошельки и остатки по ним\n\n"
            "Совет: начни с <code>/wallet_list</code> — он покажет, что вообще "
            "учтено."
        ),
        "buttons": [
            ("💰 Портфель", "balance"),
            ("📈 Прибыль за год", "pnl"),
            ("👛 Мои кошельки", "wallet_list"),
        ],
    },
    "operations": {
        "title": "🧾 <b>Операции</b>",
        "text": (
            "🧾 <b>Операции</b>\n\n"
            "Записи о сделках, переводах и аирдропах. Из них считается "
            "налог и прибыль.\n\n"
            "<b>Самый быстрый способ</b> — просто напиши операции обычным "
            "текстом, я разберу и покажу, что понял:\n"
            "<code>15.01 получил 0.5 BTC, 20.06 продал 0.3 BTC за 2.4 млн</code>\n\n"
            "<b>По одной операции</b> — по шагам, без синтаксиса: "
            "<code>/addtx</code>\n\n"
            "<b>Полностью автоматически</b> — <code>/autoimport</code>: "
            "отдай только адреса кошельков и (по возможности) выписку биржи, "
            "бот сам соберёт историю переводов, разберёт покупки и продажи "
            "и посчитает налог.\n\n"
            "<b>Точной командой</b>:\n"
            "<code>/tx_list</code> — история операций (по году, активу, типу)\n"
            "<code>/addtx &lt;тип&gt; &lt;актив&gt; &lt;количество&gt; "
            "[дата] [цена]</code>\n"
            "<code>/airdrop BTC 0.5</code> — быстрый ввод аирдропа\n"
            "<code>/import</code> — реплай на CSV-выписку биржи\n"
            "<code>/tx_del &lt;id&gt;</code> — удалить ошибочную операцию\n\n"
            "типы: airdrop, mining, staking, sell, swap_out, spend, "
            "transfer_in, transfer_out…"
        ),
        "buttons": [
            ("📥 Автоимпорт", "autoimport"),
            ("📋 История операций", "tx_list"),
            ("➕ Записать операцию", "addtx"),
            ("🎁 Аирдроп", "airdrop"),
        ],
    },
    "tax": {
        "title": "📊 <b>Отчётность</b>",
        "text": (
            "📊 <b>Отчётность</b>\n\n"
            "Сводка 3-НДФЛ и выгрузки для подачи в ФНС.\n\n"
            "<code>/tax [год]</code> — сводка: доходы, расходы, налог\n"
            "<code>/tax_pdf [год]</code> — PDF-отчёт\n"
            "<code>/tax_xlsx [год]</code> — детальная XLSX-книга\n"
            "<code>/tax_csv income|disposal [год]</code> — CSV выгрузки\n"
            "<code>/tax_setup_status</code> — что сейчас настроено\n\n"
            "Если ИНН и ФИО не заполнены, бот сам предложит заполнить их — "
            "прямо в сводке, без отдельной команды.\n\n"
            "Сроки: отчёт за 2025 подаётся до 30 апреля 2026, налог — до "
            "15 июля 2026.\n\n"
            "Перед подачей нажми «Сверить с сетью» в сводке: сверка покажет, "
            "где учёт разошёлся с блокчейном и биржами."
        ),
        "buttons": [
            ("📊 Сводка за год", "tax"),
            ("📄 PDF для ФНС", "tax_pdf"),
            ("🧾 Расходы (ст. 221)", "tax_cost"),
            ("⚙️ Настройки", "tax_setup_status"),
        ],
    },
    "gifts": {
        "title": "🎁 <b>Аренда подарков</b>",
        "text": (
            "🎁 <b>Аренда подарков</b>\n\n"
            "Доход от аренды NFT на TON — по подаркам, юзернеймам и номерам.\n\n"
            "<code>/marketapptop</code> — топ подарков: сумма и доходность "
            "(TON/сут)\n"
            "<code>/marketapptop 7</code> — то же за 7 дней (можно 30, 90…)\n"
            "<code>/marketappgifts</code> — доход по каждому подарку отдельно\n"
            "<code>/marketapprent</code> — отчёт по аренде кошелька\n\n"
            "Кошелёк берётся из настроек — он же «свой» для отчётов."
        ),
        "buttons": [
            ("🏆 Топ подарков", "marketapptop"),
            ("🎀 По подаркам", "marketappgifts"),
        ],
    },
    "game": {
        "title": "🎲 <b>Турнир на кубиках</b>",
        "text": (
            "🎲 <b>Турнир на кубиках</b>\n\n"
            "Сыграй с друзьями прямо в чате: регистрация, деление на пары, "
            "кубики и выбывание — побеждает тот, кто выкинул больше.\n\n"
            "<code>/start_tournament</code> — запустить турнир\n"
            "<code>/stop_tournament</code> — остановить\n\n"
            "Как играть:\n"
            "    1. Админ жмёт «Завершить набор» и выбирает режим\n"
            "    2. Игроки жмут «Бросить кубик»\n"
            "    3. Каждый раунд выбывает половина\n"
            "    4. Последний выживший — чемпион\n\n"
            "Режимы: 1 на 1 и «все против всех». История турниров — "
            "<code>/history</code>."
        ),
        "buttons": [
            ("🎲 Запустить турнир", "start_tournament"),
            ("🏆 Таблица лидеров", "leaderboard"),
        ],
    },
    "chat": {
        "title": "💬 <b>Спросить</b>",
        "text": (
            "💬 <b>Спросить</b>\n\n"
            "Напиши мне в личку — отвечу по существу, с живыми цифрами "
            "рынка, если речь про крипту.\n\n"
            "Кто я:\n"
            "    • Аллира — крипта, трейдинг, мемы, сарказм\n"
            "    • Лэйн — технологии, ИИ, философия, тишина\n\n"
            "Переключаться можно прямо в тексте: напиши «аллира» или «лейн».\n\n"
            "Удобства:\n"
            "    <code>/clear</code> — забыть наш разговор и начать заново\n"
            "    @AlliraCryptoQueen_bot <вопрос> — спросить из любого чата\n\n"
            "Про память: помню последние 8 реплик, через сутки забываю.\n\n"
            "Пока думаю — висит «печатает…», а под ответом появляются\n"
            "кнопки быстрых ответов. Про крипту говорю с живыми цифрами\n"
            "рынка: <code>/market</code> — срез прямо сейчас."
        ),
        "buttons": [
            ("📊 Рынок сейчас", "market"),
            ("📊 Статистика бота", "stats"),
            ("🧹 Забыть разговор", "clear"),
        ],
    },
}

MAIN_MENU = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("💼 Портфель", callback_data="menu:portfolio"),
        InlineKeyboardButton("🧾 Операции", callback_data="menu:operations"),
    ],
    [
        InlineKeyboardButton("📊 Отчётность", callback_data="menu:tax"),
        InlineKeyboardButton("🎁 Аренда подарков", callback_data="menu:gifts"),
    ],
    [
        InlineKeyboardButton("🎲 Турнир", callback_data="menu:game"),
        InlineKeyboardButton("💬 Спросить", callback_data="menu:chat"),
    ],
    [
        InlineKeyboardButton("❓ Подробная инструкция", callback_data="show_help"),
    ],
])


def _section_markup(section_id: str) -> InlineKeyboardMarkup:
    """Клавиатура раздела: быстрые действия + назад в главное меню."""
    section = MENU_SECTIONS.get(section_id) or {}
    rows = []
    buttons = section.get("buttons") or []
    for label, command in buttons:
        rows.append([InlineKeyboardButton(label, callback_data=f"quick:{command}")])
    rows.append([InlineKeyboardButton("⬅️ Главное меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        WELCOME_TEXT, parse_mode="HTML", reply_markup=MAIN_MENU,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(HELP_TEXT)


async def show_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        try:
            await query.edit_message_text(HELP_TEXT)
        except Exception as e:
            # Повторное нажатие на "Помощь" при неизменном тексте даёт
            # "message is not modified". Раньше исключение уходило в
            # error_handler -> ложный алерт админу и "Что-то сломалось"
            # пользователю на уже открытом тексте.
            if "message is not modified" not in str(e).lower():
                raise


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разделы главного меню."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    if data == "menu:home":
        await _safe_edit(query, WELCOME_TEXT, MAIN_MENU)
        return

    section_id = data.split(":", 1)[1] if ":" in data else ""
    section = MENU_SECTIONS.get(section_id)
    if not section:
        await _safe_edit(query, WELCOME_TEXT, MAIN_MENU)
        return

    await _safe_edit(query, section["text"], _section_markup(section_id))


async def quick_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Быстрое действие из меню: выполнить команду сразу.

    Команды вызываются с подменённым update: текст команды уходит в
    update.message.text, а ответ уходит в тот же чат. Так пользователю
    не нужно ничего набирать — достаточно нажать кнопку.
    """
    query = update.callback_query
    if not query:
        return
    await query.answer()

    command = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    handler = commands().get(command)
    if handler is None:
        await _safe_edit(query, "Не знаю такой команды. Открой главное меню.",
                         MAIN_MENU)
        return

    message = _MenuMessage(
        chat=query.message.chat,
        user=query.from_user,
        bot=context.bot,
        reply_to=query.message.message_id,
        text=f"/{command}",
    )
    # context.args выставляет CommandHandler. Мы зовём обработчик напрямую,
    # и там остались бы аргументы ПРЕДЫДУЩЕЙ команды — команда «сводка за
    # год» вдруг отрисовала бы чужой год.
    context.args = []
    try:
        await handler(_MenuUpdate(message), context)
    except Exception as e:
        logger.error(f"Быстрое действие {command} не выполнилось: {e}",
                     exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=message.chat_id,
                text="Не получилось выполнить команду. Попробуй ещё раз или "
                     "напиши команду текстом.",
            )
        except Exception:
            pass


async def _safe_edit(query, text: str, markup) -> None:
    """Правка сообщения с молчаливым игнором «не изменилось»."""
    try:
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=markup,
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.debug(f"Правка меню не удалась: {e}")


async def _clear_context_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сброс памяти диалога (то же, что /clear).

    Отдельная копия логики из main.py: main импортирует этот модуль,
    поэтому импортировать main отсюда нельзя.
    """
    from handlers.message_handler import HISTORY_KEY, _get_history

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


class _MenuMessage:
    """Сообщение-обёртка: команда, вызванная из меню, отвечает в чат.

    Команды читают update.message.text (аргументы) и .from_user
    (владелец). Здесь текст — это сама команда, а владелец — тот, кто
    нажал кнопку. Ответ уходит отдельным сообщением, а не реплаем на
    сообщение бота: так он виден в ленте как обычный ответ.
    """

    def __init__(self, chat, user, bot, reply_to: int | None = None, text: str = ""):
        self.chat = chat
        self.chat_id = chat.id
        self.from_user = user
        self.text = text
        self.caption = None
        self.message_id = reply_to
        self._bot = bot
        self._reply_to = reply_to

    async def reply_text(self, text, **kwargs):
        kwargs.setdefault("chat_id", self.chat_id)
        if self._reply_to is not None:
            kwargs.setdefault("reply_to_message_id", self._reply_to)
        return await self._bot.send_message(text=text, **kwargs)

    async def reply_photo(self, photo, **kwargs):
        kwargs.setdefault("chat_id", self.chat_id)
        if self._reply_to is not None:
            kwargs.setdefault("reply_to_message_id", self._reply_to)
        return await self._bot.send_photo(photo=photo, **kwargs)

    async def reply_html(self, text, **kwargs):
        return await self.reply_text(text, **kwargs)


class _MenuUpdate:
    """Update-обёртка: несёт подменённое сообщение вместо callback."""

    def __init__(self, message):
        self.message = message
        self.callback_query = None
        self.effective_user = getattr(message, "from_user", None)
        self.effective_chat = getattr(message, "chat", None)


# Команды, доступные по кнопкам меню. Значение — обработчик с обычной
# сигнатурой (update, context).
def _build_commands() -> dict:
    from handlers import (auto_import, crypto_ledger, crypto_tax, stats_command,
                          wallet_command)
    from handlers.dice_tournament import start_dice_tournament_registration
    from handlers.message_handler import market_command as _market_command

    return {
        "balance": crypto_ledger.balance_command,
        "pnl": crypto_ledger.pnl_command,
        "wallet_list": crypto_ledger.wallet_list_command,
        "tx_list": crypto_ledger.tx_list_command,
        "addtx": crypto_ledger.addtx_command,
        "airdrop": crypto_ledger.airdrop_command,
        "autoimport": auto_import.auto_import_command,
        "market": _market_command,
        "tax": crypto_tax.tax_command,
        "tax_cost": crypto_tax.tax_cost_command,
        "tax_pdf": crypto_tax.tax_pdf_command,
        "tax_setup_status": crypto_tax.tax_setup_status_command,
        "marketapptop": wallet_command.marketapptop_command,
        "marketappgifts": wallet_command.marketappgifts_command,
        "marketapprent": wallet_command.marketapprent_command,
        "start_tournament": start_dice_tournament_registration,
        "stats": stats_command.stats_command,
        "history": stats_command.history_command,
        "leaderboard": stats_command.leaderboard_command,
        "clear": _clear_context_command,
    }


_COMMAND_MAP: dict | None = None


def commands() -> dict:
    """Команды меню. Собираются лениво — при первом нажатии кнопки.

    Импорт всех хендлеров на этапе импорта main.py создавал лишние
    связи между модулями; ленивая сборка их разрывает.
    """
    global _COMMAND_MAP
    if _COMMAND_MAP is None:
        _COMMAND_MAP = _build_commands()
    return _COMMAND_MAP


def register_menu_handlers(application) -> None:
    """Подключить обработчики меню (вызывается из main.py)."""
    application.add_handler(CallbackQueryHandler(
        menu_callback, pattern=r"^menu:",
    ))
    application.add_handler(CallbackQueryHandler(
        quick_action_callback, pattern=r"^quick:",
    ))
