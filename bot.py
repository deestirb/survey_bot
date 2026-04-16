# bot.py
import os
import random
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

from questions import QUESTIONS, RANDOMIZE_GROUPS
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


# ── Randomization ──────────────────────────────────────────────────────────────

def build_question_order():
    """
    Build the question order for one respondent session.

    Questions inside RANDOMIZE_GROUPS have their internal order shuffled.
    All other questions remain in their original positions.
    Returns a list of indices into QUESTIONS (length == len(QUESTIONS)).
    """
    # Map question id → position in QUESTIONS
    id_to_idx = {q["id"]: i for i, q in enumerate(QUESTIONS)}

    # Start with the default sequential order
    order = list(range(len(QUESTIONS)))

    for group_ids in RANDOMIZE_GROUPS:
        # Resolve ids to indices; skip any unknown ids silently
        group_indices = [id_to_idx[qid] for qid in group_ids if qid in id_to_idx]
        if not group_indices:
            continue

        # Find where these indices sit in the current order
        positions_in_order = [order.index(qi) for qi in group_indices]
        first_pos = min(positions_in_order)

        # Shuffle the group and write it back into the same slice of order
        shuffled = group_indices[:]
        random.shuffle(shuffled)
        for offset, q_idx in enumerate(shuffled):
            order[first_pos + offset] = q_idx

    return order


# ── Progress bar ───────────────────────────────────────────────────────────────

def progress_bar(step, total):
    """
    Return a single-line progress indicator, e.g. '▓▓▓░░░░░░░  30%'
    step is 0-based; total is the total number of steps.
    """
    pct = round((step / total) * 100)
    filled = round(pct / 10)
    bar = "▓" * filled + "░" * (10 - filled)
    return f"{bar}  {pct}%"


# ── Build question text ────────────────────────────────────────────────────────

def build_question_text(q_idx, step, total_steps):
    """
    Format the question for display.
    Shows a percentage progress bar instead of 'Question N of M'
    so as not to discourage respondents.
    """
    q = QUESTIONS[q_idx]
    header = progress_bar(step, total_steps) + "\n\n"
    text = header + q["text"]

    if q["type"] == "multi_choice":
        max_c = q.get("max_choices")
        if max_c:
            text += f"\n\n_(Выберите не более {max_c} вариантов)_"

    return text


# ── Build keyboard ─────────────────────────────────────────────────────────────
#
# Telegram enforces a 64-byte hard limit on callback_data.
# We never put option text into callback_data — only small integers.
#
# callback_data formats (all well under 64 bytes):
#   "a|{step}|{opt_idx}"  — single answer chosen
#   "t|{step}|{opt_idx}"  — toggle a multi_choice option
#   "c|{step}"            — confirm multi_choice selection
#   "b|{step}"            — go back to previous step

def build_keyboard(q_idx, step, selected_indices=None):
    """
    Build the inline keyboard for any question type.
    'selected_indices' is a set of option indices for multi_choice questions.
    """
    q = QUESTIONS[q_idx]
    keyboard = []
    selected_indices = selected_indices or set()

    if q["type"] == "choice":
        for oi, option in enumerate(q["options"]):
            keyboard.append([
                InlineKeyboardButton(option, callback_data=f"a|{step}|{oi}")
            ])

    elif q["type"] == "scale":
        min_val = q.get("min", 1)
        max_val = q.get("max", 5)
        row = [
            InlineKeyboardButton(str(v), callback_data=f"a|{step}|{oi}")
            for oi, v in enumerate(range(min_val, max_val + 1))
        ]
        keyboard.append(row)

    elif q["type"] == "integer":
        # User types the number; only show fallback escape buttons.
        for oi, option in enumerate(q.get("fallback_options", [])):
            keyboard.append([
                InlineKeyboardButton(option, callback_data=f"a|{step}|{oi}")
            ])

    elif q["type"] == "text":
        # Purely free-text — no option buttons, only Back if applicable.
        pass

    elif q["type"] == "multi_choice":
        for oi, option in enumerate(q["options"]):
            label = f"✓  {option}" if oi in selected_indices else option
            keyboard.append([
                InlineKeyboardButton(label, callback_data=f"t|{step}|{oi}")
            ])
        if selected_indices:
            keyboard.append([
                InlineKeyboardButton("✅  Подтвердить выбор", callback_data=f"c|{step}")
            ])

    # Back button on every step except the very first
    if step > 0:
        keyboard.append([
            InlineKeyboardButton("← Назад", callback_data=f"b|{step}")
        ])

    return InlineKeyboardMarkup(keyboard)


