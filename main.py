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

# --- ✅ DECORATOR ATUALIZADO ---
def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = None
        # Verifica se a atualização veio de uma mensagem ou de um clique de botão
        if update.effective_user:
            user = update.effective_user
        elif update.callback_query:
            user = update.callback_query.from_user
        
        if not user or user.id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer("Acesso Negado!", show_alert=True)
            else:
                await update.message.reply_text("🔒 *Acesso Negado!*", parse_mode='Markdown')
            return
        
        # Passa a `update` original para a função
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- Funções do Agendador ---
async def send_post(context: ContextTypes.DEFAULT_TYPE):
    job = context.job; schedule_id = ObjectId(job.data["schedule_id"]); post = schedules_collection.find_one({"_id": schedule_id})
    if not post: logger.warning(f"Post {schedule_id} não encontrado."); job.schedule_next_run_time = None; return
    chat_id = post["chat_id"]; text = post.get("text", ""); media_file_id = post.get("media_file_id"); media_type = post.get("media_type"); buttons_data = post.get("buttons", []); pin_post = post.get("pin_post", False); last_message_id = post.get("last_sent_message_id")
    if pin_post and last_message_id:
        try: await context.bot.delete_message(chat_id=chat_id, message_id=last_message_id)
        except telegram_error.BadRequest as e: logger.warning(f"Não pôde apagar msg anterior {last_message_id}: {e}")
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
            except telegram_error.BadRequest as e: logger.error(f"Não pôde fixar msg {sent_message.message_id}: {e}")
        if post["type"] == "agendada": schedules_collection.delete_one({"_id": schedule_id})
        elif post.get("repetitions") is not None:
            if post["repetitions"] == 1: schedules_collection.delete_one({"_id": schedule_id}); job.schedule_next_run_time = None
            elif post["repetitions"] != 0: schedules_collection.update_one({"_id": schedule_id}, {"$inc": {"repetitions": -1}})
    except Exception as e: logger.error(f"Erro ao enviar post {schedule_id} para {chat_id}: {e}")

async def reload_jobs_from_db(application: Application):
    if schedules_collection is None: return
    logger.info("--- Recarregando jobs do MongoDB ---"); current_time = datetime.now(SAO_PAULO_TZ); jobs_reloaded = 0; jobs_deleted = 0
    for post in list(schedules_collection.find({})):
        schedule_id_str = str(post['_id'])
        if post['type'] == 'agendada':
            run_date = post.get('scheduled_for')
            if run_date and run_date > current_time: application.job_queue.run_once(send_post, run_date, name=schedule_id_str, data={"schedule_id": schedule_id_str}, chat_id=post['chat_id'], user_id=post['user_id']); jobs_reloaded += 1
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
    keyboard = [[InlineKeyboardButton("🆕 Agendar Postagem", callback_data='start_schedule_single')], [InlineKeyboardButton("🔁 Agendar Recorrente", callback_data='start_schedule_recurrent')], [InlineKeyboardButton("📋 Listar Agendamentos", callback_data='menu_listar')], [InlineKeyboardButton("❌ Cancelar (Ajuda)", callback_data='menu_cancelar_ajuda')]]
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
        await show_main_menu(update, context, message_text="*Aqui está sua lista.*\nO que mais deseja fazer?")
    elif query.data == 'menu_cancelar_ajuda':
        await query.message.reply_text("Para cancelar um agendamento, use o comando: `/cancelar <ID>`\nO ID você encontra na listagem.", parse_mode='Markdown')

@restricted
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name; welcome_message = (f"Olá, {user_name}! Eu sou o **BAPD** 😁")
    await update.message.reply_text(welcome_message, parse_mode='Markdown'); await show_main_menu(update, context)

@restricted
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: await show_main_menu(update, context)

@restricted
async def start_schedule_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    message_source = query.message if query else update.message
    
    # --- CÓDIGO CORRIGIDO AQUI ---
    user_id = update.effective_user.id # Obtenha o user_id do usuário que iniciou o fluxo
    context.user_data.clear() # Limpa os dados de uma conversa anterior, se houver
    context.user_data['user_id'] = user_id # Salva o user_id para uso posterior
    # --- FIM DO CÓDIGO CORRIGIDO ---

    if query:
        await query.answer(); data = query.data
        context.user_data['schedule_type'] = 'agendada' if data == 'start_schedule_single' else 'recorrente'
        await query.edit_message_text("Ok, vamos criar uma nova postagem! ✨\n\nPrimeiro, envie o ID do canal/grupo.", reply_markup=None)
    else:
        command = update.message.text
        context.user_data['schedule_type'] = 'agendada' if 'agendar' in command.lower() else 'recorrente'
        await message_source.reply_text("Ok, vamos criar uma nova postagem! ✨\n\nPrimeiro, envie o ID do canal/grupo.", reply_markup=ReplyKeyboardRemove())
    return SELECT_CHANNEL

