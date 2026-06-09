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
            result += f"- 제목: {headers.get('Subject', '없음')}\n  발신: {headers.get('From', '없음')}\n  미리보기: {snippet}\n\n"
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
        return "✅ 드래프트 저장 완료!"
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

def get_page_title(page):
    props = page.get("properties", {})
    for key in ["title", "Name", "제목", "이름"]:
        prop = props.get(key, {})
        if isinstance(prop, dict):
            title_list = prop.get("title", [])
            if title_list:
                return title_list[0].get("plain_text", "")
    return "제목없음"

def get_notion_page_content(page_id):
    try:
        blocks = notion.blocks.children.list(block_id=page_id, page_size=50).get("results", [])
        content = ""
        for block in blocks:
            block_type = block.get("type", "")
            block_data = block.get(block_type, {})
            rich_text = block_data.get("rich_text", [])
            text = "".join([t.get("plain_text", "") for t in rich_text])
            if text:
                content += f"{text}\n"
        return content[:2000] if content else ""
    except:
        return ""

def read_page_properties(page):
    props = page.get("properties", {})
    result = ""
    for key, val in props.items():
        vtype = val.get("type", "")
        text = ""
        if vtype == "title":
            text = "".join([t.get("plain_text", "") for t in val.get("title", [])])
        elif vtype == "rich_text":
            text = "".join([t.get("plain_text", "") for t in val.get("rich_text", [])])
        elif vtype == "email":
            text = val.get("email", "") or ""
        elif vtype == "phone_number":
            text = val.get("phone_number", "") or ""
        elif vtype == "number":
            num = val.get("number")
            text = str(num) if num is not None else ""
        elif vtype == "select":
            sel = val.get("select")
            text = sel.get("name", "") if sel else ""
        elif vtype == "multi_select":
            text = ", ".join([s.get("name", "") for s in val.get("multi_select", [])])
        elif vtype == "url":
            text = val.get("url", "") or ""
        elif vtype == "date":
            date = val.get("date")
            text = date.get("start", "") if date else ""
        if text:
            result += f"  {key}: {text}\n"
    return result

def search_notion_full(query):
    try:
        full_content = ""

        # 1. 키워드로 검색
        results = notion.search(query=query, page_size=20).get("results", [])

        for r in results:
            obj_type = r.get("object", "")
            parent_type = r.get("parent", {}).get("type", "")

            if obj_type == "page" and parent_type == "database_id":
                props_text = read_page_properties(r)
                if props_text:
                    full_content += f"\n--- 항목 ---\n{props_text}"

            elif obj_type == "page":
                title = get_page_title(r)
                content = get_notion_page_content(r["id"])
                if title or content:
                    full_content += f"\n=== {title} ===\n{content}\n"

            elif obj_type == "database":
                db_id = r["id"]
                title_list = r.get("title", [])
                db_title = title_list[0].get("plain_text", "DB") if title_list else "DB"
                full_content += f"\n=== 데이터베이스: {db_title} ===\n"
                try:
                    items = notion.databases.query(
                        database_id=db_id,
                        page_size=50
                    ).get("results", [])
                    for item in items:
                        props_text = read_page_properties(item)
                        if props_text:
                            full_content += props_text + "\n"
                except Exception as e:
                    full_content += f"  DB 읽기 오류: {str(e)}\n"

        return full_content[:6000] if full_content else f"'{query}' 검색 결과가 없습니다."
    except Exception as ex:
        return f"노션 오류: {str(ex)}"

def create_notion_page(title, content):
    try:
        pages = notion.search(page_size=1).get("results", [])
        parent_id = pages[0]["id"] if pages else None
        if not parent_id:
            return "노션 페이지를 찾을 수 없습니다."
        notion.pages.create(
            parent={"page_id": parent_id},
            properties={"title": {"title": [{"text": {"content": title}}]}},
            children=[{"object": "block", "type": "paragraph",
                       "paragraph": {"rich_text": [{"text": {"content": content}}]}}]
        )
        return f"✅ 노션 페이지 '{title}' 생성 완료!"
    except Exception as ex:
        return f"노션 생성 오류: {str(ex)}"

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.chat_id
    msg = update.message.text
    msg_lower = msg.lower()
    if uid not in history:
        history[uid] = []

    creds = user_credentials.get(uid)
    extra_context = ""

    if creds and any(w in msg_lower for w in ["일정", "캘린더", "schedule", "calendar", "미팅", "약속"]):
        extra_context += f"\n[Google Calendar]\n{get_calendar_events(creds)}"

    if creds and any(w in msg_lower for w in ["이메일", "메일", "gmail", "받은", "inbox"]):
        extra_context += f"\n[Gmail]\n{get_gmail_messages(creds)}"

    if any(w in msg_lower for w in ["노션", "notion"]):
        search_query = msg_lower
        for w in ["노션에서", "노션", "notion", "찾아줘", "알려줘", "보여줘"]:
            search_query = search_query.replace(w, "").strip()
        if not search_query:
            search_query = ""
        extra_context += f"\n[Notion]\n{search_notion_full(search_query)}"

    if creds and any(w in msg for w in ["드래프트", "draft", "임시저장"]):
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
            elif body_parts:
                body_parts.append(line)
        if to and subject:
            result = create_draft(creds, to, subject, "\n".join(body_parts))
            await update.message.reply_text(result)
            return

    if creds and any(w in msg for w in ["이메일 보내", "메일 보내", "발송해"]):
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
            elif body_parts:
                body_parts.append(line)
        if to and subject:
            result = send_email(creds, to, subject, "\n".join(body_parts))
            await update.message.reply_text(result)
            return

    if any(w in msg for w in ["노션에 만들어", "노션 페이지 만들어", "노션에 추가", "노션에 저장"]):
        lines = msg.split("\n")
        title = content = ""
        for line in lines:
            if "제목:" in line:
                title = line.split(":", 1)[1].strip()
            elif "내용:" in line:
                content = line.split(":", 1)[1].strip()
        result = create_notion_page(title or "새 페이지", content or msg)
        await update.message.reply_text(result)
        return

    system_msg = """당신은 개인 비서입니다. 한국어로 대화하세요.
절대로 JSON, function_calls 같은 코드를 출력하지 마세요.
제공된 실시간 데이터를 바탕으로 직접 답변하세요.

이메일 드래프트/발송 요청시:
받는사람: [이메일]
제목: [제목]
내용: [내용]"""

    if extra_context:
        system_msg += f"\n\n=== 실시간 데이터 ===\n{extra_context}"

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
