"""Форматирование текста для Telegram: Markdown → HTML и безопасное разбиение.

Зачем. Промпты персонажей прямо требуют `*курсив*` и `**жирный**`, а ответы
отправлялись как обычный текст — юзер видел `*курсив*` с asterисками вместо
наклонного шрифта. Это выглядит как сырой лог модели и разрушает впечатление
живого собеседника.

Что делает to_telegram_html:
  * `*курсив*` → <i>, `**жирный**` → <b>, `__подчёркнутый__` → <u>,
    `~~зачёркнутый~~` → <s>, `` `код` `` → <code>, блок кода → <pre>,
    `[текст](ссылка)` → <a>;
  * всё остальное экранируется: модель не может подсунуть свой HTML
    (XSS-подобная атака через ответ модели — Telegram её и не исполнил бы,
    но текст всё равно выглядел бы мусором);
  * ссылки принимаются только с http(s) — иначе это просто текст.

Разбиение на части (split_text) идёт ДО конвертации: так разрыв не
рвёт открытый тег, и Telegram не отвергает сообщение с «незакрытым <b>».
"""

from __future__ import annotations

import re
from typing import Iterable

# Telegram не принимает сообщения длиннее 4096 символов. HTML-теги ещё
# добавят к тексту десятки символов, поэтому режем по-RAW с запасом.
MAX_MESSAGE_CHARS = 3800

_CODE_BLOCK_RE = re.compile(r"```(?:[\w+-]*\n)?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
# Курсив требует непустого содержимого без пробелов по краям: иначе
# «2 * 3 * 4» превратилось бы в «2 <i> 3 </i> 4».
_ITALIC_RE = re.compile(r"\*([^*\s](?:[^*\n]*[^*\s])?)\*")
_UNDERLINE_RE = re.compile(r"__([^_\n]+)__")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_LINK_RE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]{1,500})\)")
_URL_RE = re.compile(r"(?<![\"'=(\[])(https?://[^\s<>\"')]{4,300})")
_QUOTE_LINE_RE = re.compile(r"^&gt;\s?(.*)$", re.MULTILINE)


def escape_html(text: str) -> str:
    """Экранирование для Telegram HTML (без кавычек — их Telegram не требует)."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;")\
                    .replace(">", "&gt;")


def to_telegram_html(text: str) -> str:
    """Markdown из ответа модели → безопасный Telegram HTML."""
    if not text:
        return ""
    blocks: list[str] = []

    def _stash(html: str) -> str:
        blocks.append(html)
        return f"\x00{len(blocks) - 1}\x00"

    # 1. Блоки кода — их содержимое не трогаем вообще.
    def _code_block(m: re.Match) -> str:
        body = m.group(1) or ""
        return _stash(f"<pre>{escape_html(body.rstrip())}</pre>")

    result = _CODE_BLOCK_RE.sub(_code_block, text)

    # 2. Инлайн-код.
    def _inline_code(m: re.Match) -> str:
        return _stash(f"<code>{escape_html(m.group(1))}</code>")

    result = _INLINE_CODE_RE.sub(_inline_code, result)

    # 3. Остальное экранируем, дальше работаем только с безопасным текстом.
    result = escape_html(result)

    # 4. Порядок важен: сначала жирный, иначе `**x**` превратится в
    #    `<i><i>x</i></i>`.
    result = _BOLD_RE.sub(r"<b>\1</b>", result)
    result = _UNDERLINE_RE.sub(r"<u>\1</u>", result)
    result = _STRIKE_RE.sub(r"<s>\1</s>", result)
    result = _ITALIC_RE.sub(r"<i>\1</i>", result)

    # 5. Ссылки: только http(s).
    result = _LINK_RE.sub(r'<a href="\2">\1</a>', result)
    # Одиночные ссылки — тоже кликабельными.
    result = _URL_RE.sub(r'<a href="\1">\1</a>', result)

    # 6. Цитаты (строки, начинающиеся с «>») — в блок Telegram.
    result = _QUOTE_LINE_RE.sub(r"<blockquote>\1</blockquote>", result)

    # 7. Возвращаем код обратно.
    def _restore(m: re.Match) -> str:
        idx = int(m.group(1))
        return blocks[idx] if 0 <= idx < len(blocks) else ""

    return re.sub(r"\x00(\d+)\x00", _restore, result)


def split_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Разбить текст на части не длиннее limit.

    Режем по границам абзацев, потом строк, потом предложений, и только в
    крайнем случае — по символам. Границы выбираются так, чтобы в сообщении
    не осталось одинокого слова и не разорвалась мысль.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= limit:
            current = paragraph
            continue
        # Абзац длиннее лимита — режем по строкам, потом по предложениям.
        for line in paragraph.split("\n"):
            if len(line) <= limit:
                if current:
                    chunks.append(current)
                    current = ""
                current = line
                continue
            if current:
                chunks.append(current)
                current = ""
            for piece in _split_sentences(line, limit):
                if len(piece) <= limit:
                    if current and len(current) + 1 + len(piece) <= limit:
                        current = f"{current}\n{piece}"
                    else:
                        if current:
                            chunks.append(current)
                        current = piece
                else:
                    for hard in _hard_split(piece, limit):
                        if current:
                            chunks.append(current)
                        current = hard
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def _split_sentences(line: str, limit: int) -> list[str]:
    """Разрезать строку по точкам/знакам вопроса, сохраняя их."""
    parts: list[str] = []
    buf = ""
    for chunk in re.split(r"(?<=[.!?…])\s+", line):
        if not chunk:
            continue
        if len(buf) + len(chunk) + 1 <= limit:
            buf = f"{buf} {chunk}".strip()
        else:
            if buf:
                parts.append(buf)
            buf = chunk
    if buf:
        parts.append(buf)
    return parts


def _hard_split(text: str, limit: int) -> Iterable[str]:
    """Аварийный разрез по символам — когда не помогли ни строки, ни точки."""
    for i in range(0, len(text), limit):
        yield text[i:i + limit]


def to_telegram_chunks(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Готовые к отправке сообщения: разбить и превратить в HTML.

    Порядок именно такой: сначала режем сырой текст (иначе разрыв может
    попасть внутрь HTML-тега), потом каждый кусок конвертируем отдельно.
    """
    return [to_telegram_html(chunk) for chunk in split_text(text, limit)]


def strip_html(text: str) -> str:
    """Убрать теги — запасной вариант, если Telegram отверг разметку."""
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"</?(?:b|i|u|s|pre|code|blockquote|a)\b[^>]*>", "", text)
    text = (text.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&amp;", "&"))
    return text
