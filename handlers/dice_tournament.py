import logging
import asyncio
import random
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackContext
from telegram.constants import DiceEmoji

logger = logging.getLogger(__name__)

# Состояние турнира
dice_tournament_state = {
    "active": False,
    "registration_open": False,
    "players": {},  # {user_id: {"user_id": int, "username": str, "current_roll": int, "total_score": int, "rounds_won": int, "is_eliminated": bool, "eliminated_round": int}}
    "current_round": 0,
    "round_matches": [],  # Список кортежей (p1_id, p2_id)
    "tournament_mode": None,  # "pair_match" или "all_vs_all"
    "tournament_message_id": None,
    "tournament_chat_id": None,
    "active_players_in_round": set(),  # Игроки, которые должны бросить кубик
    "player_rolls_in_round": {},  # {user_id: roll_value} для текущего раунда
    "admin_user_id": None,
    "player_with_bye": None,  # Игрок с пропуском раунда
}

def get_tournament_status_message() -> str:
    """Формирует сообщение о текущем статусе турнира."""
    if not dice_tournament_state["active"]:
        return "🎲 Турнир на кубиках не активен."

    state = dice_tournament_state
    message_lines = [f"🎲 **Турнир на Кубиках - Раунд {state['current_round']}** 🎲\n"]

    if state["registration_open"]:
        message_lines.append("⚡️ **Регистрация открыта!** Нажмите 'Участвовать', чтобы присоединиться.")
        if state["players"]:
            message_lines.append("\nУже зарегистрированы:")
            for user_id, data in state["players"].items():
                message_lines.append(f" - {data['username']} (Очки: {data.get('total_score', 0)})")
        else:
            message_lines.append("\nПока нет зарегистрированных игроков.")
        
        if len(state["players"]) >= 2:
            message_lines.append("\n\n_Администратор может завершить набор и начать турнир, нажав соответствующую кнопку._")
        else:
            message_lines.append("\n\n_Нужно как минимум 2 игрока для начала турнира._")

    else: # Registration is closed
        mode_text = "Не выбран"
        if state['tournament_mode'] == 'pair_match':
            mode_text = "Парные поединки (1 на 1)"
        elif state['tournament_mode'] == 'all_vs_all':
            mode_text = "Каждый против каждого"
        message_lines.append(f"🏆 **Режим:** {mode_text}")

        active_players_list = []
        eliminated_players_list = []

        for user_id, data in state["players"].items():
            roll_info = ""
            if not state["registration_open"] and state["current_round"] > 0:
                if user_id in state["player_rolls_in_round"]:
                    roll_info = f", бросок: {state['player_rolls_in_round'][user_id]}"
                elif user_id in state["active_players_in_round"]:
                    roll_info = ", ожидает броска"
                elif data.get("is_eliminated"):
                    roll_info = ", выбыл"
                else:
                    if user_id == state.get("player_with_bye"):
                        roll_info = ", проходит без игры (бай)"
                    # else: # No specific status needed if just active and waiting for round to start
                        # roll_info = "" 

            if data.get("is_eliminated"):
                eliminated_players_list.append(f"❌ {data['username']} (выбыл в раунде {data.get('eliminated_round', 'N/A')})")
            else:
                active_players_list.append(f"🟢 {data['username']} (Очки: {data.get('total_score', 0)}{roll_info})")
        
        if active_players_list:
            message_lines.append("\n**Активные игроки:**")
            message_lines.extend(active_players_list)
        else:
            if state["current_round"] > 0 and not any(not p_data.get("is_eliminated") for p_data in state["players"].values()):
                message_lines.append("\n**Все игроки выбыли. Турнир завершен.**")
            elif state["current_round"] == 0: # Should not happen if registration is closed and round 0
                message_lines.append("\n**Ожидание начала первого раунда...**")
            else:
                message_lines.append("\n**Активных игроков нет.**")


        if eliminated_players_list:
            message_lines.append("\n**Выбывшие игроки:**")
            message_lines.extend(eliminated_players_list)

        if state["current_round"] > 0 and not state["registration_open"]:
            if state["tournament_mode"] == "pair_match" and state["round_matches"]:
                message_lines.append("\n**Текущие матчи:**")
                for p1_id, p2_id in state["round_matches"]:
                    p1_data = state["players"].get(p1_id, {})
                    p2_data = state["players"].get(p2_id, {})
                    p1_name = p1_data.get("username", "Игрок1")
                    p2_name = p2_data.get("username", "Игрок2")
                    p1_roll = state["player_rolls_in_round"].get(p1_id, "?")
                    p2_roll = state["player_rolls_in_round"].get(p2_id, "?")
                    match_status = f" - {p1_name} ({p1_roll}) vs {p2_name} ({p2_roll})"
                    
                    if p1_id in state["player_rolls_in_round"] and p2_id in state["player_rolls_in_round"]:
                        if p1_roll > p2_roll:
                            match_status += f" → Победитель: {p1_name}"
                        elif p2_roll > p1_roll:
                            match_status += f" → Победитель: {p2_name}"
                        else:
                            match_status += " → Ничья! Переигровка"
                    
                    message_lines.append(match_status)
            
            if state.get("player_with_bye"):
                player_bye_name = state["players"].get(state["player_with_bye"], {}).get("username", "Игрок")
                message_lines.append(f"\n{player_bye_name} проходит в следующий раунд без игры (бай).")

            if state["active_players_in_round"]:
                waiting_for_roll_users = [
                    state["players"][pid]["username"] 
                    for pid in state["active_players_in_round"] 
                    if pid in state["players"] # Ensure player data exists
                ]
                if waiting_for_roll_users:
                    message_lines.append(f"\nОжидаем бросок от: {', '.join(waiting_for_roll_users)}")
            elif not state["active_players_in_round"] and any(not p_data.get("is_eliminated") for p_data in state["players"].values()):
                 # All players have rolled, or it's the start of a new round after results
                 # Check if more than 1 player remains to offer "Start Next Round"
                remaining_active_players = sum(1 for p_data in state["players"].values() if not p_data.get("is_eliminated"))
                if remaining_active_players > 1:
                     message_lines.append("\n\n_Администратор может начать следующий раунд._")
                elif remaining_active_players == 1:
                    winner_name = [p['username'] for p in state["players"].values() if not p.get("is_eliminated")][0]
                    message_lines.append(f"\n\n**Турнир близится к завершению! Победитель: {winner_name}**")


    return "\n".join(message_lines)

