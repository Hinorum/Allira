"""Режиссура длины ответа: сколько фраз, о чём и как заканчивать.

Проблема. Промпты требуют «2-5 предложений, без вступлений», но модель
на это не смотрит: на обычный вопрос в чате она выдаёт простыню на
15-20 предложений, а потом ещё и спрашивает «Хочешь, расскажу
подробнее?». Словами в промпте длину не удержать — работает конкретное
число.

Три приёма:

  1. СЦЕНАРИЙ. По природе запроса решаем, сколько фраз ждём: на «привет»
     — две, на «расскажи, как работает X» — двенадцать, на «что такое
     стейкинг» — четыре.
  2. ДИРЕКТИВА. В системный промпт уходит конкретная цифра: «3-4
     предложения, до 500 знаков». Модель держит число куда лучше, чем
     «будь кратко».
  3. РЕЖИССЁРСКАЯ СКЛЕЙКА. После ответа убираем вступление («Конечно!»,
     «Хороший вопрос!») и хвост-вопрос («Хочешь, расскажу подробнее?»),
     а если ответ всё равно длиннее сценария — подрезаем по границе
     предложения.

Чего склейка НЕ делает: не трогает код (блок с кодом режется только если
просили структуру), не режет ответы в режиме «подробно» и не выкидывает
смысл — режем всегда по границе предложения, а не по символам.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Сценарии длины.
SHORT = "short"
MEDIUM = "medium"
LONG = "long"

# Ключевые слова, по которым видно, что нужен развёрнутый ответ.
DETAILED_WORDS = (
    "подробно", "подробнее", "разложи", "разбери", "разбор", "пошагово",
    "инструкция", "гайд", "как сделать", "что делать", "как настроить",
    "как устроен", "как работает", "сравни", "анализ", "историю", "история",
    "расскажи всё", "расскажи все", "полностью", "все варианты", "всех",
    "пример", "примеры", "список", "что нужно", "что необходимо", "план",
    "стратегия", "вопросы", "faq",
)

# Мелкие разговоры, где длинный ответ — перебор.
SMALLTALK_WORDS = (
    "привет", "хай", "здаров", "здарова", "ку", "йо", "как дела", "как ты",
    "кто ты", "ты кто", "что ты умеешь", "расскажи о себе", "прикольно",
    "круто", "спасибо", "благодарю", "ок", "окей", "понятно", "ясно",
    "пока", "до встречи", "прикольный", "смешно", "хаха", "лол",
)

# Вступления — «голос ассистента», который в этой роли не нужен.
PREAMBLES = (
    "конечно!", "конечно", "с удовольствием!", "с удовольствием",
    "хороший вопрос!", "хороший вопрос", "отличный вопрос!",
    "отличный вопрос", "как вы и просили:", "как ты и просил:",
    "вот подробный разбор:", "вот разбор:", "вот ответ:",
    "вот что могу сказать:", "ясно!", "да!", "безусловно!",
    "разберём по полочкам", "давай разберём", "по порядку:",
    "вот краткий разбор", "certainly", "sure!", "of course",
    "great question", "here's the thing", "let me explain",
)

# Хвосты: предложения-оферты. Персонаж не спрашивает разрешения продолжить.
TRAILING_OFFERS = (
    "если хочешь, могу подробнее",
    "если хочешь, могу рассказать подробнее",
    "если нужно, могу подробнее",
    "если хочешь, расскажу подробнее",
    "хочешь, расскажу подробнее?",
    "хочешь подробнее?",
    "нужно что-то ещё?",
    "нужно что-то еще?",
    "есть ещё вопросы?",
    "есть еще вопросы?",
    "хочешь, разберём ещё?",
    "хочешь, разберем еще?",
    "дай знать, если нужно что-то ещё",
    "дай знать, если нужно что-то еще",
    "скажи, если хочешь подробнее",
    "tell me if you want more",
    "let me know if",
    "if you want more",
)


@dataclass
class LengthPlan:
    """Сценарий ответа: сколько фраз и знаков ждём."""

    mode: str
    max_sentences: int
    max_chars: int
    directive: str

    @property
    def is_detailed(self) -> bool:
        return self.mode == LONG


# Готовые сценарии. Директива — то, что реально попадает в промпт.
PLANS = {
    SHORT: LengthPlan(
        mode=SHORT, max_sentences=3, max_chars=320,
        directive=(
            "ДЛИНА ОТВЕТА: 2-3 предложения, до 300 знаков. "
            "Одно-два предложения — и точка. Без вступлений и без "
            "вопросов в конце."
        ),
    ),
    MEDIUM: LengthPlan(
        mode=MEDIUM, max_sentences=5, max_chars=700,
        directive=(
            "ДЛИНА ОТВЕТА: 3-5 предложений, до 600 знаков. "
            "По делу, без воды и без вопросов в конце."
        ),
    ),
    LONG: LengthPlan(
        mode=LONG, max_sentences=14, max_chars=2200,
        directive=(
            "ДЛИНА ОТВЕТА: развёрнуто, до 12-14 предложений. "
            "Структурно: короткие абзацы, списки где уместно. "
            "Без вступлений и без вопросов в конце."
        ),
    ),
}

# Длинный план, но мягче: «что такое X» — это четыре предложения.
PLAN_TECHNICAL = LengthPlan(
    mode=MEDIUM, max_sentences=6, max_chars=900,
    directive=(
        "ДЛИНА ОТВЕТА: 4-6 предложений, до 800 знаков. "
        "Объясни по сути, без воды и без вопросов в конце."
    ),
)


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def plan_for(text: str, prev_overflow: int = 0) -> LengthPlan:
    """Выбрать сценарий длины под запрос.

    prev_overflow — на сколько фраз предыдущий ответ вышел за сценарий.
    Если модель в прошлый раз разлилась, следующий раз жмём сильнее.
    """
    t = (text or "").strip().lower()
    if not t:
        return PLANS[SHORT]

    if _has(t, DETAILED_WORDS):
        plan = PLANS[LONG]
    elif _has(t, SMALLTALK_WORDS) and len(t) < 60:
        plan = PLANS[SHORT]
    elif "?" in t or _looks_process(t):
        plan = PLAN_TECHNICAL
    else:
        plan = PLANS[MEDIUM]

    if prev_overflow > 0 and plan.mode != LONG:
        # Режиссёр помнит: в прошлый раз ответ был длиннее сценария.
        plan = LengthPlan(
            mode=plan.mode,
            max_sentences=max(2, plan.max_sentences - prev_overflow),
            max_chars=max(200, int(plan.max_chars * 0.7)),
            directive=plan.directive + (
                " В прошлый раз ты растянула ответ — сейчас короче."
            ),
        )
    return plan


def _looks_process(text: str) -> bool:
    """Вопрос про устройство/процесс — там нужен чуть больший ответ."""
    starts = ("как ", "почему ", "зачем ", "что значит", "что такое",
              "чем отличается", "разница между", "из чего состоит")
    return text.startswith(starts)


def strip_preamble(text: str) -> str:
    """Убрать вступление в духе «Конечно!» / «Хороший вопрос!»."""
    if not text:
        return text
    out = text.strip()
    changed = True
    while changed:
        changed = False
        for phrase in PREAMBLES:
            low = out.lower()
            if low.startswith(phrase):
                rest = out[len(phrase):].lstrip(" \t\n,:;.!?—-–")
                # Вступление — только если после него осталось хоть что-то
                # похожее на ответ, а не пустота.
                if rest:
                    out = rest
                    changed = True
                    break
    return out


def strip_trailing_offer(text: str) -> str:
    """Убрать хвост-предложение «Хочешь, расскажу подробнее?»."""
    if not text:
        return text
    out = text.strip()
    for _ in range(3):
        low = out.lower()
        cut = None
        for phrase in TRAILING_OFFERS:
            idx = low.rfind(phrase)
            if idx > 0:
                cut = idx if cut is None else min(cut, idx)
        if cut is None:
            break
        out = out[:cut].rstrip(" \t\n,.!—-–")
    return out or text


def count_sentences(text: str) -> int:
    """Сколько предложений в тексте (примерно)."""
    if not text:
        return 0
    parts = [p for p in re.split(r"(?<=[.!?…])\s+", text.strip()) if p.strip()]
    return max(1, len(parts))


def _sentences(text: str) -> list[str]:
    return [p for p in re.split(r"(?<=[.!?…])\s+", text.strip()) if p.strip()]


def _has_code(text: str) -> bool:
    return "```" in text or "\n    " in text


def trim_to_plan(text: str, plan: LengthPlan) -> tuple[str, bool]:
    """Подрезать ответ по сценарию. Возвращает (текст, обрезан ли)."""
    if not text or plan.is_detailed:
        return text, False
    if _has_code(text):
        # Код не режем: кусок кода без начала хуже длинного ответа.
        return text, False

    sentences = _sentences(text)
    if len(sentences) <= plan.max_sentences and len(text) <= plan.max_chars:
        return text, False

    kept: list[str] = []
    size = 0
    for sentence in sentences:
        if len(kept) >= plan.max_sentences:
            break
        if kept and size + len(sentence) > plan.max_chars:
            break
        kept.append(sentence)
        size += len(sentence) + 1

    if not kept:
        return text, False
    trimmed = " ".join(kept).strip()
    if len(trimmed) < len(text.strip()):
        # Многоточие вместо обрубка: обрыв на полуслове читается как сбой.
        trimmed = trimmed.rstrip(" ,;:.!?—-–") + "…"
    return trimmed, True


def overflow_sentences(text: str, plan: LengthPlan) -> int:
    """На сколько фраж ответ вышел за сценарий (для обратной связи)."""
    return max(0, count_sentences(text) - plan.max_sentences)


def direct(answer: str, plan: LengthPlan) -> tuple[str, int]:
    """Полная режиссёрская склейка ответа.

    Возвращает (готовый текст, на сколько фраж он вылез за сценарий).
    """
    if not answer:
        return answer, 0
    out = strip_preamble(answer)
    out = strip_trailing_offer(out)
    out, _trimmed = trim_to_plan(out, plan)
    return out.strip(), overflow_sentences(answer, plan)
