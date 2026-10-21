"""Мастер ввода операций: пошаговая форма и разбор списка.

Зачем: путь «от операции до отчёта» упирался в синтаксис. Чтобы записать
одну операцию, надо было помнить `/addtx <тип> <актив> <количество>
[дата] [цена] [комментарий]`, а для десяти операций — написать десять
команд. Ошибка в одной букве — и операция не записалась.

Здесь два способа, оба короче исходного:

  * пошаговая форма — жмёшь кнопки, вводишь только число;
  * разбор списка — вставляешь операции обычным текстом («15.01 получил
    0.5 BTC, потом продал за 2.4 млн»), бот показывает разбор и
    записывает только после подтверждения.

Разбор списка идёт через LLM, поэтому ответ всегда сверяется с
предпросмотром: числа, типы и даты показываются пользователю до записи,
а каждая строка проходит ту же валидацию, что и ручной ввод.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ApplicationHandlerStop, ContextTypes

from utils.ai_responses import get_llm_response, is_service_error
from utils.common import escape_html
from utils.crypto_db import CryptoError, TxType, add_transaction

logger = logging.getLogger(__name__)

# Ключ состояния мастера в context.user_data.
WIZARD_KEY = "tx_wizard"
# Ключ отложенного разбора списка (ждёт подтверждения).
BULK_KEY = "tx_bulk"

# Типы операций, которые чаще всего вносят руками. Порядок = порядок
# кнопок: сверху то, что нужно чаще всего.
COMMON_TYPES = [
    ("airdrop", "🎁 Аирдроп"),
    ("mining", "⛏ Майнинг"),
    ("staking", "🔒 Стейкинг"),
    ("sell", "💸 Продажа"),
    ("swap_out", "🔄 Обмен"),
    ("spend", "🛒 Трата"),
    ("transfer_in", "📥 Перевод мне"),
    ("transfer_out", "📤 Перевод от меня"),
    ("gift_in", "🎀 Подарок"),
    ("reward", "🏅 Награда"),
]

# Синонимы типов: пользователь пишет по-человечески, а в базу идёт
# канонический тип.
TYPE_ALIASES = {
    "продал": "sell", "продажа": "sell", "sell": "sell", "selled": "sell",
    "куплю": "buy", "купил": "buy", "покупка": "buy", "buy": "buy",
    "аирдроп": "airdrop", "airdrop": "airdrop", "дроп": "airdrop",
    "майнинг": "mining", "mining": "mining", "майнено": "mining",
    "стейкинг": "staking", "staking": "staking", "стейбл": "staking",
    "обмен": "swap_out", "своп": "swap_out", "swap": "swap_out",
    "swap_out": "swap_out", "конвертация": "swap_out",
    "перевод": "transfer_in", "перевёл": "transfer_in", "transfer": "transfer_in",
    "transfer_in": "transfer_in", "получил": "transfer_in",
    "потратил": "spend", "трата": "spend", "spend": "spend",
    "подарок": "gift_in", "gift_in": "gift_in", "gift": "gift_in",
    "награда": "reward", "reward": "reward", "вознаграждение": "reward",
    "процент": "interest", "interest": "interest", "доход": "interest",
    "возврат": "refund", "refund": "refund",
    "доход_другой": "income_other", "income_other": "income_other",
    "вывод": "transfer_out", "transfer_out": "transfer_out",
    "отправил": "transfer_out",
}

DATE_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})")


# ------------------------------------------------------------- утилиты


def _owner_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    return int(context.bot_data.get("CRYPTO_OWNER_ID") or 0)


def normalize_type(raw: str) -> str | None:
    """Человеческое название типа → канонический тип. None — неизвестно."""
    if not raw:
        return None
    key = raw.strip().lower().replace(" ", "_")
    if key in TxType.all():
        return key
    return TYPE_ALIASES.get(key) or TYPE_ALIASES.get(raw.strip().lower())


def parse_date(raw: str) -> int | None:
    """Дата в любом разумном виде → timestamp. None — не разобрали."""
    if not raw:
        return None
    text = raw.strip().lower()
    if text in ("сегодня", "today"):
        return int(time.time())
    if text in ("вчера", "yesterday"):
        return int(time.time()) - 86400

    match = DATE_RE.search(text)
    if match:
        day, month, year = (int(x) for x in match.groups())
        if year < 100:
            year += 2000
        try:
            return int(datetime(year, month, day).timestamp())
        except ValueError:
            return None

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return None


def format_date(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


# --------------------------------------------------- пошаговая форма


def _wizard(context: ContextTypes.DEFAULT_TYPE) -> dict:
    state = context.user_data.get(WIZARD_KEY)
    if not isinstance(state, dict):
        state = {}
        context.user_data[WIZARD_KEY] = state
    return state


def _wizard_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = context.user_data.get(WIZARD_KEY)
    return isinstance(state, dict) and bool(state.get("step"))


def _wizard_clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(WIZARD_KEY, None)


def _type_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row: list[InlineKeyboardButton] = []
    for tx_type, label in COMMON_TYPES:
        row.append(InlineKeyboardButton(label, callback_data=f"wiz:type:{tx_type}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="wiz:cancel")])
    return InlineKeyboardMarkup(rows)


def _asset_keyboard(state: dict) -> InlineKeyboardMarkup:
    """Кнопки активов, которые уже встречались в учёте, + свободный ввод."""
    known = list(state.get("assets") or [])
    rows = []
    row: list[InlineKeyboardButton] = []
    for symbol in known:
        row.append(InlineKeyboardButton(symbol, callback_data=f"wiz:asset:{symbol}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("✍️ Ввести символ", callback_data="wiz:asset_ask"),
        InlineKeyboardButton("❌ Отмена", callback_data="wiz:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _price_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 По рынку", callback_data="wiz:price_market")],
        [InlineKeyboardButton("❌ Отмена", callback_data="wiz:cancel")],
    ])


def _note_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Без комментария", callback_data="wiz:note_skip")],
        [InlineKeyboardButton("❌ Отмена", callback_data="wiz:cancel")],
    ])


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Записать", callback_data="wiz:confirm")],
        [InlineKeyboardButton("❌ Отмена", callback_data="wiz:cancel_confirm")],
    ])


def _summary(state: dict) -> str:
    date = format_date(int(state.get("date") or time.time()))
    price = state.get("price")
    price_text = f"{price} ₽" if price else "по рынку"
    note = state.get("note") or ""
    lines = [
        "✅ <b>Проверь перед записью:</b>",
        "",
        f"Тип: <b>{escape_html(state.get('type', ''))}</b>",
        f"Актив: <b>{escape_html(state.get('asset', ''))}</b>",
        f"Количество: <b>{escape_html(str(state.get('amount', '')))}</b>",
        f"Дата: <b>{escape_html(date)}</b>",
        f"Цена: <b>{escape_html(price_text)}</b>",
    ]
    if note:
        lines.append(f"Комментарий: {escape_html(note)}")
    return "\n".join(lines)


async def start_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Начало пошаговой формы записи операции."""
    message = getattr(update, "message", None)
    if message is None:
        return
    state = _wizard(context)
    state.clear()
    state["step"] = "type"
    state["owner"] = _owner_id(context)
    state["assets"] = _frequent_assets(_owner_id(context))
    await message.reply_text(
        "➕ <b>Что записать?</b>\n\nВыбери тип операции — дальше спрошу только "
        "самое нужное.",
        parse_mode="HTML",
        reply_markup=_type_keyboard(),
    )


