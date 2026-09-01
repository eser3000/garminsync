import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import (
    accounts,
    health as health_router,
    igpsport as igpsport_router,
    settings as settings_router,
    sync as sync_router,
    weight as weight_router,
)
from app.db import init_db
from app.runtime import scheduler
from app.time_utils import CN_TZ

# 统一日志配置：把 garminsync 的 INFO+ 日志输出到 stdout。
# 否则 Python 默认（root 无 handler）会丢弃所有 INFO 日志，
# 导致 docker logs 看不到体重/活动/健康同步的执行过程。
# 时间戳固定按东八区显示（不依赖容器/宿主机时区），datefmt 精确到秒。
logging.Formatter.converter = lambda *args: datetime.now(CN_TZ).timetuple()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

templates = Jinja2Templates(directory=os.path.join(BASE, "app", "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="GarminSync", lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE, "app", "static")),
    name="static",
)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """静态资源每次回源校验，避免浏览器缓存旧版 JS/CSS。"""
    resp = await call_next(request)
    if request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache, max-age=0"
    return resp

_BUSINESS_ROUTERS = [
    accounts.router,
    settings_router.router,
    sync_router.router,
    weight_router.router,
    health_router.router,
    igpsport_router.router,
]
for _router in _BUSINESS_ROUTERS:
    app.include_router(_router)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"request": request})

