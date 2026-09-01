"""Garmin 账户集成：网页端 SSO 登录、短信 MFA、token 持久化与客户端加载。

设计要点：
- 中国区 domain = garmin.cn，国际区 domain = garmin.com。
- **登录不使用 garth 的 `Client.login`**：新版 garth 登录走移动端接口
  `sso.garmin.com/mobile/api/login?clientId=GCM_ANDROID_DARK`，对数据中心网络
  频繁调用会被 429 限流。因此登录统一走网页端 SSO 流程（见 web_login.py，
  与浏览器网页登录同源），获取 OAuth1/OAuth2 token 后赋给 garth Client。
- 登录是**阻塞式**的，MFA 通过 `prompt_mfa` 回调获取验证码，无原生"返回
  mfa_token 给前端"的两步式 API。
- **中国区与国际区相互独立、互不影响**：
  * 中国区走 **garth 原生链路**：登录（网页 SSO 绕 429）→ 获取 OAuth1/OAuth2
    token → 用 garth 的 `dumps()/loads()` 持久化/还原；token 过期由 garth
    请求层自动刷新（OAuth1 签名换新 OAuth2），与中国区原有的可用状态一致。
  * 国际区仅**登录流程简单化**：无双重验证，`intl_login()` 同步直接登录，
    不做两步式 MFA 等待；token 管理与中国区相同，共用 garth 原生 dumps/loads。
- token 持久化格式为 garth 原生 `dumps()` 输出 base64(JSON([oauth1, oauth2]))，
  存于数据库 Account.token 列；还原用 garth 原生 `loads()`，不依赖本项目
  自定义 token 序列化，避免 garth 版本差异与两区耦合。
- 成功完成后 Account.logged_in=True，MFA 期间 Account.mfa_pending=True。
"""
import logging
import queue
import threading
from datetime import datetime
from typing import Callable, Optional

import app.config as config
from app.db import SessionLocal
from app.models import Account

logger = logging.getLogger("garminsync")


# 每个 region 一个验证码队列；MFA 等待时阻塞于此
_mfa_queues: dict[str, "queue.Queue[str]"] = {}
_mfa_lock = threading.Lock()

# 登录冷却：记录每个 region 上次成功发起登录的时间，防止频繁点击触发 429
_last_login_at: dict[str, float] = {}
_login_cd_lock = threading.Lock()


def _get_mfa_queue(region: str) -> "queue.Queue[str]":
    with _mfa_lock:
        if region not in _mfa_queues:
            _mfa_queues[region] = queue.Queue()
        return _mfa_queues[region]


class _MfaTimeout(Exception):
    pass


def _make_prompt(region: str):
    """返回一个 prompt_mfa 回调：从队列取码，超时则抛异常。"""
    q = _get_mfa_queue(region)

    def _prompt() -> str:
        try:
            code = q.get(timeout=config.MFA_WAIT_SECONDS)
        except queue.Empty:
            raise _MfaTimeout("等待短信验证码超时，请重新登录")
        return code

    return _prompt


