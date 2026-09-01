from fastapi import APIRouter, Query

from app.api.sync_logs import clear_sync_logs, query_sync_logs
from app.runtime import scheduler

router = APIRouter()


@router.post("/api/sync/now")
def sync_now():
    scheduler.trigger()
    st = scheduler.state()
    return {**st, "ok": True, "triggered": True}


@router.get("/api/sync/status")
def sync_status():
    return scheduler.state()


@router.get("/api/sync/log")
def sync_log(limit: int = Query(20, ge=1, le=100)):
    return query_sync_logs("activity", limit)


@router.post("/api/sync/log")
def sync_log_clear():
    """清空活动同步日志。"""
    clear_sync_logs("activity")
    return {"ok": True}
