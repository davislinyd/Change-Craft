"""python -m app.cli create-admin <username> | seed-demo"""

import argparse
import getpass
import sys

from sqlalchemy import select

from app.audit import log
from app.auth import hash_password
from app.db import SessionLocal
from app.models import (
    ChangeTemplate,
    Device,
    ModuleStatus,
    Role,
    RoleAssignment,
    SkipPolicy,
    StepModule,
    StepModuleRevision,
    TemplateItem,
    User,
)
from app.users import MIN_PASSWORD


def create_admin(db, username: str):
    if db.scalar(select(User.id).filter_by(username=username)):
        sys.exit(f"帳號 {username} 已存在")
    password = getpass.getpass("密碼：")
    if len(password) < MIN_PASSWORD or password != getpass.getpass("再輸入一次："):
        sys.exit(f"密碼不一致或少於 {MIN_PASSWORD} 個字元")
    user = User(username=username, display_name=username, password_hash=hash_password(password),
                roles=[RoleAssignment(role=Role.ADMIN)])
    db.add(user)
    db.flush()
    log(db, None, "user.created", entity=user, username=username, source="cli")
    print(f"已建立管理員 {username}；請登入後在「使用者」頁面建立其他帳號並指派角色。")


# (代碼, 名稱, 適用設備, 指令, 驗證, 回復)
DEMO_MODULES = [
    ("A01", "變更前檢查", None, "", "確認無異常告警、記錄目前連線數", ""),
    ("A02", "備份設定", "NETWORK", "show running-config | redirect flash:backup-{{ date }}.cfg",
     "dir flash: | include backup", ""),
    ("A03", "建立 VLAN", "NETWORK", "vlan {{ vlan_id }}\n name {{ vlan_name }}", "show vlan brief",
     "no vlan {{ vlan_id }}"),
    ("A04", "Trunk 加入 VLAN", "NETWORK", "interface {{ uplink }}\n switchport trunk allowed vlan add {{ vlan_id }}",
     "show interfaces trunk", "interface {{ uplink }}\n switchport trunk allowed vlan remove {{ vlan_id }}"),
    ("A05", "儲存設定", "NETWORK", "write memory", "show startup-config | include vlan {{ vlan_id }}", ""),
    ("L01", "新增網卡設定", "LINUX",
     "nmcli con add type vlan con-name vlan{{ vlan_id }} dev {{ nic }} id {{ vlan_id }} ip4 {{ ip_cidr }}",
     "ip -br addr show", "nmcli con delete vlan{{ vlan_id }}"),
    ("A10", "變更後驗證", None, "", "服務連線測試、確認監控無新告警", ""),
]
DEMO_DEVICES = [
    ("core-sw-01", "NETWORK", "10.10.1.1", "Cisco", "C9500"),
    ("access-sw-12", "NETWORK", "10.10.2.12", "Cisco", "C9300"),
    ("fw-01", "NETWORK", "10.10.0.1", "Fortinet", "FG-600F"),
    ("web-prod-01", "LINUX", "10.30.1.10", "", "RHEL 9"),
    ("ad-dc01", "WINDOWS", "10.20.1.10", "", "Windows Server 2022"),
]


def seed_demo(db):
    """只建立設備、模組與範本，不建立任何帳號。"""
    if db.scalar(select(StepModule.id).limit(1)):
        sys.exit("資料庫已有模組，略過示範資料")
    for hostname, dtype, ip, vendor, model in DEMO_DEVICES:
        db.add(Device(hostname=hostname, device_type=dtype, ip=ip, vendor=vendor, model=model,
                      environment="production"))
    modules = {}
    for code, title, dtype, command, verification, rollback in DEMO_MODULES:
        m = StepModule(code=code, title=title, device_type=dtype, status=ModuleStatus.STANDARD)
        m.revisions.append(StepModuleRevision(revision=1, command=command, verification=verification,
                                              rollback=rollback, note="示範資料"))
        db.add(m)
        modules[code] = m
    tpl = ChangeTemplate(code="A", name="新增服務 VLAN", description="交換器建立 VLAN 並設定伺服器網卡")
    for seq, (code, policy, cond) in enumerate([
        ("A01", SkipPolicy.REQUIRED, ""),
        ("A02", SkipPolicy.REQUIRED, ""),
        ("A03", SkipPolicy.REQUIRED, ""),
        ("A04", SkipPolicy.CONDITIONAL, "僅適用有 Trunk 上行的交換器"),
        ("A05", SkipPolicy.REQUIRED, ""),
        ("L01", SkipPolicy.CONDITIONAL, "伺服器已有該網段介面時可省略"),
        ("A10", SkipPolicy.REQUIRED, ""),
    ], start=1):
        tpl.items.append(TemplateItem(seq=seq, module=modules[code], skip_policy=policy, condition_note=cond))
    db.add(tpl)
    print(f"已建立 {len(DEMO_DEVICES)} 台設備、{len(DEMO_MODULES)} 個標準模組、範本 A。")


def main():
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create-admin").add_argument("username")
    sub.add_parser("seed-demo")
    args = parser.parse_args()
    with SessionLocal() as db:
        if args.cmd == "create-admin":
            create_admin(db, args.username)
        else:
            seed_demo(db)
        db.commit()


if __name__ == "__main__":
    main()
