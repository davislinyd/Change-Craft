import hmac
import secrets
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy.orm import Session

from app import config
from app.auth import can
from app.db import get_db
from app.errors import DomainError
from app.labels import LABELS
from app.models import User

templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
templates.env.globals.update(L=LABELS, can=can)
templates.env.filters["dt"] = lambda v: v.astimezone(config.TZ).strftime("%Y-%m-%d %H:%M") if v else ""
templates.env.filters["dtinput"] = lambda v: v.astimezone(config.TZ).strftime("%Y-%m-%dT%H:%M") if v else ""


class LoginRequired(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.is_active:
        request.session.clear()
        raise LoginRequired()
    return user


def _csrf_token(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_hex(32)
    return request.session["csrf"]


async def verify_csrf(request: Request):
    if request.method != "POST":
        return
    form = await request.form()
    sent = str(form.get("csrf") or request.headers.get("X-CSRF-Token", ""))
    expected = request.session.get("csrf", "")
    if not sent or not hmac.compare_digest(sent.encode(), expected.encode()):
        raise HTTPException(403, "CSRF 驗證失敗，請重新整理頁面")


def render(request: Request, name: str, user: User | None = None, **ctx):
    token = _csrf_token(request)
    ctx.update(
        user=user,
        csrf_input=Markup(f'<input type="hidden" name="csrf" value="{token}">'),
        flash=request.session.pop("flash", None),
    )
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def back(request: Request, fallback="/") -> RedirectResponse:
    """回到來源頁面；只取 path/query，避免外部網址造成 open redirect。"""
    ref = urlsplit(request.headers.get("referer", ""))
    if not ref.path.startswith("/") or ref.path.startswith("//"):
        return redirect(fallback)
    return redirect(ref.path + (f"?{ref.query}" if ref.query else ""))


def flash(request: Request, message: str):
    request.session["flash"] = message


def get_or_404(db, model, obj_id):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, "找不到資料")
    return obj


def parse_dt(value: str) -> datetime | None:
    """表單的 datetime-local 值以設定時區解讀。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=config.TZ)
    except ValueError:
        raise DomainError(f"時間格式不正確：{value}") from None


def parse_date(value: str, *, end=False) -> datetime | None:
    if not value:
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise DomainError(f"日期格式不正確：{value}") from None
    return datetime.combine(d + timedelta(days=1 if end else 0), time(), config.TZ)
