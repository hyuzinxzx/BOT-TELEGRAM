import logging
import os
from datetime import datetime, timedelta, time
import pytz
from functools import wraps
from bson import ObjectId
from flask import Flask
from pymongo import MongoClient
from threading import Thread
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                    ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, error as telegram_error)
from telegram.ext import (Application, CommandHandler, ConversationHandler,
                          MessageHandler, filters, ContextTypes, CallbackQueryHandler)

# --- Configurações Iniciais ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variáveis de Ambiente e Constantes ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
MONGO_URI = os.environ.get('MONGO_URI')
ADMIN_IDS_STR = os.environ.get('ADMIN_IDS', '')
ADMIN_IDS = [int(admin_id) for admin_id in ADMIN_IDS_STR.split(',') if admin_id]
SAO_PAULO_TZ = pytz.timezone("America/Sao_Paulo")

# --- Conexão com o Banco de Dados (MongoDB) ---
try:
    client = MongoClient(MONGO_URI)
    db = client.telegram_bot_db
    schedules_collection = db.schedules
    logger.info("Conexão com MongoDB estabelecida com sucesso.")
except Exception as e:
    logger.error(f"Não foi possível conectar ao MongoDB: {e}")
    client = None; db = None; schedules_collection = None

# --- Estados da Conversa ---
(SELECT_CHANNEL, GET_MEDIA, GET_TEXT, GET_BUTTONS_PROMPT, GET_BUTTON_1_TEXT, GET_BUTTON_1_URL,
 GET_BUTTON_2_PROMPT, GET_BUTTON_2_TEXT, GET_BUTTON_2_URL, GET_PIN_OPTION, GET_SCHEDULE_TIME,
 GET_INTERVAL, GET_REPETITIONS, GET_START_TIME, AWAITING_CONFIRMATION) = range(15)

# --- Decorator de Restrição ---
def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user and update.callback_query:
            user = update.callback_query.from_user
        
        if not user or user.id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer("Acesso Negado!", show_alert=True)
            else:
                await update.message.reply_text("🔒 *Acesso Negado!*", parse_mode='Markdown')
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- Funções do Agendador ---
async def send_post(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    schedule_id = ObjectId(job.data["schedule_id"])
    post = schedules_collection.find_one({"_id": schedule_id})
    if not post:
        logger.warning(f"Post com ID {schedule_id} não encontrado. Removendo job."); job.schedule_next_run_time = None; return
    chat_id = post["chat_id"]; text = post.get("text", ""); media_file_id = post.get("media_file_id"); media_type = post.get("media_type"); buttons_data = post.get("buttons", []); pin_post = post.get("pin_post", False); last_message_id = post.get("last_sent_message_id")
    if pin_post and last_message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=last_message_id)
            logger.info(f"Mensagem anterior {last_message_id} apagada com sucesso no chat {chat_id}.")
        except telegram_error.BadRequest as e: logger.warning(f"Não foi possível apagar a mensagem anterior {last_message_id}: {e}")
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(b['text'], url=b['url'])] for b in buttons_data]) if buttons_data else None
    try:
        sent_message = None; caption_to_send = text if media_type else None; text_to_send = text if not media_type else None
        if media_type == "photo": sent_message = await context.bot.send_photo(chat_id=chat_id, photo=media_file_id, caption=caption_to_send, reply_markup=reply_markup, parse_mode='Markdown')
        elif media_type == "video": sent_message = await context.bot.send_video(chat_id=chat_id, video=media_file_id, caption=caption_to_send, reply_markup=reply_markup, parse_mode='Markdown')
        else: sent_message = await context.bot.send_message(chat_id=chat_id, text=text_to_send, reply_markup=reply_markup, parse_mode='Markdown')
        if pin_post and sent_message:
            try:
                await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent_message.message_id, disable_notification=True)
                schedules_collection.update_one({'_id': schedule_id}, {'$set': {'last_sent_message_id': sent_message.message_id}})
                logger.info(f"Nova mensagem {sent_message.message_id} fixada e ID salvo no DB.")
            except telegram_error.BadRequest as e: logger.error(f"Não foi possível fixar a nova mensagem {sent_message.message_id}: {e}")
        if post["type"] == "agendada": schedules_collection.delete_one({"_id": schedule_id}); logger.info(f"Post agendado {schedule_id} enviado e removido.")
        elif post.get("repetitions") is not None:
            if post["repetitions"] == 1: schedules_collection.delete_one({"_id": schedule_id}); job.schedule_next_run_time = None; logger.info(f"Post recorrente {schedule_id} completou repetições.")
            elif post["repetitions"] != 0: schedules_collection.update_one({"_id": schedule_id}, {"$inc": {"repetitions": -1}})
    except Exception as e: logger.error(f"Erro ao enviar post {schedule_id} para {chat_id}: {e}")

async def reload_jobs_from_db(application: Application):
    if schedules_collection is None: return
    logger.info("--- Recarregando jobs do MongoDB ---"); current_time = datetime.now(SAO_PAULO_TZ); jobs_reloaded = 0; jobs_deleted = 0
    for post in list(schedules_collection.find({})):
        schedule_id_str = str(post['_id'])
        if post['type'] == 'agendada':
            run_date = post.get('scheduled_for')
            if run_date and run_date > current_time:
                application.job_queue.run_once(send_post, run_date, name=schedule_id_str, data={"schedule_id": schedule_id_str}, chat_id=post['chat_id'], user_id=post['user_id']); jobs_reloaded += 1
            elif run_date: schedules_collection.delete_one({'_id': post['_id']}); jobs_deleted += 1
        elif post['type'] == 'recorrente':
            start_date = post.get('start_date')
            if start_date:
                interval_str = post['interval']; unit = interval_str[-1]; value = int(interval_str[:-1])
                interval_kwargs = {'minutes': value} if unit == 'm' else {'hours': value} if unit == 'h' else {'days': value}
                application.job_queue.run_repeating(send_post, interval=timedelta(**interval_kwargs), first=start_date, name=schedule_id_str, data={"schedule_id": schedule_id_str}, chat_id=post['chat_id'], user_id=post['user_id']); jobs_reloaded += 1
    logger.info(f"--- Recarregamento finalizado. {jobs_reloaded} reativados, {jobs_deleted} removidos. ---")

