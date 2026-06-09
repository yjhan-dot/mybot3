import os
import json
import anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from notion_client import Client as NotionClient
from datetime import datetime, timedelta
import pytz

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
    await update.message.reply_text(f"구글 인증 링크:\n{auth_url}\n\n인증 후 코드를 복사해서 /code 코드 형식으로 보내주세요!")

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
    await update.message.reply_text("✅ 구글 인증 완료!")

def get_calendar_events(creds):
    try:
        cal = build("calendar", "v3", credentials=creds)
        now = datetime.utcnow().isoformat() + "Z"
        tomorrow = (datetime.utcnow() + timedelta(days=2)).isoformat() + "Z"
        events = cal.events().list(
            calendarId="primary",
            timeMin=now,
            timeMax=tomorrow,
            maxResults=10,
            singleEvents=True,
            orderBy="startTime"
        ).execute().get("items", [])
        if not events:
            return "예정된 일정이 없습니다."
        result = ""
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date"))
            result += f"- {e.get('summary', '제목없음')} ({start})\n"
        return result
    except Exception as ex:
        return f"캘린더 오류: {str(ex)}"

def get_gmail_messages(creds):
    try:
        gmail = build("gmail", "v1", credentials=creds)
        messages = gmail.users().messages().list(
            userId="me", maxResults=5, q="is:unread"
        ).execute().get("messages", [])
        if not messages:
            return "읽지 않은 이메일이 없습니다."
        result = ""
        for m in messages[:5]:
            msg = gmail.users().messages().get(userId="me", id=m["id"], format="metadata").execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            result += f"- 제목: {headers.get('Subject', '없음')} | 발신: {headers.get('From', '없음')}\n"
        return result
    except Exception as ex:
        return f"Gmail 오류: {str(ex)}"

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.chat_id
    msg = update.message.text.lower()
    if uid not in history:
        history[uid] = []

    creds = user_credentials.get(uid)
    extra_context = ""

    if creds:
        if any(w in msg for w in ["일정", "캘린더", "schedule", "calendar", "미팅", "약속"]):
            events = get_calendar_events(creds)
            extra_context += f"\n[Google Calendar 일정]\n{events}"
        if any(w in msg for w in ["이메일", "메일", "gmail", "받은", "inbox"]):
            emails = get_gmail_messages(creds)
            extra_context += f"\n[Gmail 받은편지함]\n{emails}"

    system_msg = "당신은 개인 비서입니다. 한국어로 대화하세요."
    if extra_context:
        system_msg += f"\n\n실시간 데이터:{extra_context}"

    history[uid].append({"role": "user", "content": update.message.text})
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