def _frequent_assets(owner_id: int) -> list[str]:
    """Активы, которые уже встречались в учёте, — чтобы не вводить символ."""
    if not owner_id:
        return []
    try:
        from utils.database import get_db

        with get_db() as conn:
            rows = conn.execute(
                "SELECT a.symbol, COUNT(*) AS n FROM crypto_transactions t "
                "JOIN crypto_assets a ON a.id = t.asset_id "
                "WHERE t.owner_id = ? GROUP BY a.symbol ORDER BY n DESC LIMIT 8",
                (owner_id,),
            ).fetchall()
        return [r["symbol"] for r in rows]
    except Exception as e:
        logger.debug(f"Не удалось собрать частые активы: {e}")
        return []


async def _safe_edit(query, text: str, markup) -> None:
    """Правка сообщения мастера: «не изменилось» — не ошибка."""
    try:
        await query.edit_message_text(text, parse_mode="HTML",
                                     reply_markup=markup)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.debug(f"Правка мастера не удалась: {e}")


async def wizard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обработка кнопок мастера. True — апдейт обработан."""
    query = update.callback_query
    if not query or not (query.data or "").startswith("wiz:"):
        return False

    state = _wizard(context)
    action = (query.data or "")[4:]

    if action == "cancel":
        _wizard_clear(context)
        await query.answer("Отменено")
        await _safe_edit(query, "Ок, запись отменена. Начать заново: /addtx", None)
        return True

    if action.startswith("type:"):
        tx_type = action[5:]
        state["type"] = tx_type
        state["step"] = "asset"
        await query.answer()
        await _safe_edit(
            query,
            f"Тип: <b>{escape_html(tx_type)}</b>\n\nТеперь актив — символом "
            "(BTC, TON, USDT).",
            _asset_keyboard(state),
        )
        return True

    if action.startswith("asset:"):
        symbol = action[6:].strip().upper()
        if not symbol:
            return True
        state["asset"] = symbol
        state["step"] = "amount"
        await query.answer()
        await _safe_edit(
            query,
            f"Актив: <b>{escape_html(symbol)}</b>\n\nСколько? Напиши число — "
            "например <code>0.5</code>.",
            None,
        )
        return True

    if action == "asset_ask":
        state["step"] = "asset"
        await query.answer()
        await _safe_edit(query, "Напиши символ актива — например <code>BTC</code>.", None)
        return True

    if action == "date_today":
        state["date"] = int(time.time())
        state["step"] = "price"
        await query.answer()
        await _safe_edit(
            query,
            "Дата: <b>сегодня</b>\n\nЦена всей операции в рублях? Если не "
            "знаешь — нажми «По рынку», возьму котировку на дату операции.",
            _price_keyboard(),
        )
        return True

    if action == "price_market":
        state["price"] = ""
        state["step"] = "note"
        await query.answer()
        await _safe_edit(
            query,
            "Цена: <b>по рынку</b>\n\nКомментарий — необязательно: он попадёт "
            "в отчёт. Напиши пару слов или пропусти.",
            _note_keyboard(),
        )
        return True

    if action == "note_skip":
        state["note"] = ""
        state["step"] = "confirm"
        await query.answer()
        await _safe_edit(query, _summary(state), _confirm_keyboard())
        return True

    if action == "confirm":
        await query.answer("Записываю…")
        snapshot = dict(state)
        _wizard_clear(context)
        await _write_wizard_op(context, snapshot, query)
        return True

    if action == "cancel_confirm":
        _wizard_clear(context)
        await query.answer("Отменено")
        await _safe_edit(query, "Не записал. Начать заново: /addtx", None)
        return True

    return True


async def wizard_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текстовый ввод внутри мастера.

    Если мастер не активен — молча выходим, сообщение обработает общий
    хендлер. Если активен — забираем текст и останавливаем обработку
    апдейта, чтобы тот же текст не ушёл в LLM.
    """
    if not _wizard_active(context):
        return
    message = getattr(update, "message", None)
    if message is None:
        return
    text = ((getattr(message, "text", None) or "").strip())
    if not text:
        return

    state = _wizard(context)
    step = state.get("step")

    if step == "asset":
        symbol = text.split()[0].strip().upper()
        if not symbol.isalnum():
            await message.reply_text("Символ актива — буквы, например BTC.")
            raise ApplicationHandlerStop
        state["asset"] = symbol
        state["step"] = "amount"
        await message.reply_text(
            f"Актив: <b>{escape_html(symbol)}</b>\n\nСколько? Напиши число.",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

    if step == "amount":
        cleaned = text.replace(",", ".").split()[0]
        try:
            float(cleaned)
        except (TypeError, ValueError):
            await message.reply_text("Количество должно быть числом. Например: 0.5")
            raise ApplicationHandlerStop
        state["amount"] = cleaned
        state["step"] = "date"
        await message.reply_text(
            "Сколько записал. Теперь дата — нажми «Сегодня» или напиши "
            "дату в формате <code>15.01.2026</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Сегодня", callback_data="wiz:date_today")],
                [InlineKeyboardButton("❌ Отмена", callback_data="wiz:cancel")],
            ]),
        )
        raise ApplicationHandlerStop

    if step == "price":
        cleaned = text.replace(",", ".").split()[0]
        try:
            float(cleaned)
        except (TypeError, ValueError):
            await message.reply_text(
                "Цена должна быть числом в рублях. Например: 2500000 — "
                "или нажми «По рынку».",
            )
            raise ApplicationHandlerStop
        state["price"] = cleaned
        state["step"] = "note"
        await message.reply_text(
            "Цена принята. Комментарий — необязательно: он попадёт в отчёт. "
            "Напиши пару слов или пропусти.",
            parse_mode="HTML",
            reply_markup=_note_keyboard(),
        )
        raise ApplicationHandlerStop

    if step == "note":
        state["note"] = text[:200]
        state["step"] = "confirm"
        await message.reply_text(
            _summary(state), parse_mode="HTML", reply_markup=_confirm_keyboard(),
        )
        raise ApplicationHandlerStop

    # Дата пришла текстом, а не кнопкой.
    if step == "date":
        ts = parse_date(text)
        if ts is None:
            await message.reply_text(
                "Не понял дату. Формат: <code>15.01.2026</code> "
                "(или «сегодня»).",
                parse_mode="HTML",
            )
            raise ApplicationHandlerStop
        state["date"] = ts
        state["step"] = "price"
        await message.reply_text(
            "Цена всей операции в рублях? Если не знаешь — нажми «По рынку».",
            parse_mode="HTML",
            reply_markup=_price_keyboard(),
        )
        raise ApplicationHandlerStop

    raise ApplicationHandlerStop


