import os
import json
import base64
import anthropic
from email.mime.text import MIMEText
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from notion_client import Client as NotionClient
from datetime import datetime, timedelta

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
    await update.message.reply_text(f"구글 인증 링크:\n{auth_url}\n\n인증 후 코드를 /code 코드 형식으로 보내주세요!")

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
            calendarId="primary", timeMin=now, timeMax=tomorrow,
            maxResults=10, singleEvents=True, orderBy="startTime"
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

def get_notion_data(query):
    try:
        results = notion.search(query=query, page_size=5).get("results", [])
        if not results:
            return "관련 노션 페이지가 없습니다."
        result = ""
        for r in results:
            props = r.get("properties", {})
            title_prop = props.get("title", props.get("Name", {}))
            title_list = title_prop.get("title", [])
            title_text = title_list[0].get("plain_text", "제목없음") if title_list else "제목없음"
            result += f"- {title_text}\n"
        return result
    except Exception as ex:
        return f"노션 오류: {str(ex)}"

def create_notion_page(title, content):
    try:
        # 첫 번째 워크스페이스 페이지 찾기
        pages = notion.search(filter={"property": "object", "value": "page"}, page_size=1).get("results", [])
        parent_id = pages[0]["id"] if pages else None
        if not parent_id:
            return "노션 페이지를 찾을 수 없습니다."
        new_page = notion.pages.create(
            parent={"page_id": parent_id},
            properties={"title": {"title": [{"text": {"content": title}}]}},
            children=[{"object": "block", "type": "paragraph",
                       "paragraph": {"rich_text": [{"text": {"content": content}}]}}]
        )
        return f"✅ 노션 페이지 생성 완료! '{title}'"
    except Exception as ex:
        return f"노션 페이지 생성 오류: {str(ex)}"

def create_draft(creds, to, subject, body):
    try:
        gmail = build("gmail", "v1", credentials=creds)
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft = gmail.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()
        return f"✅ 드래프트 저장 완료!"
    except Exception as ex:
        return f"드래프트 오류: {str(ex)}"

def send_email(creds, to, subject, body):
    try:
        gmail = build("gmail", "v1", credentials=creds)
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        gmail.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        return "✅ 이메일 발송 완료!"
    except Exception as ex:
        return f"이메일 오류: {str(ex)}"

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.chat_id
    msg = update.message.text
    msg_lower = msg.lower()
    if uid not in history:
        history[uid] = []

    creds = user_credentials.get(uid)

    # 드래프트 저장 요청
    if creds and any(w in msg for w in ["드래프트", "draft", "임시저장"]):
        lines = msg.split("\n")
        to = subject = body = ""
        for line in lines:
            if "받는사람:" in line or "to:" in line.lower():
                to = line.split(":", 1)[1].strip()
            elif "제목:" in line or "subject:" in line.lower():
                subject = line.split(":", 1)[1].strip()
            elif "내용:" in line or "body:" in line.lower():
                body = line.split(":", 1)[1].strip()
        if to and subject:
            result = create_draft(creds, to, subject, body or msg)
            await update.message.reply_text(result)
            return

    # 이메일 보내기 요청
    if creds and any(w in msg for w in ["이메일 보내", "메일 보내", "send email"]):
        lines = msg.split("\n")
        to = subject = body = ""
        for line in lines:
            if "받는사람:" in line or "to:" in line.lower():
                to = line.split(":", 1)[1].strip()
            elif "제목:" in line or "subject:" in line.lower():
                subject = line.split(":", 1)[1].strip()
            elif "내용:" in line or "body:" in line.lower():
                body = line.split(":", 1)[1].strip()
        if to and subject:
            result = send_email(creds, to, subject, body or msg)
            await update.message.reply_text(result)
            return

    # 노션 페이지 생성 요청
    if any(w in msg for w in ["노션에 저장", "노션 페이지 만들어", "notion에 추가"]):
        lines = msg.split("\n")
        title = lines[0].replace("노션에 저장", "").replace("노션 페이지 만들어", "").strip()
        content = "\n".join(lines[1:]) if len(lines) > 1 else msg
        result = create_notion_page(title or "새 페이지", content)
        await update.message.reply_text(result)
        return

    extra_context = ""
    if creds:
        if any(w in msg_lower for w in ["일정", "캘린더", "schedule", "calendar", "미팅", "약속"]):
            extra_context += f"\n[Google Calendar]\n{get_calendar_events(creds)}"
        if any(w in msg_lower for w in ["이메일", "메일", "gmail", "받은", "inbox"]):
            extra_context += f"\n[Gmail 받은편지함]\n{get_gmail_messages(creds)}"

    if any(w in msg_lower for w in ["노션", "notion", "페이지", "할일", "todo"]):
        extra_context += f"\n[Notion]\n{get_notion_data(msg)}"

    system_msg = "당신은 개인 비서입니다. 한국어로 대화하세요. 이메일 드래프트나 발송 요청시 형식을 안내하세요: 받는사람:/제목:/내용:"
    if extra_context:
        system_msg += f"\n\n실시간 데이터:{extra_context}"

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
