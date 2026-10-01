import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://postgres@localhost:5432/changecraft_test")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("CHAT_SECRET", "chat-secret")
os.environ.setdefault("COOKIE_SECURE", "false")

from datetime import timedelta  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import changes, library  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import (  # noqa: E402
    Device,
    ModuleStatus,
    Role,
    RoleAssignment,
    SkipPolicy,
    StepModule,
    StepModuleRevision,
    User,
)


@pytest.fixture(scope="session")
def schema():
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture
def db(schema):
    """每個測試在外層 transaction 內執行，結束後整個 rollback（含 append-only 表）。"""
    conn = engine.connect()
    trans = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    yield session
    session.close()
    trans.rollback()
    conn.close()


class Factory:
    def __init__(self, db):
        self.db = db

    def user(self, username, *roles, password="password123"):
        """roles：Role 或 (Role, device_type, environment)。"""
        u = User(username=username, display_name=username, password_hash=hash_password(password))
        for r in roles:
            role, device_type, env = r if isinstance(r, tuple) else (r, None, None)
            u.roles.append(RoleAssignment(role=role, device_type=device_type, environment=env))
        self.db.add(u)
        self.db.flush()
        return u

    def device(self, hostname, device_type, ip, environment="production"):
        d = Device(hostname=hostname, device_type=device_type, ip=ip, environment=environment)
        self.db.add(d)
        self.db.flush()
        return d

    def module(self, code, title, device_type=None, command=""):
        m = StepModule(code=code, title=title, device_type=device_type, status=ModuleStatus.STANDARD)
        m.revisions.append(StepModuleRevision(revision=1, instruction=f"{title} 說明", command=command,
                                              verification="verify", rollback="rollback"))
        self.db.add(m)
        self.db.flush()
        return m

    def change(self, requester, *, devices=(), modules=(), template=None, **kw):
        t = changes.now()
        data = dict(title="測試變更", purpose="測試目的", window_start=t - timedelta(hours=1),
                    window_end=t + timedelta(hours=2), template_id=template.id if template else None)
        data.update(kw)
        cr = changes.create_change(self.db, requester, **data)
        for d in devices:
            changes.add_target(self.db, requester, cr, d.id)
        for m in modules:
            changes.add_module_step(self.db, requester, cr, m.id, SkipPolicy.REQUIRED)
        return cr

    def template(self, editor, code, items):
        tpl = library.create_template(self.db, editor, code=code, name=f"範本 {code}")
        for module, policy in items:
            library.add_template_item(self.db, editor, tpl, module_id=module.id, skip_policy=policy)
        self.db.flush()
        return tpl


@pytest.fixture
def f(db):
    return Factory(db)


@pytest.fixture
def world(f):
    """常用情境：網路 + Linux 設備、各自範圍的執行者。"""
    w = type("World", (), {})()
    w.req = f.user("req", Role.REQUESTER)
    w.appr = f.user("appr", Role.APPROVER)
    w.op_net = f.user("op_net", (Role.OPERATOR, "NETWORK", None))
    w.op_linux = f.user("op_linux", (Role.OPERATOR, "LINUX", None))
    w.editor = f.user("editor", Role.MODULE_APPROVER)
    w.sw = f.device("core-sw-01", "NETWORK", "10.10.1.1")
    w.web = f.device("web-prod-01", "LINUX", "10.30.1.10")
    w.precheck = f.module("A01", "變更前檢查")
    w.vlan = f.module("A03", "建立 VLAN", "NETWORK", command="vlan 200")
    w.nic = f.module("L01", "新增網卡設定", "LINUX")
    return w