async def get_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data['chat_id'] = int(update.message.text)
        await update.message.reply_text("Ótimo! Agora, envie a mídia (foto/vídeo) ou digite `Pular`.", reply_markup=ReplyKeyboardMarkup([['Pular']], one_time_keyboard=True)); return GET_MEDIA
    except ValueError: await update.message.reply_text("❌ ID inválido."); return SELECT_CHANNEL

async def get_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text and update.message.text.lower() == 'pular': context.user_data['media_file_id'] = None; context.user_data['media_type'] = None
    elif update.message.photo: context.user_data['media_file_id'] = update.message.photo[-1].file_id; context.user_data['media_type'] = 'photo'
    elif update.message.video: context.user_data['media_file_id'] = update.message.video.file_id; context.user_data['media_type'] = 'video'
    else: await update.message.reply_text("Formato não suportado."); return GET_MEDIA
    await update.message.reply_text("Entendido. Agora, envie o texto ou digite `Pular`.", reply_markup=ReplyKeyboardMarkup([['Pular']], one_time_keyboard=True)); return GET_TEXT

async def get_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text and update.message.text.lower() != 'pular': context.user_data['text'] = update.message.text; await update.message.reply_text("Texto salvo! ✅")
    else: context.user_data['text'] = None; await update.message.reply_text("Ok, postagem sem texto. ✅")
    reply_keyboard = [['Adicionar Botão', 'Pular']]
    await update.message.reply_text("\nQuer adicionar um botão com link?", reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)); return GET_BUTTONS_PROMPT

async def get_buttons_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'adicionar' in update.message.text.lower():
        context.user_data['buttons'] = []; await update.message.reply_text("Qual o **texto do primeiro botão**?", reply_markup=ReplyKeyboardRemove()); return GET_BUTTON_1_TEXT
    else: context.user_data['buttons'] = []; return await ask_to_pin(update, context)

async def get_button_1_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['current_button_text'] = update.message.text; await update.message.reply_text("Agora, envie o **LINK (URL)**."); return GET_BUTTON_1_URL

async def get_button_1_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text
    if not (url.startswith('http://') or url.startswith('https://')): await update.message.reply_text("❌ Link inválido."); return GET_BUTTON_1_URL
    context.user_data['buttons'].append({'text': context.user_data['current_button_text'], 'url': url})
    reply_keyboard = [['Adicionar 2º Botão', 'Finalizar Botões']]
    await update.message.reply_text("Botão adicionado! ✅\n\nDeseja adicionar outro?", reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)); return GET_BUTTON_2_PROMPT

async def get_button_2_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'adicionar' in update.message.text.lower():
        await update.message.reply_text("Qual o **texto do segundo botão**?", reply_markup=ReplyKeyboardRemove()); return GET_BUTTON_2_TEXT
    else: return await ask_to_pin(update, context)

async def get_button_2_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['current_button_text'] = update.message.text; await update.message.reply_text("E qual o **LINK (URL)**?"); return GET_BUTTON_2_URL
    
async def get_button_2_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text
    if not (url.startswith('http://') or url.startswith('https://')): await update.message.reply_text("❌ Link inválido."); return GET_BUTTON_2_URL
    context.user_data['buttons'].append({'text': context.user_data['current_button_text'], 'url': url}); return await ask_to_pin(update, context)

async def ask_to_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reply_keyboard = [['Sim, fixar e substituir'], ['Não, postagem normal']]
    await update.message.reply_text("Deseja que esta postagem seja fixada (substituindo a anterior a cada envio)?", reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True))
    return GET_PIN_OPTION

