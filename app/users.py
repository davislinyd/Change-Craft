from sqlalchemy import select

from app.audit import log
from app.auth import can, hash_password
from app.changes import _require, _require_perm, clean
from app.models import DeviceType, Role, RoleAssignment, User

MIN_PASSWORD = 10


def create_user(db, actor, *, username, display_name, password) -> User:
    _require_perm(can(actor, "user.manage"), "需要系統管理角色")
    username = clean(username)
    _require(username and clean(display_name), "帳號與名稱為必填")
    _require(len(password) >= MIN_PASSWORD, f"密碼至少 {MIN_PASSWORD} 個字元")
    _require(db.scalar(select(User.id).filter_by(username=username)) is None, "帳號已存在")
    user = User(username=username, display_name=clean(display_name), password_hash=hash_password(password))
    db.add(user)
    db.flush()
    log(db, actor, "user.created", entity=user, username=username)
    return user


def update_user(db, actor, user: User, *, display_name, chat_user_id, is_active: bool, password=""):
    _require_perm(can(actor, "user.manage"), "需要系統管理角色")
    _require(clean(display_name), "名稱為必填")
    chat_user_id = clean(chat_user_id) or None
    if chat_user_id:
        owner = db.scalar(select(User.id).filter_by(chat_user_id=chat_user_id))
        _require(owner in (None, user.id), "此聊天帳號已綁定其他使用者")
    _require(not password or len(password) >= MIN_PASSWORD, f"密碼至少 {MIN_PASSWORD} 個字元")
    _require(actor.id != user.id or is_active, "不能停用自己")
    user.display_name, user.chat_user_id, user.is_active = clean(display_name), chat_user_id, is_active
    if password:
        user.password_hash = hash_password(password)
    log(db, actor, "user.updated", entity=user, username=user.username, chat_user_id=chat_user_id,
        is_active=is_active, password_changed=bool(password))


def add_role(db, actor, user: User, *, role, device_type=None, environment=None):
    _require_perm(can(actor, "user.manage"), "需要系統管理角色")
    _require(role in list(Role), "角色不正確")
    _require(device_type in (None, *DeviceType), "設備類型不正確")
    user.roles.append(RoleAssignment(role=role, device_type=device_type, environment=clean(environment) or None))
    log(db, actor, "user.role_added", entity=user, username=user.username, role=role, device_type=device_type,
        environment=environment)


def remove_role(db, actor, user: User, assignment_id: int):
    _require_perm(can(actor, "user.manage"), "需要系統管理角色")
    a = next((a for a in user.roles if a.id == assignment_id), None)
    _require(a, "角色不存在")
    _require(not (actor.id == user.id and a.role == Role.ADMIN), "不能移除自己的系統管理角色")
    user.roles.remove(a)
    log(db, actor, "user.role_removed", entity=user, username=user.username, role=a.role)
