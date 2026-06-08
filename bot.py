import os
import anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
claude_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
history = {}

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.chat_id
    msg = update.message.text
    if uid not in history:
        history[uid] = []
    history[uid].append({"role": "user", "content": msg})
    await context.bot.send_chat_action(chat_id=uid, action="typing")
    res = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system="You are a helpful assistant. Always respond in the same language the user uses.",
        messages=history[uid]
    )
    reply = res.content[0].text
    history[uid].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history[update.message.chat_id] = []
    await update.message.reply_text("초기화됐어요 😊")

bot = Application.builder().token(TELEGRAM_TOKEN).build()
bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
bot.add_handler(CommandHandler("reset", reset))
print("Starting bot...")
bot.run_polling()
