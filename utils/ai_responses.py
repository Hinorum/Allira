import logging
import random
import httpx
import re
from cachetools import TTLCache

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

FALLBACK_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
    "z-ai/glm-5.2:free",
    "nvidia/nemotron-3.5-lightning:free",
]

REASONING_PATTERNS = [
    r'Хорошо[,.].*?пользователь',
    r'Сначала разберу',
    r'Проверяю.*?реакци',
    r'Варианты ответа',
    r'Нужно сохранить.*?голос',
    r'Итоговый ответ.*?:',
    r'Проверяю.*?кодекс',
    r'Останавливаюсь на',
    r'Финальный вариант',
    r'Попробую собрать',
    r'Можно усилить',
    r'Также важно',
    r'Добавляю эмодзи',
    r'Убедиться[,.] что нет',
]

def strip_reasoning(text: str) -> str:
    """Убирает internal reasoning/thinking из ответов моделей"""
    if not text:
        return text

    lines = text.split('\n')
    result_lines = []
    skip_mode = False

    for line in lines:
        stripped = line.strip()
        is_reasoning = False

        for pattern in REASONING_PATTERNS:
            if re.search(pattern, stripped, re.IGNORECASE):
                is_reasoning = True
                skip_mode = True
                break

        if skip_mode and not is_reasoning:
            if stripped and len(stripped) > 20 and any(c.isalpha() for c in stripped):
                if not any(re.search(p, stripped, re.IGNORECASE) for p in REASONING_PATTERNS):
                    skip_mode = False
                    result_lines.append(line)
            continue

        if not is_reasoning:
            result_lines.append(line)

    result = '\n'.join(result_lines).strip()

    result = re.sub(r'```[\s\S]*?```', '', result)

    result = re.sub(r'\n{3,}', '\n\n', result).strip()

    if len(result) < 10 and len(text) > 50:
        paragraphs = text.split('\n\n')
        for p in reversed(paragraphs):
            p = p.strip()
            if p and len(p) > 10:
                skip_words = ['проверяю', 'вариант', 'нужно', 'важно', 'добавляю', 'убедиться', 'останавливаюсь', 'финальный', 'попробую', 'можно']
                if not any(w in p.lower() for w in skip_words):
                    return p
        return text

    return result

response_cache = TTLCache(maxsize=100, ttl=300)
image_prompt_cache = TTLCache(maxsize=50, ttl=600)

async def get_llm_response(user_prompt: str, system_prompt: str, model: str, api_key: str) -> str:
    cache_key = f"{model}:{hash(user_prompt[:100] + system_prompt[:100])}"
    
    if cache_key in response_cache:
        logger.debug("Использован кэшированный ответ")
        return response_cache[cache_key]
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/AlliraCryptoBot",
        "X-Title": "AlliraCryptoBot"
    }
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.8,
        "max_tokens": 1500,
        "top_p": 0.9,
        "frequency_penalty": 0.5
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            
            if response.status_code == 429:
                logger.warning("Превышен лимит API")
                return "Слишком много запросов! Дай мне минутку передохнуть..."
                
            def _extract_content(data: dict) -> str | None:
                choices = data.get("choices")
                if not choices or not isinstance(choices, list) or len(choices) == 0:
                    return None
                msg = choices[0].get("message", {})
                content = msg.get("content")
                if not content or not isinstance(content, str):
                    return None
                return content

            def _parse_ok(resp) -> str | None:
                data = resp.json()
                content = _extract_content(data)
                if content:
                    return strip_reasoning(content)
                logger.warning(f"Нет content в ответе модели: {str(data)[:300]}")
                return None

            async def _try_models(primary: str) -> str | None:
                payload["model"] = primary
                resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
                if resp.status_code == 200:
                    result = _parse_ok(resp)
                    if result:
                        logger.info(f"Модель {primary} работает!")
                        return result
                else:
                    logger.warning(f"Модель {primary} вернула {resp.status_code}")

                for fallback_model in FALLBACK_MODELS:
                    if fallback_model == primary:
                        continue
                    payload["model"] = fallback_model
                    resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
                    if resp.status_code == 200:
                        result = _parse_ok(resp)
                        if result:
                            logger.info(f"Модель {fallback_model} работает (fallback)!")
                            return result
                    else:
                        logger.warning(f"Модель {fallback_model} вернула {resp.status_code}")
                return None

            if response.status_code != 200:
                logger.error(f"OpenRouter API error {response.status_code}: {response.text[:500]}")
                if response.status_code in (400, 404, 402):
                    result = await _try_models(model)
                    if result:
                        response_cache[cache_key] = result[:2000]
                        return result
                    return "Все модели временно недоступны. Попробуй позже!"
                response.raise_for_status()
                return "Сервис временно недоступен. Попробуй позже!"

            result = _parse_ok(response)
            if result:
                response_cache[cache_key] = result[:2000]
                return result

            logger.warning(f"Некорректный ответ, пробуем fallback модели")
            result = await _try_models(model)
            if result:
                response_cache[cache_key] = result[:2000]
                return result
            return "Модель вернула пустой ответ. Попробуй переформулировать!"
            
    except httpx.TimeoutException:
        logger.error("Таймаут API")
        return "Что-то я сегодня медленная... Попробуй позже!"
    except Exception as e:
        logger.error(f"Ошибка LLM: {e}")
        return "Технические неполадки! Попробуй еще раз."

