import logging
import asyncio
import random
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import DiceEmoji
from utils.database import save_tournament, save_tournament_players, increment_stat

logger = logging.getLogger(__name__)

last_roll_times = {}
ROLL_COOLDOWN = 2.0
MAX_REMATCHES = 3
ELIMINATION_PERCENTAGE = 4

_tournament_states: dict[int, dict] = {}
_round_announcing: set[int] = set()


def _default_state(chat_id: int = None) -> dict:
    return {
        "active": False,
        "registration_open": False,
        "players": {},
        "current_round": 0,
        "round_matches": [],
        "tournament_mode": None,
        "tournament_message_id": None,
        "tournament_chat_id": chat_id,
        "active_players_in_round": set(),
        "player_rolls_in_round": {},
        "admin_user_id": None,
        "player_with_bye": None,
        "rematch_count": 0,
        "is_final_round": False,
    }


def _get_state(chat_id: int) -> dict:
    if chat_id not in _tournament_states:
        _tournament_states[chat_id] = _default_state(chat_id)
    return _tournament_states[chat_id]


def _reset_state(chat_id: int):
    _tournament_states[chat_id] = _default_state(chat_id)


def _make_pairs(player_ids: list[int]) -> tuple[list[tuple[int, int]], int | None]:
    random.shuffle(player_ids)
    bye_player = None
    if len(player_ids) % 2 != 0:
        bye_player = player_ids.pop()
    pairs = [(player_ids[i], player_ids[i + 1]) for i in range(0, len(player_ids), 2)]
    return pairs, bye_player


def get_tournament_status_message(chat_id: int) -> str:
    state = _get_state(chat_id)

    if not state["active"]:
        return "=> Турнир не активен. Запусти через /start_tournament"

    message_lines = [f"🎲 **АРЕНА КУБИКОВ — Раунд {state['current_round']}** 🎲\n"]

    if state["registration_open"]:
        message_lines.append("**Регистрация открыта!** Жми 'Войти на арену' чтобы войти в игру.")
        if state["players"]:
            message_lines.append("\n**На арене:**")
            for user_id, data in state["players"].items():
                message_lines.append(f"    -> {data['username']} (Очки: {data.get('total_score', 0)})")
        else:
            message_lines.append("\n_Пока пусто. Будь первым._")

        if len(state["players"]) >= 2:
            message_lines.append("\n\n_Админ: жми 'Завершить набор' когда все готовы._")
        else:
            message_lines.append("\n\n_Нужно минимум 2 игрока._")
    else:
        mode_text = "1 на 1" if state["tournament_mode"] == "pair_match" else "Все против всех"
        if state.get("is_final_round"):
            mode_text += " (ФИНАЛ)"
        message_lines.append(f"**Режим:** {mode_text}")

        active_players_list = []
        eliminated_players_list = []

        for user_id, data in state["players"].items():
            roll_info = ""
            if state["current_round"] > 0:
                if user_id in state["player_rolls_in_round"]:
                    roll_info = f", бросок: {state['player_rolls_in_round'][user_id]}"
                elif user_id in state["active_players_in_round"]:
                    roll_info = ", ожидает броска"
                elif data.get("is_eliminated"):
                    roll_info = ", выбыл"
                elif user_id == state.get("player_with_bye"):
                    roll_info = ", проходит без игры (бай)"

            if data.get("is_eliminated"):
                eliminated_players_list.append(f"    ❌ {data['username']} (р. {data.get('eliminated_round', '?')})")
            else:
                active_players_list.append(f"    {data['username']} — {data.get('total_score', 0)} очк.{roll_info}")

        if active_players_list:
            message_lines.append("\n**На арене:**")
            message_lines.extend(active_players_list)

        if eliminated_players_list:
            message_lines.append("\n**Выбывшие:**")
            message_lines.extend(eliminated_players_list)

        if state["current_round"] > 0 and not state["registration_open"]:
            if state["tournament_mode"] == "pair_match" and state["round_matches"]:
                message_lines.append("\n**Матчи:**")
                for p1_id, p2_id in state["round_matches"]:
                    p1_data = state["players"].get(p1_id, {})
                    p2_data = state["players"].get(p2_id, {})
                    p1_name = p1_data.get("username", "Игрок1")
                    p2_name = p2_data.get("username", "Игрок2")
                    p1_roll = state["player_rolls_in_round"].get(p1_id, "?")
                    p2_roll = state["player_rolls_in_round"].get(p2_id, "?")
                    match_status = f"    {p1_name} ({p1_roll}) vs {p2_name} ({p2_roll})"

                    if p1_id in state["player_rolls_in_round"] and p2_id in state["player_rolls_in_round"]:
                        if p1_roll > p2_roll:
                            match_status += f" -> {p1_name} побеждает"
                        elif p2_roll > p1_roll:
                            match_status += f" -> {p2_name} побеждает"
                        else:
                            match_status += " -> Ничья"

                    message_lines.append(match_status)

            if state.get("player_with_bye"):
                player_bye_name = state["players"].get(state["player_with_bye"], {}).get("username", "Игрок")
                message_lines.append(f"\n{player_bye_name} проходит без игры (бай).")

            if state["active_players_in_round"]:
                waiting_for_roll_users = [
                    state["players"][pid]["username"]
                    for pid in state["active_players_in_round"]
                    if pid in state["players"]
                ]
                if waiting_for_roll_users:
                    message_lines.append(f"\n_Ждём броска от: {', '.join(waiting_for_roll_users)}_")
            elif not state["active_players_in_round"]:
                remaining = sum(1 for p in state["players"].values() if not p.get("is_eliminated"))
                if remaining > 1:
                    message_lines.append("\n\n_Админ, жми 'Следующий раунд'._")
                elif remaining == 1:
                    winner_name = [p['username'] for p in state["players"].values() if not p.get("is_eliminated")][0]
                    message_lines.append(f"\n\n**Почти готово. Финалист: {winner_name}**")

    return "\n".join(message_lines)


