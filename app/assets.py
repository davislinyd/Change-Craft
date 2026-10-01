import ipaddress

from sqlalchemy import select

from app.audit import log
from app.auth import can
from app.changes import _require, _require_perm, clean
from app.errors import DomainError
from app.models import Device, DeviceType

FIELDS = ("hostname", "device_type", "ip", "vendor", "model", "environment", "owner_team", "notes")


def _normalize(db, values: dict, device_id=None) -> dict:
    v = {f: clean(values.get(f)) for f in FIELDS}
    _require(v["hostname"], "主機名稱為必填")
    _require(v["device_type"] in list(DeviceType), "設備類型不正確")
    if v["ip"]:
        try:
            v["ip"] = str(ipaddress.ip_address(v["ip"]))
        except ValueError:
            raise DomainError(f"IP 格式不正確：{v['ip']}") from None
    else:
        v["ip"] = None
    dup = db.scalar(select(Device.id).filter_by(hostname=v["hostname"]))
    _require(dup is None or dup == device_id, f"主機名稱 {v['hostname']} 已存在")
    return v


def create_device(db, user, **values) -> Device:
    _require_perm(can(user, "device.manage"), "需要系統管理角色")
    device = Device(**_normalize(db, values))
    db.add(device)
    db.flush()
    log(db, user, "device.created", entity=device, hostname=device.hostname)
    return device


def update_device(db, user, device: Device, *, is_active: bool, **values):
    _require_perm(can(user, "device.manage"), "需要系統管理角色")
    v = _normalize(db, values, device.id)
    changed = {f: f"{getattr(device, f)} → {v[f]}" for f in FIELDS if str(getattr(device, f) or "") != str(v[f] or "")}
    for f in FIELDS:
        setattr(device, f, v[f])
    if device.is_active != is_active:
        changed["is_active"] = f"{device.is_active} → {is_active}"
        device.is_active = is_active
    log(db, user, "device.updated", entity=device, **{"hostname": device.hostname, **changed})
