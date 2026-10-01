from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import chat, config
from app.errors import DomainError
from app.web import admin, changes, library
from app.web.common import LoginRequired, back, flash, redirect

if not config.SECRET_KEY:
    raise RuntimeError("SECRET_KEY 環境變數未設定")

app = FastAPI(title="Change Craft", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, session_cookie="cc_session",
                   max_age=8 * 3600, same_site="lax", https_only=config.COOKIE_SECURE)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(changes.router)
app.include_router(library.router)


@app.exception_handler(LoginRequired)
def _login_required(request: Request, exc: LoginRequired):
    return redirect("/login")


@app.exception_handler(DomainError)
def _domain_error(request: Request, exc: DomainError):
    """業務規則錯誤：交易未 commit 即被丟棄，訊息以 flash 帶回原頁面。"""
    flash(request, str(exc))
    return back(request)
