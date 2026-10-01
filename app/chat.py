"""聊天 bot 整合（海聊）。

Bot 收到使用者訊息後，將 {"user_id": <聊天帳號 ID>, "text": <訊息>} POST 到 /api/chat/command，
並把回應中的 reply 傳回聊天室。請求需帶：
  X-CC-Timestamp: Unix 秒數
  X-CC-Signature: hex(HMAC-SHA256(CHAT_SECRET, f"{timestamp}." + raw_body))
聊天只能查詢與更新基礎狀態（CHAT_ACTIONS），其餘一律導回網頁。
"""

import hashlib
import hmac
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.changes import CHAT_ACTIONS, add_comment, transition
from app.db import get_db
from app.errors import DomainError
from app.labels import LABELS
from app.models import ChangeRequest, StepStatus, User

router = APIRouter(prefix="/api/chat")

MAX_SKEW = 300
HELP = (
    "可用指令：\n"
    "status <變更單號>\n"
    "start | pause | resume | finish <變更單號> [說明]\n"
    "comment <變更單號> <內容>\n"
    "計畫、步驟細節、核准與結案請至網頁操作。"
)


def _verify(request: Request, body: bytes):
    if not config.CHAT_SECRET:
        raise HTTPException(503, "chat integration disabled")
    ts = request.headers.get("X-CC-Timestamp", "")
    sig = request.headers.get("X-CC-Signature", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > MAX_SKEW:
        raise HTTPException(401, "invalid timestamp")
    expected = hmac.new(config.CHAT_SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(401, "invalid signature")


@router.post("/command")
async def command(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    _verify(request, body)
    try:
        payload = json.loads(body)
        chat_user, text = str(payload["user_id"]), str(payload["text"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "invalid payload") from None
    user = db.scalar(select(User).filter_by(chat_user_id=chat_user, is_active=True))
    if not user:
        return {"reply": "此聊天帳號尚未綁定變更管理系統帳號，請聯絡系統管理員。"}
    return {"reply": handle(db, user, text)}


def handle(db, user, text: str) -> str:
    tokens = text.split()
    if tokens and tokens[0].lstrip("/").lower() in ("change", "cc"):
        tokens = tokens[1:]
    if len(tokens) < 2:
        return HELP
    cmd, number, rest = tokens[0].lower(), tokens[1], " ".join(tokens[2:])
    cr = db.scalar(select(ChangeRequest).where(func.upper(ChangeRequest.number) == number.upper()))
    if not cr:
        return f"找不到變更 {number}"
    link = f"{config.BASE_URL}/changes/{cr.id}"
    try:
        if cmd in CHAT_ACTIONS:
            transition(db, user, cr, cmd, source="chat", comment=rest)
        elif cmd == "comment":
            add_comment(db, user, cr, rest, source="chat")
        elif cmd != "status":
            return HELP
        db.commit()
    except DomainError as e:
        db.rollback()
        return f"無法執行：{e}\n{link}"
    return f"{summary(cr)}\n{link}"


def summary(cr: ChangeRequest) -> str:
    executions = [e for s in cr.steps for e in s.executions]
    done = sum(1 for e in executions if e.status != StepStatus.PENDING)
    window = "未排定"
    if cr.window_start and cr.window_end:
        start, end = cr.window_start.astimezone(config.TZ), cr.window_end.astimezone(config.TZ)
        window = f"{start:%m/%d %H:%M} – {end:%m/%d %H:%M}"
    return (
        f"{cr.number} {cr.title}\n"
        f"狀態：{LABELS[cr.status]}｜時段：{window}\n"
        f"設備：{len(cr.targets)} 台｜步驟進度：{done}/{len(executions)}"
    )
