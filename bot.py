import os
import logging
import csv
import threading
from datetime import datetime, timedelta
from typing import Dict, List
import asyncio
import time
import base64   

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot_actions.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Константы
BOT_TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_NAME = "Telegram zayavki"
REMINDER_INTERVAL = 300  # 5 минут в секундах
ADMIN_IDS = [1132625886, 886922044]  # ID админов

# Состояния для ConversationHandler
ENTERING_SOLUTION, ENTERING_PHOTO = range(2)

# Инициализация глобальных переменных
users_roles = {}  # {user_id: 'admin'/'dispatcher'/'technician'}
applications = {}  # {application_id: application_data}
current_applications = {}  # {technician_id: application_id}s
application_counter = 0
pending_notifications = {}  # {application_id: timer_thread}
statistics = {
    'total_applications': 0,
    'resolved_applications': 0,
    'avg_resolution_time': 0,
    'technician_stats': {},
    'dispatcher_stats': {}
}

# Инициализация Google Sheets
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds_json = base64.b64decode(os.getenv("GOOGLE_CREDENTIALS")).decode()
creds = ServiceAccountCredentials.from_json_keyfile_dict(eval(creds_json), scope)



def get_worksheet():
    try:
        return gs_client.open(SPREADSHEET_NAME).sheet1
    except gspread.SpreadsheetNotFound:
        worksheet = gs_client.create(SPREADSHEET_NAME)
        return worksheet.sheet1

def log_action(user_id: int, action: str, details: str = "") -> None:
    """Логирование действий пользователей"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with open('user_actions.csv', 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([timestamp, user_id, action, details])
    except Exception as e:
        logger.error(f"Ошибка при записи лога: {e}")

def start_notification_timer(app_id: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запускает таймер для уведомления о непринятой заявке"""
    def notify_technicians():
        time.sleep(REMINDER_INTERVAL)
        if app_id in applications and applications[app_id]['status'] == 'active':
            technicians = [uid for uid, role in users_roles.items() if role == 'technician']
            for tech_id in technicians:
                try:
                    context.bot.send_message(
                        chat_id=tech_id,
                        text=f"⚠️ Заявка №{app_id} все еще ожидает принятия!\n"
                             f"Проблема: {applications[app_id]['problem']}"
                    )
                    log_action(tech_id, 'reminder_sent', f'application_{app_id}')
                except Exception as e:
                    logger.error(f"Не удалось отправить напоминание технику {tech_id}: {e}")
    
    thread = threading.Thread(target=notify_technicians)
    thread.start()
    pending_notifications[app_id] = thread

def update_statistics(app_id: str, action: str) -> None:
    """Обновляет статистику на основе действий с заявками"""
    app = applications[app_id]
    
    if action == 'created':
        statistics['total_applications'] += 1
        
        # Обновляем статистику диспетчера
        dispatcher_id = app['dispatcher_id']
        if dispatcher_id not in statistics['dispatcher_stats']:
            statistics['dispatcher_stats'][dispatcher_id] = {'created': 0}
        statistics['dispatcher_stats'][dispatcher_id]['created'] += 1
    
    elif action == 'resolved':
        statistics['resolved_applications'] += 1
        
        # Рассчитываем время решения
        created_time = datetime.strptime(app['created_time'], '%Y-%m-%d %H:%M:%S')
        resolved_time = datetime.strptime(app['resolved_time'], '%Y-%m-%d %H:%M:%S')
        resolution_time = (resolved_time - created_time).total_seconds() / 60  # в минутах
        
        # Обновляем общее среднее время
        total_time = statistics['avg_resolution_time'] * (statistics['resolved_applications'] - 1)
        statistics['avg_resolution_time'] = (total_time + resolution_time) / statistics['resolved_applications']
        
        # Обновляем статистику техника
        technician_id = app['technician_id']
        if technician_id not in statistics['technician_stats']:
            statistics['technician_stats'][technician_id] = {'resolved': 0, 'avg_time': 0}
        
        tech_stats = statistics['technician_stats'][technician_id]
        total_tech_time = tech_stats['avg_time'] * tech_stats['resolved']
        tech_stats['resolved'] += 1
        tech_stats['avg_time'] = (total_tech_time + resolution_time) / tech_stats['resolved']

