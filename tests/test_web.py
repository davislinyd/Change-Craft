import re

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import ChangeStatus, ModuleStatus, Role, SkipPolicy


@pytest.fixture
def login(db):
    def override():
        try:
            yield db
        except Exception:
            db.rollback()  # 與正式環境相同：失敗的請求不留下任何寫入
            raise

    app.dependency_overrides[get_db] = override

    def _login(username, password="password123"):
        c = TestClient(app)
        c.csrf = re.search(r'name="csrf" value="(\w+)"', c.get("/login").text).group(1)
        r = c.post("/login", data={"username": username, "password": password, "csrf": c.csrf})
        assert r.status_code == 200 and "登出" in r.text, r.text
        c.csrf = re.search(r'name="csrf" value="(\w+)"', r.text).group(1)  # 登入會重建 session
        return c

    yield _login
    app.dependency_overrides.clear()


def post(c, url, **data):
    r = c.post(url, data={"csrf": c.csrf, **data})
    assert r.status_code == 200, r.text
    return r


def test_login_required_and_bad_password(login, f):
    f.user("alice", Role.REQUESTER)
    c = TestClient(app)
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    token = re.search(r'name="csrf" value="(\w+)"', c.get("/login").text).group(1)
    r = c.post("/login", data={"username": "alice", "password": "wrong-password", "csrf": token})
    assert "帳號或密碼錯誤" in r.text


def test_csrf_required(login, f):
    f.user("alice", Role.REQUESTER)
    c = login("alice")
    assert c.post("/changes", data={"title": "x", "purpose": "y"}).status_code == 403
    assert c.post("/changes", data={"title": "x", "purpose": "y", "csrf": "bad"}).status_code == 403


def test_end_to_end_via_web(db, login, f, world):
    w = world
    f.user("admin", Role.ADMIN, Role.AUDITOR)
    f.template(w.editor, "A", [(w.precheck, SkipPolicy.REQUIRED), (w.vlan, SkipPolicy.OPTIONAL)])
    db.flush()

    a = login("admin")
    r = post(a, "/devices", hostname="access-sw-12", device_type="NETWORK", ip="10.10.2.12", environment="production")
    assert "access-sw-12" in r.text
    assert "admin" in a.get("/admin/users").text
    assert "user.login" in a.get("/audit").text

    req = login("req")
    tpl_id = re.search(r'<option value="(\d+)">A ', req.get("/changes/new").text).group(1)
    r = post(req, "/changes", title="Web VLAN", purpose="新增 VLAN", window_start="2000-01-01T00:00",
             window_end="2099-01-01T00:00", template_id=tpl_id, risk="LOW")
    cid = int(re.search(r"/changes/(\d+)/action", r.text).group(1))
    post(req, f"/changes/{cid}/targets", device_id=str(w.sw.id))
    r = post(req, f"/changes/{cid}/action", action="approve")
    assert "申請人不能核准自己的變更" in r.text or "目前狀態" in r.text  # 規則錯誤以 flash 顯示
    post(req, f"/changes/{cid}/action", action="submit")
    assert "Web VLAN" in req.get("/changes", params={"ip": "10.10.1.0/24"}).text
    assert "格式不正確" in req.get("/changes", params={"ip": "bad"}).text

    post(login("appr"), f"/changes/{cid}/action", action="approve")
    op = login("op_net")
    r = post(op, f"/changes/{cid}/action", action="start")
    for eid in re.findall(rf"/changes/{cid}/executions/(\d+)", r.text):
        post(op, f"/changes/{cid}/executions/{eid}", status="DONE", output="ok")
    post(op, f"/changes/{cid}/action", action="finish")

    r = post(req, f"/changes/{cid}/record", result="SUCCESS", verification_result="ping ok", rollback_used="true")
    assert "已結案" in r.text
    db.expire_all()
    from app.models import ChangeRequest
    cr = db.get(ChangeRequest, cid)
    assert cr.status == ChangeStatus.CLOSED and cr.rollback_used

    r = req.get(f"/devices/{w.sw.id}")
    assert cr.number in r.text
    ed = login("editor")
    for url in ("/", "/modules", f"/modules/{w.vlan.id}", "/change-templates", "/reviews", f"/changes/{cid}"):
        assert ed.get(url).status_code == 200, url
    assert req.get("/audit").status_code == 403


def test_module_and_review_via_web(db, login, f, world):
    ed = login("editor")
    r = post(ed, "/modules", code="W01", title="重啟 IIS", device_type="WINDOWS", command="iisreset")
    mid = int(re.search(r"/modules/(\d+)/status", r.text).group(1))
    post(ed, f"/modules/{mid}/status", status="STANDARD")
    post(ed, f"/modules/{mid}/revise", note="加上驗證", command="iisreset", verification="Get-Service W3SVC")
    from app.models import StepModule
    m = db.get(StepModule, mid)
    db.refresh(m)
    assert m.status == ModuleStatus.STANDARD and m.current_revision == 2
