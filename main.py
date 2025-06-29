# -*- coding: utf-8 -*-
import logging
import os
from datetime import datetime, timedelta, time
import pytz
from functools import wraps

import firebase_admin
from firebase_admin import credentials, firestore
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, error as telegram_error)
from telegram.ext import (Application, CommandHandler, ConversationHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler)

# --- Configurações ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variáveis de Ambiente e Constantes ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
ADMIN_IDS_STR = os.environ.get('ADMIN_IDS', '')
ADMIN_IDS = [int(admin_id) for admin_id in ADMIN_IDS_STR.split(',') if admin_id]
SAO_PAULO_TZ = pytz.timezone("America/Sao_Paulo")

# --- Conexão com Firebase ---
try:
    cred = credentials.Certificate("credentials.json")
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("✅ Conexão com Firebase (Firestore) estabelecida.")
except Exception as e:
    logger.error(f"CRÍTICO: Falha ao conectar ao Firebase: {e}")
    db = None

# --- Estados da Conversa ---
(AWAITING_CHANNEL, AWAITING_MEDIA, AWAITING_TEXT, AWAITING_BUTTON_PROMPT, 
 AWAITING_BUTTON_TEXT, AWAITING_BUTTON_URL, AWAITING_PIN_OPTION, AWAITING_SCHEDULE_TIME,
 AWAITING_INTERVAL, AWAITING_REPETITIONS, AWAITING_START_TIME, AWAITING_CONFIRMATION) = range(12)

# --- Decorator de Restrição ---
def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            logger.warning(f"Acesso negado para user ID: {user_id}")
            if update.callback_query:
                await update.callback_query.answer("Acesso Negado!", show_alert=True)
            else:
                await update.message.reply_text("🔒 Acesso Negado!", parse_mode='Markdown')
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- Funções do Agendador (Scheduler) ---
async def send_post(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    schedule_id = job.data["schedule_id"]
    doc_ref = db.collection('schedules').document(schedule_id)
    post_doc = doc_ref.get()

    if not post_doc.exists:
        logger.warning(f"Post {schedule_id} não encontrado. Removendo job.")
        job.schedule_next_run_time = None
        return
    
    post = post_doc.to_dict()
    # ... (Lógica de envio da mensagem, igual à anterior) ...
    try:
        # (Sua lógica de envio de foto, vídeo ou texto aqui...)
        if post.get("type") == "agendada":
            doc_ref.delete()
        elif post.get("repetitions") is not None:
            if post["repetitions"] == 1: doc_ref.delete()
            elif post["repetitions"] != 0: doc_ref.update({"repetitions": firestore.Increment(-1)})
    except Exception as e:
        logger.error(f"Falha ao enviar post {schedule_id}: {e}")

async def reload_jobs_from_db(application: Application):
    if db is None: return
    logger.info("--- Recarregando jobs do Firestore ---")
    # ... (Sua lógica de reload, igual à anterior) ...

# --- Lógica do ConversationHandler ---
@restricted
async def start_schedule_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    schedule_type = query.data.split('_')[-1] # single or recurrent
    context.user_data.clear()
    context.user_data['type'] = 'agendada' if schedule_type == 'single' else 'recorrente'
    await query.edit_message_text("Ok, vamos criar um agendamento. Primeiro, envie o ID ou @username do canal de destino.")
    return AWAITING_CHANNEL

@restricted
async def get_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['chat_id'] = update.message.text
    await update.message.reply_text("Canal salvo. Agora envie a foto, vídeo ou digite /pular para enviar só texto.")
    return AWAITING_MEDIA

@restricted
async def get_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    if message.photo:
        context.user_data['media_file_id'] = message.photo[-1].file_id
        context.user_data['media_type'] = 'photo'
    elif message.video:
        context.user_data['media_file_id'] = message.video.file_id
        context.user_data['media_type'] = 'video'
    await update.message.reply_text("Mídia salva. Agora, digite o texto da postagem. Use formatação Markdown se desejar.")
    return AWAITING_TEXT

@restricted
async def skip_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ok, sem mídia. Agora, digite o texto da postagem. Use formatação Markdown se desejar.")
    return AWAITING_TEXT

@restricted
async def get_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['text'] = update.message.text
    reply_keyboard = [["Sim"], ["Não"]]
    await update.message.reply_text(
        "Texto salvo. Deseja adicionar um botão de URL?",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True),
    )
    return AWAITING_BUTTON_PROMPT

@restricted
async def get_button_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.lower() == 'sim':
        await update.message.reply_text("Ok, envie o texto do botão.", reply_markup=ReplyKeyboardRemove())
        return AWAITING_BUTTON_TEXT
    else:
        await update.message.reply_text("Ok, sem botões. Deseja fixar esta mensagem no canal?", reply_markup=ReplyKeyboardMarkup([["Sim"], ["Não"]], one_time_keyboard=True))
        return AWAITING_PIN_OPTION

@restricted
async def get_button_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.setdefault('buttons', []).append({'text': update.message.text})
    await update.message.reply_text("Texto do botão salvo. Agora envie a URL completa (ex: https://google.com).")
    return AWAITING_BUTTON_URL

@restricted
async def get_button_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['buttons'][-1]['url'] = update.message.text
    # Aqui você poderia adicionar lógica para mais botões, mas vamos simplificar
    await update.message.reply_text("Botão salvo. Deseja fixar a postagem no canal?", reply_markup=ReplyKeyboardMarkup([["Sim"], ["Não"]], one_time_keyboard=True))
    return AWAITING_PIN_OPTION

