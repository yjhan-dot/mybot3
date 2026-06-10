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

# ===== GOOGLE AUTH =====
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

# ===== GOOGLE CALENDAR =====
def get_calendar_events(creds):
    try:
        cal = build("calendar", "v3", credentials=creds)
        now = datetime.utcnow().isoformat() + "Z"
        future = (datetime.utcnow() + timedelta(days=30)).isoformat() + "Z"
        events = cal.events().list(
            calendarId="primary", timeMin=now, timeMax=future,
            maxResults=30, singleEvents=True, orderBy="startTime"
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

# ===== GMAIL =====
def get_email_body(msg):
    """이메일 본문 추출"""
    try:
        payload = msg.get("payload", {})
        parts = payload.get("parts", [])
        
        if not parts:
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")[:1000]
        
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")[:1000]
            elif part.get("mimeType") == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    text = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                    # HTML 태그 제거
                    import re
                    text = re.sub('<[^<]+?>', '', text)
                    return text[:1000]
        return ""
    except:
        return ""

def search_gmail_full(creds, query):
    """Gmail 전체 검색 - 제목, 내용, 발신자, 라벨 모두"""
    try:
        gmail = build("gmail", "v1", credentials=creds)
        
        # 다양한 검색 방식 시도
        search_queries = [
            query,  # 원본 쿼리
            f"subject:{query}",  # 제목 검색
            f"from:{query}",  # 발신자 검색
            f"to:{query}",  # 수신자 검색
        ]
        
        found_ids = set()
        messages = []
        
        for sq in search_queries:
            try:
                results = gmail.users().messages().list(
                    userId="me", maxResults=5, q=sq
                ).execute().get("messages", [])
                for m in results:
                    if m["id"] not in found_ids:
                        found_ids.add(m["id"])
                        messages.append(m)
            except:
                pass
        
        if not messages:
            return f"'{query}' 관련 이메일이 없습니다."
        
        result = ""
        for m in messages[:15]:
            try:
                msg = gmail.users().messages().get(
                    userId="me", id=m["id"], format="full"
                ).execute()
                headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
                body = get_email_body(msg)
                labels = msg.get("labelIds", [])
                
                result += f"{'='*30}\n"
                result += f"제목: {headers.get('Subject', '없음')}\n"
                result += f"발신: {headers.get('From', '없음')}\n"
                result += f"수신: {headers.get('To', '없음')}\n"
                result += f"날짜: {headers.get('Date', '없음')}\n"
                result += f"라벨: {', '.join(labels)}\n"
                if body:
                    result += f"내용:\n{body}\n"
                result += "\n"
            except:
                pass
        
        return result[:6000]
    except Exception as ex:
        return f"Gmail 검색 오류: {str(ex)}"

def get_gmail_labels(creds):
    """Gmail 라벨 목록"""
    try:
        gmail = build("gmail", "v1", credentials=creds)
        labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
        return [l["name"] for l in labels]
    except:
        return []

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

# ===== NOTION =====
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
        elif vtype == "checkbox":
            text = str(val.get("checkbox", ""))
        elif vtype == "files":
            files = val.get("files", [])
            urls = []
            for f in files:
                if f.get("type") == "external":
                    urls.append(f.get("external", {}).get("url", ""))
                else:
                    urls.append(f.get("file", {}).get("url", ""))
            text = ", ".join([u for u in urls if u])
        elif vtype == "formula":
            formula = val.get("formula", {})
            ftype = formula.get("type", "")
            if ftype == "string":
                text = formula.get("string", "") or ""
            elif ftype == "number":
                num = formula.get("number")
                text = str(num) if num is not None else ""
        elif vtype == "relation":
            pass
        elif vtype == "rollup":
            pass
        
        # rich_text 타입에 이메일처럼 보이는 값 체크
        if not text and vtype == "rich_text":
            raw = "".join([t.get("plain_text", "") for t in val.get("rich_text", [])])
            if raw:
                text = raw

        if text:
            result += f"  {key}: {text}\n"
    return result

def read_pdf_bytes(content):
    try:
        pdf_file = io.BytesIO(content)
        reader = PyPDF2.PdfReader(pdf_file)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text[:3000] if text else ""
    except:
        return ""

def read_docx_bytes(content):
    try:
        doc_file = io.BytesIO(content)
        doc = docx.Document(doc_file)
        text = "\n".join([p.text for p in doc.paragraphs if p.text])
        return text[:3000] if text else ""
    except:
        return ""

def read_file_from_url(url, filename=""):
    """URL에서 파일 다운로드 후 읽기"""
    try:
        headers = {"Authorization": f"Bearer {NOTION_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=30)
        content = response.content
        
        fname = filename.lower() if filename else url.lower()
        if fname.endswith(".pdf") or "pdf" in fname:
            return read_pdf_bytes(content)
        elif fname.endswith((".docx", ".doc")) or "doc" in fname:
            return read_docx_bytes(content)
        else:
            try:
                return content.decode("utf-8")[:2000]
            except:
                return f"[바이너리 파일: {len(content)} bytes]"
    except Exception as ex:
        return f"파일 읽기 오류: {str(ex)}"

def get_notion_page_content_full(page_id):
    """노션 페이지 전체 내용 + 첨부 파일까지"""
    try:
        blocks = notion.blocks.children.list(block_id=page_id, page_size=100).get("results", [])
        content = ""
        file_contents = ""
        
        for block in blocks:
            block_type = block.get("type", "")
            block_data = block.get(block_type, {})
            
            # 텍스트 내용
            rich_text = block_data.get("rich_text", [])
            text = "".join([t.get("plain_text", "") for t in rich_text])
            if text:
                content += f"{text}\n"
            
            # 파일 첨부
            if block_type in ["file", "pdf"]:
                file_data = block_data
                file_type = file_data.get("type", "")
                if file_type == "external":
                    url = file_data.get("external", {}).get("url", "")
                else:
                    url = file_data.get("file", {}).get("url", "")
                name = file_data.get("name", "파일")
                if url:
                    file_content = read_file_from_url(url, name)
                    if file_content:
                        file_contents += f"\n[첨부파일: {name}]\n{file_content}\n"
            
            # 하위 블록
            if block.get("has_children"):
                try:
                    sub_blocks = notion.blocks.children.list(block_id=block["id"]).get("results", [])
                    for sub in sub_blocks:
                        sub_type = sub.get("type", "")
                        sub_data = sub.get(sub_type, {})
                        sub_text = "".join([t.get("plain_text", "") for t in sub_data.get("rich_text", [])])
                        if sub_text:
                            content += f"  {sub_text}\n"
                except:
                    pass
        
        return (content + file_contents)[:4000]
    except:
        return ""

def get_page_title(page):
    props = page.get("properties", {})
    for key in ["title", "Name", "제목", "이름"]:
        prop = props.get(key, {})
        if isinstance(prop, dict):
            title_list = prop.get("title", [])
            if title_list:
                return title_list[0].get("plain_text", "")
    return "제목없음"

def scan_all_notion(query=""):
    """노션 전체 스캔 - 모든 DB, 페이지, 첨부파일"""
    try:
        full_content = ""
        query_lower = query.lower()
        query_words = [w for w in query_lower.split() if len(w) > 1] if query_lower else []

        # 전체 검색
        all_results = notion.search(page_size=100).get("results", [])
        
        # DB ID 수집
        db_ids = set()
        for r in all_results:
            if r.get("object") == "database":
                db_ids.add(r["id"])
            elif r.get("object") == "page":
                parent = r.get("parent", {})
                if parent.get("type") == "database_id":
                    db_ids.add(parent["database_id"])

        # 모든 DB 항목 스캔
        for db_id in db_ids:
            try:
                items = notion.databases.query(
                    database_id=db_id,
                    page_size=100
                ).get("results", [])
                
                for item in items:
                    props_text = read_page_properties(item)
                    page_content = get_notion_page_content_full(item["id"])
                    full_text = (props_text + page_content).lower()
                    
                    # 검색어 매칭
                    if not query_words or any(w in full_text for w in query_words):
                        full_content += f"\n{'='*20}\n"
                        full_content += props_text
                        if page_content:
                            full_content += f"  [페이지 내용]\n{page_content[:1000]}\n"
            except:
                pass

        # 일반 페이지 스캔
        for r in all_results:
            if r.get("object") == "page" and r.get("parent", {}).get("type") != "database_id":
                title = get_page_title(r)
                content = get_notion_page_content_full(r["id"])
                full_text = (title + content).lower()
                if not query_words or any(w in full_text for w in query_words):
                    full_content += f"\n=== {title} ===\n{content[:1000]}\n"

        return full_content[:8000] if full_content else f"'{query}' 관련 데이터 없음."
    except Exception as ex:
        return f"노션 스캔 오류: {str(ex)}"

# ===== DROPBOX =====
def dropbox_list_folder(path=""):
    try:
        result = dbx.files_list_folder(path if path else "")
        files = []
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FileMetadata):
                files.append(f"📄 {entry.path_display}")
            elif isinstance(entry, dropbox.files.FolderMetadata):
                files.append(f"📁 {entry.path_display}/")
        return "\n".join(files) if files else "파일 없음"
    except Exception as ex:
        return f"드롭박스 오류: {str(ex)}"

def dropbox_read_file(path):
    try:
        metadata, response = dbx.files_download(path)
        content = response.content
        if path.lower().endswith(".pdf"):
            return read_pdf_bytes(content)
        elif path.lower().endswith((".docx", ".doc")):
            return read_docx_bytes(content)
        else:
            try:
                return content.decode("utf-8")[:3000]
            except:
                return f"바이너리 파일: {len(content)} bytes"
    except Exception as ex:
        return f"파일 읽기 오류: {str(ex)}"

def dropbox_scan_folder(folder_path):
    """드롭박스 폴더 전체 스캔 + 파일 내용 읽기"""
    try:
        result = dbx.files_list_folder(folder_path, recursive=True)
        all_content = ""
        
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FileMetadata):
                path = entry.path_display
                if path.lower().endswith((".pdf", ".docx", ".doc", ".txt")):
                    file_content = dropbox_read_file(path)
                    if file_content:
                        all_content += f"\n{'='*20}\n[파일: {path}]\n{file_content[:2000]}\n"
        
        return all_content[:8000] if all_content else "읽을 수 있는 파일이 없습니다."
    except Exception as ex:
        return f"드롭박스 스캔 오류: {str(ex)}"