async def start_tournament_from_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускает турнир из меню /start"""
    query = update.callback_query
    if not query: # Should not happen if called from button
        if update.message: # If somehow called as a command by mistake
             await start_dice_tournament_registration(update, context)
        return
        
    await query.answer()
    # We need to pass the original message context if `start_dice_tournament_registration` expects it
    # For callback queries, update.callback_query.message is the message the button is attached to.
    await start_dice_tournament_registration(update, context) # Pass the whole update
    
async def update_tournament_message(context: ContextTypes.DEFAULT_TYPE, chat_id_override: int = None, message_id_override: int = None):
    """Обновляет сообщение с турнирной таблицей."""
    state = dice_tournament_state
    chat_id = chat_id_override if chat_id_override else state.get("tournament_chat_id")
    message_id = message_id_override if message_id_override else state.get("tournament_message_id")

    if not chat_id or not message_id:
        logger.warning("Нет ID чата или сообщения для обновления турнирной таблицы.")
        return

    try:
        current_text = get_tournament_status_message()
        reply_markup = None
        buttons = []

        if state["registration_open"]:
            buttons.append([InlineKeyboardButton("Участвовать 🚀", callback_data="register_for_tournament")])
            # Кнопка для администратора для завершения регистрации
            # Эту кнопку должен видеть только администратор, но проверка user_id здесь затруднительна.
            # Предполагаем, что обработчик callback_data="end_registration_and_start_round" проверит права администратора.
            if len(state["players"]) >= 2:
                buttons.append([InlineKeyboardButton("Завершить набор и выбрать режим ⚙️", callback_data="end_registration_and_start_round")])
            reply_markup = InlineKeyboardMarkup(buttons)
        
        elif state["active"] and not state["registration_open"]: # Регистрация закрыта, турнир идет
            if state["active_players_in_round"]: # Если есть игроки, которые должны бросить кубик
                buttons.append([InlineKeyboardButton("Бросить кубик 🎲", callback_data="make_tournament_roll")])
                reply_markup = InlineKeyboardMarkup(buttons)
            else: # Все бросили кубики в текущем раунде или раунд еще не начался (после выбора режима)
                active_players_count = sum(1 for p_data in state["players"].values() if not p_data.get("is_eliminated"))
                if active_players_count > 1 and state["current_round"] > 0 : # Если есть активные игроки и это не первый раунд (где режим только что выбран)
                    # Кнопка для администратора для начала следующего раунда
                    buttons.append([InlineKeyboardButton("Начать следующий раунд ➡️", callback_data="end_registration_and_start_round")])
                    reply_markup = InlineKeyboardMarkup(buttons)
                elif active_players_count <=1 and state["current_round"] > 0:
                    # Турнир завершен или есть победитель, кнопки не нужны, будет сообщение о завершении
                    pass
                # Если current_round == 0, это значит, что режим только что выбран, и ожидается начало первого раунда.
                # end_registration_and_start_round должен был уже запустить первый раунд.
                # Если active_players_in_round пусто, но current_round > 0, значит все бросили.
                # announce_round_results должен был быть вызван.

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=current_text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        logger.info(f"Сообщение турнирной таблицы ({message_id}) в чате ({chat_id}) обновлено.")
    except Exception as e:
        logger.error(f"Ошибка при обновлении сообщения турнирной таблицы: {e}", exc_info=True)
        if "Message to edit not found" in str(e) or "message is not modified" in str(e).lower():
            state["tournament_message_id"] = None # Сбрасываем ID, чтобы его можно было пересоздать
            logger.warning(f"Сообщение турнирной таблицы не найдено или не изменено, ID сброшен. Ошибка: {e}")
            # Попытка отправить новое сообщение, если турнир все еще активен
            if state["active"] and chat_id: # Убедимся, что chat_id есть
                logger.info("Попытка отправить новое сообщение о статусе турнира, так как старое не найдено/не изменено.")
                try:
                    # Генерируем разметку заново для нового сообщения
                    new_reply_markup = None
                    new_buttons = []
                    if state["registration_open"]:
                        new_buttons.append([InlineKeyboardButton("Участвовать 🚀", callback_data="register_for_tournament")])
                        if len(state["players"]) >= 2:
                             new_buttons.append([InlineKeyboardButton("Завершить набор и выбрать режим ⚙️", callback_data="end_registration_and_start_round")])
                        new_reply_markup = InlineKeyboardMarkup(new_buttons)
                    elif state["active"] and not state["registration_open"] and state["active_players_in_round"]:
                         new_buttons.append([InlineKeyboardButton("Бросить кубик 🎲", callback_data="make_tournament_roll")])
                         new_reply_markup = InlineKeyboardMarkup(new_buttons)
                    # ... (добавить другие состояния для кнопок при необходимости)

                    sent_message = await context.bot.send_message(
                        chat_id=chat_id,
                        text=get_tournament_status_message(), # Получаем свежий текст статуса
                        parse_mode="Markdown",
                        reply_markup=new_reply_markup # Используем свежую разметку
                    )
                    state["tournament_message_id"] = sent_message.message_id
                    state["tournament_chat_id"] = sent_message.chat_id # На всякий случай обновляем chat_id
                    logger.info(f"Отправлено новое сообщение о статусе турнира, ID: {sent_message.message_id}")
                except Exception as send_e:
                    logger.error(f"Не удалось отправить новое сообщение о статусе турнира: {send_e}", exc_info=True)


async def announce_round_results(context: ContextTypes.DEFAULT_TYPE):
    """Анонсирует результаты текущего раунда, определяет выбывших и обновляет очки."""
    state = dice_tournament_state
    chat_id = state.get("tournament_chat_id")
    if not chat_id:
        logger.error("announce_round_results: chat_id не найден.")
        return

    round_num = state["current_round"]
    message_lines = [f"--- **Результаты Раунда {round_num}** ---\n"]
    eliminated_this_round_ids = set()

    if state["tournament_mode"] == "pair_match":
        rematch_matches = []
        
        for p1_id, p2_id in state["round_matches"]:
            p1_data = state["players"].get(p1_id)
            p2_data = state["players"].get(p2_id)

            if not p1_data or not p2_data:
                logger.warning(f"Данные игрока не найдены для матча {p1_id} vs {p2_id} в раунде {round_num}")
                continue
            
            if p1_data.get("is_eliminated") or p2_data.get("is_eliminated"):
                # Этот матч не должен был состояться, если один из игроков уже выбыл до начала матча.
                # Но если выбывание произошло в этом же раунде из-за другого матча (невозможно в pair_match),
                # или если это "бай" игрок, то пропускаем.
                logger.info(f"Один из игроков в матче {p1_data.get('username')} vs {p2_data.get('username')} уже выбыл или не участвовал.")
                continue

            p1_roll = state["player_rolls_in_round"].get(p1_id) # Не должно быть 0, если игрок бросил
            p2_roll = state["player_rolls_in_round"].get(p2_id)

            # Проверяем, что оба игрока сделали бросок. Если нет, это ошибка в логике.
            if p1_roll is None or p2_roll is None:
                logger.error(f"Ошибка: не все броски сделаны для матча {p1_data.get('username')} ({p1_roll}) vs {p2_data.get('username')} ({p2_roll})")
                # Можно добавить логику обработки такой ситуации, например, засчитать техническое поражение
                # или попросить игроков перебросить. Пока просто логируем.
                message_lines.append(f"⚠️ Ошибка обработки матча: {p1_data.get('username')} vs {p2_data.get('username')} - не все броски засчитаны.")
                continue


            if p1_roll == p2_roll:
                rematch_matches.append((p1_id, p2_id))
                message_lines.append(
                    f"⚔️ {p1_data['username']} ({p1_roll}) vs {p2_data['username']} ({p2_roll}) -> "
                    f"Ничья! Переигровка в следующем раунде."
                )
            elif p1_roll > p2_roll:
                state["players"][p1_id]["total_score"] += 1
                state["players"][p2_id]["is_eliminated"] = True
                state["players"][p2_id]["eliminated_round"] = round_num
                eliminated_this_round_ids.add(p2_id)
                message_lines.append(
                    f"⚔️ {p1_data['username']} ({p1_roll}) vs {p2_data['username']} ({p2_roll}) -> "
                    f"Победитель: **{p1_data['username']}**! "
                    f"{p2_data['username']} выбывает."
                )
            else: # p2_roll > p1_roll
                state["players"][p2_id]["total_score"] += 1
                state["players"][p1_id]["is_eliminated"] = True
                state["players"][p1_id]["eliminated_round"] = round_num
                eliminated_this_round_ids.add(p1_id)
                message_lines.append(
                    f"⚔️ {p1_data['username']} ({p1_roll}) vs {p2_data['username']} ({p2_roll}) -> "
                    f"Победитель: **{p2_data['username']}**! "
                    f"{p1_data['username']} выбывает."
                )
        
        state["round_matches"] = rematch_matches # Обновляем список матчей (только для переигровок)

    elif state["tournament_mode"] == "all_vs_all":
        active_round_players = [
            data for uid, data in state["players"].items() 
            if not data.get("is_eliminated") and uid in state["player_rolls_in_round"]
        ]
        
        if not active_round_players:
            message_lines.append("Нет игроков, сделавших бросок в этом раунде, или все выбыли.")
        else:
            active_round_players.sort(key=lambda x: state["player_rolls_in_round"].get(x["user_id"], 0))
            
            message_lines.append("\n**Результаты бросков (Каждый против каждого):**")
            for p_data in active_round_players:
                roll = state["player_rolls_in_round"].get(p_data["user_id"], "?")
                message_lines.append(f" - {p_data['username']}: {roll}")
                # В режиме "все против всех" очки могут начисляться по-другому, например, за сам бросок или за позицию.
                # Текущая логика просто +1 очко за участие в раунде. Это можно изменить.
                state["players"][p_data["user_id"]]["total_score"] += 1 

            # Логика выбывания для "all_vs_all"
            if len(active_round_players) > 2: # Выбывание имеет смысл, если игроков больше 2
                num_to_eliminate = max(1, len(active_round_players) // 4) # Выбывает 25%, минимум 1
                
                # Убедимся, что не выбывают все, если остался только один победитель
                if len(active_round_players) - num_to_eliminate < 1 and len(active_round_players) > 1:
                    num_to_eliminate = len(active_round_players) - 1


                eliminated_candidates = active_round_players[:num_to_eliminate]
                
                eliminated_names_this_round = []
                for p_data_to_elim in eliminated_candidates:
                    # Дополнительная проверка, чтобы не выбить уже выбывшего (хотя active_round_players должны быть не выбывшими)
                    if not state["players"][p_data_to_elim["user_id"]].get("is_eliminated"):
                        state["players"][p_data_to_elim["user_id"]]["is_eliminated"] = True
                        state["players"][p_data_to_elim["user_id"]]["eliminated_round"] = round_num
                        eliminated_this_round_ids.add(p_data_to_elim["user_id"])
                        eliminated_names_this_round.append(p_data_to_elim["username"])
                
                if eliminated_names_this_round:
                    message_lines.append(f"\n🚫 Выбывают: {', '.join(eliminated_names_this_round)}")
                elif len(active_round_players) > 1 : # Если никого не выбили, но игроки есть
                    message_lines.append("\nВ этом раунде никто не выбывает (по результатам бросков).")
            elif len(active_round_players) <=2 and len(active_round_players) > 0 :
                 message_lines.append("\nВ этом раунде никто не выбывает (мало игроков).")


    await context.bot.send_message(chat_id=chat_id, text="\n".join(message_lines), parse_mode="Markdown")
    
    state["player_rolls_in_round"].clear()
    state["active_players_in_round"].clear()
    # player_with_bye сбрасывается при начале нового раунда, здесь его трогать не нужно,
    # так как он мог повлиять на формирование матчей текущего раунда.
    # state["player_with_bye"] = None # Сбрасывается в end_registration_and_start_round

    await update_tournament_message(context) # Обновляем основное сообщение турнира

    remaining_players = [
        p_data for p_data in state["players"].values() if not p_data.get("is_eliminated")
    ]

    if len(remaining_players) <= 1:
        await end_tournament(context, remaining_players[0] if remaining_players else None)
    else:
        # Кнопка "Начать следующий раунд" будет показана через update_tournament_message,
        # если выполнены условия (админ, все бросили, есть активные игроки).
        # Дополнительное сообщение админу не обязательно, если основное сообщение обновляется корректно.
        # admin_id = state.get("admin_user_id")
        # if admin_id:
        #     admin_name = state["players"].get(admin_id, {}).get("username", "Администратор")
        #     # Это сообщение может быть избыточным, если update_tournament_message правильно показывает кнопку
        #     await context.bot.send_message(
        #         chat_id=chat_id,
        #         text=f"Раунд {round_num} завершен. {admin_name}, если готовы, начинайте следующий раунд через основное сообщение турнира."
        #     )
        pass


async def end_tournament(context: ContextTypes.DEFAULT_TYPE, winner_data: dict = None):
    """Завершает турнир и объявляет победителя."""
    state = dice_tournament_state
    chat_id = state.get("tournament_chat_id")
    message_id = state.get("tournament_message_id") # Получаем ID сообщения для возможного редактирования

    if not chat_id:
        logger.error("end_tournament: chat_id не найден.")
        return

    message = "**🎉 Турнир на Кубиках ЗАВЕРШЕН! 🎉**\n\n"
    
    final_winner = None
    if winner_data and not winner_data.get("is_eliminated"):
        final_winner = winner_data
    else:
        active_players = [p_data for p_data in state["players"].values() if not p_data.get("is_eliminated")]
        if len(active_players) == 1:
            final_winner = active_players[0]
        elif not active_players and state["players"]: # Все выбыли, но игроки были
             # Попробуем найти последнего невыбывшего по очкам или раунду выбывания
            sorted_by_score_then_round = sorted(
                state["players"].values(), 
                key=lambda p: (p.get("total_score", 0), -p.get("eliminated_round", float('inf'))), 
                reverse=True
            )
            if sorted_by_score_then_round:
                # Если все выбыли, но есть история, можно объявить победителя по очкам среди всех
                 message += "Все игроки выбыли. Победитель определяется по наибольшему количеству очков:\n"
                 final_winner = sorted_by_score_then_round[0]


    if final_winner:
        message += f"🌟 **Победитель:** {final_winner['username']} с {final_winner.get('total_score', 0)} очками!"
    elif not state["players"]: # Если вообще не было игроков
        message += "Турнир завершен, но не было зарегистрировано ни одного игрока."
    else: # Если есть игроки, но победитель не ясен (например, несколько невыбывших с одинаковыми очками - маловероятно с текущей логикой)
        message += "Турнир завершен. Победитель не определен однозначно или все игроки выбыли."
        # Можно добавить вывод таблицы лидеров
        if state["players"]:
            message += "\n\n**Итоговая таблица:**\n"
            sorted_players = sorted(state["players"].values(), key=lambda p: p.get("total_score", 0), reverse=True)
            for p_data in sorted_players:
                status = "выбыл" if p_data.get("is_eliminated") else "активен"
                message += f"- {p_data['username']}: {p_data.get('total_score',0)} ({status})\n"


    # Пытаемся отредактировать исходное сообщение турнира, чтобы убрать кнопки
    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=message, # Итоговое сообщение
                parse_mode="Markdown",
                reply_markup=None # Убираем все кнопки
            )
            logger.info(f"Сообщение турнира ({message_id}) обновлено с результатами завершения.")
        except Exception as e:
            logger.warning(f"Не удалось отредактировать сообщение турнира при завершении: {e}. Отправка нового сообщения.")
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")
    else: # Если ID сообщения нет, просто отправляем новое
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")


    logger.info(f"Турнир в чате {chat_id} завершен. Сброс состояния.")
    # Сброс состояния турнира
    dice_tournament_state.clear()
    dice_tournament_state.update({
        "active": False, "registration_open": False, "players": {}, "current_round": 0,
        "round_matches": [], "tournament_mode": None, "tournament_message_id": None,
        "tournament_chat_id": None, "active_players_in_round": set(),
        "player_rolls_in_round": {}, "admin_user_id": None, "player_with_bye": None,
    })

# --- Обработчики команд и колбэков ---

async def start_dice_tournament_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускает регистрацию на турнир."""
    chat_id = None
    user_id = None
    message_to_reply = None # Сообщение, на которое будем отвечать или которое будем редактировать

    if update.message: # Если это команда /start_tournament
        chat_id = update.message.chat_id
        user_id = update.message.from_user.id
        message_to_reply = update.message # Будем отвечать на команду
    elif update.callback_query and update.callback_query.message: # Если это callback от кнопки /start
        # Это callback от кнопки "Начать турнир" из /start
        query = update.callback_query
        await query.answer() # Важно ответить на callback
        chat_id = query.message.chat_id
        user_id = query.from_user.id
        message_to_reply = query.message # Будем редактировать сообщение с кнопкой /start или отправлять новое
    else:
        logger.warning("start_dice_tournament_registration вызван без message или callback_query.")
        return

    if dice_tournament_state["active"]:
        existing_admin = dice_tournament_state.get("admin_user_id")
        reply_text = f"Турнир уже активен (администратор: ID {existing_admin}). Сначала завершите текущий турнир (/stop_tournament)."
        if message_to_reply:
            await message_to_reply.reply_text(reply_text)
        elif chat_id: # Если нет message_to_reply, но есть chat_id
             await context.bot.send_message(chat_id=chat_id, text=reply_text)
        return

    # Сброс состояния перед началом нового турнира
    dice_tournament_state.clear()
    dice_tournament_state.update({
        "active": True, "registration_open": True, "players": {}, "current_round": 0,
        "round_matches": [], "tournament_mode": None, "tournament_message_id": None,
        "tournament_chat_id": chat_id, "active_players_in_round": set(),
        "player_rolls_in_round": {}, "admin_user_id": user_id, "player_with_bye": None,
    })

    status_message_text = get_tournament_status_message()
    
    # Кнопки для начального сообщения
    initial_buttons = [[InlineKeyboardButton("Участвовать 🚀", callback_data="register_for_tournament")]]
    # Кнопка "Завершить набор" появится, когда будет >= 2 игроков, через update_tournament_message

    initial_reply_markup = InlineKeyboardMarkup(initial_buttons)
    
    try:
        if update.callback_query: # Если пришли из callback от /start
            # Редактируем сообщение, на котором была кнопка "Начать турнир"
            # или отправляем новое, если редактирование невозможно
            try:
                await message_to_reply.edit_text( # message_to_reply это query.message
                    text=status_message_text,
                    reply_markup=initial_reply_markup,
                    parse_mode="Markdown"
                )
                dice_tournament_state["tournament_message_id"] = message_to_reply.message_id
            except Exception as edit_err:
                logger.warning(f"Не удалось отредактировать сообщение для начала турнира ({edit_err}), отправка нового.")
                sent_message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=status_message_text,
                    reply_markup=initial_reply_markup,
                    parse_mode="Markdown"
                )
                dice_tournament_state["tournament_message_id"] = sent_message.message_id
        else: # Если это команда /start_tournament
            sent_message = await message_to_reply.reply_text(
                text=status_message_text,
                reply_markup=initial_reply_markup,
                parse_mode="Markdown"
            )
            dice_tournament_state["tournament_message_id"] = sent_message.message_id
        
        logger.info(f"Регистрация на турнир начата в чате {chat_id} администратором {user_id}. ID сообщения: {dice_tournament_state['tournament_message_id']}")

    except Exception as e:
        logger.error(f"Ошибка при отправке начального сообщения турнира: {e}", exc_info=True)
        # Попытка уведомить пользователя об ошибке, если возможно
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="Не удалось начать турнир. Попробуйте позже.")