async def _write_wizard_op(context, state: dict, query) -> None:
    """Записывает операцию, собранную по шагам."""
    owner = state.get("owner") or _owner_id(context)
    try:
        add_transaction(
            owner_id=owner,
            ts=int(state.get("date") or time.time()),
            asset_symbol=state.get("asset") or "",
            tx_type=state.get("type") or TxType.AIRDROP,
            amount=state.get("amount") or "0",
            fiat_value_rub=(state.get("price") or None),
            note=state.get("note") or "",
            source="manual",
        )
    except CryptoError as e:
        await _reply(query, f"❌ Не записал: {escape_html(str(e))}")
        return
    except Exception as e:
        logger.error(f"Мастер: запись не удалась: {e}", exc_info=True)
        await _reply(query, "❌ Что-то сломалось при записи. Попробуй ещё раз.")
        return

    date = format_date(int(state.get("date") or time.time()))
    await _reply(
        query,
        f"✅ <b>Записал:</b> {escape_html(state.get('type', ''))} "
        f"{escape_html(state.get('asset', ''))} "
        f"{escape_html(str(state.get('amount', '')))} ({escape_html(date)})\n\n"
        f"Дальше: <code>/tax</code> — сводка за год.",
    )


async def _reply(query, text: str) -> None:
    try:
        await query.message.reply_text(text, parse_mode="HTML")
    except Exception:
        try:
            await query.get_bot().send_message(
                chat_id=query.message.chat_id, text=text, parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Не удалось ответить в мастере: {e}")


# ------------------------------------------- разбор списка операций

PARSE_SYSTEM_PROMPT = (
    "Ты — разборщик операций с криптовалютой для бухгалтерии 3-НДФЛ.\n"
    "Из свободного текста пользователя извлеки операции и верни ТОЛЬКО JSON "
    "объект вида:\n"
    '{"operations": [{"type": "...", "asset": "...", "amount": "...", '
    '"date": "ДД.ММ.ГГГГ", "price_rub": "...", "note": "..."}]}\n\n'
    "Правила:\n"
    "- type — один из: airdrop, mining, staking, sell, swap_out, spend, "
    "transfer_in, transfer_out, gift_in, reward, interest, refund, "
    "income_other, buy.\n"
    "- asset — символ в верхнем регистре (BTC, ETH, TON, USDT, NOT…).\n"
    "- amount — количество как строка с точкой: \"0.5\".\n"
    "- date — ДД.ММ.ГГГГ. Если даты нет — пустая строка.\n"
    "- price_rub — цена всей операции в рублях строкой, если названа. "
    "Если цена названа в тысячах/миллионах — переведи в рубли. Если нет — "
    "пустая строка.\n"
    "- note — короткая заметка своими словами, если пользователь что-то "
    "сказал. Иначе пустая строка.\n"
    "- Если операций не найдено — верни {\"operations\": []}.\n"
    "Не выдумывай операции, которых нет в тексте. Не добавляй пояснений."
)


def _parse_operations_payload(raw: str) -> list[dict]:
    """Ответ модели → список операций. Мусор и битые строки отбрасываются."""
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        # Модель иногда оборачивает JSON в код-блок.
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    ops = data.get("operations")
    if not isinstance(ops, list):
        return []

    clean: list[dict] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        tx_type = normalize_type(str(op.get("type") or ""))
        asset = str(op.get("asset") or "").strip().upper()
        amount = str(op.get("amount") or "").strip().replace(",", ".")
        if not tx_type or not asset or not amount:
            continue
        try:
            float(amount)
        except (TypeError, ValueError):
            continue
        price = str(op.get("price_rub") or "").strip().replace(",", ".")
        if price:
            try:
                float(price)
            except (TypeError, ValueError):
                price = ""
        clean.append({
            "type": tx_type,
            "asset": asset,
            "amount": amount,
            "date": str(op.get("date") or "").strip(),
            "price_rub": price,
            "note": str(op.get("note") or "").strip()[:200],
        })
    return clean


def format_preview(ops: list[dict]) -> str:
    """Предпросмотр разбора: что именно будет записано."""
    lines = ["🔎 <b>Так я понял твой список:</b>", ""]
    for i, op in enumerate(ops, 1):
        date = op["date"] or "сегодня"
        price = f", {op['price_rub']} ₽" if op["price_rub"] else ""
        note = f" — {escape_html(op['note'])}" if op["note"] else ""
        lines.append(
            f"{i}. <b>{escape_html(op['type'])}</b> "
            f"{escape_html(op['asset'])} "
            f"{escape_html(op['amount'])}"
            f" · {escape_html(date)}{price}{note}"
        )
    lines.append("")
    lines.append(f"Всего операций: <b>{len(ops)}</b>. Записать?")
    return "\n".join(lines)


async def parse_operations_list(text: str, model: str, api_key: str) -> list[dict]:
    """Разбор свободного текста в список операций."""
    prompt = (
        f"Сегодня {datetime.now().strftime('%d.%m.%Y')}.\n\n"
        "Операции пользователя:\n"
        f"{text[:3000]}\n\n"
        "Верни JSON со списком операций."
    )
    raw = await get_llm_response(
        user_prompt=prompt,
        system_prompt=PARSE_SYSTEM_PROMPT,
        model=model,
        api_key=api_key,
        json_mode=True,
    )
    if not raw or is_service_error(raw):
        logger.warning("Разбор списка операций не удался")
        return []
    return _parse_operations_payload(raw)


# ------------------------------------------- разбор списка: диалог


def _bulk_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Разобрать список", callback_data="wiz:bulk_parse")],
        [
            InlineKeyboardButton("✍️ Записать одну", callback_data="wiz:bulk_one"),
            InlineKeyboardButton("❌ Отмена", callback_data="wiz:bulk_cancel"),
        ],
    ])


