# bot.py
import os
import logging
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from questions import QUESTIONS
from database import init_db, create_response_row, save_answer, finalize_response, get_stats

# ── Setup ──────────────────────────────────────────────────────────────────────

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

SURVEY = 1

# Find your Telegram user ID by messaging @userinfobot on Telegram.
ADMIN_USER_ID = 123456789  # ← REPLACE WITH YOUR TELEGRAM USER ID


# ── Helper: build question text ────────────────────────────────────────────────

def build_question_text(question_index):
    """Format the question text with a progress indicator."""
    q = QUESTIONS[question_index]
    total = len(QUESTIONS)
    text = f"*Вопрос {question_index + 1} из {total}*\n\n{q['text']}"

    if q["type"] == "multi_choice":
        max_c = q.get("max_choices")
        if max_c:
            text += f"\n\n_(Выберите не более {max_c} вариантов)_"

    return text


# ── Helper: build keyboard ─────────────────────────────────────────────────────
#
# IMPORTANT: Telegram enforces a 64-byte hard limit on callback_data.
# Long Russian option strings would exceed this instantly, so we never put
# option text into callback_data. Instead we pass small integer indices and
# look up the real text in the handler.
#
# callback_data formats used (all well under 64 bytes):
#   "a|{q_idx}|{opt_idx}"   — single answer (choice / scale / integer fallback)
#   "t|{q_idx}|{opt_idx}"   — toggle a multi_choice option on/off
#   "c|{q_idx}"             — confirm multi_choice selection
#   "b|{q_idx}"             — go back to the previous question

def build_keyboard(question_index, selected_indices=None):
    """
    Build the inline keyboard for any question type.
    'selected_indices' is a set of option indices (ints) for multi_choice questions.
    """
    q = QUESTIONS[question_index]
    keyboard = []
    selected_indices = selected_indices or set()
    qi = question_index

    if q["type"] == "choice":
        for oi, option in enumerate(q["options"]):
            keyboard.append([
                InlineKeyboardButton(option, callback_data=f"a|{qi}|{oi}")
            ])

    elif q["type"] == "scale":
        min_val = q.get("min", 1)
        max_val = q.get("max", 5)
        row = [
            InlineKeyboardButton(str(v), callback_data=f"a|{qi}|{oi}")
            for oi, v in enumerate(range(min_val, max_val + 1))
        ]
        keyboard.append(row)

    elif q["type"] == "integer":
        # No main buttons — the user types the number freely.
        # Only show fallback escape options as buttons.
        for oi, option in enumerate(q.get("fallback_options", [])):
            keyboard.append([
                InlineKeyboardButton(option, callback_data=f"a|{qi}|{oi}")
            ])

    elif q["type"] == "multi_choice":
        for oi, option in enumerate(q["options"]):
            label = f"✓  {option}" if oi in selected_indices else option
            keyboard.append([
                InlineKeyboardButton(label, callback_data=f"t|{qi}|{oi}")
            ])
        if selected_indices:
            keyboard.append([
                InlineKeyboardButton("✅  Подтвердить выбор", callback_data=f"c|{qi}")
            ])

    # Back button on every question except the first
    if question_index > 0:
        keyboard.append([
            InlineKeyboardButton("← Назад", callback_data=f"b|{qi}")
        ])

    return InlineKeyboardMarkup(keyboard)


# ── Helper: resolve the display text for a selected option index ───────────────

def option_text(question_index, opt_index):
    """
    Return the display text for a given option index.
    Handles choice, multi_choice, scale, and integer fallback uniformly.
    """
    q = QUESTIONS[question_index]

    if q["type"] == "scale":
        min_val = q.get("min", 1)
        return str(min_val + opt_index)

    if q["type"] == "integer":
        return q["fallback_options"][opt_index]

    return q["options"][opt_index]


# ── Helper: advance to the next question or finish the survey ──────────────────

