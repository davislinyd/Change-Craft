import ipaddress
from datetime import datetime

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import INET

from app.changes import clean
from app.errors import DomainError
from app.models import ChangeRequest, ChangeTarget, Device, PlanStep


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_changes(db, *, q="", hostname="", ip="", item="", purpose="", status="",
                   date_from: datetime | None = None, date_to: datetime | None = None, limit=200):
    """依時間、設備名稱、IP、變更項目、變更目的搜尋。設備條件同時比對變更當下的快照與目前的設備資料。"""
    cr = ChangeRequest
    stmt = select(cr).order_by(cr.id.desc()).limit(limit)

    if q := clean(q):
        like = _like(q)
        stmt = stmt.where(or_(*(c.ilike(like, escape="\\") for c in
                                (cr.number, cr.title, cr.purpose, cr.reason, cr.change_items))))
    if hostname := clean(hostname):
        like = _like(hostname)
        stmt = stmt.where(cr.id.in_(
            select(ChangeTarget.change_id).join(Device)
            .where(or_(ChangeTarget.hostname.ilike(like, escape="\\"), Device.hostname.ilike(like, escape="\\")))
        ))
    if ip := clean(ip):
        try:
            net = str(ipaddress.ip_network(ip, strict=False))
        except ValueError:
            raise DomainError(f"IP 或網段格式不正確：{ip}") from None
        net = cast(net, INET)
        stmt = stmt.where(cr.id.in_(
            select(ChangeTarget.change_id).join(Device)
            .where(or_(ChangeTarget.ip.op("<<=")(net), Device.ip.op("<<=")(net)))
        ))
    if item := clean(item):
        like = _like(item)
        stmt = stmt.where(or_(
            cr.change_items.ilike(like, escape="\\"),
            cr.id.in_(select(PlanStep.change_id).where(or_(PlanStep.code.ilike(like, escape="\\"),
                                                           PlanStep.title.ilike(like, escape="\\")))),
        ))
    if purpose := clean(purpose):
        like = _like(purpose)
        stmt = stmt.where(or_(cr.purpose.ilike(like, escape="\\"), cr.title.ilike(like, escape="\\")))
    if status:
        stmt = stmt.where(cr.status == status)
    # 時間：以實際執行時間為準，未執行則用排定時段，與查詢區間有重疊即符合。
    start = func.coalesce(cr.started_at, cr.window_start, cr.created_at)
    end = func.coalesce(cr.finished_at, cr.window_end, start)
    if date_from:
        stmt = stmt.where(end >= date_from)
    if date_to:
        stmt = stmt.where(start < date_to)
    return list(db.scalars(stmt))
