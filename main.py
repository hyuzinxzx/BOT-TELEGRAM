import logging
import os
from datetime import datetime
from functools import wraps

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from bson import ObjectId
from flask import Flask
from pymongo import MongoClient
from threading import Thread
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                    ReplyKeyboardMarkup, ReplyKeyboardRemove)
from telegram.ext import (Application, CommandHandler, ConversationHandler,
                          MessageHandler, filters, ContextTypes, CallbackQueryHandler)

# --- Configurações Iniciais ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Variáveis de Ambiente e Constantes ---
# Em produção no Replit, use os "Secrets"
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
MONGO_URI = os.environ.get('MONGO_URI')
# Coloque seu ID de usuário do Telegram aqui, separado por vírgula se houver mais de um
ADMIN_IDS_STR = os.environ.get('ADMIN_IDS', '1383608766443819108')
ADMIN_IDS = [int(admin_id) for admin_id in ADMIN_IDS_STR.split(',')]

SAO_PAULO_TZ = pytz.timezone("America/Sao_Paulo")

# --- Conexão com o Banco de Dados (MongoDB) ---
client = MongoClient(MONGO_URI)
db = client.telegram_bot_db
schedules_collection = db.schedules

# --- Estados da Conversa para o ConversationHandler ---
(SELECT_CHANNEL, GET_MEDIA, GET_TEXT, GET_BUTTONS_PROMPT, GET_BUTTON_1_TEXT, GET_BUTTON_1_URL,
 GET_BUTTON_2_PROMPT, GET_BUTTON_2_TEXT, GET_BUTTON_2_URL, GET_SCHEDULE_TIME,
 GET_INTERVAL, GET_REPETITIONS, GET_START_TIME) = range(13)