async def _advance(target, context, question_index, now, *, is_message=False):
    """
    Move to the next question after an answer has been recorded.
    'target' is either a CallbackQuery or a Message object.
    """
    next_index = question_index + 1
    context.user_data["current_question"] = next_index
    context.user_data["question_start"] = now

    if next_index >= len(QUESTIONS):
        end_time = now
        total_seconds = (end_time - context.user_data["survey_start"]).total_seconds()
        total_minutes = round(total_seconds / 60, 1)

        finalize_response(
            row_id=context.user_data["row_id"],
            end_time=end_time.isoformat(),
            total_seconds=total_seconds
        )

        completion_text = (
            f"✅ *Опрос завершён!*\n\n"
            f"Спасибо за участие! Вы ответили на все {len(QUESTIONS)} вопросов "
            f"за *{total_minutes} минут(-ы)*.\n\nВаши ответы сохранены."
        )
        if is_message:
            await target.reply_text(completion_text, parse_mode="Markdown")
        else:
            await target.edit_message_text(completion_text, parse_mode="Markdown")

        return ConversationHandler.END

    next_text = build_question_text(next_index)
    next_kb = build_keyboard(next_index)

    if is_message:
        await target.reply_text(next_text, reply_markup=next_kb, parse_mode="Markdown")
    else:
        await target.edit_message_text(next_text, reply_markup=next_kb, parse_mode="Markdown")

    return SURVEY


# ── Command: /start ────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.now()

    context.user_data["current_question"] = 0
    context.user_data["answers"] = {}
    context.user_data["question_times"] = {}
    context.user_data["survey_start"] = now
    context.user_data["question_start"] = now

    row_id = create_response_row(
        user_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "",
        start_time=now.isoformat()
    )
    context.user_data["row_id"] = row_id

    await update.message.reply_text(
        f"Здравствуйте, {user.first_name}! Добро пожаловать в опрос.\n\n"
        f"Всего *{len(QUESTIONS)} вопросов*. На каждом вопросе есть кнопка "
        f"«← Назад», если захотите изменить предыдущий ответ.\n\nНачнём!",
        parse_mode="Markdown"
    )

    await update.message.reply_text(
        build_question_text(0),
        reply_markup=build_keyboard(0),
        parse_mode="Markdown"
    )

    return SURVEY


