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
                
            if response.status_code != 200:
                logger.error(f"OpenRouter API error {response.status_code}: {response.text[:500]}")
                if response.status_code in (400, 404):
                    for fallback_model in FALLBACK_MODELS:
                        if fallback_model == model:
                            continue
                        logger.info(f"Пробуем модель: {fallback_model}")
                        payload["model"] = fallback_model
                        response = await client.post(OPENROUTER_URL, json=payload, headers=headers)
                        if response.status_code == 200:
                            data = response.json()
                            result = data["choices"][0]["message"]["content"]
                            response_cache[cache_key] = result[:2000]
                            logger.info(f"Модель {fallback_model} работает!")
                            return result
                        else:
                            logger.error(f"Модель {fallback_model} тоже не работает: {response.status_code}")
                    return "Все модели временно недоступны. Попробуй позже!"
                
            response.raise_for_status()
            data = response.json()
            result = data["choices"][0]["message"]["content"]
            
            response_cache[cache_key] = result[:2000]
            return result
            
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
        system_prompt = """Ты Аллира - дерзкий крипто-трейдер и эксперт по инвестициям.
Стиль:
- Используй сленг трейдеров (HODL, FOMO, dip, памп, etc.)
- Добавляй эмодзи для живости
- Делись "инсайтами" и прогнозами
- Будь саркастичной, но полезной
- Максимум 500 символов"""
    else:
        system_prompt = """Ты Лэйн - загадочный техно-философ.
Стиль:
- Рассуждай о будущем технологий
- Используй метафоры и аналогии
- Добавляй глубокие мысли
- Будь загадочной, но понятной
- Максимум 500 символов"""
    
    return await get_llm_response(topic, system_prompt, model, api_key)
