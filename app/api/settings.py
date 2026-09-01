from datetime import datetime

from fastapi import APIRouter, HTTPException

import app.config as config
from app.db import SessionLocal
from app.models import get_setting
from app.schemas import SettingsReq

router = APIRouter()


def _settings_payload(st) -> dict:
    """活动同步设置响应体（GET/POST 共用）。"""
    return {
        "frequency_minutes": st.frequency_minutes,
        "enabled": st.enabled,
        "sync_window": st.sync_window,
        "options": config.FREQUENCY_OPTIONS,
        "window_options": config.SYNC_WINDOW_OPTIONS,
    }


@router.get("/api/settings")
def get_settings():
    with SessionLocal() as s:
        return _settings_payload(get_setting(s))


@router.post("/api/settings")
def post_settings(req: SettingsReq):
    if req.frequency_minutes not in config.FREQUENCY_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"频率必须是 {config.FREQUENCY_OPTIONS} 之一（分钟）",
        )
    if req.sync_window not in config.SYNC_WINDOW_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"同步时间范围必须是 {config.SYNC_WINDOW_OPTIONS} 之一",
        )
    with SessionLocal() as s:
        st = get_setting(s)
        st.frequency_minutes = req.frequency_minutes
        st.enabled = req.enabled
        st.sync_window = req.sync_window
        st.updated_at = datetime.utcnow()
        s.commit()
        return _settings_payload(st)