# ── Callback: all button presses ───────────────────────────────────────────────

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    action = parts[0]
    now = datetime.now()

    # ── BACK ──────────────────────────────────────────────────────────────
    if action == "b":
        current_index = int(parts[1])
        prev_index = current_index - 1

        elapsed = (now - context.user_data.get("question_start", now)).total_seconds()
        existing = context.user_data["question_times"].get(current_index, 0)
        context.user_data["question_times"][current_index] = existing + elapsed

        context.user_data.pop(f"mc_{current_index}", None)
        context.user_data["current_question"] = prev_index
        context.user_data["question_start"] = now

        prev_answer = context.user_data["answers"].get(prev_index)
        extra = f"\n\n_Ваш предыдущий ответ: *{prev_answer}*_" if prev_answer else ""

        # Restore tick state when going back to a multi_choice question
        prev_q = QUESTIONS[prev_index]
        selected_indices = set()
        if prev_q["type"] == "multi_choice" and prev_answer:
            saved_texts = prev_answer.split(" | ")
            for oi, opt in enumerate(prev_q["options"]):
                if opt in saved_texts:
                    selected_indices.add(oi)
            context.user_data[f"mc_{prev_index}"] = selected_indices

        await query.edit_message_text(
            build_question_text(prev_index) + extra,
            reply_markup=build_keyboard(prev_index, selected_indices=selected_indices),
            parse_mode="Markdown"
        )
        return SURVEY

    # ── TOGGLE multi_choice option ────────────────────────────────────────
    if action == "t":
        question_index = int(parts[1])
        opt_index = int(parts[2])
        q = QUESTIONS[question_index]
        max_choices = q.get("max_choices")

        key = f"mc_{question_index}"
        selected_indices = context.user_data.get(key, set())

        if opt_index in selected_indices:
            selected_indices.discard(opt_index)
        else:
            if max_choices and len(selected_indices) >= max_choices:
                await query.answer(
                    f"Можно выбрать не более {max_choices} вариантов.",
                    show_alert=True
                )
                return SURVEY
            selected_indices.add(opt_index)

        context.user_data[key] = selected_indices

        await query.edit_message_text(
            build_question_text(question_index),
            reply_markup=build_keyboard(question_index, selected_indices=selected_indices),
            parse_mode="Markdown"
        )
        return SURVEY

    # ── CONFIRM multi_choice ──────────────────────────────────────────────
    if action == "c":
        question_index = int(parts[1])
        key = f"mc_{question_index}"
        selected_indices = context.user_data.get(key, set())

        if not selected_indices:
            await query.answer("Пожалуйста, выберите хотя бы один вариант.", show_alert=True)
            return SURVEY

        q = QUESTIONS[question_index]
        ordered_texts = [q["options"][oi] for oi in sorted(selected_indices)]
        answer = " | ".join(ordered_texts)

        elapsed = (now - context.user_data.get("question_start", now)).total_seconds()
        existing = context.user_data["question_times"].get(question_index, 0)
        total_q_seconds = existing + elapsed

        context.user_data["question_times"][question_index] = total_q_seconds
        context.user_data["answers"][question_index] = answer
        context.user_data.pop(key, None)

        save_answer(
            row_id=context.user_data["row_id"],
            question_index=question_index,
            answer=answer,
            seconds_spent=total_q_seconds
        )

        return await _advance(query, context, question_index, now)

    # ── SINGLE ANSWER (choice / scale / integer fallback button) ──────────
    if action == "a":
        question_index = int(parts[1])
        opt_index = int(parts[2])
        answer = option_text(question_index, opt_index)

        elapsed = (now - context.user_data.get("question_start", now)).total_seconds()
        existing = context.user_data["question_times"].get(question_index, 0)
        total_q_seconds = existing + elapsed

        context.user_data["question_times"][question_index] = total_q_seconds
        context.user_data["answers"][question_index] = answer

        save_answer(
            row_id=context.user_data["row_id"],
            question_index=question_index,
            answer=answer,
            seconds_spent=total_q_seconds
        )

        return await _advance(query, context, question_index, now)


# ── Message handler: typed numeric input for "integer" questions ───────────────

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    current_index = context.user_data.get("current_question", 0)
    q = QUESTIONS[current_index]

    if q["type"] != "integer":
        await update.message.reply_text(
            "Пожалуйста, используйте кнопки для ответа на этот вопрос. "
            "Если кнопки не видны, отправьте /start чтобы начать заново."
        )
        return SURVEY

    raw = update.message.text.strip()

    if not raw.isdigit() or int(raw) <= 0:
        await update.message.reply_text(
            "Пожалуйста, введите целое положительное число.\n"
            "Например: *25* (для возраста) или *50000* (для зарплаты).",
            parse_mode="Markdown"
        )
        return SURVEY

    answer = raw

    elapsed = (now - context.user_data.get("question_start", now)).total_seconds()
    existing = context.user_data["question_times"].get(current_index, 0)
    total_q_seconds = existing + elapsed

    context.user_data["question_times"][current_index] = total_q_seconds
    context.user_data["answers"][current_index] = answer

    save_answer(
        row_id=context.user_data["row_id"],
        question_index=current_index,
        answer=answer,
        seconds_spent=total_q_seconds
    )

    return await _advance(update.message, context, current_index, now, is_message=True)


# ── Command: /stats (admin only) ──────────────────────────────────────────────

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    completed, in_progress, avg_time = get_stats()
    avg_minutes = round(avg_time / 60, 1) if avg_time else 0

    await update.message.reply_text(
        f"📊 *Статистика опроса*\n\n"
        f"✅ Завершено: *{completed}*\n"
        f"🔄 В процессе: *{in_progress}*\n"
        f"⏱ Среднее время заполнения: *{avg_minutes} мин.*",
        parse_mode="Markdown"
    )


# ── Command: /cancel ──────────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Опрос отменён. Чтобы начать заново, отправьте /start."
    )
    return ConversationHandler.END


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SURVEY: [
                CallbackQueryHandler(handle_answer),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("stats", stats))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
