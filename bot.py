import os
import json
import asyncio
import anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from notion_client import Client as NotionClient

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify"
]

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
notion = NotionClient(auth=NOTION_TOKEN)
history = {}
user_credentials = {}

async def start_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    creds_data = json.loads(GOOGLE_CREDENTIALS)
    flow = Flow.from_client_config(creds_data, scopes=SCOPES)
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_url, _ = flow.authorization_url(prompt="consent")
    context.user_data["flow"] = flow
    await update.message.reply_text(
        f"구글 인증 링크:\n{auth_url}\n\n"
        "인증 후 코드를 복사해서 /code 코드입력 형식으로 보내주세요!"
    )

async def set_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("사용법: /code 인증코드")
        return
    code = context.args[0]
    flow = context.user_data.get("flow")
    if not flow:
        await update.message.reply_text("/auth 먼저 실행해주세요!")
        return
    flow.fetch_token(code=code)
    user_credentials[update.message.chat_id] = flow.credentials
    await update.message.reply_text("✅ 구글 인증 완료! 이제 캘린더와 Gmail을 사용할 수 있어요!")

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.chat_id
    msg = update.message.text
    if uid not in history:
        history[uid] = []
    
    creds = user_credentials.get(uid)
    google_context = ""
    
    if creds:
        try:
            cal = build("calendar", "v3", credentials=creds)
            events = cal.events().list(
                calendarId="primary",
                maxResults=5,
                singleEvents=True,
                orderBy="startTime"
            ).execute().get("items", [])
            if events:
                google_context += "\n[다가오는 일정]\n"
                for e in events:
                    start = e["start"].get("dateTime", e["start"].get("date"))
                    google_context += f"- {e['summary']} ({start})\n"
        except:
            pass

    system_msg = """당신은 개인 비서입니다. 한국어로 대화하세요.
사용자의 Google Calendar, Gmail, Notion에 접근할 수 있습니다."""
    
    if google_context:
        system_msg += f"\n\n현재 컨텍스트:{google_context}"

    history[uid].append({"role": "user", "content": msg})
    await context.bot.send_chat_action(chat_id=uid, action="typing")
    
    res = claude_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=system_msg,
        messages=history[uid]
    )
    reply = res.content[0].text
    history[uid].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history[update.message.chat_id] = []
    await update.message.reply_text("초기화됐어요 😊")

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("auth", start_auth))
app.add_handler(CommandHandler("code", set_code))
app.add_handler(CommandHandler("reset", reset))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
print("Starting bot...")
app.run_polling()