async def start_tournament_from_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        if update.message:
            await start_dice_tournament_registration(update, context)
        return

    await query.answer()
    await start_dice_tournament_registration(update, context)


async def update_tournament_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
    if chat_id is None:
        return

    state = _get_state(chat_id)
    message_id = state.get("tournament_message_id")

    if not message_id:
        return

    try:
        current_text = get_tournament_status_message(chat_id)
        reply_markup = None
        buttons = []

        if state["registration_open"]:
            buttons.append([InlineKeyboardButton("Войти на арену", callback_data="register_for_tournament")])
            if len(state["players"]) >= 2:
                buttons.append([InlineKeyboardButton("Завершить набор", callback_data="end_registration_and_start_round")])
            reply_markup = InlineKeyboardMarkup(buttons)
        elif state["active"] and not state["registration_open"]:
            if state["active_players_in_round"]:
                buttons.append([InlineKeyboardButton("Бросить кубик", callback_data="make_tournament_roll")])
                reply_markup = InlineKeyboardMarkup(buttons)
            else:
                active_count = sum(1 for p in state["players"].values() if not p.get("is_eliminated"))
                if active_count > 1 and state["current_round"] > 0:
                    buttons.append([InlineKeyboardButton("Следующий раунд", callback_data="end_registration_and_start_round")])
                    reply_markup = InlineKeyboardMarkup(buttons)

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=current_text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Ошибка обновления турнирной таблицы: {e}")
        if "Message to edit not found" in str(e) or "message is not modified" in str(e).lower():
            state["tournament_message_id"] = None
            if state["active"] and chat_id:
                try:
                    sent = await context.bot.send_message(
                        chat_id=chat_id,
                        text=get_tournament_status_message(chat_id),
                        parse_mode="Markdown"
                    )
                    state["tournament_message_id"] = sent.message_id
                except Exception as send_e:
                    logger.error(f"Не удалось отправить новое сообщение: {send_e}")