# ── Resolve option text from index ────────────────────────────────────────────

def option_text(q_idx, opt_idx):
    """Return the display text for a given option index."""
    q = QUESTIONS[q_idx]
    if q["type"] == "scale":
        return str(q.get("min", 1) + opt_idx)
    if q["type"] == "integer":
        return q["fallback_options"][opt_idx]
    return q["options"][opt_idx]


# ── Advance to next step or finish ────────────────────────────────────────────

async def _advance(target, context, current_step, now, *, is_message=False):
    """
    Move to the next step after an answer has been saved.
    'target' is a CallbackQuery or a Message object.
    """
    question_order = context.user_data["question_order"]
    next_step = current_step + 1
    context.user_data["current_step"] = next_step
    context.user_data["question_start"] = now

    # ── Survey complete ────────────────────────────────────────────────────
    if next_step >= len(question_order):
        total_seconds = (now - context.user_data["survey_start"]).total_seconds()
        total_minutes = round(total_seconds / 60, 1)

        finalize_response(
            row_id=context.user_data["row_id"],
            end_time=now.isoformat(),
            total_seconds=total_seconds
        )

        completion_text = (
            "✅ *Опрос завершён!*\n\n"
            f"Спасибо за участие! Вы ответили на все вопросы "
            f"за *{total_minutes} минут(-ы)*.\n\nВаши ответы сохранены."
        )
        if is_message:
            await target.reply_text(completion_text, parse_mode="Markdown")
        else:
            await target.edit_message_text(completion_text, parse_mode="Markdown")
        return ConversationHandler.END

    # ── Show next question ─────────────────────────────────────────────────
    next_q_idx = question_order[next_step]
    text = build_question_text(next_q_idx, next_step, len(question_order))
    kb = build_keyboard(next_q_idx, next_step)

    if is_message:
        await target.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await target.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")

    return SURVEY


