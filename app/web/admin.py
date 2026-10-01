from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import assets, users
from app.audit import log
from app.auth import can, verify_password
from app.db import get_db
from app.models import ChangeTarget, Device, DeviceType, Event, Role, User
from app.web.common import current_user, flash, get_or_404, redirect, render, verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])


# ---- 登入 ----


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.scalar(select(User).filter_by(username=username.strip()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        flash(request, "帳號或密碼錯誤")
        return redirect("/login")
    request.session.clear()  # 防止 session fixation
    request.session["uid"] = user.id
    log(db, user, "user.login", entity=user)
    db.commit()
    return redirect("/")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


# ---- 設備 ----


@router.get("/devices")
def device_list(request: Request, q: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Device).order_by(Device.hostname)
    if q.strip():
        stmt = stmt.where(Device.hostname.icontains(q.strip(), autoescape=True))
    return render(request, "devices.html", user, devices=db.scalars(stmt).all(), q=q,
                  device_types=list(DeviceType))


@router.post("/devices")
def device_create(hostname: str = Form(...), device_type: str = Form(...), ip: str = Form(""),
                  vendor: str = Form(""), model: str = Form(""), environment: str = Form(""),
                  owner_team: str = Form(""), notes: str = Form(""),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = assets.create_device(db, user, hostname=hostname, device_type=device_type, ip=ip, vendor=vendor,
                             model=model, environment=environment, owner_team=owner_team, notes=notes)
    db.commit()
    return redirect(f"/devices/{d.id}")


@router.get("/devices/{did}")
def device_detail(did: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = get_or_404(db, Device, did)
    # 設備本身不記錄變更；歷史一律經由 ChangeTarget 反查主變更。
    history = db.scalars(select(ChangeTarget).filter_by(device_id=d.id).order_by(ChangeTarget.id.desc())).all()
    return render(request, "device_detail.html", user, d=d, history=history, device_types=list(DeviceType))


@router.post("/devices/{did}")
def device_update(did: int, hostname: str = Form(...), device_type: str = Form(...), ip: str = Form(""),
                  vendor: str = Form(""), model: str = Form(""), environment: str = Form(""),
                  owner_team: str = Form(""), notes: str = Form(""), is_active: bool = Form(False),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    assets.update_device(db, user, get_or_404(db, Device, did), is_active=is_active, hostname=hostname,
                         device_type=device_type, ip=ip, vendor=vendor, model=model, environment=environment,
                         owner_team=owner_team, notes=notes)
    db.commit()
    return redirect(f"/devices/{did}")


# ---- 使用者與角色 ----


def _require(user, perm):
    if not can(user, perm):
        raise HTTPException(403, "權限不足")


@router.get("/admin/users")
def user_list(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "user.manage")
    return render(request, "users.html", user, users=db.scalars(select(User).order_by(User.username)).all(),
                  roles=list(Role), device_types=list(DeviceType))


@router.post("/admin/users")
def user_create(username: str = Form(...), display_name: str = Form(...), password: str = Form(...),
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    users.create_user(db, user, username=username, display_name=display_name, password=password)
    db.commit()
    return redirect("/admin/users")


@router.post("/admin/users/{uid}")
def user_update(uid: int, display_name: str = Form(...), chat_user_id: str = Form(""),
                is_active: bool = Form(False), password: str = Form(""),
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    users.update_user(db, user, get_or_404(db, User, uid), display_name=display_name, chat_user_id=chat_user_id,
                      is_active=is_active, password=password)
    db.commit()
    return redirect("/admin/users")


@router.post("/admin/users/{uid}/roles")
def role_add(uid: int, role: str = Form(...), device_type: str = Form(""), environment: str = Form(""),
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    users.add_role(db, user, get_or_404(db, User, uid), role=role, device_type=device_type or None,
                   environment=environment)
    db.commit()
    return redirect("/admin/users")


@router.post("/admin/users/{uid}/roles/{rid}/delete")
def role_remove(uid: int, rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    users.remove_role(db, user, get_or_404(db, User, uid), rid)
    db.commit()
    return redirect("/admin/users")


# ---- 稽核 ----


@router.get("/audit")
def audit_log(request: Request, action: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "audit.view")
    stmt = select(Event).order_by(Event.id.desc()).limit(500)
    if action.strip():
        stmt = stmt.where(Event.action.startswith(action.strip(), autoescape=True))
    return render(request, "audit.html", user, events=db.scalars(stmt).all(), action=action)