def dropbox_search_and_read(query):
    """드롭박스 검색 + 파일 내용 읽기"""
    try:
        result = dbx.files_search_v2(query)
        content = ""
        
        for match in result.matches[:10]:
            metadata = match.metadata
            if hasattr(metadata, 'metadata'):
                entry = metadata.metadata
                path = entry.path_display
                content += f"\n[파일: {path}]\n"
                file_content = dropbox_read_file(path)
                if file_content:
                    content += file_content[:2000] + "\n"
        
        return content[:6000] if content else f"'{query}' 파일 없음."
    except Exception as ex:
        return f"드롭박스 검색 오류: {str(ex)}"

def dropbox_save_file(path, content):
    try:
        dbx.files_upload(
            content.encode("utf-8"),
            path,
            mode=dropbox.files.WriteMode.overwrite
        )
        return f"✅ 드롭박스 저장 완료! 경로: {path}"
    except Exception as ex:
        return f"저장 오류: {str(ex)}"

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

# ===== MAIN CHAT =====
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

    # Gmail - 항상 전체 검색
    if creds and any(w in msg_lower for w in ["이메일", "메일", "gmail", "email", "mail"]):
        search_q = msg_lower
        for w in ["이메일", "메일", "gmail", "email", "mail", "찾아줘", "검색", "알려줘",
                  "받은", "보내줘", "드래프트", "draft", "에서", "에게", "한테"]:
            search_q = search_q.replace(w, "").strip()
        extra_context += f"\n[Gmail 검색: '{search_q}']\n{search_gmail_full(creds, search_q if search_q else 'is:inbox')}"

    # 노션 - 주소/이름/트랜젝션 등 포함시 항상 전체 스캔
    notion_trigger = any(w in msg_lower for w in [
        "노션", "notion", "찾아줘", "찾아서", "트랜젝션", "transaction",
        "주소", "address", "고객", "클로징", "closing", "계약", "파일",
        "정보", "이름", "전화", "이메일 주소"
    ])
    if notion_trigger:
        search_q = msg_lower
        for w in ["노션에서", "노션", "notion", "찾아줘", "찾아서", "알려줘",
                  "드래프트", "만들어줘", "영어로", "한국어로", "작성해줘", "이메일"]:
            search_q = search_q.replace(w, "").strip()
        notion_data = scan_all_notion(search_q)
        extra_context += f"\n[Notion 전체 데이터]\n{notion_data}"
        if creds:
            extra_context += f"\n[Google Calendar]\n{get_calendar_events(creds)}"

    # 드롭박스 - Binding 폴더 자동 포함
    if any(w in msg_lower for w in ["드롭박스", "dropbox", "binding", "바인딩", "서류", "계약서"]):
        # Binding 폴더 스캔
        binding_content = dropbox_scan_folder("/Binding")
        extra_context += f"\n[Dropbox Binding 폴더]\n{binding_content}"
        
        # 추가 검색
        search_q = msg_lower
        for w in ["드롭박스", "dropbox", "binding", "바인딩", "서류", "찾아줘", "읽어줘"]:
            search_q = search_q.replace(w, "").strip()
        if search_q:
            search_result = dropbox_search_and_read(search_q)
            extra_context += f"\n[Dropbox 검색: '{search_q}']\n{search_result}"

    # 직접 드래프트
    if creds and "받는사람:" in msg:
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

    system_msg = """You are a personal real estate assistant. Respond in the same language the user uses.
NEVER output JSON, function_calls, or code in your response.
Use the provided real-time data to answer directly and take action.

## Auto Draft Creation
When user asks to find info and create a draft, use provided data and output at the END:

DRAFT_TO: [email]
DRAFT_SUBJECT: [subject]
DRAFT_BODY_START
[email body]
DRAFT_BODY_END

## Dropbox File Save
DROPBOX_SAVE_PATH: [path]
DROPBOX_SAVE_CONTENT_START
[content]
DROPBOX_SAVE_CONTENT_END"""

    if extra_context:
        system_msg += f"\n\n=== Real-time Data ===\n{extra_context}"

    history[uid].append({"role": "user", "content": msg})
    await context.bot.send_chat_action(chat_id=uid, action="typing")

    res = claude_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        system=system_msg,
        messages=history[uid]
    )
    reply = res.content[0].text

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