# ── Command: /start ────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    now = datetime.now()

    question_order = build_question_order()

    context.user_data["question_order"] = question_order
    context.user_data["current_step"] = 0
    context.user_data["answers"] = {}         # {q_idx: answer_text}
    context.user_data["question_times"] = {}  # {q_idx: seconds_spent}
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
        f"Здравствуйте, {user.first_name}!\n\n"
        "Вас приветствует социологический опрос. "
        "Заполнение займёт примерно *10–15 минут*.\n\n"
        "🔒 *Анонимность:* все ответы полностью анонимны. "
        "Ваши личные данные не собираются и не передаются третьим лицам.\n\n"
        "На каждом вопросе есть кнопка «← Назад», "
        "если захотите изменить предыдущий ответ.\n\n"
        "Начнём!",
        parse_mode="Markdown"
    )

    first_q_idx = question_order[0]
    await update.message.reply_text(
        build_question_text(first_q_idx, 0, len(question_order)),
        reply_markup=build_keyboard(first_q_idx, 0),
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

    question_order = context.user_data["question_order"]

    # ── BACK ──────────────────────────────────────────────────────────────
    if action == "b":
        current_step = int(parts[1])
        prev_step = current_step - 1
        current_q_idx = question_order[current_step]
        prev_q_idx = question_order[prev_step]

        # Accumulate time on the step being left
        elapsed = (now - context.user_data.get("question_start", now)).total_seconds()
        existing = context.user_data["question_times"].get(current_q_idx, 0)
        context.user_data["question_times"][current_q_idx] = existing + elapsed

        # Discard in-progress multi_choice selection on the step being left
        context.user_data.pop(f"mc_{current_step}", None)

        context.user_data["current_step"] = prev_step
        context.user_data["question_start"] = now

        prev_answer = context.user_data["answers"].get(prev_q_idx)
        extra = f"\n\n_Ваш предыдущий ответ: *{prev_answer}*_" if prev_answer else ""

        # Restore tick state if going back to a multi_choice question
        prev_q = QUESTIONS[prev_q_idx]
        selected_indices = set()
        if prev_q["type"] == "multi_choice" and prev_answer:
            saved_texts = prev_answer.split(" | ")
            for oi, opt in enumerate(prev_q["options"]):
                if opt in saved_texts:
                    selected_indices.add(oi)
            context.user_data[f"mc_{prev_step}"] = selected_indices

        await query.edit_message_text(
            build_question_text(prev_q_idx, prev_step, len(question_order)) + extra,
            reply_markup=build_keyboard(prev_q_idx, prev_step, selected_indices),
            parse_mode="Markdown"
        )
        return SURVEY

    # ── TOGGLE multi_choice ───────────────────────────────────────────────
    if action == "t":
        step = int(parts[1])
        opt_index = int(parts[2])
        q_idx = question_order[step]
        q = QUESTIONS[q_idx]
        max_choices = q.get("max_choices")

        key = f"mc_{step}"
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
            build_question_text(q_idx, step, len(question_order)),
            reply_markup=build_keyboard(q_idx, step, selected_indices),
            parse_mode="Markdown"
        )
        return SURVEY

    # ── CONFIRM multi_choice ──────────────────────────────────────────────
    if action == "c":
        step = int(parts[1])
        q_idx = question_order[step]
        key = f"mc_{step}"
        selected_indices = context.user_data.get(key, set())

        if not selected_indices:
            await query.answer("Пожалуйста, выберите хотя бы один вариант.", show_alert=True)
            return SURVEY

        q = QUESTIONS[q_idx]
        ordered_texts = [q["options"][oi] for oi in sorted(selected_indices)]
        answer = " | ".join(ordered_texts)

        elapsed = (now - context.user_data.get("question_start", now)).total_seconds()
        existing = context.user_data["question_times"].get(q_idx, 0)
        total_q_seconds = existing + elapsed

        context.user_data["question_times"][q_idx] = total_q_seconds
        context.user_data["answers"][q_idx] = answer
        context.user_data.pop(key, None)

        save_answer(
            row_id=context.user_data["row_id"],
            question_index=q_idx,
            answer=answer,
            seconds_spent=total_q_seconds
        )
        return await _advance(query, context, step, now)

    # ── SINGLE ANSWER (choice / scale / integer fallback) ─────────────────
    if action == "a":
        step = int(parts[1])
        opt_index = int(parts[2])
        q_idx = question_order[step]
        answer = option_text(q_idx, opt_index)

        elapsed = (now - context.user_data.get("question_start", now)).total_seconds()
        existing = context.user_data["question_times"].get(q_idx, 0)
        total_q_seconds = existing + elapsed

        context.user_data["question_times"][q_idx] = total_q_seconds
        context.user_data["answers"][q_idx] = answer

        save_answer(
            row_id=context.user_data["row_id"],
            question_index=q_idx,
            answer=answer,
            seconds_spent=total_q_seconds
        )
        return await _advance(query, context, step, now)


# ── Message handler: typed input for "integer" and "text" questions ────────────

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    question_order = context.user_data.get("question_order", [])
    current_step = context.user_data.get("current_step", 0)

    if not question_order:
        await update.message.reply_text(
            "Отправьте /start чтобы начать опрос."
        )
        return SURVEY

    q_idx = question_order[current_step]
    q = QUESTIONS[q_idx]

    # ── "integer" type ────────────────────────────────────────────────────
    if q["type"] == "integer":
        raw = update.message.text.strip()
        if not raw.isdigit() or int(raw) <= 0:
            await update.message.reply_text(
                "Пожалуйста, введите целое положительное число.\n"
                "Например: *25* (для возраста) или *50000* (для зарплаты).",
                parse_mode="Markdown"
            )
            return SURVEY
        answer = raw

    # ── "text" type ───────────────────────────────────────────────────────
    elif q["type"] == "text":
        answer = update.message.text.strip()
        if not answer:
            await update.message.reply_text(
                "Пожалуйста, введите Ваш ответ текстом и отправьте его."
            )
            return SURVEY

    # ── Any other type: remind to use buttons ─────────────────────────────
    else:
        await update.message.reply_text(
            "Пожалуйста, используйте кнопки для ответа на этот вопрос. "
            "Если кнопки не видны, отправьте /start чтобы начать заново."
        )
        return SURVEY

    elapsed = (now - context.user_data.get("question_start", now)).total_seconds()
    existing = context.user_data["question_times"].get(q_idx, 0)
    total_q_seconds = existing + elapsed

    context.user_data["question_times"][q_idx] = total_q_seconds
    context.user_data["answers"][q_idx] = answer

    save_answer(
        row_id=context.user_data["row_id"],
        question_index=q_idx,
        answer=answer,
        seconds_spent=total_q_seconds
    )

    return await _advance(update.message, context, current_step, now, is_message=True)


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
