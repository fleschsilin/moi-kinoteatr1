

import os

from telegram import Update

from telegram.ext import (

    Application,

    CommandHandler,

    MessageHandler,

    ContextTypes,

    filters

)

TOKEN = os.getenv("BOT_TOKEN")

CHANNEL_ID = os.getenv("CHANNEL_ID")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(

        "🎬 Добро пожаловать в Мой кинотеатр!\n"

        "Отправь видео, чтобы опубликовать его в канале."

    )

async def receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not CHANNEL_ID:

        await update.message.reply_text("Канал ещё не настроен.")

        return

    await context.bot.copy_message(

        chat_id=CHANNEL_ID,

        from_chat_id=update.effective_chat.id,

        message_id=update.message.message_id

    )

    await update.message.reply_text("🎬 Фильм опубликован!")

def main():

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(MessageHandler(filters.VIDEO, receive_video))

    app.run_polling()

if __name__ == "__main__":

    main()
