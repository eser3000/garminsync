from fastapi import APIRouter, HTTPException

from app.runtime import garmin_service
from app.schemas import ChinaLoginReq, IntlLoginReq, LogoutReq, MfaReq

router = APIRouter()


@router.post("/api/accounts/china/login")
def china_login(req: ChinaLoginReq):
    try:
        return garmin_service.china_login(req.email, req.password)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/api/accounts/china/mfa")
def china_mfa(req: MfaReq):
    try:
        return garmin_service.china_mfa(req.mfa_token, req.code)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/api/accounts/intl/login")
def intl_login(req: IntlLoginReq):
    try:
        return garmin_service.intl_login(req.email, req.password)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/api/accounts/intl/mfa")
def intl_mfa(req: MfaReq):
    try:
        return garmin_service.intl_mfa(req.mfa_token, req.code)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/api/accounts/logout")
def logout(req: LogoutReq):
    garmin_service.logout(req.region)
    return {"ok": True}


@router.get("/api/accounts/status")
def status():
    return garmin_service.status()