@restricted
async def get_pin_option(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['pin_post'] = (update.message.text.lower() == 'sim')
    
    if context.user_data['type'] == 'agendada':
        await update.message.reply_text("Entendido. Agora envie a data e hora do agendamento no formato: DD/MM/AAAA HH:MM", reply_markup=ReplyKeyboardRemove())
        return AWAITING_SCHEDULE_TIME
    else: # recorrente
        await update.message.reply_text("Entendido. Agora defina o intervalo. Ex: 30m, 12h, 1d (minutos, horas, dias).", reply_markup=ReplyKeyboardRemove())
        return AWAITING_INTERVAL

@restricted
async def get_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        dt_obj = datetime.strptime(update.message.text, '%d/%m/%Y %H:%M')
        context.user_data['scheduled_for'] = SAO_PAULO_TZ.localize(dt_obj)
        await confirm_schedule(update, context)
        return AWAITING_CONFIRMATION
    except ValueError:
        await update.message.reply_text("Formato inválido. Tente novamente: DD/MM/AAAA HH:MM")
        return AWAITING_SCHEDULE_TIME

@restricted
async def get_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['interval'] = update.message.text
    await update.message.reply_text("Intervalo salvo. Quantas vezes deve repetir? (Digite 0 para infinito)")
    return AWAITING_REPETITIONS

@restricted
async def get_repetitions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['repetitions'] = int(update.message.text)
    await update.message.reply_text("Repetições salvas. Qual a data e hora de início? (DD/MM/AAAA HH:MM)")
    return AWAITING_START_TIME

@restricted
async def get_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        dt_obj = datetime.strptime(update.message.text, '%d/%m/%Y %H:%M')
        context.user_data['start_date'] = SAO_PAULO_TZ.localize(dt_obj)
        await confirm_schedule(update, context)
        return AWAITING_CONFIRMATION
    except ValueError:
        await update.message.reply_text("Formato inválido. Tente novamente: DD/MM/AAAA HH:MM")
        return AWAITING_START_TIME

async def confirm_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Monta uma mensagem de resumo para o usuário confirmar
    summary = "📋 *Resumo do Agendamento*\n\n"
    # ... (crie uma mensagem de resumo bonita com os dados de context.user_data)
    await update.message.reply_text(
        summary + "\n\nConfirma o agendamento?",
        reply_markup=ReplyKeyboardMarkup([["✅ Confirmar"], ["❌ Cancelar"]], one_time_keyboard=True),
        parse_mode='Markdown'
    )

@restricted
async def save_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        user_data = context.user_data
        user_data['created_at'] = firestore.SERVER_TIMESTAMP
        user_data['user_id'] = update.effective_user.id

        update_time, doc_ref = db.collection('schedules').add(user_data)
        logger.info(f"Novo agendamento salvo com ID: {doc_ref.id}")

        schedule_id = doc_ref.id
        post_data = {"schedule_id": schedule_id}

        if user_data['type'] == 'agendada':
            context.application.job_queue.run_once(send_post, user_data['scheduled_for'], data=post_data, name=schedule_id)
        else:
            # ... Lógica para agendar job recorrente ...
            pass

        await update.message.reply_text("✅ Agendamento criado com sucesso!", reply_markup=ReplyKeyboardRemove())
        await show_main_menu(update, context)

    except Exception as e:
        logger.error(f"Erro ao salvar agendamento: {e}")
        await update.message.reply_text("❌ Ocorreu um erro ao salvar. Tente novamente.", reply_markup=ReplyKeyboardRemove())
    
    context.user_data.clear()
    return ConversationHandler.END

@restricted
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Operação cancelada.", reply_markup=ReplyKeyboardRemove())
    await show_main_menu(update, context)
    return ConversationHandler.END

# --- Funções de Menu ---
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🆕 Agendar Postagem", callback_data='start_schedule_single')],
        [InlineKeyboardButton("🔁 Agendar Recorrente", callback_data='start_schedule_recurrent')],
        [InlineKeyboardButton("📋 Listar Agendamentos", callback_data='menu_listar')],
    ]
    # ... (resto da função igual à anterior)

# ... (outras funções como start_command, list_posts) ...

def main() -> None:
    if not all([TELEGRAM_TOKEN, db, ADMIN_IDS]):
        logger.error("FATAL: Variáveis de ambiente ou conexão com DB ausentes.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_schedule_flow, pattern='^start_schedule_')
        ],
        states={
            AWAITING_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel)],
            AWAITING_MEDIA: [
                MessageHandler(filters.PHOTO | filters.VIDEO, get_media),
                CommandHandler('pular', skip_media)
            ],
            AWAITING_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_text)],
            AWAITING_BUTTON_PROMPT: [MessageHandler(filters.Regex('^(Sim|Não)$'), get_button_prompt)],
            AWAITING_BUTTON_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_button_text)],
            AWAITING_BUTTON_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_button_url)],
            AWAITING_PIN_OPTION: [MessageHandler(filters.Regex('^(Sim|Não)$'), get_pin_option)],
            AWAITING_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_schedule_time)],
            AWAITING_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_interval)],
            AWAITING_REPETITIONS: [MessageHandler(filters.Regex(r'^\d+$'), get_repetitions)],
            AWAITING_START_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_start_time)],
            AWAITING_CONFIRMATION: [MessageHandler(filters.Regex('^✅ Confirmar$'), save_schedule)],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex('^❌ Cancelar$'), cancel)],
    )

    application.add_handler(conv_handler)
    # ... (Adicione seus outros handlers: start, list, etc.) ...
    
    application.post_init = reload_jobs_from_db
    
    logger.info("🚀 Bot em execução...")
    application.run_polling()

if __name__ == '__main__':
    main()

