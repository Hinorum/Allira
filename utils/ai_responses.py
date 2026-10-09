import asyncio
import logging
import re
import time
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

# Цепочка для запросов с изображениями: обычные текстовые модели отклоняют
# image_url (400), поэтому у неё свой список — только те, что едят картинки.
VISION_FALLBACK_MODELS = [
    "google/gemma-4-31b-it:free",
    "qwen/qwen3.8-27b:free",
]

_model_failures: dict[str, int] = {}
_model_last_failure: dict[str, float] = {}
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_RESET = 300

# Структурированный разбор рассуждений вместо эвристики по регуляркам.
# Модели-размышляющие (reasoning) возвращают служебные размышления в поле
# reasoning/reasoning_content — их мы вырезаем на уровне ответа, а не угадываем
# по тексту пользователя.
_STRUCTURED_REASONING_KEYS = ("reasoning", "reasoning_content")

# Нуль-байт как разделитель: промпты склеиваются однозначно, без риска коллизии
# вида "ab" + "c" == "a" + "bc".
_CACHE_SEP = "\x00"


def _cache_source(user_prompt: str, system_prompt: str) -> str:
    return user_prompt + _CACHE_SEP + system_prompt

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


def _is_fence_language_tag(line: str) -> bool:
    """Строка вида 'python', 'js' после ``` — это маркер языка, не текст."""
    return bool(re.fullmatch(r"[a-zA-Z0-9_+#.-]{1,20}", line.strip()))


def _drop_reasoning_fence(match: re.Match) -> str:
    """Убирает из ответа блоки кода, внутри которых лежит рассуждение.

    Код НЕ трогаем: техническому боту ответ с кодом — главный результат.
    Раньше здесь стоял слепой `re.sub(r'```...```', '', result)`, который
    вырезал любые примеры кода из ответа модели.
    """
    block = match.group(0)
    body = block[3:-3]
    if body.startswith("\n"):
        body = body[1:]

    lines = [line for line in body.split("\n") if line.strip()]
    if not lines:
        return block

    first = lines[0].strip()
    if _is_fence_language_tag(first) and len(lines) > 1:
        first = lines[1].strip()

    if any(first.lower().startswith(hint.lower()) for hint in _PREFIX_HINTS):
        return ""
    return block


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
    result = re.sub(r'```[\s\S]*?```', _drop_reasoning_fence, result)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()

    # Если фильтр съел почти всё — не оставляем пользователя с пустотой.
    # Пустой результат опасен вдвойне: reply_text("") падает в Telegram.
    if not result or (len(result) < 10 and len(text) > 50):
        return text

    return result


response_cache = TTLCache(maxsize=100, ttl=300)

# Служебные ответы-заглушки. Раньше их ловили сниффингом префиксов прямо
# в main.py ("response.startswith('Технические')...") — любая переформулировка
# ломала проверку. Теперь это единый источник правды, который меняет только
# get_llm_response.
ERR_RATE_LIMITED = "Слишком много запросов! Дай мне минутку передохнуть..."
ERR_NO_MODELS = "Все модели временно недоступны. Попробуй позже!"
ERR_SERVICE_DOWN = "Сервис временно недоступен. Попробуй позже!"
ERR_EMPTY_ANSWER = "Модель вернула пустой ответ. Попробуй переформулировать!"
ERR_TECHNICAL = "Технические неполадки! Попробуй еще раз."

SERVICE_ERRORS = (
    ERR_RATE_LIMITED,
    ERR_NO_MODELS,
    ERR_SERVICE_DOWN,
    ERR_EMPTY_ANSWER,
    ERR_TECHNICAL,
)


def is_service_error(text: str) -> bool:
    """True, если модель вернула заглушку вместо реального ответа."""
    return text in SERVICE_ERRORS

def _is_circuit_open(model: str) -> bool:
    """True, если модель заблокирована после серии ошибок.

    Раньше блокировка была навсегда: счётчик рос, а константа
    CIRCUIT_BREAKER_RESET не использовалась — после трёх сбоев модель
    не работала до рестарта процесса. Теперь по истечении паузы
    пробуем её снова: провайдер мог оправиться.
    """
    failures = _model_failures.get(model, 0)
    if failures < CIRCUIT_BREAKER_THRESHOLD:
        return False

    last_failure = _model_last_failure.get(model, 0.0)
    if time.time() - last_failure >= CIRCUIT_BREAKER_RESET:
        _model_failures.pop(model, None)
        _model_last_failure.pop(model, None)
        logger.info(f"Срок блокировки модели {model} истёк — пробуем снова")
        return False
    return True

def _record_failure(model: str):
    _model_failures[model] = _model_failures.get(model, 0) + 1
    _model_last_failure[model] = time.time()

