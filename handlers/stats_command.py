import logging
from telegram import Update
from telegram.ext import ContextTypes

from utils.database import (
    get_tournament_history, get_tournament_leaderboard,
    get_total_users, get_total_tournaments, get_messages_today,
    get_stat, get_active_users, get_top_speakers
)

logger = logging.getLogger(__name__)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_users = get_total_users()
    total_tournaments = get_total_tournaments()
    messages_today = get_messages_today()
    total_messages = get_stat("total_messages")

    top_users = await get_active_users(days=30, limit=5)
    top_speakers = await get_top_speakers(limit=3)

    lines = [
        "**=> СТАТИСТИКА БОТА**\n",
        f"Юзеров всего: {total_users}",
        f"Сообщений сегодня: {messages_today}",
        f"Всего сообщений: {total_messages}",
        f"Турниров: {total_tournaments}\n",
    ]

    if top_users:
        lines.append("**Топ по активности (30д):**")
        for i, u in enumerate(top_users, 1):
            name = u.get("username") or u.get("first_name") or "???"
            lines.append(f"    {i}. {name} — {u['message_count']}")

    if top_speakers:
        lines.append("\n**Кто чаще отвечает:**")
        for s in top_speakers:
            emoji = "⚡" if s["speaker"] == "allira" else "🌊"
            lines.append(f"    {emoji} {s['speaker']}: {s['count']}")

    await update.message.reply_text("\n".join(lines))


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = await get_tournament_history(limit=10)

    if not history:
        await update.message.reply_text("=> Турниров пока не было. Запусти первый: /start_tournament")
        return

    lines = ["**=> ИСТОРИЯ ТУРНИРОВ**\n"]

    for i, t in enumerate(history, 1):
        mode = "1на1" if t.get("mode") == "pair_match" else "всеvsвсе"
        winner = t.get("winner_name") or "нет"
        ended = t.get("ended_at", "")[:10]
        players = t.get("total_players", t.get("player_count", "?"))
        rounds = t.get("total_rounds", "?")

        lines.append(f"**#{t['id']}** — {ended}")
        lines.append(f"    Режим: {mode} | Игроков: {players} | Раундов: {rounds}")
        lines.append(f"    Победитель: **{winner}**\n")

    await update.message.reply_text("\n".join(lines))


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    leaders = await get_tournament_leaderboard(limit=10)

    if not leaders:
        await update.message.reply_text("=> Пока нет чемпионов. Сыграй турнир!")
        return

    lines = ["**=> ТАБЛИЦА ЛИДЕРОВ**\n"]

    medals = ["🥇", "🥈", "🥉"]
    for i, l in enumerate(leaders, 1):
        prefix = medals[i-1] if i <= 3 else f"    {i}."
        name = l.get("username") or "???"
        lines.append(f"{prefix} {name} — {l['wins']} побед")

    await update.message.reply_text("\n".join(lines))
