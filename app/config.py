import os
from zoneinfo import ZoneInfo

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg://postgres@localhost:5432/changecraft")
# 必填：簽署 session cookie。
SECRET_KEY = os.environ.get("SECRET_KEY", "")
# 聊天 bot 呼叫 /api/chat/command 時用來驗證 HMAC 簽章；空字串代表停用聊天整合。
CHAT_SECRET = os.environ.get("CHAT_SECRET", "")
# 聊天回覆中的網頁連結前綴。
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"
# 畫面顯示與表單輸入使用的時區；資料庫一律存 UTC。
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Taipei"))
