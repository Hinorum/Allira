import logging
import re
import random
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from utils.ai_responses import get_llm_response, decide_speaker
from prompts.loader import load_prompt

logger = logging.getLogger(__name__)

RESPONSE_CHANCE = 0.3

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    
    message = update.message
    bot_data = context.bot_data
    bot_username = bot_data.get("bot_username", "")
    
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
    
    clean_text = re.sub(f"@{bot_username}", "", message.text, flags=re.IGNORECASE).strip()
    if not clean_text:
        clean_text = "Привет!"
    
    speaker = decide_speaker(clean_text)
    model = bot_data["LANE_MODEL"] if speaker == "lane" else bot_data["DEFAULT_MODEL"]
    
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    
    try:
        response = await get_llm_response(
            user_prompt=clean_text,
            system_prompt=load_prompt(speaker),
            model=model,
            api_key=bot_data["OPENROUTER_API_KEY"]
        )
        
        if len(response) > 4000:
            response = response[:3997] + "..."
        
        await message.reply_text(response, reply_to_message_id=message.message_id)
        logger.info(f"Ответ отправлен в канале как {speaker}")
        
    except Exception as e:
        logger.error(f"Ошибка в канале: {e}")
        await message.reply_text("Сбой системы! Попробуй позже.", 
                               reply_to_message_id=message.message_id)

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    
    message = update.message
    text = message.text.strip()
    bot_data = context.bot_data
    
    logger.info(f"Личное сообщение от {message.from_user.username}: {text[:50]}...")
    
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
            for i in range(0, len(response), 4000):
                chunk = response[i:i+4000]
                await update.message.reply_text(chunk)
        else:
            await update.message.reply_text(response)
            
        logger.info(f"Ответ отправлен в ЛС как {speaker}")
        
    except Exception as e:
        logger.error(f"Ошибка в ЛС: {e}")
        await update.message.reply_text(
            "Ой! Что-то пошло не так. Попробуй написать еще раз!"
    )
