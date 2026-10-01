import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import changes
from app.db import get_db
from app.main import app
from app.models import ChangeStatus


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def send(client, user_id, text, *, secret="chat-secret", ts=None):
    body = json.dumps({"user_id": user_id, "text": text}).encode()
    ts = str(ts or int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return client.post("/api/chat/command", content=body,
                       headers={"X-CC-Timestamp": ts, "X-CC-Signature": sig, "Content-Type": "application/json"})


@pytest.fixture
def approved(db, f, world):
    world.op_net.chat_user_id = "chat-op"
    world.req.chat_user_id = "chat-req"
    cr = f.change(world.req, devices=[world.sw], modules=[world.precheck])
    changes.transition(db, world.req, cr, "submit")
    changes.transition(db, world.appr, cr, "approve")
    db.commit()  # 只釋放 savepoint；讓 chat 失敗時的 rollback 不會連同測試資料一起退回
    return cr


def test_signature_and_replay_window(client, approved):
    assert send(client, "chat-op", "status x", secret="wrong").status_code == 401
    assert send(client, "chat-op", "status x", ts=int(time.time()) - 600).status_code == 401
    assert "尚未綁定" in send(client, "stranger", f"status {approved.number}").json()["reply"]


def test_basic_status_updates(client, approved):
    n = approved.number
    reply = send(client, "chat-op", f"/change start {n}").json()["reply"]
    assert "執行中" in reply and f"/changes/{approved.id}" in reply
    assert "暫停" in send(client, "chat-op", f"pause {n} 等待廠商").json()["reply"]
    assert approved.status == ChangeStatus.PAUSED
    send(client, "chat-op", f"resume {n}")
    reply = send(client, "chat-op", f"finish {n}").json()["reply"]
    assert "無法執行" in reply and "未處理" in reply  # 步驟細節必須在網頁完成
    assert approved.status == ChangeStatus.IN_PROGRESS


def test_chat_respects_rbac_and_web_only_actions(client, approved):
    n = approved.number
    assert "無法執行" in send(client, "chat-req", f"start {n}").json()["reply"]  # 申請人沒有執行權限
    assert "可用指令" in send(client, "chat-req", f"approve {n}").json()["reply"]
    assert "可用指令" in send(client, "chat-req", f"close {n}").json()["reply"]
    assert "找不到" in send(client, "chat-req", "status CR-00000000-9999").json()["reply"]
    assert approved.status == ChangeStatus.APPROVED
