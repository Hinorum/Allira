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

# Структурированный разбор рассуждений вместо эвристики по регуляркам.
# Модели-размышляющие (reasoning) возвращают служебные размышления в поле
# reasoning/reasoning_content — их мы вырезаем на уровне ответа, а не угадываем
# по тексту пользователя.
_STRUCTURED_REASONING_KEYS = ("reasoning", "reasoning_content")

# Эвристика по префиксам остаётся только как страховка для моделей, которые
# пишут рассуждения прямо в content. Порог — только явные служебные обороты.
_PREFIX_HINTS = (
    "Сначала разберу",
    "Давайте разберём",
    "Нужно сохранить",
    "Проверяю кодекс",
    "Останавливаюсь на",
    "Финальный вариант:",
    "Итоговый ответ:",
)


def extract_reasoning(data: dict) -> str | None:
    """Достаёт рассуждение из reasoning-моделей по структуре ответа."""
    for key in _STRUCTURED_REASONING_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        for key in _STRUCTURED_REASONING_KEYS:
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def strip_reasoning(text: str) -> str:
    """Убирает рассуждения, попавшие в content. Работает по абзацам, не по строкам."""
    if not text:
        return text

    # Разбираем по абзацам: рассуждение почти всегда отдельным блоком,
    # а ответ — финальным. Резать по строкам опасно, это портило текст по середине.
    paragraphs = [p for p in re.split(r'\n\s*\n', text) if p.strip()]
    if not paragraphs:
        return text

    kept: list[str] = []
    for idx, para in enumerate(paragraphs):
        first_line = para.strip().split('\n', 1)[0].lower()
        is_reasoning = any(first_line.startswith(hint.lower()) for hint in _PREFIX_HINTS)
        if not is_reasoning:
            kept.append(para)
        elif idx == len(paragraphs) - 1 and not kept:
            # Весь текст выглядит как рассуждение — лучше отдать его, чем молчать
            return text

    result = '\n\n'.join(kept).strip()
    result = re.sub(r'```[\s\S]*?```', '', result)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()

    # Если фильтр съел почти всё — не оставляем пользователя с пустотой.
    if len(result) < 10 and len(text) > 50:
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

async def get_llm_response(
    user_prompt: str,
    system_prompt: str,
    model: str,
    api_key: str,
    history: list[dict] | None = None,
) -> str:
    # В кэш попадает только чистый первый запрос: история диалога делает каждый
    # следующий запрос уникальным, и кэш перестал бы вообще срабатывать.
    cache_key = f"{model}:{uuid.uuid5(uuid.NAMESPACE_DNS, user_prompt + '\x00' + system_prompt)}"

    # Кэш только для запросов без истории: с историей каждый запрос уникален.
    cacheable = not history
    if cacheable and cache_key in response_cache:
        logger.debug("Использован кэшированный ответ")
        return response_cache[cache_key]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/AlliraCryptoBot",
        "X-Title": "AlliraCryptoBot"
    }

    # История диалога идёт перед новым сообщением. Порядок ролей важен для
    # всех провайдеров, поэтому system всегда первый, а не по времени.
    messages = [{"role": "system", "content": system_prompt}]
    for turn in (history or []):
        role = turn.get("role")
        content = str(turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:2000]})
    messages.append({"role": "user", "content": user_prompt[:4000]})

    payload = {
        "model": model,
        "messages": messages,
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
            # У reasoning-моделей content иногда пуст, а полезный текст лежит
            # рядом в поле рассуждения — тогда ответ собираем из него.
            reasoning = extract_reasoning(data)
            if not content and reasoning:
                content = reasoning
            if not content:
                logger.warning(f"Нет content в ответе модели: {str(data)[:300]}")
                return None
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
                    if cacheable:
                        response_cache[cache_key] = result[:2000]
                    return result
                return "Все модели временно недоступны. Попробуй позже!"
            return "Сервис временно недоступен. Попробуй позже!"

        result = _parse_ok(resp)
        if result:
            _record_success(model)
            if cacheable:
                response_cache[cache_key] = result[:2000]
            return result

        logger.warning(f"Некорректный ответ, пробуем fallback модели")
        result = await _try_models(model)
        if result:
            if cacheable:
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
