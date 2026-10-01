import hashlib
import hmac
import secrets

from app.models import Role

# 權限 → 可取得該權限的角色。ADMIN 刻意不含變更核准/執行，以維持職責分離。
PERMISSIONS = {
    "change.create": {Role.REQUESTER},
    "change.approve": {Role.APPROVER},
    "change.execute": {Role.OPERATOR},
    "module.edit": {Role.MODULE_EDITOR, Role.MODULE_APPROVER},
    "module.approve": {Role.MODULE_APPROVER},
    "device.manage": {Role.ADMIN},
    "user.manage": {Role.ADMIN},
    "audit.view": {Role.AUDITOR, Role.ADMIN},
}


def can(user, perm: str, scope=None) -> bool:
    """scope 為具有 device_type / environment 屬性的物件（Device 或 ChangeTarget）；None 代表只檢查角色。"""
    allowed = PERMISSIONS[perm]
    return any(a.role in allowed and _in_scope(a, scope) for a in user.roles)


def _in_scope(assignment, scope) -> bool:
    if scope is None:
        return True
    return (assignment.device_type is None or assignment.device_type == scope.device_type) and (
        assignment.environment is None or assignment.environment == scope.environment
    )


_SCRYPT = dict(n=2**14, r=8, p=1)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    except ValueError:
        return False
    return hmac.compare_digest(calc.hex(), digest)
