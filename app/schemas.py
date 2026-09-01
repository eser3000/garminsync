from pydantic import BaseModel


class ChinaLoginReq(BaseModel):
    email: str
    password: str


class IntlLoginReq(BaseModel):
    email: str
    password: str


class MfaReq(BaseModel):
    mfa_token: str
    code: str


class LogoutReq(BaseModel):
    region: str


class SettingsReq(BaseModel):
    frequency_minutes: int
    enabled: bool = False
    sync_window: str = "3months"


class XiaomiLoginStartReq(BaseModel):
    account: str
    password: str


class XiaomiLoginCodeReq(BaseModel):
    login_id: str
    code: str


class WeightSettingsReq(BaseModel):
    weight_enabled: bool
    weight_sync_window: str = "3months"


class HealthSettingsReq(BaseModel):
    health_enabled: bool
    health_sync_window: str = "1month"


class IgpsportLoginReq(BaseModel):
    username: str
    password: str