async def get_pin_option(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['pin_post'] = 'sim' in update.message.text.lower()
    return await ask_for_schedule_time(update, context)

async def ask_for_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data['schedule_type'] == 'recorrente':
        await update.message.reply_text("Tudo pronto! Qual o intervalo? (Ex: `30m`)", reply_markup=ReplyKeyboardRemove()); return GET_INTERVAL
    else: await update.message.reply_text("Tudo pronto! Para quando agendar? (AAAA-MM-DD HH:MM)", reply_markup=ReplyKeyboardRemove()); return GET_SCHEDULE_TIME

async def get_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    interval_str = update.message.text.lower(); value_str = interval_str[:-1]; unit = interval_str[-1]
    try: value = int(value_str); assert unit in ['m', 'h', 'd'] and value > 0
    except: await update.message.reply_text("Formato inválido."); return GET_INTERVAL
    context.user_data['interval_value'] = value; context.user_data['interval_unit'] = unit
    await update.message.reply_text("Intervalo definido! ✅\n\nQuantas vezes repetir? (`0` para infinito)"); return GET_REPETITIONS

async def get_repetitions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try: repetitions = int(update.message.text); assert repetitions >= 0
    except: await update.message.reply_text("Envie um número válido."); return GET_REPETITIONS
    context.user_data['repetitions'] = repetitions
    await update.message.reply_text("Quando devo começar? (AAAA-MM-DD HH:MM)"); return GET_START_TIME

async def pre_schedule_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['final_schedule_time'] = update.message.text
    return await confirm_schedule_details(update, context)

async def confirm_schedule_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    summary = f"📝 *Confirme os detalhes do agendamento:*\n\n"
    summary += f"**Tipo:** {'Recorrente' if ud['schedule_type'] == 'recorrente' else 'Único'}\n"
    summary += f"**Destino:** `{ud['chat_id']}`\n"
    summary += f"**Mídia:** {'Sim' if ud.get('media_file_id') else 'Não'}\n"
    summary += f"**Fixar:** {'Sim' if ud.get('pin_post', False) else 'Não'}\n"
    if ud['schedule_type'] == 'recorrente':
        summary += f"**Início:** `{ud['final_schedule_time']}`\n"; summary += f"**Intervalo:** `{ud.get('interval_value')}{ud.get('interval_unit')}`\n"; summary += f"**Repetições:** {'Infinitas' if ud.get('repetitions') == 0 else ud.get('repetitions')}\n"
    else: summary += f"**Data:** `{ud['final_schedule_time']}`\n"
    keyboard = [[InlineKeyboardButton("✅ Confirmar", callback_data='confirm_yes')], [InlineKeyboardButton("❌ Cancelar", callback_data='confirm_no')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(summary, parse_mode='Markdown', reply_markup=reply_markup)
    return AWAITING_CONFIRMATION

async def process_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    if query.data == 'confirm_yes':
        await query.edit_message_text("✅ Confirmado! Salvando e agendando...")
        return await schedule_post(update, context)
    else: await query.edit_message_text("❌ Agendamento cancelado."); return await cancel_conversation(update, context)

async def schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        ud = context.user_data; time_str = ud['final_schedule_time']; is_recurrent = ud['schedule_type'] == 'recorrente'
        try:
            schedule_dt_naive = datetime.strptime(time_str, "%Y-%m-%d %H:%M"); schedule_dt_aware = SAO_PAULO_TZ.localize(schedule_dt_naive)
            # Adicionado um ajuste para garantir que a comparação seja correta com o fuso horário atual.
            # Se a data for no passado, exibe erro.
            if schedule_dt_aware < datetime.now(SAO_PAULO_TZ): 
                await context.bot.send_message(chat_id=ud['user_id'], text="❌ A data/hora agendada deve ser no futuro."); 
                return ConversationHandler.END
        except ValueError: 
            await context.bot.send_message(chat_id=ud['user_id'], text="❌ Formato de data/hora inválido. Use AAAA-MM-DD HH:MM."); 
            return ConversationHandler.END
        
        # user_id agora garantido em context.user_data
        post_data = {"user_id": ud['user_id'], "chat_id": ud['chat_id'], "type": ud['schedule_type'], "media_file_id": ud.get('media_file_id'), "media_type": ud.get('media_type'), "text": ud.get('text'), "buttons": ud.get('buttons', []), "created_at": datetime.now(SAO_PAULO_TZ), "pin_post": ud.get('pin_post', False)}
        if is_recurrent: post_data['interval'] = f"{ud['interval_value']}{ud['interval_unit']}"; post_data['repetitions'] = ud['repetitions']; post_data['start_date'] = schedule_dt_aware
        else: post_data['scheduled_for'] = schedule_dt_aware
        result = schedules_collection.insert_one(post_data); schedule_id = result.inserted_id
        job_data = {"schedule_id": str(schedule_id)}
        if is_recurrent:
            unit = ud['interval_unit']; value = ud['interval_value']
            interval_kwargs = {'minutes': value} if unit == 'm' else {'hours': value} if unit == 'h' else {'days': value}
            context.job_queue.run_repeating(send_post, interval=timedelta(**interval_kwargs), first=schedule_dt_aware, name=str(schedule_id), data=job_data)
        else: context.job_queue.run_once(send_post, schedule_dt_aware, name=str(schedule_id), data=job_data)
        await context.bot.send_message(chat_id=ud['user_id'], text="🚀 **Sucesso!** Postagem agendada.")
        ud.clear(); return ConversationHandler.END
    except Exception as e:
        # Fallback mais robusto para user_id em caso de erro.
        # update.effective_user.id sempre estará disponível aqui.
        user_id = context.user_data.get('user_id', update.effective_user.id) 
        error_text = f"🚨 Erro ao salvar:\n\n`{e}`"; 
        await context.bot.send_message(chat_id=user_id, text=error_text, parse_mode='Markdown')
        logger.error(f"ERRO NO AGENDAMENTO: {e}", exc_info=True)
        context.user_data.clear() # Limpa os dados do usuário para evitar problemas em agendamentos futuros
        return ConversationHandler.END

@restricted
async def list_posts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_source = update.callback_query.message if update.callback_query else update.message
    message = "📅 *Suas Postagens Agendadas*\n\n"; found_any = False
    if schedules_collection is not None:
        for post in schedules_collection.find({"user_id": update.effective_user.id}).sort("created_at", -1):
            found_any = True; post_type = "Agendada" if post['type'] == 'agendada' else "Recorrente"; text_snippet = (post.get('text') or "Sem texto")[:50] + "..."
            message += f"🆔 `{post['_id']}`\n🎯 `Alvo`: {post['chat_id']}\n🔄 `Tipo`: {post_type}\n📝 `Texto`: _{text_snippet}_\n\n"
    if not found_any: message = "Você ainda não tem postagens agendadas."
    if update.callback_query: await message_source.edit_text(message, parse_mode='Markdown')
    else: await message_source.reply_text(message, parse_mode='Markdown')

@restricted
async def cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not context.args: await update.message.reply_text("Uso: `/cancelar <ID>`"); return
        schedule_id_str = context.args[0]; schedule_id = ObjectId(schedule_id_str)
        jobs = context.job_queue.get_jobs_by_name(schedule_id_str)
        if jobs:
            for job in jobs: job.schedule_removal()
        deleted_post = schedules_collection.find_one_and_delete({"_id": schedule_id, "user_id": update.effective_user.id})
        if not deleted_post and not jobs: await update.message.reply_text("❌ Agendamento não encontrado."); return
        await update.message.reply_text(f"✅ Agendamento `{schedule_id_str}` cancelado com sucesso!")
    except (IndexError): await update.message.reply_text("Uso incorreto: `/cancelar <ID>`")
    except Exception as e: await update.message.reply_text(f"Ocorreu um erro: {e}")

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear(); await update.message.reply_text("Processo cancelado.", reply_markup=ReplyKeyboardRemove()); return ConversationHandler.END

# --- Configuração do Keep-Alive ---
app = Flask(__name__)
@app.route('/')
def index(): return "Bot is running!"
def run_flask(): app.run(host='0.0.0.0', port=8080)

# --- Função Principal ---
def main() -> None:
    if not all([TELEGRAM_TOKEN, MONGO_URI, ADMIN_IDS]): logger.error("ERRO CRÍTICO: Variáveis de ambiente não definidas."); return
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(reload_jobs_from_db).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('agendar', start_schedule_flow), CommandHandler('recorrente', start_schedule_flow), CallbackQueryHandler(start_schedule_flow, pattern='^start_schedule_')],
        states={
            SELECT_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel)], GET_MEDIA: [MessageHandler(filters.ALL & ~filters.COMMAND, get_media)],
            GET_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_text)], GET_BUTTONS_PROMPT: [MessageHandler(filters.Regex('^(Adicionar Botão|Pular)$'), get_buttons_prompt)],
            GET_BUTTON_1_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_button_1_text)], GET_BUTTON_1_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_button_1_url)],
            GET_BUTTON_2_PROMPT: [MessageHandler(filters.Regex('^(Adicionar 2º Botão|Finalizar Botões)$'), get_button_2_prompt)], GET_BUTTON_2_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_button_2_text)],
            GET_BUTTON_2_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_button_2_url)], GET_PIN_OPTION: [MessageHandler(filters.Regex('^(Sim, fixar e substituir|Não, postagem normal)$'), get_pin_option)],
            GET_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, pre_schedule_confirmation)], GET_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_interval)],
            GET_REPETITIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_repetitions)], GET_START_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, pre_schedule_confirmation)],
            AWAITING_CONFIRMATION: [CallbackQueryHandler(process_confirmation, pattern='^confirm_')]
        },
        fallbacks=[CommandHandler('cancelar_conversa', cancel_conversation)],
    )
    application.add_handler(CommandHandler("start", start_command)); application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("listagem", list_posts)); application.add_handler(CommandHandler("cancelar", cancel_post))
    application.add_handler(CallbackQueryHandler(handle_simple_menu_clicks, pattern='^menu_')); application.add_handler(conv_handler)
    job_queue = application.job_queue
    job_queue.run_daily(weekly_cleanup, time=time(hour=3, minute=0, tzinfo=SAO_PAULO_TZ), days=(6,), name="weekly_cleanup_job")
    flask_thread = Thread(target=run_flask); flask_thread.daemon = True; flask_thread.start()
    application.run_polling()

if __name__ == "__main__":
    main()