async def announce_round_results(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if chat_id in _round_announcing:
        return
    _round_announcing.add(chat_id)

    try:
        state = _get_state(chat_id)
        round_num = state["current_round"]
        message_lines = [f"**=> Итоги раунда {round_num}:**\n"]
        eliminated_this_round = set()

        if state["tournament_mode"] == "pair_match":
            rematch_matches = []

            for p1_id, p2_id in state["round_matches"]:
                p1_data = state["players"].get(p1_id)
                p2_data = state["players"].get(p2_id)

                if not p1_data or not p2_data:
                    continue
                if p1_data.get("is_eliminated") or p2_data.get("is_eliminated"):
                    continue

                p1_roll = state["player_rolls_in_round"].get(p1_id)
                p2_roll = state["player_rolls_in_round"].get(p2_id)

                if p1_roll is None or p2_roll is None:
                    message_lines.append(f"⚠️ {p1_data.get('username')} vs {p2_data.get('username')} — не все броски засчитаны.")
                    continue

                if p1_roll == p2_roll:
                    state["rematch_count"] = state.get("rematch_count", 0) + 1
                    if state["rematch_count"] >= MAX_REMATCHES:
                        winner_id = random.choice([p1_id, p2_id])
                        loser_id = p2_id if winner_id == p1_id else p1_id
                        state["players"][winner_id]["total_score"] += 1
                        state["players"][loser_id]["is_eliminated"] = True
                        state["players"][loser_id]["eliminated_round"] = round_num
                        eliminated_this_round.add(loser_id)
                        winner_name = state["players"][winner_id]["username"]
                        message_lines.append(
                            f"    {p1_data['username']} ({p1_roll}) vs {p2_data['username']} ({p2_roll}) -> "
                            f"Ничья ×{state['rematch_count']}! Жребий: **{winner_name}** проходит"
                        )
                    else:
                        rematch_matches.append((p1_id, p2_id))
                        message_lines.append(
                            f"    {p1_data['username']} ({p1_roll}) vs {p2_data['username']} ({p2_roll}) -> "
                            f"Ничья (переигровка #{state['rematch_count']})"
                        )
                elif p1_roll > p2_roll:
                    state["players"][p1_id]["total_score"] += 1
                    state["players"][p2_id]["is_eliminated"] = True
                    state["players"][p2_id]["eliminated_round"] = round_num
                    eliminated_this_round.add(p2_id)
                    message_lines.append(
                        f"    {p1_data['username']} ({p1_roll}) vs {p2_data['username']} ({p2_roll}) -> "
                        f"**{p1_data['username']}** побеждает"
                    )
                else:
                    state["players"][p2_id]["total_score"] += 1
                    state["players"][p1_id]["is_eliminated"] = True
                    state["players"][p1_id]["eliminated_round"] = round_num
                    eliminated_this_round.add(p1_id)
                    message_lines.append(
                        f"    {p1_data['username']} ({p1_roll}) vs {p2_data['username']} ({p2_roll}) -> "
                        f"**{p2_data['username']}** побеждает"
                    )

            state["round_matches"] = rematch_matches
            if not rematch_matches:
                state["rematch_count"] = 0

        elif state["tournament_mode"] == "all_vs_all":
            active_players = [
                data for uid, data in state["players"].items()
                if not data.get("is_eliminated") and uid in state["player_rolls_in_round"]
            ]

            if not active_players:
                message_lines.append("Нет бросков в этом раунде.")
            else:
                active_players.sort(key=lambda x: state["player_rolls_in_round"].get(x["user_id"], 0))

                message_lines.append("\n**Броски:**")
                for p_data in active_players:
                    roll = state["player_rolls_in_round"].get(p_data["user_id"], "?")
                    message_lines.append(f"    {p_data['username']}: {roll}")

                remaining_after = len(active_players)

                if state.get("is_final_round") and remaining_after == 2:
                    p1, p2 = active_players[0], active_players[1]
                    r1 = state["player_rolls_in_round"].get(p1["user_id"], 0)
                    r2 = state["player_rolls_in_round"].get(p2["user_id"], 0)
                    if r1 > r2:
                        state["players"][p2["user_id"]]["is_eliminated"] = True
                        state["players"][p2["user_id"]]["eliminated_round"] = round_num
                        eliminated_this_round.add(p2["user_id"])
                        message_lines.append(f"\n**Финал:** {p1['username']} ({r1}) > {p2['username']} ({r2})")
                    elif r2 > r1:
                        state["players"][p1["user_id"]]["is_eliminated"] = True
                        state["players"][p1["user_id"]]["eliminated_round"] = round_num
                        eliminated_this_round.add(p1["user_id"])
                        message_lines.append(f"\n**Финал:** {p2['username']} ({r2}) > {p1['username']} ({r1})")
                    else:
                        winner = random.choice([p1, p2])
                        loser = p2 if winner["user_id"] == p1["user_id"] else p1
                        state["players"][loser["user_id"]]["is_eliminated"] = True
                        state["players"][loser["user_id"]]["eliminated_round"] = round_num
                        eliminated_this_round.add(loser["user_id"])
                        message_lines.append(f"\n**Финал:** Ничья! Жребий: **{winner['username']}** побеждает")
                elif remaining_after > 2:
                    num_to_eliminate = max(1, remaining_after // ELIMINATION_PERCENTAGE)
                    if remaining_after - num_to_eliminate < 1:
                        num_to_eliminate = remaining_after - 1

                    for p_data in active_players[:num_to_eliminate]:
                        uid = p_data["user_id"]
                        if not state["players"][uid].get("is_eliminated"):
                            state["players"][uid]["is_eliminated"] = True
                            state["players"][uid]["eliminated_round"] = round_num
                            eliminated_this_round.add(uid)
                            message_lines.append(f"\n_Выбывает: {p_data['username']}_")
                else:
                    message_lines.append("\n_Мало игроков — все проходят._")

        await context.bot.send_message(chat_id=chat_id, text="\n".join(message_lines), parse_mode="Markdown")

        state["player_rolls_in_round"].clear()
        state["active_players_in_round"].clear()
        state["player_with_bye"] = None

        await update_tournament_message(context, chat_id)

        remaining = [p for p in state["players"].values() if not p.get("is_eliminated")]

        if len(remaining) <= 1:
            await end_tournament(context, chat_id, remaining[0] if remaining else None)
        elif len(remaining) == 2 and state["tournament_mode"] == "all_vs_all" and not state.get("is_final_round"):
            state["is_final_round"] = True
            await context.bot.send_message(
                chat_id=chat_id,
                text="🏁 **ФИНАЛ!** Осталось двое. Кто бросит больше — тот чемпион!"
            )

    finally:
        _round_announcing.discard(chat_id)


async def end_tournament(context: ContextTypes.DEFAULT_TYPE, chat_id: int, winner_data: dict = None):
    state = _get_state(chat_id)
    message_id = state.get("tournament_message_id")

    message = "**=> ИГРА ОКОНЧЕНА**\n\n"

    final_winner = None
    if winner_data and not winner_data.get("is_eliminated"):
        final_winner = winner_data
    else:
        active = [p for p in state["players"].values() if not p.get("is_eliminated")]
        if len(active) == 1:
            final_winner = active[0]
        elif not active and state["players"]:
            sorted_players = sorted(
                state["players"].values(),
                key=lambda p: (p.get("total_score", 0), -p.get("eliminated_round", 0)),
                reverse=True
            )
            if sorted_players:
                message += "Все игроки выбыли. Победитель по очкам:\n"
                final_winner = sorted_players[0]

    if final_winner:
        message += f"**Чемпион:** {final_winner['username']} — {final_winner.get('total_score', 0)} очков. 👑"
    elif not state["players"]:
        message += "Никто не пришёл. Пустая арена — грустно."
    else:
        message += "Победитель не определён."
        sorted_players = sorted(state["players"].values(), key=lambda p: p.get("total_score", 0), reverse=True)
        message += "\n\n**Таблица:**\n"
        for p_data in sorted_players:
            status = "выбыл" if p_data.get("is_eliminated") else "активен"
            message += f"- {p_data['username']}: {p_data.get('total_score', 0)} ({status})\n"

    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=message, parse_mode="Markdown", reply_markup=None
            )
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")
    else:
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")

    if message_id and chat_id:
        try:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

    try:
        winner_id = None
        winner_name = None
        if final_winner:
            winner_id = final_winner.get("user_id")
            winner_name = final_winner.get("username")

        total_rounds = state.get("current_round", 0)
        total_players = len(state.get("players", {}))
        admin_id = state.get("admin_user_id", 0)

        tid = await save_tournament(chat_id, admin_id, state.get("tournament_mode", ""),
                               total_rounds, total_players, winner_id, winner_name)
        if tid:
            await save_tournament_players(tid, state["players"])
            increment_stat("total_tournaments")
    except Exception as e:
        logger.error(f"Ошибка сохранения турнира: {e}")

    _reset_state(chat_id)
    logger.info(f"Турнир в чате {chat_id} завершен")


async def start_dice_tournament_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = None
    user_id = None
    message_to_reply = None

    if update.message:
        chat_id = update.message.chat_id
        user_id = update.message.from_user.id
        message_to_reply = update.message
    elif update.callback_query and update.callback_query.message:
        query = update.callback_query
        chat_id = query.message.chat_id
        user_id = query.from_user.id
        message_to_reply = query.message
    else:
        return

    state = _get_state(chat_id)

    if state["active"]:
        reply_text = "=> Турнир уже идёт. Сначала заверши текущий (/stop_tournament)."
        if message_to_reply:
            await message_to_reply.reply_text(reply_text)
        return

    _reset_state(chat_id)
    state = _get_state(chat_id)
    state.update({
        "active": True,
        "registration_open": True,
        "tournament_chat_id": chat_id,
        "admin_user_id": user_id,
    })

    status_text = get_tournament_status_message(chat_id)
    initial_buttons = [[InlineKeyboardButton("Войти на арену", callback_data="register_for_tournament")]]
    reply_markup = InlineKeyboardMarkup(initial_buttons)

    try:
        if update.callback_query:
            try:
                await message_to_reply.edit_text(text=status_text, reply_markup=reply_markup, parse_mode="Markdown")
                state["tournament_message_id"] = message_to_reply.message_id
            except Exception:
                sent = await context.bot.send_message(chat_id=chat_id, text=status_text, reply_markup=reply_markup, parse_mode="Markdown")
                state["tournament_message_id"] = sent.message_id
        else:
            sent = await message_to_reply.reply_text(text=status_text, reply_markup=reply_markup, parse_mode="Markdown")
            state["tournament_message_id"] = sent.message_id

        if state.get("tournament_message_id") and chat_id:
            try:
                await context.bot.pin_chat_message(chat_id=chat_id, message_id=state["tournament_message_id"], disable_notification=True)
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Ошибка начала турнира: {e}")
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="=> Что-то пошло не так. Попробуй позже.")