def _record_success(model: str):
    _model_failures.pop(model, None)
    _model_last_failure.pop(model, None)

# Сколько запросов к OpenRouter может лететь одновременно. Без ограничителя
# всплеск сообщений давал столько же параллельных вызовов — это упиралось
# в лимиты бесплатного тарифа и в RAM free-инстанса.
LLM_CONCURRENCY = 4
_llm_semaphore = asyncio.Semaphore(LLM_CONCURRENCY)


@with_retry(max_retries=2, base_delay=1.0)
async def _call_openrouter(payload: dict, headers: dict, timeout: float = 25.0):
    client = await get_client()
    async with _llm_semaphore:
        return await client.post(OPENROUTER_URL, json=payload, headers=headers, timeout=timeout)

async def get_llm_response(
    user_prompt: str,
    system_prompt: str,
    model: str,
    api_key: str,
    history: list[dict] | None = None,
    images: list[str] | None = None,
) -> str:
    # Разделитель вынесен в переменную: обратный слэш внутри f-string запрещён
    # на Python 3.11, а на Render зафиксирован именно 3.11.11.
    cache_key = f"{model}:{uuid.uuid5(uuid.NAMESPACE_DNS, _cache_source(user_prompt, system_prompt))}"

    # Кэш только для запросов без истории и без картинок: ключ строится по
    # тексту, а с изображениями одинаковый текст даёт разные ответы.
    cacheable = not history and not images
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

    # Мультимодальная часть: текст + картинки (OpenRouter принимает
    # data-URL в image_url). Без картинок content остаётся строкой —
    # формат, который ждут все текстовые модели.
    def _user_content(imgs: list[str] | None):
        if not imgs:
            return user_prompt[:4000]
        parts: list[dict] = [{"type": "text", "text": user_prompt[:4000]}]
        parts.extend(
            {"type": "image_url", "image_url": {"url": url}} for url in imgs
        )
        return parts

    messages.append({"role": "user", "content": _user_content(images)})

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
        if not content:
            # У reasoning-моделей content пуст, а полезный текст лежит рядом
            # в поле рассуждения. Раньше эта ветка была мёртвой (`if not content`
            # стояло внутри `if content:`), и такие ответы отбрасывались.
            content = extract_reasoning(data)
        if not content:
            logger.warning(f"Нет content в ответе модели: {str(data)[:300]}")
            return None
        return strip_reasoning(content)

    async def _try_models(primary: str, imgs: list[str] | None = None) -> str | None:
        if imgs:
            # С картинками пробуем только vision-модели: текстовые отвечают
            # 400 и зря тратят лимит circuit breaker.
            chain = [primary] + [m for m in VISION_FALLBACK_MODELS if m != primary]
        else:
            chain = [primary] + [m for m in FALLBACK_MODELS if m != primary]

        messages[-1]["content"] = _user_content(imgs)

        for m in chain:
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

    async def _run(imgs: list[str] | None) -> tuple[str | None, str | None]:
        """Один проход по цепочке моделей. Возвращает (ответ, сервисная ошибка)."""
        messages[-1]["content"] = _user_content(imgs)
        payload["messages"] = messages
        resp = await _call_openrouter(payload, headers, timeout=30.0)

        if resp.status_code == 429:
            logger.warning("Превышен лимит API")
            return None, ERR_RATE_LIMITED

        if resp.status_code != 200:
            logger.error(f"OpenRouter API error {resp.status_code}: {resp.text[:500]}")
            if resp.status_code in (400, 404, 402):
                result = await _try_models(model, imgs)
                return (result, None) if result else (None, ERR_NO_MODELS)
            return None, ERR_SERVICE_DOWN

        result = _parse_ok(resp)
        if result:
            _record_success(model)
            return result, None

        logger.warning("Некорректный ответ, пробуем fallback модели")
        result = await _try_models(model, imgs)
        if result:
            return result, None
        return None, ERR_EMPTY_ANSWER

    try:
        result, err = await _run(images)
        if result:
            if cacheable:
                response_cache[cache_key] = result[:2000]
            return result

        # Деградация: ни одна vision-модель не съела картинку — отвечаем
        # текстом, только быстрее молчать, чем молчать совсем.
        if images and err not in (ERR_RATE_LIMITED, ERR_TECHNICAL):
            logger.info("Картинки не обработались, повторяем без изображений")
            result, err = await _run(None)
            if result:
                return result

        return err or ERR_NO_MODELS

    except Exception as e:
        logger.error(f"Ошибка LLM: {e}")
        return ERR_TECHNICAL

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

    # Равный счёт — детерминированный дефолт. Раньше здесь был случайный
    # выбор: один и тот же текст скалывал то под Аллиру, то под Лэйн, а кэш
    # ответов (модель + промпт) из-за смены персонажа промахивался вхолостую.
    return "allira"

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