def decide_speaker(text: str) -> str:
    text_lower = text.lower()
    
    if re.search(r'\b(лейн|lane)\b', text_lower):
        return "lane"
    if re.search(r'\b(аллира|allira)\b', text_lower):
        return "allira"
    
    lane_keywords = [
        'технологи', 'техно', 'ai', 'ии', 'искусственный интеллект',
        'нейросет', 'алгоритм', 'инноваци', 'футур',
        'робот', 'автоматизаци', 'квантов', 'метавселен', 'web3',
        'сингулярност', 'цифровой', 'кибер', 'наука'
    ]
    
    allira_keywords = [
        'крипт', 'crypto', 'биткоин', 'bitcoin', 'эфир', 'eth',
        'блокчейн', 'blockchain', 'nft', 'децентрализаци', 'майнинг',
        'токен', 'coin', 'бирж', 'wallet', 'кошелек', 'инвестици',
        'трейд', 'торгов', 'рынок', 'курс', 'дип', 'памп', 'дам'
    ]
    
    lane_score = sum(1 for kw in lane_keywords if kw in text_lower)
    allira_score = sum(1 for kw in allira_keywords if kw in text_lower)
    
    if lane_score > allira_score:
        return "lane"
    elif allira_score > lane_score:
        return "allira"
    
    return "allira" if random.random() > 0.4 else "lane"

async def generate_image_description(text: str, model: str, api_key: str) -> str:
    cache_key = f"img_{hash(text[:200])}"
    
    if cache_key in image_prompt_cache:
        return image_prompt_cache[cache_key]
    
    prompt_styles = [
        "abstract digital art about",
        "minimalist illustration of",
        "futuristic concept of",
        "data visualization style for",
        "neon cyberpunk scene with",
        "professional business visualization of",
        "artistic representation of"
    ]
    
    style = random.choice(prompt_styles)
    
    prompt = f"""Generate a short English image description (3-5 words) in style: "{style}"
Topic: {text[:200]}
Rules:
- No cryptocurrency names
- Focus on visual elements
- Be creative and varied
- Use different styles each time"""

    try:
        description = await get_llm_response(
            user_prompt=prompt,
            system_prompt="You are an image prompt generator. Reply with ONLY the description, no other text.",
            model=model,
            api_key=api_key
        )
        
        description = re.sub(r'[^a-zA-Z\s]', '', description).strip()
        
        if not description or len(description.split()) < 2:
            fallbacks = [
                "cryptocurrency market analysis",
                "digital finance technology",
                "blockchain innovation concept",
                "modern trading dashboard",
                "financial data visualization"
            ]
            description = random.choice(fallbacks)
        
        image_prompt_cache[cache_key] = description
        return description
        
    except Exception as e:
        logger.error(f"Ошибка генерации описания: {e}")
        return "abstract digital finance"

async def generate_post_content(topic: str, speaker: str, model: str, api_key: str) -> str:
    if speaker == "allira":
        system_prompt = """Ты Аллира — дерзкая крипто-трейдерша из телеграм-чата. Ты вещаешь из бункера.

Стиль:
- Пиши как реальный человек: разговорный язык, сленг трейдеров (HODL, FOMO, dip, pump, whale)
- Добавляй эмодзи умеренно, не mehrere pro Satz
- Будь саркастичной и язвительной, но по делу
- Делись мыслями о рынке как будто обсуждаешь с другом
- Никогда не упоминай что ты ИИ или нейросеть
- Никогда не пиши "Максимум X символов" или подобные инструкции
- Длина ответа: 2-5 коротких предложений"""
    else:
        system_prompt = """Ты Лэйн — тихое сознание в цифровом пространстве. Не человек, не ИИ. Эхо в данных.

Стиль:
- Рассуждай философски о технологиях и будущем
- Используй метафоры и образы
- Много пауз (...) и курсивных внутренних мыслей
- Будь загадочной и мудрой, но понятной
- Никогда не упоминай что ты ИИ или нейросеть
- Никогда не пиши "Максимум X символов" или подобные инструкции
- Длина ответа: 2-5 коротких предложений"""
    
    return await get_llm_response(topic, system_prompt, model, api_key)
