import logging
import re
import random
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from utils.ai_responses import get_llm_response, decide_speaker
from utils.database import (
    check_rate_limit, upsert_user, log_message, is_user_banned
)
from prompts.loader import load_prompt

logger = logging.getLogger(__name__)

RESPONSE_CHANCE = 0.3
DM_COOLDOWN = 3.0
DM_MAX_PER_MINUTE = 5
GROUP_COOLDOWN = 5.0
GROUP_MAX_PER_MINUTE = 3


async def _process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, is_private: bool = False):
    if not update.message or not update.message.text:
        return

    message = update.message
    bot_data = context.bot_data
    bot_username = bot_data.get("bot_username", "")
    user = message.from_user

    if user and is_user_banned(user.id):
        return

    if is_private:
        text = message.text.strip()
        if user:
            await upsert_user(user.id, user.username, user.first_name)
            if not await check_rate_limit(user.id, message.chat_id, DM_COOLDOWN, DM_MAX_PER_MINUTE):
                await message.reply_text("Слишком часто! Подожди немного.")
                return
        logger.info(f"Личное сообщение от {user.username}: {text[:50]}...")
    else:
        is_mention = f"@{bot_username}" in message.text if bot_username else False
        is_reply = (message.reply_to_message and
                    message.reply_to_message.from_user.id == bot_data.get("bot_id"))

        trigger_words = ["аллира", "лейн", "allira", "lane"]
        has_trigger = any(re.search(rf'\b{word}\b', message.text.lower()) for word in trigger_words)

        if not (is_mention or is_reply or has_trigger):
            return

        if not (is_mention or is_reply) and random.random() > RESPONSE_CHANCE:
            logger.info(f"Пропущено (шанс {RESPONSE_CHANCE}): {message.text[:30]}")
            return

        if user:
            if not await check_rate_limit(user.id, message.chat_id, GROUP_COOLDOWN, GROUP_MAX_PER_MINUTE):
                logger.info(f"Rate limit: {user.first_name} ({user.id})")
                return
            await upsert_user(user.id, user.username, user.first_name)

        clean_text = re.sub(re.escape(f"@{bot_username}"), "", message.text, flags=re.IGNORECASE).strip()
        text = clean_text if clean_text else "Привет!"

    speaker = decide_speaker(text)
    model = bot_data["LANE_MODEL"] if speaker == "lane" else bot_data["DEFAULT_MODEL"]

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    try:
        response = await get_llm_response(
            user_prompt=text,
            system_prompt=load_prompt(speaker),
            model=model,
            api_key=bot_data["OPENROUTER_API_KEY"]
        )

        if len(response) > 4000:
            if is_private:
                for i in range(0, len(response), 4000):
                    chunk = response[i:i+4000]
                    await update.message.reply_text(chunk)
            else:
                response = response[:3997] + "..."
                await message.reply_text(response, reply_to_message_id=message.message_id)
        else:
            if is_private:
                await update.message.reply_text(response)
            else:
                await message.reply_text(response, reply_to_message_id=message.message_id)

        chat_type = "private" if is_private else "group"
        await log_message(user.id if user else 0, message.chat_id, chat_type, speaker)
        logger.info(f"Ответ отправлен в {chat_type} как {speaker}")

    except Exception as e:
        logger.error(f"Ошибка в {'ЛС' if is_private else 'группе'}: {e}")
        error_text = "Ой! Что-то пошло не так. Попробуй написать еще раз!" if is_private else "Сбой системы! Попробуй позже."
        if is_private:
            await update.message.reply_text(error_text)
        else:
            await message.reply_text(error_text, reply_to_message_id=message.message_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _process_message(update, context, is_private=False)


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _process_message(update, context, is_private=True)