def generate_report() -> str:
    """Генерирует текстовый отчет со статистикой"""
    report = "📊 Статистика работы системы:\n\n"
    
    report += f"Всего заявок: {statistics['total_applications']}\n"
    if statistics['total_applications'] > 0:
        report += f"Решено заявок: {statistics['resolved_applications']} ({statistics['resolved_applications']/statistics['total_applications']*100:.1f}%)\n"
    else:
        report += "Решено заявок: 0 (0%)\n"
    
    report += f"Среднее время решения: {statistics['avg_resolution_time']:.1f} мин.\n\n"
    
    report += "📌 Статистика диспетчеров:\n"
    for disp_id, stats in statistics['dispatcher_stats'].items():
        report += f"- ID {disp_id}: создано {stats['created']} заявок\n"
    
    report += "\n🔧 Статистика техников:\n"
    for tech_id, stats in statistics['technician_stats'].items():
        report += f"- ID {tech_id}: решено {stats['resolved']} заявок, среднее время {stats['avg_time']:.1f} мин.\n"
    
    return report

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS and user_id not in users_roles:
        users_roles[user_id] = 'admin'
        log_action(user_id, 'admin_login')
        await update.message.reply_text('Вы авторизованы как Админ. Используйте /help для списка команд.')
    elif user_id in users_roles:
        role = users_roles[user_id]
        log_action(user_id, 'user_login')
        await update.message.reply_text(f'Вы авторизованы как {role.capitalize()}. Используйте /help для списка команд.')
    else:
        log_action(user_id, 'unauthorized_login_attempt')
        await update.message.reply_text('Вы не авторизованы. Обратитесь к администратору.')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_roles:
        await update.message.reply_text('Вы не авторизованы.')
        return
    
    role = users_roles[user_id]
    
    if role == 'admin':
        text = """
Команды админа:
/setdispatcher - Добавить диспетчера
/settechnic - Добавить техника
/removetechnic - удалить техника 
/removedispatcher - удалить диспетчера
/roles - все роли с именами
/activeapplications - активные заявки
/allapplication - все заявки за день
/report - статистика работы
/exportlogs - экспорт логов действий
        """
    elif role == 'dispatcher':
        text = """
Команды диспетчера:
/activeapplications - активные заявки
/allapplication - все заявки за день
/report - статистика работы
        """
    elif role == 'technician':
        text = """
Команды техника:
/myapplications - все выполненные заявки техника
/activeapplication - текущая заявка
        """
    
    await update.message.reply_text(text)
    log_action(user_id, 'help_requested')

async def set_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS and users_roles.get(user_id) != 'admin':
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    try:
        new_dispatcher_id = int(context.args[0])
        users_roles[new_dispatcher_id] = 'dispatcher'
        await update.message.reply_text(f"✅ Пользователь {new_dispatcher_id} назначен диспетчером.")
        log_action(user_id, 'set_dispatcher', f'user_{new_dispatcher_id}')
        
        try:
            await context.bot.send_message(
                chat_id=new_dispatcher_id,
                text="Вас назначили диспетчером! Используйте /help для списка команд."
            )
        except Exception as e:
            await update.message.reply_text(f"Не удалось уведомить пользователя {new_dispatcher_id}.")
            logger.error(f"Ошибка уведомления нового диспетчера: {e}")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /setdispatcher <user_id>")

