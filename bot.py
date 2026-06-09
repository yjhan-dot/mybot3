import os
import json
import base64
import anthropic
from email.mime.text import MIMEText
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
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
        future = (datetime.utcnow() + timedelta(days=7)).isoformat() + "Z"
        events = cal.events().list(
            calendarId="primary", timeMin=now, timeMax=future,
            maxResults=20, singleEvents=True, orderBy="startTime"
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

def get_gmail_messages(creds, query="is:unread"):
    try:
        gmail = build("gmail", "v1", credentials=creds)
        messages = gmail.users().messages().list(
            userId="me", maxResults=10, q=query
        ).execute().get("messages", [])
        if not messages:
            return "이메일이 없습니다."
        result = ""
        for m in messages[:10]:
            msg = gmail.users().messages().get(userId="me", id=m["id"], format="metadata").execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            snippet = msg.get("snippet", "")[:100]
            result += f"- 제목: {headers.get('Subject', '없음')}\n  발신: {headers.get('From', '없음')}\n  내용: {snippet}\n\n"
        return result
    except Exception as ex:
        return f"Gmail 오류: {str(ex)}"

def create_draft(creds, to, subject, body):
    try:
        gmail = build("gmail", "v1", credentials=creds)
        message = MIMEText(body, 'plain', 'utf-8')
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        gmail.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        return f"✅ 드래프트 저장 완료!"
    except Exception as ex:
        return f"드래프트 오류: {str(ex)}"

def send_email(creds, to, subject, body):
    try:
        gmail = build("gmail", "v1", credentials=creds)
        message = MIMEText(body, 'plain', 'utf-8')
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
        return "✅ 이메일 발송 완료!"
    except Exception as ex:
        return f"이메일 오류: {str(ex)}"

def get_notion_pages(query=""):
    try:
        results = notion.search(query=query, page_size=10).get("results", [])
        if not results:
            return "노션 페이지가 없습니다."
        result = ""
        for r in results:
            obj_type = r.get("object", "")
            if obj_type == "page":
                props = r.get("properties", {})
                title_prop = props.get("title", props.get("Name", props.get("제목", {})))
                title_list = title_prop.get("title", []) if isinstance(title_prop, dict) else []
                title_text = title_list[0].get("plain_text", "제목없음") if title_list else "제목없음"
                page_id = r.get("id", "")
                result += f"- [{title_text}] (ID: {page_id})\n"
        return result or "페이지를 찾을 수 없습니다."
    except Exception as ex:
        return f"노션 오류: {str(ex)}"

def get_notion_page_content(page_id):
    try:
        blocks = notion.blocks.children.list(block_id=page_id).get("results", [])
        content = ""
        for block in blocks:
            block_type = block.get("type", "")
            block_data = block.get(block_type, {})
            rich_text = block_data.get("rich_text", [])
            text = "".join([t.get("plain_text", "") for t in rich_text])
            if text:
                content += f"{text}\n"
        return content or "내용이 없습니다."
    except Exception as ex:
        return f"페이지 내용 오류: {str(ex)}"

def create_notion_page(title, content, parent_id=None):
    try:
        if not parent_id:
            pages = notion.search(
                filter={"property": "object", "value": "page"}, page_size=1
            ).get("results", [])
            parent_id = pages[0]["id"] if pages else None
        if not parent_id:
            return "노션 부모 페이지를 찾을 수 없습니다."
        notion.pages.create(
            parent={"page_id": parent_id},
            properties={"title": {"title": [{"text": {"content": title}}]}},
            children=[{
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": content}}]}
            }]
        )
        return f"✅ 노션 페이지 '{title}' 생성 완료!"
    except Exception as ex:
        return f"노션 페이지 생성 오류: {str(ex)}"

def update_notion_page(page_id, content):
    try:
        notion.blocks.children.append(
            block_id=page_id,
            children=[{
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": content}}]}
            }]
        )
        return "✅ 노션 페이지 업데이트 완료!"
    except Exception as ex:
        return f"노션 업데이트 오류: {str(ex)}"

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.chat_id
    msg = update.message.text
    msg_lower = msg.lower()
    if uid not in history:
        history[uid] = []

    creds = user_credentials.get(uid)
    extra_context = ""

    # 구글 캘린더
    if creds and any(w in msg_lower for w in ["일정", "캘린더", "schedule", "calendar", "미팅", "약속"]):
        extra_context += f"\n[Google Calendar - 앞으로 7일]\n{get_calendar_events(creds)}"

    # Gmail
    if creds and any(w in msg_lower for w in ["이메일", "메일", "gmail", "받은", "inbox", "읽지"]):
        extra_context += f"\n[Gmail]\n{get_gmail_messages(creds)}"

    # 노션 검색
    if any(w in msg_lower for w in ["노션", "notion"]):
        search_query = msg.replace("노션", "").replace("notion", "").strip()
        notion_pages = get_notion_pages(search_query)
        extra_context += f"\n[Notion 페이지 목록]\n{notion_pages}"

        # 페이지 내용 읽기 요청
        if any(w in msg_lower for w in ["읽어", "내용", "보여줘", "확인"]):
            pages = notion.search(query=search_query, page_size=3).get("results", [])
            for p in pages[:2]:
                pid = p.get("id", "")
                props = p.get("properties", {})
                title_prop = props.get("title", props.get("Name", props.get("제목", {})))
                title_list = title_prop.get("title", []) if isinstance(title_prop, dict) else []
                title_text = title_list[0].get("plain_text", "제목없음") if title_list else "제목없음"
                page_content = get_notion_page_content(pid)
                extra_context += f"\n[{title_text} 내용]\n{page_content}"

    system_msg = """당신은 개인 비서입니다. 한국어로 대화하세요.
다음 기능을 수행할 수 있습니다:
- Google Calendar: 일정 확인
- Gmail: 이메일 확인, 드래프트 저장, 발송
- Notion: 페이지 검색, 내용 확인, 생성, 업데이트

이메일 드래프트/발송 요청시 이 형식을 요청하세요:
받는사람: [이메일]
제목: [제목]
내용: [내용]

노션 페이지 생성 요청시 이 형식을 요청하세요:
제목: [제목]
내용: [내용]"""

    if extra_context:
        system_msg += f"\n\n실시간 데이터:{extra_context}"

    # 드래프트 저장
    if creds and any(w in msg for w in ["드래프트", "draft", "임시저장"]):
        lines = msg.split("\n")
        to = subject = body_lines = ""
        body_parts = []
        for line in lines:
            if "받는사람:" in line:
                to = line.split(":", 1)[1].strip()
            elif "제목:" in line:
                subject = line.split(":", 1)[1].strip()
            elif "내용:" in line:
                body_parts.append(line.split(":", 1)[1].strip())
            elif to and subject and body_parts:
                body_parts.append(line)
        if to and subject:
            body = "\n".join(body_parts)
            result = create_draft(creds, to, subject, body)
            await update.message.reply_text(result)
            return

    # 이메일 발송
    if creds and any(w in msg for w in ["이메일 보내", "메일 보내", "발송"]):
        lines = msg.split("\n")
        to = subject = ""
        body_parts = []
        for line in lines:
            if "받는사람:" in line:
                to = line.split(":", 1)[1].strip()
            elif "제목:" in line:
                subject = line.split(":", 1)[1].strip()
            elif "내용:" in line:
                body_parts.append(line.split(":", 1)[1].strip())
            elif to and subject and body_parts:
                body_parts.append(line)
        if to and subject:
            body = "\n".join(body_parts)
            result = send_email(creds, to, subject, body)
            await update.message.reply_text(result)
            return

    # 노션 페이지 생성
    if any(w in msg for w in ["노션에 만들어", "노션 페이지 만들어", "노션에 추가", "노션에 저장"]):
        lines = msg.split("\n")
        title = content = ""
        for line in lines:
            if "제목:" in line:
                title = line.split(":", 1)[1].strip()
            elif "내용:" in line:
                content = line.split(":", 1)[1].strip()
        if not title:
            title = lines[0].replace("노션에 만들어", "").replace("노션 페이지 만들어", "").strip()
        result = create_notion_page(title or "새 페이지", content or msg)
        await update.message.reply_text(result)
        return

    history[uid].append({"role": "user", "content": msg})
    await context.bot.send_chat_action(chat_id=uid, action="typing")

    res = claude_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
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