async def start_bulk_flow(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          text: str) -> None:
    """Предлагает разобрать свободный текст как список операций."""
    message = getattr(update, "message", None)
    if message is None:
        return
    context.user_data[BULK_KEY] = {"text": text[:3000]}
    await message.reply_text(
        "🧾 Похоже, это не команда, а описание операций.\n\n"
        "Могу разобрать список и показать, что понял, — запишу только после "
        "твоего подтверждения. Или запишу одну операцию по шагам.",
        parse_mode="HTML",
        reply_markup=_bulk_keyboard(),
    )


async def bulk_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Кнопки разбора списка."""
    query = update.callback_query
    if not query or not (query.data or "").startswith("wiz:bulk_"):
        return False

    action = (query.data or "")[8:]
    state = context.user_data.get(BULK_KEY)
    if not isinstance(state, dict) or not state.get("text"):
        await query.answer("Список потерялся, пришли его ещё раз")
        return True

    if action == "cancel":
        context.user_data.pop(BULK_KEY, None)
        await query.answer("Отменено")
        await _safe_edit(query, "Ок, не записал.", None)
        return True

    if action == "one":
        context.user_data.pop(BULK_KEY, None)
        await query.answer()
        await _safe_edit(query, "Запишем по шагам 👇", None)
        await start_wizard(update, context)
        return True

    if action == "parse":
        await query.answer("Разбираю…")
        text = state.get("text") or ""
        model = context.bot_data.get("DEFAULT_MODEL", "")
        api_key = context.bot_data.get("OPENROUTER_API_KEY", "")
        ops = await parse_operations_list(text, model, api_key)
        if not ops:
            await _safe_edit(
                query,
                "🤷 Не понял ни одной операции. Напиши их проще, например:\n"
                "<code>15.01 получил 0.5 BTC</code>\n"
                "<code>20.06 продал 0.3 BTC за 2.4 млн</code>",
                _bulk_keyboard(),
            )
            return True

        state["ops"] = ops
        context.user_data[BULK_KEY] = state
        await _safe_edit(
            query,
            format_preview(ops),
            InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ Записать {len(ops)}",
                                      callback_data="wiz:bulk_write")],
                [InlineKeyboardButton("❌ Отмена", callback_data="wiz:bulk_cancel")],
            ]),
        )
        return True

    if action == "write":
        await query.answer("Записываю…")
        ops = state.get("ops") or []
        context.user_data.pop(BULK_KEY, None)
        await write_bulk(context, ops, query)
        return True

    return True


async def write_bulk(context: ContextTypes.DEFAULT_TYPE, ops: list[dict],
                     query) -> None:
    """Записывает разобранный список. Каждая строка — с проверкой."""
    owner = _owner_id(context)
    written = 0
    failed: list[str] = []

    for op in ops:
        ts = parse_date(op.get("date") or "") or int(time.time())
        try:
            add_transaction(
                owner_id=owner,
                ts=ts,
                asset_symbol=op["asset"],
                tx_type=op["type"],
                amount=op["amount"],
                fiat_value_rub=(op.get("price_rub") or None),
                note=op.get("note") or "",
                source="manual",
            )
            written += 1
        except CryptoError as e:
            failed.append(f"{op['asset']} {op['amount']} — {e}")
        except Exception as e:
            logger.error(f"Массовая запись: {op} не удалась: {e}", exc_info=True)
            failed.append(f"{op['asset']} {op['amount']} — не удалось записать")

    text = f"✅ <b>Записал операций: {written}</b>"
    if failed:
        text += "\n\nНе получилось:\n"
        for line in failed[:5]:
            text += f"• {escape_html(line)}\n"
    text += "\n\nДальше: <code>/tax</code> — сводка за год."
    await _reply(query, text)