async def register_for_tournament(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Регистрирует игрока в турнире."""
    query = update.callback_query
    if not query or not query.from_user or not query.message: # Добавлена проверка query.message
        logger.warning("register_for_tournament: нет query, query.from_user или query.message")
        if query: await query.answer("Ошибка: не удалось обработать ваш запрос.")
        return
        
    user = query.from_user
    
    state = dice_tournament_state
    if not state["active"]:
        await query.answer("Турнир не активен.", show_alert=True)
        # Попытка обновить сообщение, если оно еще существует
        if state.get("tournament_message_id") and state.get("tournament_chat_id"):
            try:
                await query.message.edit_text("Турнир не активен.", reply_markup=None)
            except Exception: pass # Игнорируем ошибку редактирования, если сообщение уже удалено/изменено
        return
    if not state["registration_open"]:
        await query.answer("Регистрация на турнир уже завершена.", show_alert=True)
        return

    if user.id in state["players"]:
        await query.answer(f"{user.first_name}, вы уже зарегистрированы!", show_alert=False)
    else:
        state["players"][user.id] = {
            "user_id": user.id,
            "username": user.first_name or f"User_{user.id}", # Используем first_name
            "current_roll": 0,
            "total_score": 0,
            "rounds_won": 0,
            "is_eliminated": False,
            "eliminated_round": 0,
        }
        await query.answer(f"Вы зарегистрированы, {user.first_name}!", show_alert=False)
        logger.info(f"Пользователь {user.first_name} ({user.id}) зарегистрировался на турнир.")
        await update_tournament_message(context, chat_id_override=query.message.chat_id, message_id_override=query.message.message_id)


async def end_registration_and_start_round(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Завершает регистрацию (если она открыта) и предлагает выбрать режим.
    Или начинает новый раунд турнира (если регистрация закрыта и предыдущий раунд завершен).
    """
    query = update.callback_query
    user_id = None
    message_to_interact_with = None # Сообщение, которое будем редактировать

    if query and query.from_user and query.message:
        await query.answer() # Отвечаем на callback как можно раньше
        user_id = query.from_user.id
        message_to_interact_with = query.message
    # Эта функция сейчас вызывается только через callback, команда /end_registration... не предусмотрена
    # elif update.message and update.message.from_user:
    #     user_id = update.message.from_user.id
    #     message_to_interact_with = update.message
    else:
        logger.warning("end_registration_and_start_round: нет query, query.from_user или query.message.")
        if query: await query.answer("Ошибка обработки команды.", show_alert=True)
        return
    
    state = dice_tournament_state
    if user_id != state.get("admin_user_id"):
        await query.answer("Только администратор турнира может выполнять это действие.", show_alert=True)
        return
    
    if not state["active"]:
        msg_text = "Турнир не активен. Начните новый с /start_tournament или через меню /start."
        try:
            await message_to_interact_with.edit_text(msg_text, reply_markup=None)
        except Exception: # Если редактирование не удалось
            await context.bot.send_message(chat_id=message_to_interact_with.chat_id, text=msg_text)
        return

    # --- Сценарий 1: Завершение регистрации и выбор режима ---
    if state["registration_open"]:
        if len(state["players"]) < 2:
            await query.answer("Недостаточно игроков для начала турнира (нужно минимум 2).", show_alert=True)
            # Сообщение не меняем, чтобы кнопка "Участвовать" осталась
            return

        state["registration_open"] = False # Закрываем регистрацию
        logger.info(f"Регистрация завершена администратором {user_id}.")
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Парные поединки (1 на 1)", callback_data="set_tournament_mode:pair_match")],
            [InlineKeyboardButton("Каждый против каждого", callback_data="set_tournament_mode:all_vs_all")]
        ])
        msg_to_send = "Регистрация завершена! ✅\n\nТеперь выберите режим турнира:"
        try:
            await message_to_interact_with.edit_text(text=msg_to_send, reply_markup=keyboard, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка при редактировании сообщения для выбора режима: {e}")
            # Если не удалось отредактировать, отправляем новое, но это может запутать пользователя
            await context.bot.send_message(chat_id=message_to_interact_with.chat_id, text=msg_to_send, reply_markup=keyboard, parse_mode="Markdown")
        return 

    # --- Сценарий 2: Начало следующего раунда (регистрация уже закрыта) ---
    # Этот блок вызывается, когда админ нажимает "Начать следующий раунд"
    if not state["registration_open"] and state["current_round"] > 0:
        if state["active_players_in_round"]: # Проверка, все ли бросили кубики
            await query.answer("Не все игроки сделали бросок в текущем раунде. Дождитесь завершения.", show_alert=True)
            return

        # Все бросили, можно начинать следующий раунд
        state["current_round"] += 1
        round_num = state["current_round"]
        logger.info(f"Администратор {user_id} начинает раунд {round_num}.")

        current_active_player_ids = [
            pid for pid, p_data in state["players"].items() if not p_data.get("is_eliminated")
        ]
        
        if len(current_active_player_ids) <= 1: # Если остался 1 или 0 игроков
            await end_tournament(context, state["players"].get(current_active_player_ids[0]) if current_active_player_ids else None)
            return

        state["active_players_in_round"] = set(current_active_player_ids) # Устанавливаем, кто должен бросить
        state["player_rolls_in_round"].clear() # Очищаем броски предыдущего раунда
        state["player_with_bye"] = None # Сбрасываем "бай" игрока

        if state["tournament_mode"] == "pair_match":
            # Формируем пары, включая переигровочные матчи, если они есть
            next_round_matches = list(state.get("round_matches", [])) # Берем матчи для переигровки
            
            # Игроки, не участвующие в переигровках и не выбывшие
            players_for_new_pairs = [
                pid for pid in current_active_player_ids 
                if not any(pid in match for match in next_round_matches)
            ]
            random.shuffle(players_for_new_pairs)

            if len(players_for_new_pairs) % 2 != 0:
                state["player_with_bye"] = players_for_new_pairs.pop()
                bye_player_name = state["players"].get(state["player_with_bye"], {}).get("username", "Игрок")
                await context.bot.send_message(
                    chat_id=message_to_interact_with.chat_id, # Отправляем в чат турнира
                    text=f"ℹ️ В раунде {round_num} игрок {bye_player_name} получает 'бай' и проходит дальше автоматически."
                )
                logger.info(f"Игрок {state['player_with_bye']} ({bye_player_name}) получает 'бай' в раунде {round_num}.")
            
            for i in range(0, len(players_for_new_pairs), 2):
                if i + 1 < len(players_for_new_pairs):
                    next_round_matches.append((players_for_new_pairs[i], players_for_new_pairs[i+1]))
            
            state["round_matches"] = next_round_matches
            logger.info(f"Сформированы пары для раунда {round_num}: {state['round_matches']}")
        
        # Обновляем основное сообщение турнира, чтобы показать новые пары/статус и кнопку "Бросить кубик"
        await update_tournament_message(context, chat_id_override=message_to_interact_with.chat_id, message_id_override=message_to_interact_with.message_id)
        
        # Дополнительно можно отправить сообщение о начале раунда, если update_tournament_message не достаточно информативен
        # round_start_msg = f"🚀 **Начинается Раунд {round_num}!**\n"
        # if state["tournament_mode"] == "pair_match":
        #     round_start_msg += "Пары определены (см. выше). "
        # round_start_msg += "Активные игроки, пожалуйста, бросьте кубик!"
        # await context.bot.send_message(chat_id=message_to_interact_with.chat_id, text=round_start_msg, parse_mode="Markdown")

        return

    # Если ни один из сценариев не подошел (маловероятно при правильной логике кнопок)
    logger.warning(f"end_registration_and_start_round: Неопределенное состояние турнира для пользователя {user_id}. State: {state}")
    await query.answer("Не удалось определить действие. Проверьте состояние турнира.", show_alert=True)


async def set_tournament_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Устанавливает режим турнира и начинает первый раунд."""
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        logger.warning("set_tournament_mode: нет query, query.from_user или query.message")
        if query: await query.answer("Ошибка обработки.", show_alert=True)
        return
        
    user_id = query.from_user.id
    await query.answer() # Отвечаем на callback
    
    state = dice_tournament_state
    if user_id != state.get("admin_user_id"):
        await query.edit_message_text("Только администратор, запустивший турнир, может выбрать режим.", reply_markup=None)
        return
    
    if state["tournament_mode"] is not None: # Режим уже установлен
        await query.edit_message_text(f"Режим турнира уже установлен: {state['tournament_mode']}. Чтобы изменить, перезапустите турнир.", reply_markup=None)
        return
    
    if state["registration_open"]: # Регистрация еще не закрыта
        await query.edit_message_text("Сначала завершите регистрацию игроков.", reply_markup=None)
        return

    try:
        _, mode = query.data.split(":")
    except ValueError:
        logger.error(f"Ошибка разбора callback_data для set_tournament_mode: {query.data}")
        await query.edit_message_text("Ошибка выбора режима. Попробуйте снова.", reply_markup=None)
        return

    state["tournament_mode"] = mode
    state["current_round"] = 1 # Это первый раунд
    round_num = state["current_round"]
    logger.info(f"Администратор {user_id} выбрал режим турнира: {mode}. Начинается раунд {round_num}.")

    active_player_ids = [
        uid for uid, data in state["players"].items() if not data.get("is_eliminated")
    ]
    
    if not active_player_ids or len(active_player_ids) < 2: # Проверка на случай, если все игроки "выбыли" до начала
        await query.edit_message_text("Нет достаточного количества активных игроков для начала турнира.", reply_markup=None)
        await end_tournament(context) # Завершаем турнир, так как играть некому
        return

    state["active_players_in_round"] = set(active_player_ids) # Все активные игроки должны бросить
    state["player_rolls_in_round"].clear()
    state["player_with_bye"] = None
    state["round_matches"] = [] # Очищаем матчи предыдущего "нулевого" состояния

    if mode == "pair_match":
        # Перемешиваем ID активных игроков
        shuffled_active_ids = list(active_player_ids) # Создаем копию для перемешивания
        random.shuffle(shuffled_active_ids)

        if len(shuffled_active_ids) % 2 != 0:
            state["player_with_bye"] = shuffled_active_ids.pop() # Последний получает "бай"
            bye_player_name = state["players"].get(state["player_with_bye"], {}).get("username", "Игрок")
            # Отправляем отдельное сообщение о "бай", так как edit_message_text перезатрет его
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"ℹ️ Нечетное количество игроков. {bye_player_name} получает 'бай' в раунде {round_num} и проходит дальше автоматически."
            )
            logger.info(f"Игрок {state['player_with_bye']} ({bye_player_name}) получает 'бай' в раунде {round_num}.")

        # Формируем пары из оставшихся
        for i in range(0, len(shuffled_active_ids), 2):
            if i + 1 < len(shuffled_active_ids):
                state["round_matches"].append((shuffled_active_ids[i], shuffled_active_ids[i+1]))
        logger.info(f"Сформированы пары для раунда {round_num}: {state['round_matches']}")
    
    # Обновляем основное сообщение турнира. Оно покажет режим, раунд и кнопку "Бросить кубик".
    await update_tournament_message(context, chat_id_override=query.message.chat_id, message_id_override=query.message.message_id)
    
    # Можно отправить дополнительное сообщение о начале турнира, если нужно
    # start_message = f"🚀 **Турнир официально начинается!**\nРежим: {'Парные поединки' if mode == 'pair_match' else 'Каждый против каждого'}.\n"
    # start_message += f"**Раунд {round_num}.** Участники, бросайте кубики!"
    # await context.bot.send_message(chat_id=query.message.chat_id, text=start_message, parse_mode="Markdown")


async def make_tournament_roll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает бросок кубика игроком в турнире."""
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        logger.warning("make_tournament_roll: нет query, query.from_user или query.message")
        if query: await query.answer("Ошибка обработки.", show_alert=True)
        return

    user = query.from_user
    
    state = dice_tournament_state
    if not state["active"] or state["registration_open"]:
        await query.answer("Турнир не активен или еще идет регистрация.", show_alert=True)
        return

    if user.id not in state["players"] or state["players"][user.id].get("is_eliminated"):
        await query.answer(f"{user.first_name}, вы не участвуете в этом раунде или уже выбыли.", show_alert=True)
        return

    if user.id not in state["active_players_in_round"]:
        # Проверяем, может игрок уже бросил кубик
        if user.id in state["player_rolls_in_round"]:
            await query.answer(f"{user.first_name}, вы уже сделали бросок в этом раунде: {state['player_rolls_in_round'][user.id]}.", show_alert=True)
        else:
            await query.answer(f"{user.first_name}, сейчас не ваша очередь бросать или вы не участвуете в текущем матче/раунде.", show_alert=True)
        return
    
    # Отвечаем на callback до отправки кубика, чтобы кнопка не "зависала"
    await query.answer("Бросаем кубик...")

    try:
        # Отправляем кубик от имени бота в чат турнира
        dice_message = await context.bot.send_dice(
            chat_id=query.message.chat_id, 
            emoji=DiceEmoji.DICE
            # reply_to_message_id=query.message.message_id # Не обязательно отвечать на сообщение с кнопкой
        )
        dice_value = dice_message.dice.value
        logger.info(f"Игрок {user.first_name} ({user.id}) бросил кубик в чате {query.message.chat_id}: {dice_value}")

        state["player_rolls_in_round"][user.id] = dice_value
        state["players"][user.id]["current_roll"] = dice_value # Это поле можно убрать, если не используется где-то еще
        state["active_players_in_round"].discard(user.id) # Удаляем игрока из списка ожидающих броска

        # Сообщаем о результате броска (можно сделать reply на сообщение с кубиком)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"🎲 {user.first_name} бросил(а) кубик и получил(а): **{dice_value}**!",
            reply_to_message_id=dice_message.message_id, # Отвечаем на сам кубик
            parse_mode="Markdown"
        )
        
        # Обновляем основное сообщение турнира, чтобы отразить бросок
        await update_tournament_message(context, chat_id_override=query.message.chat_id, message_id_override=state["tournament_message_id"])


        if not state["active_players_in_round"]: # Если все, кто должен был, бросили
            logger.info(f"Все игроки ({list(state['player_rolls_in_round'].keys())}) сделали броски в раунде {state['current_round']}.")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"Все активные игроки сделали броски в раунде {state['current_round']}! Подведение итогов..."
            )
            await asyncio.sleep(2) # Небольшая задержка для наглядности
            await announce_round_results(context)
            
    except Exception as e:
        logger.error(f"Ошибка при броске кубика для {user.first_name} ({user.id}): {e}", exc_info=True)
        # Уведомляем пользователя об ошибке через answer, если это еще возможно
        try:
            await query.answer("Произошла ошибка при броске кубика. Попробуйте еще раз.", show_alert=True)
        except: # Если answer уже был вызван или query устарел
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"{user.first_name}, произошла ошибка при вашем броске. Попробуйте снова нажать кнопку.")


async def stop_tournament_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Останавливает текущий турнир (команда администратора)."""
    if not update.message or not update.message.from_user:
        logger.warning("stop_tournament_command вызван без сообщения или пользователя.")
        return

    user_id = update.message.from_user.id
    state = dice_tournament_state

    if not state["active"]:
        await update.message.reply_text("Нет активного турнира для остановки.")
        return

    if user_id != state.get("admin_user_id"):
        await update.message.reply_text("Только администратор, запустивший турнир, может его остановить.")
        return

    logger.info(f"Администратор {user_id} останавливает турнир в чате {state.get('tournament_chat_id')}.")
    # end_tournament отправит сообщение о завершении и сбросит состояние
    await end_tournament(context, winner_data=None) # Передаем None, так как это принудительная остановка
    # Дополнительное сообщение о том, что турнир остановлен администратором, если end_tournament не достаточно ясно это говорит
    await update.message.reply_text("Турнир был принудительно остановлен администратором.")