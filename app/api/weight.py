"""小米体重同步相关 API：小米账户绑定 + 体重同步设置/触发/状态/日志。"""
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app.api.sync_logs import clear_sync_logs, query_sync_logs
from app.config import SYNC_WINDOW_OPTIONS
from app.db import SessionLocal
from app.models import XiaomiAccount, get_setting
from app.runtime import scheduler
from app.schemas import (
    WeightSettingsReq,
    XiaomiLoginCodeReq,
    XiaomiLoginStartReq,
)
from app.services.xiaomi_login import (
    drop_login_session,
    get_login_session,
    new_login_session,
)

router = APIRouter()

# 体重上传目标固定双区（写死，界面无需选择）
WEIGHT_TARGETS_FIXED = ["intl", "china"]
WEIGHT_SCHEDULE_FIXED = "每天 00:00"


def _weight_payload(st) -> dict:
    """体重同步设置响应体（GET/POST 共用）。"""
    return {
        "weight_enabled": st.weight_enabled,
        "weight_sync_window": st.weight_sync_window,
        "window_options": SYNC_WINDOW_OPTIONS,
        "targets": WEIGHT_TARGETS_FIXED,
        "schedule": WEIGHT_SCHEDULE_FIXED,
    }


def _save_xiaomi_credentials(creds: dict) -> str:
    """保存小米凭据到数据库，返回 userId。"""
    with SessionLocal() as s:
        acc = s.query(XiaomiAccount).first()
        if acc is None:
            acc = XiaomiAccount(id=1)
        acc.user_id = creds["user_id"]
        acc.pass_token = creds["pass_token"]
        acc.ssecurity = creds["ssecurity"]
        acc.logged_in = True
        acc.updated_at = datetime.utcnow()
        s.add(acc)
        s.commit()
        return acc.user_id


def _xiaomi_step_response(login_id: str, result: dict):
    """统一处理登录状态机返回，成功则保存凭据。"""
    if result.get("ok"):
        creds = result["credentials"]
        user_id = _save_xiaomi_credentials(creds)
        drop_login_session(login_id)
        return {"ok": True, "step": "success", "user_id": user_id}
    if result.get("error"):
        drop_login_session(login_id)
        raise HTTPException(status_code=400, detail=result["error"])
    # 还需要继续交互（图形验证码 / 2FA 验证码）
    return {
        "ok": False,
        "login_id": login_id,
        "step": result["step"],
        "captcha_img": result.get("captcha_img"),
        "target": result.get("target", ""),
    }


@router.post("/api/xiaomi/login/start")
def xiaomi_login_start(req: XiaomiLoginStartReq):
    """第一步：小米账号 + 密码 登录（免抓包）。"""
    account = req.account.strip()
    if not account or not req.password:
        raise HTTPException(status_code=400, detail="请输入小米账号和密码")
    login_id, session = new_login_session()
    return _xiaomi_step_response(login_id, session.start(account, req.password))


@router.post("/api/xiaomi/login/captcha")
def xiaomi_login_captcha(req: XiaomiLoginCodeReq):
    """提交图形验证码。"""
    session = get_login_session(req.login_id)
    if session is None:
        raise HTTPException(status_code=400, detail="登录会话已过期，请重新开始")
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="请输入图形验证码")
    return _xiaomi_step_response(req.login_id, session.submit_captcha(req.code.strip()))


@router.post("/api/xiaomi/login/verify")
def xiaomi_login_verify(req: XiaomiLoginCodeReq):
    """提交短信/邮箱 2FA 验证码。"""
    session = get_login_session(req.login_id)
    if session is None:
        raise HTTPException(status_code=400, detail="登录会话已过期，请重新开始")
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="请输入短信/邮箱验证码")
    return _xiaomi_step_response(req.login_id, session.submit_verify(req.code.strip()))


@router.post("/api/xiaomi/logout")
def xiaomi_logout():
    with SessionLocal() as s:
        acc = s.query(XiaomiAccount).first()
        if acc:
            acc.user_id = ""
            acc.pass_token = ""
            acc.ssecurity = ""
            acc.logged_in = False
            acc.updated_at = datetime.utcnow()
            s.commit()
    return {"ok": True}


@router.get("/api/xiaomi/status")
def xiaomi_status():
    with SessionLocal() as s:
        acc = s.query(XiaomiAccount).first()
        if acc and acc.logged_in and acc.user_id:
            return {"logged_in": True, "user_id": acc.user_id}
        return {"logged_in": False}


@router.get("/api/weight/settings")
def weight_settings():
    with SessionLocal() as s:
        return _weight_payload(get_setting(s))


@router.post("/api/weight/settings")
def post_weight_settings(req: WeightSettingsReq):
    if req.weight_sync_window not in SYNC_WINDOW_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"体重同步周期必须是 {SYNC_WINDOW_OPTIONS} 之一",
        )
    with SessionLocal() as s:
        st = get_setting(s)
        st.weight_enabled = req.weight_enabled
        st.weight_sync_window = req.weight_sync_window
        st.updated_at = datetime.utcnow()
        s.commit()
        return _weight_payload(st)


@router.post("/api/weight/sync/now")
def weight_sync_now():
    scheduler.trigger("weight")
    st = scheduler.state("weight")
    return {**st, "ok": True, "triggered": True}


@router.get("/api/weight/sync/status")
def weight_sync_status():
    return scheduler.state("weight")


@router.get("/api/weight/sync/log")
def weight_sync_log(limit: int = Query(20, ge=1, le=100)):
    return query_sync_logs("weight", limit)


@router.post("/api/weight/sync/log")
def weight_sync_log_clear():
    """清空体重同步日志。"""
    clear_sync_logs("weight")
    return {"ok": True}
