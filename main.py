import logging
from datetime import time
from zoneinfo import ZoneInfo
import os

# Импорт из python-telegram-bot v20+
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# НАСТРОЙКИ

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set in environment variables!")

# Время по умолчанию (01:00 МСК), если пользователь не задал своё
DEFAULT_HOUR = 1
DEFAULT_MINUTE = 0

# Имя задачи в JobQueue. 
# Для удобства используем одно имя, но будем различать их по "data" (там chat_id).
JOB_NAME = "daily_questions_job"

# === ХРАНИЛИЩЕ СОСТОЯНИЙ ===

# user_states: словарь вида {chat_id: { ... }} — для каждого пользователя или группы своя запись.
# Пример структуры для одного chat_id:
# {
#     "state": "idle" | "answering" | "waiting_for_tomorrow_goal",
#     "answers": { "Цель на сегодня": None/'yes'/'no', ... },
#     "messages": { "Цель на сегодня": <message_id>, ... },
#     "send_hour": <int>,
#     "send_minute": <int>
# }
user_states = {}

# Список вопросов, которые бот задаёт
QUESTIONS = ["Цель на сегодня", "Новое", "Развитие", "Спорт"]


def main():
    """
    Точка входа. «Синхронный» запуск бота (application.run_polling())
    без дополнительного asyncio.run(...).
    """
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("settime", settime_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Запускаем бота блокирующим методом
    application.run_polling()


# === ОБРАБОТЧИКИ КОМАНД ===

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start — инициализация (или переинициализация) для текущего chat_id.
    Задаём дефолтное время (либо берём уже сохранённое), 
    создаём/обновляем ежедневную задачу в JobQueue.
    """
    chat_id = update.effective_chat.id

    # Инициализируем запись в user_states, если ещё нет
    user_data = user_states.setdefault(chat_id, {})
    user_data.setdefault("state", "idle")
    user_data.setdefault("send_hour", DEFAULT_HOUR)
    user_data.setdefault("send_minute", DEFAULT_MINUTE)

    # Удаляем предыдущее задание (если было) и ставим новое
    _remove_existing_job(context, JOB_NAME, chat_id)
    _add_daily_job(context, chat_id, user_data["send_hour"], user_data["send_minute"])

    await update.message.reply_text(
        "Привет! Я бот, который каждый день в назначенное время будет задавать 4 вопроса:\n"
        "1) Цель на сегодня\n"
        "2) Новое\n"
        "3) Развитие\n"
        "4) Спорт\n\n"
        "После ответов я спрошу «Цель на завтра». \n\n"
        "Чтобы изменить время рассылки, используйте команду:\n"
        "/settime ЧЧ:ММ (например, /settime 02:30)."
    )


async def settime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /settime ЧЧ:ММ — установить или изменить время ежедневной рассылки вопросов для текущего chat_id.
    """
    chat_id = update.effective_chat.id
    user_data = user_states.setdefault(chat_id, {})

    # Проверяем аргументы
    if len(context.args) != 1:
        await update.message.reply_text("Формат команды: /settime ЧЧ:ММ (например, /settime 02:30).")
        return

    time_str = context.args[0]
    try:
        hour_str, minute_str = time_str.split(":")
        hour = int(hour_str)
        minute = int(minute_str)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("Неверное время")
    except:
        await update.message.reply_text("Неверный формат времени. Пример: /settime 02:30")
        return

    # Сохраняем в user_data
    user_data["send_hour"] = hour
    user_data["send_minute"] = minute

    # Удаляем старую job, ставим новую
    _remove_existing_job(context, JOB_NAME, chat_id)
    _add_daily_job(context, chat_id, hour, minute)

    await update.message.reply_text(
        f"Новое время ежедневной рассылки установлено: {hour:02d}:{minute:02d} (МСК)."
    )


# === ОБРАБОТЧИК ИНЛАЙН-КНОПОК ===

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработка нажатий на inline-кнопки (❌ / ✅).
    """
    query = update.callback_query
    await query.answer()  # убираем "loading..."

    chat_id = query.message.chat_id
    user_data = user_states.get(chat_id)
    if not user_data or user_data.get("state") != "answering":
        return  # Игнорируем, если не в нужном состоянии

    data = query.data  # Например "Цель на сегодня|yes" или "Спорт|no"
    question, answer = data.split("|")

    # Проверим, не ответил ли уже юзер на этот вопрос
    if user_data["answers"].get(question) is not None:
        return  # уже есть ответ — игнорируем повторные клики

    # Сохраняем ответ
    user_data["answers"][question] = answer

    # Редактируем кнопки: оставляем только ту, на которую нажали (❌ или ✅)
    if answer == "yes":
        new_keyboard = [[InlineKeyboardButton("✅", callback_data="chosen")]]
    else:
        new_keyboard = [[InlineKeyboardButton("❌", callback_data="chosen")]]

    try:
        await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))
    except:
        pass

    # Если все 4 вопроса уже отвечены, переходим к "Цель на завтра"
    if all(v is not None for v in user_data["answers"].values()):
        user_data["state"] = "waiting_for_tomorrow_goal"
        await context.bot.send_message(chat_id, "Цель на завтра?")


# === ОБРАБОТЧИК СООБЩЕНИЙ (ТЕКСТ) ===

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатываем все текстовые сообщения (кроме команд).
    Если пользователь в состоянии "waiting_for_tomorrow_goal", считаем этот текст «целью на завтра».
    """
    chat_id = update.effective_chat.id
    user_data = user_states.get(chat_id)
    if not user_data:
        return

    if user_data.get("state") == "waiting_for_tomorrow_goal":
        goal_text = update.message.text
        logging.info(f"[{chat_id}] Цель на завтра: {goal_text}")

        # Меняем состояние на idle
        user_data["state"] = "idle"
        await update.message.reply_text("Цель на завтра принята! Жду тебя завтра в назначенное время.")


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ JOBQUEUE ===

def _remove_existing_job(context: ContextTypes.DEFAULT_TYPE, job_name: str, chat_id: int):
    """
    Удаляем из JobQueue все задачи с именем job_name и data=chat_id (если такие есть).
    Это нужно, чтобы не было дублей при изменении времени.
    """
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        if job.data == chat_id:
            job.schedule_removal()


def _add_daily_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int, hour: int, minute: int):
    """
    Добавляем в JobQueue новое задание, которое будет срабатывать каждый день в hour:minute (MSK).
    """
    context.job_queue.run_daily(
        send_daily_questions, 
        time=time(hour=hour, minute=minute, tzinfo=ZoneInfo("Europe/Moscow")),
        days=(0, 1, 2, 3, 4, 5, 6),  # все дни недели
        name=JOB_NAME,               # имя задачи
        data=chat_id                 # в data храним chat_id, чтобы знать, кому отправлять
    )


# === ФУНКЦИЯ, КОТОРУЮ ВЫЗЫВАЕТ JOBQUEUE КАЖДЫЙ ДЕНЬ ===

async def send_daily_questions(context: ContextTypes.DEFAULT_TYPE):
    """
    Вызывается JobQueue в установленное время (для каждого chat_id своё).
    Отправляет 4 вопроса с кнопками.
    """
    chat_id = context.job.data
    user_data = user_states.setdefault(chat_id, {})

    user_data["state"] = "answering"
    user_data["answers"] = {q: None for q in QUESTIONS}
    user_data["messages"] = {}

    for question in QUESTIONS:
        keyboard = [
            [
                InlineKeyboardButton("❌", callback_data=f"{question}|no"),
                InlineKeyboardButton("✅", callback_data=f"{question}|yes")
            ]
        ]
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=question,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        user_data["messages"][question] = msg.message_id


# Запуск
if __name__ == "__main__":
    main()