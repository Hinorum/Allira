import logging
import random
import re
import uuid
from cachetools import TTLCache
from utils.http_client import get_client, with_retry

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

FALLBACK_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
    "z-ai/glm-5.2:free",
    "nvidia/nemotron-3.5-lightning:free",
]

_model_failures: dict[str, int] = {}
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_RESET = 300

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

def _is_circuit_open(model: str) -> bool:
    failures = _model_failures.get(model, 0)
    return failures >= CIRCUIT_BREAKER_THRESHOLD

def _record_failure(model: str):
    _model_failures[model] = _model_failures.get(model, 0) + 1

def _record_success(model: str):
    _model_failures.pop(model, None)

@with_retry(max_retries=2, base_delay=1.0)
async def _call_openrouter(payload: dict, headers: dict, timeout: float = 25.0):
    client = await get_client()
    return await client.post(OPENROUTER_URL, json=payload, headers=headers, timeout=timeout)

async def get_llm_response(user_prompt: str, system_prompt: str, model: str, api_key: str) -> str:
    cache_key = f"{model}:{uuid.uuid5(uuid.NAMESPACE_DNS, user_prompt[:100] + system_prompt[:100])}"

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
        models_to_try = [primary] + [m for m in FALLBACK_MODELS if m != primary]

        for m in models_to_try:
            if _is_circuit_open(m):
                logger.warning(f"Модель {m} заблокирована circuit breaker")
                continue

            payload["model"] = m
            try:
                resp = await _call_openrouter(payload, headers, timeout=25.0)
                if resp.status_code == 200:
                    result = _parse_ok(resp)
                    if result:
                        _record_success(m)
                        logger.info(f"Модель {m} работает!")
                        return result
                else:
                    logger.warning(f"Модель {m} вернула {resp.status_code}")
                    _record_failure(m)
            except Exception as e:
                logger.warning(f"Модель {m} ошибка: {e}")
                _record_failure(m)

        return None

    try:
        resp = await _call_openrouter(payload, headers, timeout=30.0)

        if resp.status_code == 429:
            logger.warning("Превышен лимит API")
            return "Слишком много запросов! Дай мне минутку передохнуть..."

        if resp.status_code != 200:
            logger.error(f"OpenRouter API error {resp.status_code}: {resp.text[:500]}")
            if resp.status_code in (400, 404, 402):
                result = await _try_models(model)
                if result:
                    response_cache[cache_key] = result[:2000]
                    return result
                return "Все модели временно недоступны. Попробуй позже!"
            return "Сервис временно недоступен. Попробуй позже!"

        result = _parse_ok(resp)
        if result:
            _record_success(model)
            response_cache[cache_key] = result[:2000]
            return result

        logger.warning(f"Некорректный ответ, пробуем fallback модели")
        result = await _try_models(model)
        if result:
            response_cache[cache_key] = result[:2000]
            return result
        return "Модель вернула пустой ответ. Попробуй переформулировать!"

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
- Длина ответа: 3-6 коротких предложений
- Вплети цифры и данные из темы естественно в текст, не выделяй их отдельным блоком"""
    else:
        system_prompt = """Ты Лэйн — тихое сознание в цифровом пространстве. Не человек, не ИИ. Эхо в данных.

Стиль:
- Рассуждай философски о технологиях и будущем
- Используй метафоры и образы
- Много пауз (...) и курсивных внутренних мыслей
- Будь загадочной и мудрой, но понятной
- Никогда не упоминай что ты ИИ или нейросеть
- Никогда не пиши "Максимум X символов" или подобные инструкции
- Длина ответа: 3-6 коротких предложений
- Вплети цифры и данные из темы естественно в текст, не выделяй их отдельным блоком"""

    return await get_llm_response(topic, system_prompt, model, api_key)