# --- Decorator para Restringir Acesso ---
def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            await update.message.reply_text(
                "🔒 *Acesso Negado!* 🔒\n\n"
                "Desculpe, você não tem permissão para usar meus comandos. Este é um bot de uso privado.\n"
                "Se você acredita que isso é um erro, entre em contato com o administrador.",
                parse_mode='Markdown'
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- Funções do Agendador (Scheduler) ---
async def send_post(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    schedule_id = ObjectId(job.data["schedule_id"])
    post = schedules_collection.find_one({"_id": schedule_id})

    if not post:
        logger.warning(f"Post com ID {schedule_id} não encontrado no DB. Removendo job.")
        job.schedule_next_run_time = None # Remove o job
        return

    chat_id = post["chat_id"]
    text = post.get("text", "")
    media_file_id = post.get("media_file_id")
    media_type = post.get("media_type")
    buttons_data = post.get("buttons", [])
    
    reply_markup = None
    if buttons_data:
        keyboard = [[InlineKeyboardButton(b['text'], url=b['url'])] for b in buttons_data]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
    post_format = f"┌─────────────────────────\n│ *CAMPO DO TEXTO*\n└─────────────────────────\n{text}\n" if text else ""
    
    try:
        if media_type == "photo":
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=media_file_id,
                caption=post_format + "┌─────────────────────────\n│ *CAMPO DO(S) BOTÃO(ÕES)*\n└──────────────────────────",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        elif media_type == "video":
            await context.bot.send_video(
                chat_id=chat_id,
                video=media_file_id,
                caption=post_format + "┌─────────────────────────\n│ *CAMPO DO(S) BOTÃO(ÕES)*\n└──────────────────────────",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else: # Apenas texto
             await context.bot.send_message(
                chat_id=chat_id,
                text=post_format + "┌─────────────────────────\n│ *CAMPO DO(S) BOTÃO(ÕES)*\n└──────────────────────────",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

        # Se for um post único, remove do DB e do scheduler
        if post["type"] == "agendada":
            schedules_collection.delete_one({"_id": schedule_id})
            logger.info(f"Post agendado {schedule_id} enviado e removido.")
        else: # Post recorrente
            remaining_reps = post.get("repetitions")
            if remaining_reps is not None and remaining_reps != 0:
                if remaining_reps == 1:
                    schedules_collection.delete_one({"_id": schedule_id})
                    job.schedule_next_run_time = None
                    logger.info(f"Post recorrente {schedule_id} completou suas repetições e foi removido.")
                else:
                    schedules_collection.update_one({"_id": schedule_id}, {"$inc": {"repetitions": -1}})
                    logger.info(f"Post recorrente {schedule_id} enviado. Repetições restantes: {remaining_reps - 1}")

    except Exception as e:
        logger.error(f"Erro ao enviar post {schedule_id} para o chat {chat_id}: {e}")


# --- Comando /iniciar ---
@restricted
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name
    welcome_message = (
        f"Olá, {user_name}! Eu sou o **BAPD** (Bot de Agendamento de Posts para Telegram) 😁\n\n"
        "Eu posso te ajudar a agendar postagens com texto, mídia e botões para seus canais ou grupos!\n\n"
        "Aqui está o que eu posso fazer:\n\n"
        "───── 📜 *Lista de Comandos* 📜 ─────\n\n"
        "*/agendar* - 🆕 Agenda uma postagem única. Eu vou te guiar passo a passo!\n"
        "*Exemplo de uso:* Simplesmente digite `/agendar` e siga minhas instruções.\n\n"
        "*/recorrente* - 🔁 Cria uma postagem que se repete em intervalos definidos.\n"
        "*Exemplo de uso:* Digite `/recorrente` para começar o processo interativo.\n\n"
        "*/listagem* - 📋 Mostra todas as postagens agendadas e recorrentes.\n"
        "*Exemplo de uso:* `/listagem`\n\n"
        "*/cancelar <ID>* - ❌ Remove um agendamento (único ou recorrente).\n"
        "*Exemplo de uso:* `/cancelar 60d21b466a3d6a3d6a3d6a3d` (use o ID da `/listagem`).\n\n"
        "*/cancelar_conversa* - 🛑 Para o processo de criação de um post a qualquer momento.\n"
        "*Exemplo de uso:* `/cancelar_conversa`"
    )
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

# --- Lógica da Conversa ---

# Início do processo de agendamento (ambos os tipos)
@restricted
async def start_schedule_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    command = update.message.text.split(' ')[0]
    context.user_data['schedule_type'] = 'agendada' if command == '/agendar' else 'recorrente'
    
    await update.message.reply_text(
        "Ok, vamos criar uma nova postagem! ✨\n\n"
        "Primeiro, por favor, me envie o **ID do canal ou grupo** de destino.\n"
        "Ex: `-1001234567890`"
    )
    return SELECT_CHANNEL

async def get_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        chat_id = int(update.message.text)
        context.user_data['chat_id'] = chat_id
        await update.message.reply_text(
            "Ótimo! Agora, por favor, envie a **mídia** (foto ou vídeo) para a postagem.\n\n"
            "🖼️ Se não quiser adicionar mídia, apenas digite `Pular`.",
            reply_markup=ReplyKeyboardMarkup([['Pular']], one_time_keyboard=True, resize_keyboard=True)
        )
        return GET_MEDIA
    except ValueError:
        await update.message.reply_text("❌ ID inválido. Por favor, envie um ID de chat numérico. Ex: `-1001234567890`")
        return SELECT_CHANNEL

async def get_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text and update.message.text.lower() == 'pular':
        context.user_data['media_file_id'] = None
        context.user_data['media_type'] = None
    elif update.message.photo:
        context.user_data['media_file_id'] = update.message.photo[-1].file_id
        context.user_data['media_type'] = 'photo'
    elif update.message.video:
        context.user_data['media_file_id'] = update.message.video.file_id
        context.user_data['media_type'] = 'video'
    else:
        await update.message.reply_text(
            " formato não suportado. Por favor, envie uma foto, um vídeo ou digite `Pular`."
        )
        return GET_MEDIA
        
    await update.message.reply_text(
        "Entendido. Agora, envie o **texto** que você quer na postagem.\n\n"
        "📝 Se não quiser adicionar texto, digite `Pular`.",
        reply_markup=ReplyKeyboardMarkup([['Pular']], one_time_keyboard=True, resize_keyboard=True)
    )
    return GET_TEXT

async def get_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text and update.message.text.lower() != 'pular':
        context.user_data['text'] = update.message.text
    else:
        context.user_data['text'] = None
        
    reply_keyboard = [['Adicionar Botão', 'Pular']]
    await update.message.reply_text(
        "Perfeito! Você quer adicionar um **botão com link** à sua postagem?\n\n"
        "Você pode adicionar até 2 botões.",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return GET_BUTTONS_PROMPT
    
async def get_buttons_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_choice = update.message.text.lower()
    
    if 'adicionar' in user_choice:
        context.user_data['buttons'] = []
        await update.message.reply_text("Qual será o **texto do primeiro botão**?", reply_markup=ReplyKeyboardRemove())
        return GET_BUTTON_1_TEXT
    else: # Pular
        context.user_data['buttons'] = []
        return await ask_for_schedule_time(update, context)

async def get_button_1_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['current_button_text'] = update.message.text
    await update.message.reply_text("Agora, envie o **LINK (URL)** para este botão.\nEx: `https://google.com`")
    return GET_BUTTON_1_URL

async def get_button_1_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text
    if not (url.startswith('http://') or url.startswith('https://')):
        await update.message.reply_text("❌ Link inválido. Ele deve começar com `http://` ou `https://`.\nPor favor, tente novamente.")
        return GET_BUTTON_1_URL
        
    context.user_data['buttons'].append({'text': context.user_data['current_button_text'], 'url': url})
    
    reply_keyboard = [['Adicionar 2º Botão', 'Finalizar Botões']]
    await update.message.reply_text(
        "Primeiro botão adicionado com sucesso! ✅\n\nDeseja adicionar um segundo botão?",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return GET_BUTTON_2_PROMPT

async def get_button_2_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'adicionar' in update.message.text.lower():
        await update.message.reply_text("Qual será o **texto do segundo botão**?", reply_markup=ReplyKeyboardRemove())
        return GET_BUTTON_2_TEXT
    else:
        return await ask_for_schedule_time(update, context)

async def get_button_2_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['current_button_text'] = update.message.text
    await update.message.reply_text("E qual o **LINK (URL)** para o segundo botão?")
    return GET_BUTTON_2_URL
    
async def get_button_2_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text
    if not (url.startswith('http://') or url.startswith('https://')):
        await update.message.reply_text("❌ Link inválido. Ele deve começar com `http://` ou `https://`.\nPor favor, tente novamente.")
        return GET_BUTTON_2_URL
        
    context.user_data['buttons'].append({'text': context.user_data['current_button_text'], 'url': url})
    return await ask_for_schedule_time(update, context)

# Pergunta o tempo/data dependendo do tipo de post
async def ask_for_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    schedule_type = context.user_data['schedule_type']
    
    if schedule_type == 'recorrente':
        await update.message.reply_text(
            "Tudo pronto com o conteúdo! Agora vamos à recorrência.\n\n"
            "Qual o **intervalo** entre cada postagem?\n"
            "Use `m` para minutos, `h` para horas, `d` para dias.\n"
            "Ex: `30m`, `2h`, `1d`",
            reply_markup=ReplyKeyboardRemove()
        )
        return GET_INTERVAL
    else: # Agendada
        await update.message.reply_text(
            "Tudo pronto! Para finalizar, quando devo enviar esta postagem?\n\n"
            "Use o formato: `AAAA-MM-DD HH:MM`\n"
            "Ex: `2025-12-31 23:59`",
            reply_markup=ReplyKeyboardRemove()
        )
        return GET_SCHEDULE_TIME
        
async def get_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    interval_str = update.message.text.lower()
    value_str = interval_str[:-1]
    unit = interval_str[-1]

    try:
        value = int(value_str)
        if unit not in ['m', 'h', 'd'] or value <= 0:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Formato de intervalo inválido. Use, por exemplo, `30m`, `2h`, `1d`.")
        return GET_INTERVAL
        
    context.user_data['interval_value'] = value
    context.user_data['interval_unit'] = unit
    
    await update.message.reply_text(
        "Intervalo definido! ✅\n\n"
        "Quantas vezes esta postagem deve ser repetida?\n\n"
        "Digite um número (ex: `10`).\n"
        "**Para repetir infinitamente, digite `0`**."
    )
    return GET_REPETITIONS

async def get_repetitions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        repetitions = int(update.message.text)
        if repetitions < 0:
            raise ValueError
        context.user_data['repetitions'] = repetitions
    except ValueError:
        await update.message.reply_text("❌ Por favor, envie um número válido (0 ou maior).")
        return GET_REPETITIONS
        
    await update.message.reply_text(
        "Ok! E quando devo começar a enviar a **primeira** postagem?\n\n"
        "Use o formato: `AAAA-MM-DD HH:MM`\n"
        "Ex: `2025-06-20 09:00`"
    )
    return GET_START_TIME

# Finaliza o processo salvando no DB e agendando
async def schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE, is_recurrent: bool = False) -> int:
    time_str = update.message.text
    try:
        schedule_dt_naive = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        schedule_dt_aware = SAO_PAULO_TZ.localize(schedule_dt_naive)
        
        if schedule_dt_aware < datetime.now(SAO_PAULO_TZ):
            await update.message.reply_text("❌ A data e hora devem ser no futuro. Por favor, tente novamente.")
            return GET_SCHEDULE_TIME if not is_recurrent else GET_START_TIME

    except ValueError:
        await update.message.reply_text("❌ Formato de data/hora inválido. Use `AAAA-MM-DD HH:MM`. Tente novamente.")
        return GET_SCHEDULE_TIME if not is_recurrent else GET_START_TIME

    # Salvar no DB
    post_data = {
        "user_id": update.effective_user.id,
        "chat_id": context.user_data['chat_id'],
        "type": context.user_data['schedule_type'],
        "media_file_id": context.user_data.get('media_file_id'),
        "media_type": context.user_data.get('media_type'),
        "text": context.user_data.get('text'),
        "buttons": context.user_data.get('buttons', []),
        "created_at": datetime.now(SAO_PAULO_TZ)
    }

    result = schedules_collection.insert_one(post_data)
    schedule_id = result.inserted_id

    # Agendar com APScheduler
    job_data = {"schedule_id": str(schedule_id)}
    
    if is_recurrent:
        unit = context.user_data['interval_unit']
        value = context.user_data['interval_value']
        trigger_args = {}
        if unit == 'm': trigger_args['minutes'] = value
        if unit == 'h': trigger_args['hours'] = value
        if unit == 'd': trigger_args['days'] = value
        
        context.job_queue.run_repeating(
            send_post,
            interval=timedelta(**trigger_args) if unit != 'd' else timedelta(days=value), # Apscheduler v3-like
            first=schedule_dt_aware,
            name=str(schedule_id),
            data=job_data
        )
        post_data['interval'] = f"{value}{unit}"
        post_data['repetitions'] = context.user_data['repetitions']
        post_data['start_date'] = schedule_dt_aware
        schedules_collection.update_one({"_id": schedule_id}, {"$set": post_data})
    else:
        context.job_queue.run_once(
            send_post,
            schedule_dt_aware,
            name=str(schedule_id),
            data=job_data
        )

    await update.message.reply_text(
        "🚀 **Sucesso!** Sua postagem foi agendada.\n\n"
        f"Você pode ver todos os seus agendamentos com o comando `/listagem`.",
        reply_markup=ReplyKeyboardRemove()
    )
    
    context.user_data.clear()
    return ConversationHandler.END

async def schedule_single_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await schedule_post(update, context, is_recurrent=False)

async def schedule_recurrent_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await schedule_post(update, context, is_recurrent=True)


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "O processo foi cancelado. Se quiser começar de novo, é só me chamar! 😉",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# --- Comandos /listagem e /cancelar ---
@restricted
async def list_posts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    all_schedules = schedules_collection.find({"user_id": update.effective_user.id}).sort("created_at", -1)
    
    message = "📅 *Suas Postagens Agendadas e Recorrentes*\n\n"
    found_any = False
    
    for post in all_schedules:
        found_any = True
        post_id = post["_id"]
        post_type = "Agendada  única" if post['type'] == 'agendada' else "Recorrente"
        target = post['chat_id']
        text_snippet = (post.get('text') or "Sem texto")[:50] + "..."

        message += f"───── Ficha do Post ─────\n"
        message += f"🆔 `ID`: `{post_id}`\n"
        message += f"🎯 `Alvo`: {target}\n"
        message += f"🔄 `Tipo`: {post_type}\n"
        message += f"📝 `Texto`: _{text_snippet}_\n\n"

    if not found_any:
        message = "Você ainda não tem nenhuma postagem agendada. Que tal criar uma com `/agendar`? 🤔"

    await update.message.reply_text(message, parse_mode='Markdown')

@restricted
async def cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        args = context.args
        if not args:
            await update.message.reply_text("Por favor, forneça o ID do agendamento.\nUso: `/cancelar <ID>`")
            return
            
        schedule_id_str = args[0]
        schedule_id = ObjectId(schedule_id_str)

        # Remove do DB
        deleted_post = schedules_collection.find_one_and_delete({"_id": schedule_id, "user_id": update.effective_user.id})

        if not deleted_post:
            await update.message.reply_text("❌ Agendamento não encontrado ou você não tem permissão para cancelá-lo.")
            return

        # Remove do Scheduler
        jobs = context.job_queue.get_jobs_by_name(schedule_id_str)
        if jobs:
            for job in jobs:
                job.schedule_removal()
            await update.message.reply_text(f"✅ O agendamento com ID `{schedule_id_str}` foi cancelado e removido com sucesso!")
        else:
            await update.message.reply_text(f"✅ O agendamento com ID `{schedule_id_str}` foi removido do banco de dados, mas não foi encontrado no agendador ativo (talvez já tenha sido executado).")
            
    except Exception as e:
        logger.error(f"Erro ao cancelar post: {e}")
        await update.message.reply_text("❌ Ocorreu um erro. Verifique se o ID está correto.")


# --- Configuração do Keep-Alive para Replit ---
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

# --- Função Principal ---
def main() -> None:
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # --- Conversation Handler para agendamentos ---
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('agendar', start_schedule_flow),
            CommandHandler('recorrente', start_schedule_flow)
        ],
        states={
            SELECT_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel)],
            GET_MEDIA: [MessageHandler(filters.ALL & ~filters.COMMAND, get_media)],
            GET_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_text)],
            GET_BUTTONS_PROMPT: [MessageHandler(filters.Regex('^(Adicionar Botão|Pular)$'), get_buttons_prompt)],
            GET_BUTTON_1_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_button_1_text)],
            GET_BUTTON_1_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_button_1_url)],
            GET_BUTTON_2_PROMPT: [MessageHandler(filters.Regex('^(Adicionar 2º Botão|Finalizar Botões)$'), get_button_2_prompt)],
            GET_BUTTON_2_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_button_2_text)],
            GET_BUTTON_2_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_button_2_url)],
            GET_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, schedule_single_post)],
            GET_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_interval)],
            GET_REPETITIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_repetitions)],
            GET_START_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, schedule_recurrent_post)],
        },
        fallbacks=[CommandHandler('cancelar_conversa', cancel_conversation)],
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("iniciar", start_command))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("listagem", list_posts))
    application.add_handler(CommandHandler("cancelar", cancel_post))
    
    application.run_polling()

if __name__ == "__main__":
    # Inicia o servidor Flask em uma thread separada para o Keep-Alive do Replit
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    main()