async def register_for_tournament(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        if query:
            await query.answer("Ошибка.")
        return

    user = query.from_user
    chat_id = query.message.chat_id
    state = _get_state(chat_id)

    if not state["active"]:
        await query.answer("Турнир не активен.", show_alert=True)
        return
    if not state["registration_open"]:
        await query.answer("Регистрация уже завершена.", show_alert=True)
        return

    if user.id in state["players"]:
        await query.answer(f"{user.first_name}, ты уже на арене!", show_alert=False)
    else:
        state["players"][user.id] = {
            "user_id": user.id,
            "username": user.first_name or f"User_{user.id}",
            "total_score": 0,
            "is_eliminated": False,
            "eliminated_round": 0,
        }
        await query.answer(f"{user.first_name}, ты на арене!", show_alert=False)
        await update_tournament_message(context, chat_id)


async def end_registration_and_start_round(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        if query:
            await query.answer("Ошибка.", show_alert=True)
        return

    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    state = _get_state(chat_id)

    if user_id != state.get("admin_user_id"):
        await query.answer("Только админ может это сделать.", show_alert=True)
        return

    if not state["active"]:
        try:
            await query.message.edit_text("Турнир не активен.", reply_markup=None)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text="Турнир не активен.")
        return

    if state["registration_open"]:
        if len(state["players"]) < 2:
            await query.answer("Мало игроков (нужно минимум 2).", show_alert=True)
            return

        state["registration_open"] = False

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 на 1", callback_data="set_tournament_mode:pair_match")],
            [InlineKeyboardButton("Все против всех", callback_data="set_tournament_mode:all_vs_all")]
        ])
        try:
            await query.message.edit_text(text="=> Набор окончен. Выбирай режим:", reply_markup=keyboard, parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text="=> Набор окончен. Выбирай режим:", reply_markup=keyboard, parse_mode="Markdown")
        return

    if not state["registration_open"] and state["current_round"] > 0:
        if state["active_players_in_round"]:
            await query.answer("Не все игроки сделали бросок.", show_alert=True)
            return

        state["current_round"] += 1
        round_num = state["current_round"]

        active_ids = [pid for pid, p in state["players"].items() if not p.get("is_eliminated")]

        if len(active_ids) <= 1:
            await end_tournament(context, chat_id, state["players"].get(active_ids[0]) if active_ids else None)
            return

        state["active_players_in_round"] = set(active_ids)
        state["player_rolls_in_round"].clear()
        state["player_with_bye"] = None

        if state["tournament_mode"] == "pair_match":
            next_matches = list(state.get("round_matches", []))
            new_pair_ids = [pid for pid in active_ids if not any(pid in m for m in next_matches)]

            if new_pair_ids:
                pairs, bye_id = _make_pairs(new_pair_ids)
                next_matches.extend(pairs)
                if bye_id is not None:
                    state["player_with_bye"] = bye_id
                    bye_name = state["players"].get(bye_id, {}).get("username", "Игрок")
                    state["active_players_in_round"].discard(bye_id)
                    await context.bot.send_message(chat_id=chat_id, text=f"_{bye_name} получает бай._")

            state["round_matches"] = next_matches

        await update_tournament_message(context, chat_id)
        return

    logger.warning(f"Неопределенное состояние для {user_id}")
    await query.answer("Что-то пошло не так.", show_alert=True)


