"""小米账号密码登录（Web 交互式：图形验证码 / 短信邮箱 2FA）。

移植自 garmin-weight-sync 的 src/xiaomi/login.py（同款登录协议）：

1. GET  /pass/serviceLogin?_json=true&sid=miothealth  获取 _sign / qs / callback
2. POST /pass/serviceLoginAuth2                       提交账号 + MD5 密码
3. 需要时返回图形验证码（captCode + ick cookie）或短信/邮箱 2FA
   （identity/list -> sendXxxTicket -> verifyXxx）
4. 成功后跟随 location 换取 ssecurity / userId / passToken

交互状态由 XiaomiLoginSession 维护（内存会话，TTL 10 分钟）：
- start(account, password)         -> step: captcha / verify / success / error
- submit_captcha(code)             -> 图形验证码提交后的状态流转
- submit_verify(code)              -> 2FA 验证码提交后的状态流转
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import string
import time
import uuid

import requests

logger = logging.getLogger(__name__)

SDK_VERSION = "4.2.29"
SID = "miothealth"  # 与 app/services/xiaomi.py 的 APP_ID 保持一致
FLAG_PHONE = 4
FLAG_EMAIL = 8
BASE = "https://account.xiaomi.com"
SESSION_TTL = 600  # 会话 10 分钟过期

# 内存登录会话表：login_id -> XiaomiLoginSession
LOGIN_SESSIONS: dict[str, "XiaomiLoginSession"] = {}


class XiaomiLoginError(Exception):
    """小米登录协议异常。"""


def _random_device_id(length: int = 16) -> str:
    seq = string.ascii_uppercase + string.digits
    return "".join(random.choice(seq) for _ in range(length))


def _parse_auth_response(content) -> dict:
    body = content
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not body.startswith(b"&&&START&&&"):
        raise XiaomiLoginError(f"小米登录响应格式错误: {body[:120]}")
    return json.loads(body[11:])


class XiaomiLoginSession:
    """一次小米账号密码登录的交互会话（可多次提交验证码）。"""

    def __init__(self):
        self.session = requests.Session()
        self.sid = SID
        self.device_id = _random_device_id()
        self.auth: dict = {}          # 中间状态: username/password/ick/flag/identity_session
        self.ssecurity: bytes | None = None
        self.cookies: dict = {}
        self.credentials: dict | None = None
        self.created_at = time.time()

    # ---------- 对外交互 ----------

    def start(self, account: str, password: str, captcha_code: str | None = None) -> dict:
        """提交账号密码（可带图形验证码）。返回 step dict。"""
        try:
            res1 = self._service_login(account, password, captcha_code)
        except XiaomiLoginError as exc:
            return {"ok": False, "error": str(exc)}
        except requests.RequestException as exc:
            return {"ok": False, "error": f"网络错误: {exc}"}

        if res1.get("captchaUrl"):
            try:
                data = self._get_captcha(res1["captchaUrl"])
            except XiaomiLoginError as exc:
                return {"ok": False, "error": str(exc)}
            self.auth = {"username": account, "password": password, "ick": data["ick"]}
            return {"ok": False, "step": "captcha", "captcha_img": data["img_b64"]}

        if res1.get("notificationUrl"):
            return self._handle_notification(res1["notificationUrl"])

        return self._finalize(res1)

    def submit_captcha(self, code: str) -> dict:
        """提交图形验证码：登录阶段 或 2FA 发码前阶段。"""
        if "flag" in self.auth and "identity_session" in self.auth:
            return self._send_ticket(
                self.auth["flag"], self.auth["identity_session"], code
            )
        return self.start(self.auth["username"], self.auth["password"], captcha_code=code)

    def submit_verify(self, code: str) -> dict:
        """提交短信/邮箱 2FA 验证码。"""
        flag = self.auth.get("flag")
        if not flag:
            return {"ok": False, "error": "登录会话已失效，请重新开始"}
        key = "Phone" if flag == FLAG_PHONE else "Email"
        try:
            r = self.session.post(
                f"{BASE}/identity/auth/verify{key}",
                cookies={"identity_session": self.auth["identity_session"]},
                params={"_flag": flag, "ticket": code, "trust": "false", "_json": "true"},
                timeout=20,
            )
            res1 = _parse_auth_response(r.content)
            if res1.get("code", 0) != 0 and not res1.get("location"):
                return {
                    "ok": False,
                    "error": f"验证码校验失败: {res1.get('desc') or res1.get('code')}",
                }
        except (XiaomiLoginError, requests.RequestException) as exc:
            return {"ok": False, "error": f"验证码提交失败: {exc}"}
        return self._finalize(res1)

    # ---------- 协议步骤 ----------

    def _service_login(self, username: str, password: str, captcha_code: str | None = None) -> dict:
        r = self.session.get(
            f"{BASE}/pass/serviceLogin",
            cookies={"sdkVersion": SDK_VERSION, "deviceId": self.device_id},
            params={"_json": "true", "sid": self.sid},
            timeout=20,
        )
        res1 = _parse_auth_response(r.content)

        cookies = {"sdkVersion": SDK_VERSION, "deviceId": self.device_id}
        data = {
            "_json": "true",
            "sid": res1["sid"],
            "callback": res1["callback"],
            "_sign": res1["_sign"],
            "qs": res1["qs"],
            "user": username,
            "hash": hashlib.md5(password.encode()).hexdigest().upper(),
        }
        if captcha_code:
            cookies["ick"] = self.auth["ick"]
            data["captCode"] = captcha_code

        r = self.session.post(
            f"{BASE}/pass/serviceLoginAuth2", cookies=cookies, data=data, timeout=20
        )
        return _parse_auth_response(r.content)

    def _get_captcha(self, captcha_url: str) -> dict:
        r = self.session.get(BASE + captcha_url, timeout=20)
        if "ick" not in r.cookies:
            raise XiaomiLoginError("验证码接口缺少 ick cookie")
        return {
            "img_b64": base64.b64encode(r.content).decode("utf-8"),
            "ick": r.cookies["ick"],
        }

    def _handle_notification(self, notification_url: str) -> dict:
        """2FA 流程：identity/list -> sendXxxTicket（可能要求图形验证码）-> verify。"""
        if "/fe/service/identity/authStart" not in notification_url:
            return {"ok": False, "error": "2FA 通知地址异常"}
        url = notification_url.replace("/fe/service/identity/authStart", "/identity/list")
        try:
            r = self.session.get(url, timeout=20)
            res1 = _parse_auth_response(r.content)
            if res1.get("code") != 2:
                return {"ok": False, "error": f"2FA 初始化失败: {res1.get('desc') or res1.get('code')}"}
            flag = res1.get("flag")
            if flag not in (FLAG_EMAIL, FLAG_PHONE):
                return {"ok": False, "error": f"不支持的安全验证方式 flag={flag}"}
            identity_session = r.cookies.get("identity_session")
            if not identity_session:
                return {"ok": False, "error": "2FA 缺少 identity_session"}
            return self._send_ticket(flag, identity_session)
        except (XiaomiLoginError, requests.RequestException) as exc:
            return {"ok": False, "error": f"2FA 初始化失败: {exc}"}

    def _send_ticket(self, flag: int, identity_session: str, captcha_code: str | None = None) -> dict:
        """发送短信/邮箱验证码；可能要求先过图形验证码（icode 字段）。"""
        key = "Phone" if flag == FLAG_PHONE else "Email"
        try:
            r = self.session.get(
                f"{BASE}/identity/auth/verify{key}",
                cookies={"identity_session": identity_session},
                params={"_flag": flag, "_json": "true"},
                timeout=20,
            )
            res1 = _parse_auth_response(r.content)
            if res1.get("code", 0) != 0 and not res1.get("location"):
                return {"ok": False, "error": f"2FA 初始化失败: {res1.get('desc') or res1.get('code')}"}

            if captcha_code:
                cookies = {"identity_session": identity_session, "ick": self.auth["ick"]}
                data = {"retry": 0, "icode": captcha_code, "_json": "true"}
            else:
                cookies = {"identity_session": identity_session}
                data = {"retry": 0, "icode": "", "_json": "true"}

            r = self.session.post(
                f"{BASE}/identity/auth/send{key}Ticket", cookies=cookies, data=data, timeout=20
            )
            res2 = _parse_auth_response(r.content)

            self.auth = {"flag": flag, "identity_session": identity_session}

            captcha_url = res2.get("captchaUrl")
            if captcha_url:
                cdata = self._get_captcha(captcha_url)
                self.auth["ick"] = cdata["ick"]
                return {"ok": False, "step": "captcha", "captcha_img": cdata["img_b64"]}

            if res2.get("code", 0) != 0 and not res2.get("location"):
                return {
                    "ok": False,
                    "error": f"发送验证码失败: {res2.get('desc') or res2.get('code')}",
                }

            return {"ok": False, "step": "verify", "target": res1.get(f"masked{key}", "")}
        except (XiaomiLoginError, requests.RequestException) as exc:
            return {"ok": False, "error": f"发送验证码失败: {exc}"}

    def _finalize(self, data: dict) -> dict:
        """跟随 location 换取 ssecurity / userId / passToken。"""
        try:
            location = data.get("location")
            if not location:
                code = data.get("code")
                desc = str(data.get("desc", ""))
                return {
                    "ok": False,
                    "error": f"登录失败: code={code} desc={desc}",
                }

            r1 = self.session.get(location, timeout=20)
            self.cookies = {k: v for k, v in r1.cookies.items()}

            merged = dict(data)
            for r2 in r1.history:
                merged.update({k: v for k, v in r2.cookies.items()})
                ext = r2.headers.get("extension-pragma")
                if ext:
                    try:
                        merged.update(json.loads(ext))
                    except (ValueError, TypeError):
                        pass

            ssecurity_b64 = merged.get("ssecurity")
            if not ssecurity_b64:
                return {"ok": False, "error": "登录成功但缺少 ssecurity"}
            self.ssecurity = base64.b64decode(ssecurity_b64)
            self.credentials = {
                "user_id": str(merged.get("userId") or ""),
                "pass_token": merged.get("passToken") or "",
                "ssecurity": ssecurity_b64,
            }
            return {"ok": True, "credentials": self.credentials}
        except (KeyError, ValueError, TypeError, requests.RequestException) as exc:
            return {"ok": False, "error": f"登录凭据获取失败: {exc}"}


# ---------- 会话管理 ----------

def new_login_session() -> tuple[str, XiaomiLoginSession]:
    """创建新登录会话，返回 (login_id, session)。"""
    login_id = uuid.uuid4().hex
    LOGIN_SESSIONS[login_id] = XiaomiLoginSession()
    return login_id, LOGIN_SESSIONS[login_id]


def get_login_session(login_id: str) -> XiaomiLoginSession | None:
    """取会话（惰性清理过期会话）。"""
    now = time.time()
    expired = [k for k, s in list(LOGIN_SESSIONS.items()) if now - s.created_at > SESSION_TTL]
    for k in expired:
        LOGIN_SESSIONS.pop(k, None)
    return LOGIN_SESSIONS.get(login_id)


def drop_login_session(login_id: str) -> None:
    LOGIN_SESSIONS.pop(login_id, None)