async def weekly_cleanup(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot; logger.info("--- Iniciando limpeza semanal ---"); deleted_by_user = 0; deleted_by_chat = 0
    if schedules_collection is None: return
    try:
        all_user_ids = schedules_collection.distinct("user_id")
        for user_id in all_user_ids:
            try: await bot.get_chat(user_id)
            except telegram_error.BadRequest: result = schedules_collection.delete_many({"user_id": user_id}); deleted_by_user += result.deleted_count
        all_chat_ids = schedules_collection.distinct("chat_id")
        for chat_id in all_chat_ids:
            try: await bot.get_chat(chat_id)
            except (telegram_error.BadRequest, telegram_error.Forbidden): result = schedules_collection.delete_many({"chat_id": chat_id}); deleted_by_chat += result.deleted_count
    except Exception as e: logger.error(f"Erro na limpeza semanal: {e}")
    logger.info(f"--- Limpeza finalizada. Removidos por usuário: {deleted_by_user}. Removidos por chat: {deleted_by_chat}. ---")

# --- Funções do Menu ---
async def build_main_menu_keyboard():
    keyboard = [[InlineKeyboardButton("🆕 Agendar Postagem", callback_data='start_schedule_single')], [InlineKeyboardButton("🔁 Agendar Recorrente", callback_data='start_schedule_recurrent')], [InlineKeyboardButton("📋 Listar Agendamentos", callback_data='menu_listar')], [InlineKeyboardButton("📝 Editar (Ajuda)", callback_data='menu_editar_ajuda')]]
    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str = "👇 Escolha uma opção:"):
    reply_markup = await build_main_menu_keyboard()
    if update.callback_query:
        try: await update.callback_query.edit_message_text(text=message_text, reply_markup=reply_markup, parse_mode='Markdown')
        except telegram_error.BadRequest as e:
            if "Message is not modified" not in str(e): logger.warning(f"Erro ao editar msg do menu: {e}")
    else: await update.message.reply_text(text=message_text, reply_markup=reply_markup, parse_mode='Markdown')

# --- Handlers ---
@restricted
async def handle_simple_menu_clicks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    if query.data == 'menu_listar':
        await list_posts(update, context)
        # Não reenviamos o menu aqui para a lista ficar visível
    elif query.data == 'menu_editar_ajuda':
        await query.message.reply_text("Para editar, use: `/editar <ID>`", parse_mode='Markdown')

@restricted
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name; welcome_message = (f"Olá, {user_name}! Eu sou o **BAPD** 😁")
    await update.message.reply_text(welcome_message, parse_mode='Markdown'); await show_main_menu(update, context)

@restricted
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: await show_main_menu(update, context)

# ... (Todo o ConversationHandler de criação de post)

# --- ✅ FUNÇÃO DE LISTAGEM ATUALIZADA ---
@restricted
async def list_posts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_source = update.callback_query.message if update.callback_query else update.message
    message = "📅 *Suas Postagens Agendadas*\n\n"; found_any = False
    if schedules_collection is not None:
        for post in schedules_collection.find({"user_id": update.effective_user.id}).sort("created_at", -1):
            found_any = True; post_type = "Agendada" if post['type'] == 'agendada' else "Recorrente"; 
            text_snippet = (post.get('text') or "Sem texto")[:50]
            if len(post.get('text', '')) > 50: text_snippet += "..."

            message += f"🆔 `{post['_id']}`\n"
            message += f"🎯 `Alvo`: {post['chat_id']}\n"
            message += f"🔄 `Tipo`: {post_type}\n"
            
            if post['type'] == 'recorrente':
                interval = post.get('interval', 'N/D')
                repetitions_val = post.get('repetitions', 'N/D')
                repetitions_text = "Infinitas" if repetitions_val == 0 else repetitions_val
                start_date_aware = post.get('start_date')
                start_date_str = start_date_aware.strftime('%d/%m/%Y às %H:%M') if start_date_aware else "N/D"
                message += f"⏳ `Intervalo`: A cada {interval}\n"
                message += f"🔁 `Repetições`: {repetitions_text}\n"
                message += f"▶️ `Início`: {start_date_str}\n"
            else: # Agendada
                scheduled_for_aware = post.get('scheduled_for')
                scheduled_for_str = scheduled_for_aware.strftime('%d/%m/%Y às %H:%M') if scheduled_for_aware else "N/D"
                message += f"🗓️ `Data de Envio`: {scheduled_for_str}\n"

            message += f"📝 `Texto`: _{text_snippet}_\n\n"
            
    if not found_any: message = "Você ainda não tem postagens agendadas."

    # Se foi chamado por um botão, edita a mensagem do menu. Se foi por comando, envia uma nova.
    if update.callback_query:
        await message_source.edit_text(message, parse_mode='Markdown')
    else:
        await message_source.reply_text(message, parse_mode='Markdown')

# (O resto do código, como /cancelar, a função main(), etc. permanece o mesmo)
# ...
