from fastapi import APIRouter, HTTPException

from app.schemas import IgpsportLoginReq
from app.services.igpsport import IgpsportError, LoginExpired, get_service

router = APIRouter()


@router.post("/api/igpsport/login")
def igpsport_login(req: IgpsportLoginReq):
    try:
        return get_service().login(req.username, req.password)
    except (IgpsportError, LoginExpired) as e:
        raise HTTPException(400, str(e))


@router.post("/api/igpsport/logout")
def igpsport_logout():
    get_service().logout()
    return {"ok": True}


@router.get("/api/igpsport/status")
def igpsport_status():
    """登录信息。同步结果已并入活动同步日志（模块④），不再单独提供。"""
    return get_service().status()
