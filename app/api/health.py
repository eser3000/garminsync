"""佳明健康数据同步相关 API：中国区每日健康数据 -> 国际区。

同步范围：睡眠 / 心率 / 压力 / 身体电量 / 血氧 / 呼吸 / 步数。
设置/触发/状态/日志均独立于活动与体重同步。
"""
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app.api.sync_logs import clear_sync_logs, query_sync_logs
from app.config import SYNC_WINDOW_OPTIONS
from app.db import SessionLocal
from app.models import get_setting
from app.runtime import scheduler
from app.schemas import HealthSettingsReq

router = APIRouter()

# 健康同步固定每天 03:00 自动执行（写死，界面无需配置）
HEALTH_SCHEDULE_FIXED = "每天 03:00"
HEALTH_TARGET_FIXED = "国际区"


def _health_payload(st) -> dict:
    """健康同步设置响应体（GET/POST 共用）。"""
    return {
        "health_enabled": st.health_enabled,
        "health_sync_window": st.health_sync_window,
        "window_options": SYNC_WINDOW_OPTIONS,
        "target": HEALTH_TARGET_FIXED,
        "schedule": HEALTH_SCHEDULE_FIXED,
    }


@router.get("/api/health/settings")
def health_settings():
    with SessionLocal() as s:
        return _health_payload(get_setting(s))


@router.post("/api/health/settings")
def post_health_settings(req: HealthSettingsReq):
    # 非法值必须拒绝：否则会存进库，读取时静默回落成默认 30 天，
    # 表现为"选什么都按最近一个月同步"。
    if req.health_sync_window not in SYNC_WINDOW_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"健康同步周期必须是 {SYNC_WINDOW_OPTIONS} 之一",
        )
    with SessionLocal() as s:
        st = get_setting(s)
        st.health_enabled = req.health_enabled
        st.health_sync_window = req.health_sync_window
        st.updated_at = datetime.utcnow()
        s.commit()
        return _health_payload(st)


@router.post("/api/health/sync/now")
def health_sync_now():
    scheduler.trigger("health")
    st = scheduler.state("health")
    return {**st, "ok": True, "triggered": True}


@router.get("/api/health/sync/status")
def health_sync_status():
    return scheduler.state("health")


@router.get("/api/health/sync/log")
def health_sync_log(limit: int = Query(20, ge=1, le=100)):
    return query_sync_logs("health", limit)


@router.post("/api/health/sync/log")
def health_sync_log_clear():
    """清空健康数据同步日志。"""
    clear_sync_logs("health")
    return {"ok": True}