async def set_tournament_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        if query:
            await query.answer("Ошибка.", show_alert=True)
        return

    user_id = query.from_user.id
    chat_id = query.message.chat_id
    await query.answer()

    state = _get_state(chat_id)

    if user_id != state.get("admin_user_id"):
        await query.edit_message_text("Только админ может выбрать режим.", reply_markup=None)
        return

    if state["tournament_mode"] is not None:
        await query.edit_message_text(f"Режим уже выбран: {state['tournament_mode']}.", reply_markup=None)
        return

    if state["registration_open"]:
        await query.edit_message_text("Сначала заверши набор.", reply_markup=None)
        return

    try:
        _, mode = query.data.split(":")
    except ValueError:
        await query.edit_message_text("Ошибка. Попробуй снова.", reply_markup=None)
        return

    state["tournament_mode"] = mode
    state["current_round"] = 1
    round_num = state["current_round"]

    active_ids = [uid for uid, data in state["players"].items() if not data.get("is_eliminated")]

    if len(active_ids) < 2:
        await query.edit_message_text("Мало игроков.", reply_markup=None)
        await end_tournament(context, chat_id)
        return

    state["active_players_in_round"] = set(active_ids)
    state["player_rolls_in_round"].clear()
    state["player_with_bye"] = None
    state["round_matches"] = []

    if mode == "pair_match":
        pairs, bye_id = _make_pairs(list(active_ids))
        state["round_matches"] = pairs
        if bye_id is not None:
            state["player_with_bye"] = bye_id
            state["active_players_in_round"].discard(bye_id)
            bye_name = state["players"].get(bye_id, {}).get("username", "Игрок")
            await context.bot.send_message(chat_id=chat_id, text=f"_{bye_name} получает бай и проходит автоматически._")

    await update_tournament_message(context, chat_id)