class GarminService:
    def __init__(self, session_factory=SessionLocal):
        self.Session = session_factory

    # ---------- token 持久化 ----------
    def _fetch_avatar_url(self, client) -> str:
        """登录成功后拉取 Garmin 头像 URL；失败返回空串（不阻塞登录）。"""
        try:
            p = client.profile
            if isinstance(p, dict):
                return (
                    p.get("profileImageUrlMedium")
                    or p.get("profileImageUrlLarge")
                    or p.get("profileImageUrlSmall")
                    or ""
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("获取 Garmin 头像失败：%s", e)
        return ""

    def _save_token(self, region: str, client, email: str, mfa_pending: bool):
        # garth 原生 dumps() 格式：base64(JSON([oauth1, oauth2]))
        # 与中国区原有持久化完全一致，任何 garth 版本都能用 loads() 还原。
        token_b64 = client.dumps()
        avatar_url = "" if mfa_pending else self._fetch_avatar_url(client)
        with self.Session() as s:
            a = s.get(Account, region)
            if a is None:
                a = Account(region=region)
                s.add(a)
            a.email = email or a.email
            try:
                a.username = client.username or a.username
            except Exception:
                pass
            if avatar_url:
                a.avatar_url = avatar_url
            a.token = token_b64
            a.logged_in = not mfa_pending
            a.mfa_pending = mfa_pending
            a.updated_at = datetime.utcnow()
            s.commit()

    def _load_client(self, region: str, domain: str):
        """从持久化 token 加载客户端（garth 原生 loads）。

        序列化/反序列化完全走 garth 的 dumps/loads：加载出的 oauth1/oauth2
        是 garth 原生 token 类（自动绑定 OAuth1 引用），token 过期由 garth
        请求层自动刷新（OAuth1 签名换新 OAuth2），不依赖本项目自定义实现，
        对任何 garth 版本都兼容，也不存在两区耦合。
        """
        from garth import Client

        with self.Session() as s:
            a = s.get(Account, region)
            if a is None or not a.token:
                return None
            try:
                client = Client(domain=domain)
                client.loads(a.token)
                return client
            except Exception:
                return None

    # ---------- 登录（线程 + Queue 两步式） ----------
    def _login_blocking(
        self,
        region: str,
        domain: str,
        email: str,
        password: str,
        prompt_mfa: Optional[Callable[[], str]] = None,
    ):
        from garth import Client

        from app.services.web_login import web_sso_login

        client = Client(domain=domain)
        # 网页端 SSO 登录（绕开 garth 移动端接口 mobile/api/login 的 429 限流）
        client.oauth1_token, client.oauth2_token = web_sso_login(
            client, email, password, prompt_mfa or _make_prompt(region)
        )
        # 登录成功（含 MFA 完成），持久化
        self._save_token(region, client, email, mfa_pending=False)
        return client

    def _login_direct(
        self, region: str, domain: str, email: str, password: str
    ) -> dict:
        """同步直接登录（无 MFA 场景），立即返回结果，不做两步式等待。

        若账号意外需要 MFA，则快速失败并给出明确错误。
        """
        import time as _time

        with _login_cd_lock:
            last = _last_login_at.get(region, 0.0)
            remain = config.LOGIN_COOLDOWN_SECONDS - (_time.time() - last)
            if remain > 0:
                return {
                    "ok": False,
                    "error": f"操作过于频繁，请等待约 {int(remain)} 秒后再试",
                }
            _last_login_at[region] = _time.time()

        def _no_mfa() -> str:
            raise RuntimeError("该账号需要 MFA 验证码，但当前区域不支持两步验证")

        try:
            client = self._login_blocking(
                region, domain, email, password, prompt_mfa=_no_mfa
            )
            return {"ok": True, "username": client.username}
        except Exception as e:  # noqa: BLE001
            logger.warning("登录失败 [%s]: %s", region, e)
            return {"ok": False, "error": str(e) or e.__class__.__name__}

    def _start_login(self, region: str, domain: str, email: str, password: str) -> dict:
        """启动后台登录线程。返回 mfa_required 指示是否需要稍后调用 mfa()。"""
        # 登录冷却：同一 region 在冷却期内不重复向 Garmin 发登录请求，避免 429
        import time as _time

        with _login_cd_lock:
            last = _last_login_at.get(region, 0.0)
            remain = config.LOGIN_COOLDOWN_SECONDS - (_time.time() - last)
            if remain > 0:
                return {
                    "mfa_required": False,
                    "mfa_token": region,
                    "username": None,
                    "error": f"操作过于频繁，请等待约 {int(remain)} 秒后再试",
                }
            _last_login_at[region] = _time.time()

        # 先清空可能存在的旧队列
        q = _get_mfa_queue(region)
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break

        result = {}

        def _runner():
            try:
                client = self._login_blocking(region, domain, email, password)
                result["username"] = client.username
                result["error"] = None
            except _MfaTimeout as e:
                # 超时：标记需要重新登录
                with self.Session() as s:
                    a = s.get(Account, region)
                    if a:
                        a.mfa_pending = False
                        a.logged_in = False
                        s.commit()
                result["error"] = str(e)
            except Exception as e:  # noqa: BLE001
                result["error"] = str(e) or e.__class__.__name__

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        # 给用户一点时间返回（线程尚未拿到 MFA 时无法判断是否需要验证码）。
        # 这里轮询 result，如果在 MFA_WAIT_SECONDS 内没结束也没报错，说明卡在 MFA。
        waited = 0.0
        step = 0.2
        while waited < min(config.MFA_WAIT_SECONDS, 10.0):
            if "error" in result or "username" in result:
                break
            t.join(timeout=step)
            waited += step
        if "error" in result:
            logger.warning("登录失败 [%s]: %s", region, result["error"])
            return {"mfa_required": False, "mfa_token": region, "username": None,
                    "error": result["error"]}
        if "username" in result:
            return {"mfa_required": False, "mfa_token": region, "username": result["username"]}
        # 仍然在等待（卡在 MFA）
        return {"mfa_required": True, "mfa_token": region, "username": None}

    def china_login(self, email: str, password: str) -> dict:
        return self._start_login("china", "garmin.cn", email, password)

    def intl_login(self, email: str, password: str) -> dict:
        # 国际区账号无双重验证：直接同步登录，立即返回结果
        return self._login_direct("intl", "garmin.com", email, password)

    def china_mfa(self, mfa_token: str, code: str) -> dict:
        return self._submit_mfa("china", code)

    def intl_mfa(self, mfa_token: str, code: str) -> dict:
        return self._submit_mfa("intl", code)

    def _submit_mfa(self, region: str, code: str) -> dict:
        q = _get_mfa_queue(region)
        q.put(code.strip())
        # 轮询登录线程结果
        waited = 0.0
        step = 0.3
        while waited < config.MFA_WAIT_SECONDS:
            # 检查该 region 是否已登录
            with self.Session() as s:
                a = s.get(Account, region)
                if a and a.logged_in and not a.mfa_pending:
                    return {"ok": True, "username": a.username}
                if a and not a.logged_in and not a.mfa_pending:
                    # 登录失败（超时或验证码错误导致线程结束但未登录）
                    return {"ok": False, "username": None,
                            "error": "登录失败，验证码可能错误或已超时，请重新登录"}
            waited += step
            import time as _t
            _t.sleep(step)
        return {"ok": False, "username": None, "error": "等待登录完成超时"}

    # ---------- 登出 / 状态 ----------
    def logout(self, region: str):
        with self.Session() as s:
            a = s.get(Account, region)
            if a:
                a.logged_in = False
                a.mfa_pending = False
                a.username = ""
                a.email = ""
                a.token = ""
                s.commit()

    def status(self) -> dict:
        def info(region: str) -> dict:
            with self.Session() as s:
                a = s.get(Account, region)
                return {
                    "logged_in": bool(a and a.logged_in and not a.mfa_pending),
                    "username": a.username if a else "",
                    "email": a.email if a else "",
                    "avatar_url": a.avatar_url if a else "",
                }

        return {"china": info("china"), "intl": info("intl")}



    def get_client(self, region: str, domain: str):
        """按 region/domain 加载已登录的客户端；未登录或失败返回 None。

        国际区未登录不影响中国区抓取：调用方只用 china 端即可单独抓取。
        """
        return self._load_client(region, domain)
