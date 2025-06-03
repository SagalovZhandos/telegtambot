import os
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    JobQueue
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы
BOT_TOKEN = "8105161394:AAH48_kSNunJuSMzmML4f0tfZrfquG1QgrY"
SPREADSHEET_NAME = "Telegram zayavki"
REMINDER_INTERVAL = 300  # 5 минут в секундах

# Роли пользователей
ADMIN_ID = 886922044  # Ваш ID 1132625886  886922044
users_roles = {}  # {user_id: 'admin'/'dispatcher'/'technician'}

# Инициализация Google Sheets
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name("service_account.json", scope)
gs_client = gspread.authorize(creds)

# Активные заявки и напоминания
active_requests = {}
reminder_jobs = {}

def get_worksheet():
    try:
        return gs_client.open(SPREADSHEET_NAME).sheet1
    except gspread.SpreadsheetNotFound:
        worksheet = gs_client.create(SPREADSHEET_NAME)
        return worksheet.sheet1

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or users_roles.get(user_id) == 'admin'

def is_dispatcher(user_id: int) -> bool:
    return users_roles.get(user_id) == 'dispatcher'

def is_technician(user_id: int) -> bool:
    return users_roles.get(user_id) == 'technician'

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для обработки заявок.\n\n"
        "Формат заявки:\n"
        "серийный номер: ...\n"
        "проблема: ...\n"
        "телефон водителя: ...\n"
        "госномер: ...\n"
        "автопарк: ..."
    )

async def set_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    try:
        user_id = int(context.args[0])
        users_roles[user_id] = 'dispatcher'
        await update.message.reply_text(f"✅ Пользователь {user_id} назначен диспетчером.")
        try:
            await context.bot.send_message(user_id, "Вас назначили диспетчером!")
        except:
            await update.message.reply_text(f"Не удалось уведомить пользователя {user_id}.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /setdispatcher <user_id>")

