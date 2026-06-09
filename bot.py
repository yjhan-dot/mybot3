import os
import json
import base64
import io
import requests
import anthropic
import dropbox
import PyPDF2
import docx
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
DROPBOX_TOKEN = os.environ["DROPBOX_TOKEN"]

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify"
]

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
notion = NotionClient(auth=NOTION_TOKEN)
dbx = dropbox.Dropbox(DROPBOX_TOKEN)
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
        return f"✅ 드래프트 저장 완료!\n받는사람: {to}\n제목: {subject}"
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

def read_pdf_from_url(url):
    try:
        headers = {"Authorization": f"Bearer {NOTION_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=30)
        pdf_file = io.BytesIO(response.content)
        reader = PyPDF2.PdfReader(pdf_file)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text[:4000] if text else "PDF 내용을 읽을 수 없습니다."
    except Exception as ex:
        return f"PDF 읽기 오류: {str(ex)}"

def read_docx_from_url(url):
    try:
        headers = {"Authorization": f"Bearer {NOTION_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=30)
        doc_file = io.BytesIO(response.content)
        doc = docx.Document(doc_file)
        text = "\n".join([para.text for para in doc.paragraphs if para.text])
        return text[:4000] if text else "Word 파일 내용을 읽을 수 없습니다."
    except Exception as ex:
        return f"Word 읽기 오류: {str(ex)}"

def get_notion_page_files(page_id):
    """노션 페이지에서 첨부 파일 URL 가져오기"""
    try:
        blocks = notion.blocks.children.list(block_id=page_id, page_size=100).get("results", [])
        files_content = ""
        for block in blocks:
            block_type = block.get("type", "")
            
            # 파일 블록
            if block_type == "file":
                file_data = block.get("file", {})
                file_type = file_data.get("type", "")
                if file_type == "external":
                    url = file_data.get("external", {}).get("url", "")
                else:
                    url = file_data.get("file", {}).get("url", "")
                name = block.get("file", {}).get("name", "파일")
                if url:
                    files_content += f"\n[파일: {name}]\n"
                    if url.lower().endswith(".pdf"):
                        files_content += read_pdf_from_url(url)
                    elif url.lower().endswith((".docx", ".doc")):
                        files_content += read_docx_from_url(url)
                    else:
                        files_content += f"파일 URL: {url}\n"
            
            # PDF 블록
            elif block_type == "pdf":
                pdf_data = block.get("pdf", {})
                file_type = pdf_data.get("type", "")
                if file_type == "external":
                    url = pdf_data.get("external", {}).get("url", "")
                else:
                    url = pdf_data.get("file", {}).get("url", "")
                if url:
                    files_content += f"\n[PDF 파일]\n"
                    files_content += read_pdf_from_url(url)
            
            # 이미지 블록 (URL만 제공)
            elif block_type == "image":
                img_data = block.get("image", {})
                file_type = img_data.get("type", "")
                if file_type == "external":
                    url = img_data.get("external", {}).get("url", "")
                else:
                    url = img_data.get("file", {}).get("url", "")
                if url:
                    files_content += f"\n[이미지: {url}]\n"

        return files_content if files_content else "첨부 파일이 없습니다."
    except Exception as ex:
        return f"파일 읽기 오류: {str(ex)}"

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
                    items = notion.databases.query(database_id=db_id, page_size=50).get("results", [])
                    for item in items:
                        props_text = read_page_properties(item)
                        if props_text:
                            full_content += props_text + "\n"
                except Exception as e:
                    full_content += f"  DB 읽기 오류: {str(e)}\n"
        return full_content[:6000] if full_content else f"'{query}' 검색 결과가 없습니다."
    except Exception as ex:
        return f"노션 오류: {str(ex)}"

def dropbox_list_files(path=""):
    try:
        result = dbx.files_list_folder(path if path else "")
        files = []
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FileMetadata):
                files.append(f"📄 {entry.path_display} ({entry.size:,} bytes)")
            elif isinstance(entry, dropbox.files.FolderMetadata):
                files.append(f"📁 {entry.path_display}/")
        return "\n".join(files) if files else "파일이 없습니다."
    except Exception as ex:
        return f"드롭박스 오류: {str(ex)}"

def dropbox_read_file(path):
    try:
        metadata, response = dbx.files_download(path)
        content = response.content
        if path.lower().endswith(".pdf"):
            return read_pdf_from_url_bytes(content)
        elif path.lower().endswith((".docx", ".doc")):
            return read_docx_from_bytes(content)
        else:
            try:
                return content.decode("utf-8")[:3000]
            except:
                return f"바이너리 파일입니다. 크기: {len(content):,} bytes"
    except Exception as ex:
        return f"파일 읽기 오류: {str(ex)}"

def read_pdf_from_url_bytes(content):
    try:
        pdf_file = io.BytesIO(content)
        reader = PyPDF2.PdfReader(pdf_file)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text[:4000] if text else "PDF 내용을 읽을 수 없습니다."
    except Exception as ex:
        return f"PDF 읽기 오류: {str(ex)}"

def read_docx_from_bytes(content):
    try:
        doc_file = io.BytesIO(content)
        doc = docx.Document(doc_file)
        text = "\n".join([para.text for para in doc.paragraphs if para.text])
        return text[:4000] if text else "Word 파일 내용을 읽을 수 없습니다."
    except Exception as ex:
        return f"Word 읽기 오류: {str(ex)}"

def dropbox_save_file(path, content):
    try:
        dbx.files_upload(
            content.encode("utf-8"),
            path,
            mode=dropbox.files.WriteMode.overwrite
        )
        return f"✅ 드롭박스에 저장 완료! 경로: {path}"
    except Exception as ex:
        return f"파일 저장 오류: {str(ex)}"

def dropbox_search_files(query):
    try:
        result = dbx.files_search_v2(query)
        files = []
        for match in result.matches[:10]:
            metadata = match.metadata
            if hasattr(metadata, 'metadata'):
                entry = metadata.metadata
                files.append(f"- {entry.path_display}")
        return "\n".join(files) if files else f"'{query}' 파일을 찾을 수 없습니다."
    except Exception as ex:
        return f"드롭박스 검색 오류: {str(ex)}"

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

    # 구글 캘린더
    if creds and any(w in msg_lower for w in ["일정", "캘린더", "schedule", "calendar", "미팅", "약속"]):
        extra_context += f"\n[Google Calendar]\n{get_calendar_events(creds)}"

    # Gmail
    if creds and any(w in msg_lower for w in ["이메일", "메일", "gmail", "받은", "inbox"]):
        extra_context += f"\n[Gmail]\n{get_gmail_messages(creds)}"

    # 노션 파일 읽기
    if any(w in msg_lower for w in ["노션", "notion"]) and any(w in msg_lower for w in ["파일", "pdf", "첨부", "문서", "word"]):
        search_query = msg_lower
        for w in ["노션에서", "노션", "notion", "파일", "pdf", "첨부", "문서", "읽어", "분석", "열어"]:
            search_query = search_query.replace(w, "").strip()
        results = notion.search(query=search_query, page_size=5).get("results", [])
        for r in results:
            if r.get("object") == "page":
                title = get_page_title(r)
                files = get_notion_page_files(r["id"])
                if "첨부 파일이 없습니다." not in files:
                    extra_context += f"\n[노션 페이지 '{title}' 첨부파일]\n{files}"

    # 노션 일반 검색
    elif any(w in msg_lower for w in ["노션", "notion"]) or (any(w in msg_lower for w in ["드래프트", "draft"]) and any(w in msg_lower for w in ["찾아서", "노션", "notion"])):
        search_query = msg_lower
        for w in ["노션에서", "노션", "notion", "찾아줘", "알려줘", "보여줘", "드래프트", "draft", "이메일", "만들어줘"]:
            search_query = search_query.replace(w, "").strip()
        notion_data = search_notion_full(search_query if search_query else msg)
        extra_context += f"\n[Notion 데이터]\n{notion_data}"
        if creds and any(w in msg_lower for w in ["드래프트", "draft"]):
            extra_context += f"\n[Google Calendar]\n{get_calendar_events(creds)}"

    # 드롭박스
    if any(w in msg_lower for w in ["드롭박스", "dropbox"]):
        if any(w in msg_lower for w in ["목록", "list", "파일 보여", "뭐 있어"]):
            extra_context += f"\n[Dropbox 파일 목록]\n{dropbox_list_files()}"
        elif any(w in msg_lower for w in ["읽어", "열어", "분석"]):
            search_q = msg_lower
            for w in ["드롭박스", "dropbox", "읽어줘", "열어줘", "분석해줘"]:
                search_q = search_q.replace(w, "").strip()
            search_result = dropbox_search_files(search_q)
            extra_context += f"\n[Dropbox 검색]\n{search_result}"
            # 파일 경로가 있으면 내용 읽기
            if "/" in search_result:
                path = search_result.split("- ")[1].split("\n")[0].strip() if "- " in search_result else ""
                if path:
                    content = dropbox_read_file(path)
                    extra_context += f"\n[파일 내용]\n{content}"
        elif any(w in msg_lower for w in ["찾아", "검색"]):
            search_q = msg_lower.replace("드롭박스", "").replace("dropbox", "").replace("찾아줘", "").replace("검색", "").strip()
            extra_context += f"\n[Dropbox 검색]\n{dropbox_search_files(search_q)}"

    # 직접 드래프트 형식
    if creds and any(w in msg for w in ["드래프트", "draft"]) and "받는사람:" in msg:
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

    # 이메일 발송
    if creds and any(w in msg for w in ["이메일 보내", "메일 보내", "발송해"]) and "받는사람:" in msg:
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

    # 드롭박스 저장
    if any(w in msg for w in ["드롭박스에 저장", "dropbox에 저장"]):
        lines = msg.split("\n")
        path = "/새파일.txt"
        content_parts = []
        for line in lines:
            if "경로:" in line:
                path = line.split(":", 1)[1].strip()
            elif "내용:" in line:
                content_parts.append(line.split(":", 1)[1].strip())
            elif content_parts:
                content_parts.append(line)
        result = dropbox_save_file(path, "\n".join(content_parts))
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
        result = create_notion_page(title or "새 페이지", content or msg)
        await update.message.reply_text(result)
        return

    system_msg = """당신은 개인 비서입니다. 한국어로 대화하세요.
절대로 JSON, function_calls 같은 코드를 출력하지 마세요.
제공된 실시간 데이터를 바탕으로 직접 답변하세요.

## 드래프트 자동 생성
노션/드롭박스 데이터에서 정보를 찾아 드래프트를 만들 때 반드시 아래 형식으로 답변 끝에 추가:

DRAFT_TO: [이메일주소]
DRAFT_SUBJECT: [제목]
DRAFT_BODY_START
[이메일 본문]
DRAFT_BODY_END

## 드롭박스 파일 저장
파일을 드롭박스에 저장할 때:
DROPBOX_SAVE_PATH: [경로]
DROPBOX_SAVE_CONTENT_START
[내용]
DROPBOX_SAVE_CONTENT_END"""

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

    # 자동 드래프트 생성
    if creds and "DRAFT_TO:" in reply:
        try:
            to = reply.split("DRAFT_TO:")[1].split("\n")[0].strip()
            subject = reply.split("DRAFT_SUBJECT:")[1].split("\n")[0].strip()
            body = reply.split("DRAFT_BODY_START")[1].split("DRAFT_BODY_END")[0].strip()
            draft_result = create_draft(creds, to, subject, body)
            clean_reply = reply.split("DRAFT_TO:")[0].strip()
            await update.message.reply_text(clean_reply)
            await update.message.reply_text(draft_result)
        except:
            await update.message.reply_text(reply)
    elif "DROPBOX_SAVE_PATH:" in reply:
        try:
            path = reply.split("DROPBOX_SAVE_PATH:")[1].split("\n")[0].strip()
            content = reply.split("DROPBOX_SAVE_CONTENT_START")[1].split("DROPBOX_SAVE_CONTENT_END")[0].strip()
            save_result = dropbox_save_file(path, content)
            clean_reply = reply.split("DROPBOX_SAVE_PATH:")[0].strip()
            await update.message.reply_text(clean_reply)
            await update.message.reply_text(save_result)
        except:
            await update.message.reply_text(reply)
    else:
        await update.message.reply_text(reply)

    history[uid].append({"role": "assistant", "content": reply})

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