async def set_technician(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS and users_roles.get(user_id) != 'admin':
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    try:
        new_technician_id = int(context.args[0])
        users_roles[new_technician_id] = 'technician'
        await update.message.reply_text(f"✅ Пользователь {new_technician_id} назначен техником.")
        log_action(user_id, 'set_technician', f'user_{new_technician_id}')
        
        try:
            await context.bot.send_message(
                chat_id=new_technician_id,
                text="Вас назначили техником! Используйте /help для списка команд."
            )
        except Exception as e:
            await update.message.reply_text(f"Не удалось уведомить пользователя {new_technician_id}.")
            logger.error(f"Ошибка уведомления нового техника: {e}")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /settechnic <user_id>")

async def remove_technician(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS and users_roles.get(user_id) != 'admin':
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    try:
        technician_id = int(context.args[0])
        if users_roles.get(technician_id) == 'technician':
            del users_roles[technician_id]
            await update.message.reply_text(f"✅ Пользователь {technician_id} больше не техник.")
            log_action(user_id, 'remove_technician', f'user_{technician_id}')
        else:
            await update.message.reply_text("Этот пользователь не является техником.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /removetechnic <user_id>")

async def remove_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS and users_roles.get(user_id) != 'admin':
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    try:
        dispatcher_id = int(context.args[0])
        if users_roles.get(dispatcher_id) == 'dispatcher':
            del users_roles[dispatcher_id]
            await update.message.reply_text(f"✅ Пользователь {dispatcher_id} больше не диспетчер.")
            log_action(user_id, 'remove_dispatcher', f'user_{dispatcher_id}')
        else:
            await update.message.reply_text("Этот пользователь не является диспетчером.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /removedispatcher <user_id>")

async def list_roles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS and users_roles.get(user_id) != 'admin':
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
    log_action(user_id, 'list_roles_viewed')

async def active_applications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_roles:
        await update.message.reply_text('Вы не авторизованы.')
        return
    
    active_apps = [app for app in applications.values() if app['status'] == 'active']
    
    if not active_apps:
        await update.message.reply_text('Нет активных заявок.')
        return
    
    text = "Активные заявки:\n\n"
    for app in active_apps:
        text += f"Заявка №{app['id']}\n"
        text += f"Сер. номер: {app['serial']}\n"
        text += f"Гос. номер: {app['bus']}\n"
        text += f"Автопарк: {app['garage']}\n"
        text += f"Водитель: {app['phone']}\n"
        text += f"Проблема: {app['problem']}\n"
        text += f"Время создания: {app['created_time']}\n\n"
    
    await update.message.reply_text(text)
    log_action(user_id, 'viewed_active_applications')

async def all_applications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_roles:
        await update.message.reply_text('Вы не авторизованы.')
        return
    
    today = datetime.now().strftime('%Y-%m-%d')
    today_apps = [app for app in applications.values() if app['created_time'].startswith(today)]
    
    if not today_apps:
        await update.message.reply_text('Сегодня не было заявок.')
        return
    
    text = f"Все заявки за {today}:\n\n"
    for app in today_apps:
        status = "Активная" if app['status'] == 'active' else "Решена"
        text += f"Заявка №{app['id']} ({status})\n"
        text += f"Сер. номер: {app['serial']}\n"
        text += f"Гос. номер: {app['bus']}\n"
        text += f"Автопарк: {app['garage']}\n"
        text += f"Водитель: {app['phone']}\n"
        text += f"Проблема: {app['problem']}\n"
        if app['status'] == 'resolved':
            text += f"Решение: {app['solution']}\n"
            text += f"Техник: {app['technician_name']}\n"
            text += f"Время решения: {app['resolved_time']}\n"
        text += "\n"
    
    await update.message.reply_text(text)
    log_action(user_id, 'viewed_all_applications')

async def my_applications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_roles or users_roles[user_id] != 'technician':
        await update.message.reply_text('Эта команда только для техников.')
        return
    
    my_apps = [app for app in applications.values() if app.get('technician_id') == user_id and app['status'] == 'resolved']
    
    if not my_apps:
        await update.message.reply_text('У вас нет выполненных заявок.')
        return
    
    text = "Ваши выполненные заявки:\n\n"
    for app in my_apps:
        text += f"Заявка №{app['id']}\n"
        text += f"Сер. номер: {app['serial']}\n"
        text += f"Гос. номер: {app['bus']}\n"
        text += f"Автопарк: {app['garage']}\n"
        text += f"Водитель: {app['phone']}\n"
        text += f"Проблема: {app['problem']}\n"
        text += f"Решение: {app['solution']}\n"
        text += f"Время решения: {app['resolved_time']}\n\n"
    
    await update.message.reply_text(text)
    log_action(user_id, 'viewed_my_applications')

async def current_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_roles or users_roles[user_id] != 'technician':
        await update.message.reply_text('Эта команда только для техников.')
        return
    
    if user_id not in current_applications:
        await update.message.reply_text('У вас нет текущей заявки.')
        return
    
    app_id = current_applications[user_id]
    app = applications[app_id]
    
    text = f"Текущая заявка №{app['id']}:\n"
    text += f"Сер. номер: {app['serial']}\n"
    text += f"Гос. номер: {app['bus']}\n"
    text += f"Автопарк: {app['garage']}\n"
    text += f"Водитель: {app['phone']}\n"
    text += f"Проблема: {app['problem']}\n"
    text += f"Время создания: {app['created_time']}\n"
    
    await update.message.reply_text(text)
    log_action(user_id, 'viewed_current_application', f'application_{app_id}')

async def handle_dispatcher_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_roles or users_roles[user_id] != 'dispatcher':
        await update.message.reply_text('Вы не диспетчер.')
        return
    
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

    global application_counter
    application_counter += 1
    app_id = str(application_counter)
    
    applications[app_id] = {
        "id": app_id,
        "serial": data["серийный номер"],
        "problem": data["проблема"],
        "phone": data["телефон водителя"],
        "bus": data["госномер"],
        "garage": data["автопарк"],
        "status": "active",
        "created_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "dispatcher_id": user_id,
        "dispatcher_name": update.effective_user.full_name,
        "technician_id": None,
        "technician_name": None,
        "solution": None,
        "photo": None,
        "resolved_time": None
    }
    
    log_action(user_id, 'application_created', f'application_{app_id}')
    update_statistics(app_id, 'created')
    
    text_message = (
        f"📥 *Новая заявка #{app_id}*\n"
        f"📟 Серийный номер: {data['серийный номер']}\n"
        f"🔧 Проблема: {data['проблема']}\n"
        f"📞 Телефон водителя: {data['телефон водителя']}\n"
        f"🚌 Госномер: {data['госномер']}\n"
        f"🏢 Автопарк: {data['автопарк']}")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять", callback_data=f"accept:{app_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{app_id}")
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
        await update.message.reply_text(f"✅ Заявка #{app_id} успешно отправлена техникам!")
    else:
        await update.message.reply_text("❌ Не удалось отправить заявку техникам. Нет доступных техников.")

    # Настраиваем напоминание
    if technicians:
        start_notification_timer(app_id, context)

async def handle_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id not in users_roles or users_roles[user_id] != 'technician':
        await query.edit_message_text("❌ Только техники могут принимать заявки.")
        return
    
    action, app_id = query.data.split(":")
    if app_id not in applications or applications[app_id]['status'] != 'active':
        await query.edit_message_text("❌ Заявка уже обработана.")
        return
    
    applications[app_id]['status'] = 'in_progress'
    applications[app_id]['technician_id'] = user_id
    applications[app_id]['technician_name'] = query.from_user.full_name
    current_applications[user_id] = app_id
    
    # Отменяем напоминание
    if app_id in pending_notifications:
        pending_notifications[app_id].join()
        del pending_notifications[app_id]
    
    log_action(user_id, 'application_accepted', f'application_{app_id}')
    
    if action == "accept":
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🟢 Решено", callback_data=f"resolved:{app_id}"),
            InlineKeyboardButton("🔴 Не решено", callback_data=f"unresolved:{app_id}")
        ]])
        await query.edit_message_text("✅ Вы приняли заявку. Укажи статус:", reply_markup=keyboard)
        
        # Уведомляем диспетчеров
        dispatchers = [uid for uid, role in users_roles.items() if role == 'dispatcher']
        for disp_id in dispatchers:
            try:
                await context.bot.send_message(
                    disp_id,
                    f"☑️ Заявку #{app_id} принял: {query.from_user.full_name}"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления диспетчера {disp_id}: {e}")
        
        # Уведомляем других техников
        technicians = [uid for uid, role in users_roles.items() if role == 'technician' and uid != user_id]
        for tech_id in technicians:
            try:
                await context.bot.send_message(
                    tech_id,
                    f"ℹ️ Заявку #{app_id} уже принял другой техник: {query.from_user.full_name}"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления техника {tech_id}: {e}")

    elif action == "reject":
        await query.edit_message_text("🔕 Вы отклонили заявку.")
        log_action(user_id, 'application_rejected', f'application_{app_id}')
        
        # Уведомляем диспетчеров об отказе
        dispatchers = [uid for uid, role in users_roles.items() if role == 'dispatcher']
        for disp_id in dispatchers:
            try:
                await context.bot.send_message(
                    disp_id,
                    f"❌ Заявку #{app_id} отклонил: {query.from_user.full_name}"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления диспетчера {disp_id}: {e}")

async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    action, app_id = query.data.split(":")
    if app_id not in applications or applications[app_id]['technician_id'] != user_id:
        await query.edit_message_text("❌ Вы не назначены на эту заявку.")
        return
    
    applications[app_id]['status'] = "Решено" if action == "resolved" else "Не решено"
    await query.edit_message_text("✍️ Опиши, как ты решил проблему:")
    return ENTERING_SOLUTION

async def enter_solution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in current_applications:
        await update.message.reply_text('У вас нет активной заявки.')
        return ConversationHandler.END
    
    app_id = current_applications[user_id]
    applications[app_id]['solution'] = update.message.text
    log_action(user_id, 'solution_entered', f'application_{app_id}')
    
    await update.message.reply_text('📸 Теперь отправьте фото как подтверждение.')
    return ENTERING_PHOTO

async def enter_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in current_applications:
        await update.message.reply_text('У вас нет активной заявки.')
        return ConversationHandler.END
    
    app_id = current_applications[user_id]
    application = applications[app_id]
    
    # Получаем фото
    photo_file = await update.message.photo[-1].get_file()
    photo_path = f'photos/application_{app_id}.jpg'
    os.makedirs('photos', exist_ok=True)
    await photo_file.download_to_drive(photo_path)
    
    # Обновляем статус заявки
    application['status'] = 'resolved'
    application['resolved_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    application['photo'] = photo_path
    
    log_action(user_id, 'photo_uploaded', f'application_{app_id}')
    update_statistics(app_id, 'resolved')
    
    # Удаляем заявку из текущих
    del current_applications[user_id]
    
    # Уведомляем диспетчеров
    caption = (
        f"📄 Заявка #{app_id} выполнена\n"
        f"🧑‍🔧 Техник: {update.message.from_user.full_name}\n"
        f"📟 Серийный: {application['serial']}\n"
        f"📞 Телефон водителя: {application['phone']}\n"
        f"🚌 Госномер: {application['bus']}\n"
        f"🏢 Автопарк: {application['garage']}\n"
        f"📆 Дата: {application['resolved_time']}\n"
        f"⚙️ Статус: {application['status']}\n"
        f"📝 Решение: {application['solution']}")

    dispatchers = [uid for uid, role in users_roles.items() if role == 'dispatcher']
    for disp_id in dispatchers:
        try:
            await context.bot.send_photo(
                chat_id=disp_id,
                photo=open(photo_path, 'rb'),
                caption=caption
            )
        except Exception as e:
            logger.error(f"Ошибка отправки фото диспетчеру {disp_id}: {e}")

    # Сохраняем в Google Sheets
    try:
        worksheet = get_worksheet()
        worksheet.append_row([
            application['created_time'],
            application['serial'],
            application['bus'],
            application['garage'],
            application['phone'],
            application['problem'],
            application['status'],
            application['solution'],
            application['resolved_time'],
            application['dispatcher_name'],
            application['technician_name'],
            photo_path
        ])
    except Exception as e:
        logger.error(f"Ошибка записи в Google Таблицу: {e}")
        await context.bot.send_message(
            chat_id=ADMIN_IDS[0],
            text=f"Ошибка при записи заявки №{app_id} в Google Sheets: {e}"
        )

    await update.message.reply_text("✅ Заявка завершена. Спасибо!")
    return ConversationHandler.END

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_roles or users_roles[user_id] not in ['admin', 'dispatcher']:
        await update.message.reply_text('Эта команда только для админа и диспетчеров.')
        return
    
    report = generate_report()
    await update.message.reply_text(report)
    log_action(user_id, 'report_generated')

async def export_logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text('Эта команда только для админа.')
        return
    
    try:
        with open('user_actions.csv', 'rb') as f:
            await context.bot.send_document(
                chat_id=update.effective_user.id,
                document=f,
                filename=f'actions_log_{datetime.now().date()}.csv'
            )
        log_action(update.effective_user.id, 'logs_exported')
    except Exception as e:
        await update.message.reply_text(f'Ошибка при экспорте логов: {e}')
        logger.error(f"Ошибка при экспорте логов: {e}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    log_action(user_id, 'operation_cancelled')
    await update.message.reply_text('Действие отменено.')
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Исключение при обработке обновления:", exc_info=context.error)
    
    if update and update.effective_user:
        log_action(update.effective_user.id if update else 'system', 'error_occurred', str(context.error))
        
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"Произошла ошибка при обработке сообщения от {update.effective_user.id}: {context.error}"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение об ошибке админу {admin_id}: {e}")

def main():
    # Создаем папки для хранения данных
    os.makedirs('photos', exist_ok=True)
    os.makedirs('logs', exist_ok=True)
    
    # Инициализируем файл логов
    if not os.path.exists('user_actions.csv'):
        with open('user_actions.csv', 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp', 'user_id', 'action', 'details'])
    
    # Добавляем админов по умолчанию
    for admin_id in ADMIN_IDS:
        users_roles[admin_id] = 'admin'

    # Создаем Application
    app = Application.builder().token(BOT_TOKEN).build()

    # Обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("setdispatcher", set_dispatcher))
    app.add_handler(CommandHandler("settechnic", set_technician))
    app.add_handler(CommandHandler("removetechnic", remove_technician))
    app.add_handler(CommandHandler("removedispatcher", remove_dispatcher))
    app.add_handler(CommandHandler("roles", list_roles))
    app.add_handler(CommandHandler("activeapplications", active_applications))
    app.add_handler(CommandHandler("allapplication", all_applications))
    app.add_handler(CommandHandler("myapplications", my_applications))
    app.add_handler(CommandHandler("activeapplication", current_application))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("exportlogs", export_logs_command))

    # Обработчик сообщений от диспетчеров
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(users_roles.get('dispatcher', [])), handle_dispatcher_message))

    # Обработчик кнопок для техников
    app.add_handler(CallbackQueryHandler(handle_response, pattern="^(accept|reject):"))

    # Обработчик решения заявок техниками
    tech_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_status, pattern="^(resolved|unresolved):")],
        states={
            ENTERING_SOLUTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_solution)],
            ENTERING_PHOTO: [MessageHandler(filters.PHOTO, enter_photo)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    app.add_handler(tech_conv_handler)

    # Обработчик ошибок
    app.add_error_handler(error_handler)

    logger.info("🤖 Бот запущен!")

     # Для Railway (чтобы не крашилось из-за отсутствия веб-сервера)
    if "RAILWAY_ENVIRONMENT" in os.environ:
        import threading
        from flask import Flask
        
        # Создаем Flask-приложение в отдельном потоке
        web = Flask(__name__)
        
        @web.route("/")
        def home():
            return "Telegram Bot is running!"
        
        def run_flask():
            web.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
        
        flask_thread = threading.Thread(target=run_flask)
        flask_thread.daemon = True
        flask_thread.start()


    app.run_polling()

if __name__ == "__main__":
    main()