async def set_technician(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    try:
        user_id = int(context.args[0])
        users_roles[user_id] = 'technician'
        await update.message.reply_text(f"✅ Пользователь {user_id} назначен техником.")
        try:
            await context.bot.send_message(user_id, "Вас назначили техником!")
        except:
            await update.message.reply_text(f"Не удалось уведомить пользователя {user_id}.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /settechnic <user_id>")

async def list_roles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    if not users_roles:
        await update.message.reply_text("Нет назначенных ролей.")
        return
    
    message = "Список ролей:\n"
    for user_id, role in users_roles.items():
        try:
            user = await context.bot.get_chat(user_id)
            message += f"{user.first_name} (ID: {user_id}) - {role}\n"
        except:
            message += f"ID: {user_id} - {role}\n"
    
    await update.message.reply_text(message)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    if is_dispatcher(user_id):
        await handle_dispatcher(update, context)
    elif is_technician(user_id):
        await handle_solution(update, context)
    else:
        await update.message.reply_text("❌ У вас нет прав для работы с заявками.")

async def handle_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    lines = text.split('\n')
    data = {}
    for line in lines:
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip().lower()] = value.strip()

    required = ["серийный номер", "проблема", "телефон водителя", "госномер", "автопарк"]
    if not all(k in data for k in required):
        await update.message.reply_text("❗ Неполные данные. Нужно: серийный номер, проблема, телефон водителя, госномер, автопарк.")
        return

    request_id = str(datetime.now().timestamp())
    active_requests[request_id] = {
        "serial": data["серийный номер"],
        "problem": data["проблема"],
        "phone": data["телефон водителя"],
        "bus": data["госномер"],
        "garage": data["автопарк"],
        "created_time": datetime.now(),
        "accepted_time": None,
        "assigned": False,
        "tech_id": None,
        "status": None,
        "solution": None,
        "photo": None,
        "dispatcher_id": update.message.from_user.id  # Сохраняем ID диспетчера
    }

    text_message = (
        f"📥 *Новая заявка #{request_id[-4:]}*\n"
        f"📟 Серийный номер: {data['серийный номер']}\n"
        f"🔧 Проблема: {data['проблема']}\n"
        f"📞 Телефон водителя: {data['телефон водителя']}\n"
        f"🚌 Госномер: {data['госномер']}\n"
        f"🏢 Автопарк: {data['автопарк']}")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять", callback_data=f"accept:{request_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{request_id}")
    ]])

    # Отправляем всем техникам
    technicians = [uid for uid, role in users_roles.items() if role == 'technician']
    sent_to_technicians = False
    for tech_id in technicians:
        try:
            await context.bot.send_message(
                chat_id=tech_id, 
                text=text_message, 
                parse_mode="Markdown", 
                reply_markup=keyboard
            )
            sent_to_technicians = True
        except Exception as e:
            logger.error(f"Ошибка отправки технику {tech_id}: {e}")

    # Уведомляем диспетчера о статусе отправки
    if sent_to_technicians:
        await update.message.reply_text(f"✅ Заявка #{request_id[-4:]} успешно отправлена техникам!")
    else:
        await update.message.reply_text("❌ Не удалось отправить заявку техникам. Нет доступных техников.")

    # Настраиваем напоминание только если есть кому напоминать
    if technicians:
        reminder_jobs[request_id] = context.job_queue.run_repeating(
            send_reminder,
            interval=REMINDER_INTERVAL,
            first=REMINDER_INTERVAL,
            data={'request_id': request_id},
            name=f'reminder_{request_id}'
        )

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    request_id = job.data['request_id']
    request = active_requests.get(request_id)
    
    if not request or request["assigned"]:
        if request_id in reminder_jobs:
            reminder_jobs[request_id].schedule_removal()
            del reminder_jobs[request_id]
        return
    
    # Проверяем, прошло ли 5 минут с момента создания заявки
    if datetime.now() - request["created_time"] < timedelta(seconds=REMINDER_INTERVAL):
        return
    
    text_message = (
        f"⏰ *Напоминание: заявка #{request_id[-4:]} еще активна!*\n"
        f"📟 Серийный номер: {request['serial']}\n"
        f"🔧 Проблема: {request['problem']}\n"
        f"📞 Телефон водителя: {request['phone']}\n"
        f"🚌 Госномер: {request['bus']}\n"
        f"🏢 Автопарк: {request['garage']}\n\n"
        f"❗ Никто не принял заявку в течение 5 минут!")

    # Отправляем напоминание всем техникам
    technicians = [uid for uid, role in users_roles.items() if role == 'technician']
    for tech_id in technicians:
        try:
            await context.bot.send_message(
                chat_id=tech_id, 
                text=text_message, 
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки напоминания технику {tech_id}: {e}")

    # Уведомляем диспетчера
    try:
        await context.bot.send_message(
            chat_id=request["dispatcher_id"],
            text=f"⏰ Заявка #{request_id[-4:]} все еще не принята техниками!"
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления диспетчера: {e}")

async def handle_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, req_id = query.data.split(":")
    request = active_requests.get(req_id)

    if not request or request["assigned"]:
        await query.edit_message_text("❌ Заявка уже обработана.")
        return

    request["assigned"] = True
    request["tech_id"] = query.from_user.id
    request["accepted_time"] = datetime.now()

    # Отменяем напоминание для этой заявки
    if req_id in reminder_jobs:
        reminder_jobs[req_id].schedule_removal()
        del reminder_jobs[req_id]

    if action == "accept":
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🟢 Решено", callback_data=f"resolved:{req_id}"),
            InlineKeyboardButton("🔴 Не решено", callback_data=f"unresolved:{req_id}")
        ]])
        await query.edit_message_text("✅ Вы приняли заявку. Укажи статус:", reply_markup=keyboard)
        
        # Уведомляем диспетчеров
        dispatchers = [uid for uid, role in users_roles.items() if role == 'dispatcher']
        for disp_id in dispatchers:
            try:
                await context.bot.send_message(
                    disp_id,
                    f"☑️ Заявку #{req_id[-4:]} принял: {query.from_user.full_name}"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления диспетчера {disp_id}: {e}")
        
        # Уведомляем других техников
        technicians = [uid for uid, role in users_roles.items() if role == 'technician' and uid != query.from_user.id]
        for tech_id in technicians:
            try:
                await context.bot.send_message(
                    tech_id,
                    f"ℹ️ Заявку #{req_id[-4:]} уже принял другой техник: {query.from_user.full_name}"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления техника {tech_id}: {e}")

    elif action == "reject":
        await query.edit_message_text("🔕 Вы отклонили заявку.")
        # Уведомляем диспетчеров об отказе
        dispatchers = [uid for uid, role in users_roles.items() if role == 'dispatcher']
        for disp_id in dispatchers:
            try:
                await context.bot.send_message(
                    disp_id,
                    f"❌ Заявку #{req_id[-4:]} отклонил: {query.from_user.full_name}"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления диспетчера {disp_id}: {e}")

async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, req_id = query.data.split(":")
    request = active_requests.get(req_id)

    if request and query.from_user.id == request["tech_id"]:
        request["status"] = "Решено" if action == "resolved" else "Не решено"
        await query.edit_message_text("✍️ Опиши, как ты решил проблему:")
    else:
        await query.edit_message_text("❌ Вы не назначены на эту заявку.")

async def handle_solution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    for req_id, req in active_requests.items():
        if req["tech_id"] == user_id and req["solution"] is None:
            req["solution"] = update.message.text
            await update.message.reply_text("📸 Теперь отправьте фото как подтверждение.")
            return

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    for req_id, req in active_requests.items():
        if req["tech_id"] == user_id and req["photo"] is None:
            req["photo"] = update.message.photo[-1].file_id

            caption = (
                f"📄 Заявка #{req_id[-4:]} выполнена\n"
                f"🧑‍🔧 Техник: {update.message.from_user.full_name}\n"
                f"📟 Серийный: {req['serial']}\n"
                f"📞 Телефон водителя: {req['phone']}\n"
                f"🚌 Госномер: {req['bus']}\n"
                f"🏢 Автопарк: {req['garage']}\n"
                f"📆 Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"⚙️ Статус: {req['status']}\n"
                f"📝 Решение: {req['solution']}")

            # Уведомляем диспетчеров
            dispatchers = [uid for uid, role in users_roles.items() if role == 'dispatcher']
            for disp_id in dispatchers:
                try:
                    await context.bot.send_photo(
                        chat_id=disp_id,
                        photo=req["photo"],
                        caption=caption
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки фото диспетчеру {disp_id}: {e}")

            # Сохраняем в Google Sheets
            try:
                worksheet = get_worksheet()
                worksheet.append_row([
                    req["created_time"].strftime("%Y-%m-%d"),
                    req["created_time"].strftime("%H:%M:%S"),
                    req["accepted_time"].strftime("%H:%M:%S") if req["accepted_time"] else "",
                    datetime.now().strftime("%H:%M:%S"),
                    req["serial"],
                    req["problem"],
                    req["phone"],
                    req["bus"],
                    req["garage"],
                    update.message.from_user.full_name,
                    req["status"],
                    req["solution"],
                    req["photo"]
                ])
            except Exception as e:
                logger.error(f"Ошибка записи в Google Таблицу: {e}")

            await update.message.reply_text("✅ Заявка завершена. Спасибо!")
            return

async def list_technician_applications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_technician(user_id):
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    message = "📋 Ваши заявки:\n\n"
    has_applications = False
    
    for req_id, req in active_requests.items():
        if req["tech_id"] == user_id:
            has_applications = True
            status = "🟢 Решена" if req["status"] == "Решено" else "🟠 В работе" if req["status"] else "🔴 Не решена"
            message += (
                f"📌 Заявка #{req_id[-4:]} от {req['created_time'].strftime('%d.%m.%Y %H:%M')}\n"
                f"📟 Серийный: {req['serial']}\n"
                f"🔧 Проблема: {req['problem']}\n"
                f"🏢 Автопарк: {req['garage']}\n"
                f"📞 Телефон: {req['phone']}\n"
                f"🚌 Госномер: {req['bus']}\n"
                f"⚙️ Статус: {status}\n\n"
            )
    
    if not has_applications:
        message = "У вас нет заявок."
    
    await update.message.reply_text(message)

async def list_active_applications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_technician(user_id):
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    message = "📋 Ваши активные заявки:\n\n"
    has_active = False
    
    for req_id, req in active_requests.items():
        if req["tech_id"] == user_id and not req["photo"]:
            has_active = True
            status = "🟠 В работе" if req["status"] else "🟡 Ожидает решения"
            message += (
                f"📌 Заявка #{req_id[-4:]} от {req['created_time'].strftime('%d.%m.%Y %H:%M')}\n"
                f"📟 Серийный: {req['serial']}\n"
                f"🔧 Проблема: {req['problem']}\n"
                f"🏢 Автопарк: {req['garage']}\n"
                f"📞 Телефон: {req['phone']}\n"
                f"🚌 Госномер: {req['bus']}\n"
                f"⚙️ Статус: {status}\n\n"
            )
    
    if not has_active:
        message = "У вас нет активных заявок."
    
    await update.message.reply_text(message)

def main():
    # Добавляем админа по умолчанию
    users_roles[ADMIN_ID] = 'admin'

    # Создаем Application с JobQueue
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)  # Разрешаем параллельные обновления
        .build()
    )
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("setdispatcher", set_dispatcher))
    app.add_handler(CommandHandler("settechnic", set_technician))
    app.add_handler(CommandHandler("roles", list_roles))
    app.add_handler(CommandHandler("allmyapplication", list_technician_applications))
    app.add_handler(CommandHandler("activeapplication", list_active_applications))
    
    # Обработчики сообщений
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_response, pattern="^(accept|reject):"))
    app.add_handler(CallbackQueryHandler(handle_status, pattern="^(resolved|unresolved):"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("🤖 Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()