async def make_tournament_roll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        if query:
            await query.answer("Ошибка.", show_alert=True)
        return

    user = query.from_user
    chat_id = query.message.chat_id
    state = _get_state(chat_id)

    if not state["active"] or state["registration_open"]:
        await query.answer("Турнир не начат или регистрация открыта.", show_alert=True)
        return

    if user.id not in state["players"] or state["players"][user.id].get("is_eliminated"):
        await query.answer(f"{user.first_name}, ты не в этом раунде.", show_alert=True)
        return

    if user.id not in state["active_players_in_round"]:
        if user.id in state["player_rolls_in_round"]:
            await query.answer(f"{user.first_name}, ты уже бросил: {state['player_rolls_in_round'][user.id]}.", show_alert=True)
        else:
            await query.answer(f"{user.first_name}, сейчас не твоя очередь.", show_alert=True)
        return

    now = time.time()
    last_roll = last_roll_times.get(user.id, 0)
    if now - last_roll < ROLL_COOLDOWN:
        remaining = round(ROLL_COOLDOWN - (now - last_roll), 1)
        await query.answer(f"Подожди {remaining}с.", show_alert=True)
        return
    last_roll_times[user.id] = now

    await query.answer("Бросаем кубик...")

    try:
        dice_message = await context.bot.send_dice(chat_id=chat_id, emoji=DiceEmoji.DICE)
        dice_value = dice_message.dice.value

        state["player_rolls_in_round"][user.id] = dice_value
        state["active_players_in_round"].discard(user.id)

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"_{user.first_name} бросил: **{dice_value}**!_",
            reply_to_message_id=dice_message.message_id,
            parse_mode="Markdown"
        )

        await update_tournament_message(context, chat_id)

        if not state["active_players_in_round"]:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Все бросили в раунде {state['current_round']}. Подсчитываю..."
            )
            await asyncio.sleep(2)
            await announce_round_results(context, chat_id)

    except Exception as e:
        logger.error(f"Ошибка броска для {user.first_name}: {e}")
        try:
            await query.answer("Ошибка при броске. Попробуй снова.", show_alert=True)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=f"{user.first_name}, ошибка при броске. Нажми кнопку снова.")


async def stop_tournament_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    state = _get_state(chat_id)

    if not state["active"]:
        await update.message.reply_text("Нет активного турнира.")
        return

    if user_id != state.get("admin_user_id"):
        await update.message.reply_text("Только админ может остановить турнир.")
        return

    await end_tournament(context, chat_id, winner_data=None)
    await update.message.reply_text("Турнир остановлен.")
